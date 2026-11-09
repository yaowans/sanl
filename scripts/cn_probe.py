#!/usr/bin/env python3
"""
cn-probe —— 中国境内 TCP 连通性探针

部署在中国大陆境内 VPS / 家宽小鸡上，供 sanl（境外服务器）调用，
用于判断某个 IP:端口 从境内是否可达，从而识别被 GFW 拦截的节点。

设计要点：
- 零第三方依赖，仅用 Python 标准库，方便在任意国内小鸡上直接跑
- 同步 socket 连接探测，单次探测超时可控（默认 3s）
- 支持批量：一次请求可测多个 IP:port，避免逐个 HTTP 往返
- 共享密钥鉴权（X-Probe-Key），防止被滥用
- 出口 IP 自报：返回本机公网出口 IP，便于校验探针位置确实在境内

用法：
    python3 cn_probe.py --port 8899 --key <密钥> --bind 0.0.0.0

接口：
    GET  /health              → {"ok":true,"egress_ip":"1.2.3.4",...}
    POST /check              → body: {"targets":["1.2.3.4:443", ...], "timeout":3}
                              → {"results":[{"target":"1.2.3.4:443","reachable":true,"latency_ms":12,"error":""}]}
"""
import argparse
import json
import os
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def safe_print(msg: str):
    """Windows 控制台默认 GBK，中文/emoji 会抛 UnicodeEncodeError"""
    try:
        print(msg)
    except UnicodeEncodeError:
        print(msg.encode("ascii", "replace").decode("ascii"))


# 共享密钥（启动参数覆盖）；未设置则拒绝所有探测请求
PROBE_KEY = os.environ.get("CN_PROBE_KEY", "")
MAX_TARGETS = 500
DEFAULT_TIMEOUT = 3.0
MAX_TIMEOUT = 10.0
MAX_BODY = 64 * 1024

# 出口 IP 缓存（探测一次，24h 刷新）
_egress_cache = {"ip": "", "ts": 0.0}
_egress_lock = threading.Lock()


def get_egress_ip() -> str:
    """取本机公网出口 IP（用于校验探针是否真在境内）"""
    with _egress_lock:
        if _egress_cache["ip"] and (time.time() - _egress_cache["ts"]) < 86400:
            return _egress_cache["ip"]
    ip = ""
    for url in ("https://myip.ipip.net", "https://api.ipify.org",
                "https://ifconfig.me/ip", "https://ipinfo.io/ip"):
        try:
            import urllib.request
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            with urllib.request.urlopen(req, timeout=6) as r:
                ip = r.read(64).decode("utf-8", "replace").strip()
            if ip:
                # ipip.net 返回 "当前 IP：218.76.43.171  来自于：中国 湖南 长沙  电信"
                if "：" in ip:
                    ip = ip.split("：", 1)[1].strip().split()[0]
                ip = ip.strip()
                break
        except Exception:
            continue
    if ip:
        with _egress_lock:
            _egress_cache["ip"] = ip
            _egress_cache["ts"] = time.time()
    return ip


def parse_target(raw: str):
    """解析 'ip:port'；支持 IPv4 与 [IPv6]:port"""
    raw = raw.strip()
    if not raw:
        return None, None
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0 or end + 2 > len(raw) or raw[end + 1] != ":":
            return None, None
        return raw[1:end], int(raw[end + 2:])
    parts = raw.rsplit(":", 1)
    if len(parts) != 2:
        return None, None
    host, port_s = parts
    try:
        port = int(port_s)
    except ValueError:
        return None, None
    if not host or not (0 < port < 65536):
        return None, None
    return host, port


def probe_one(host: str, port: int, timeout: float) -> dict:
    """单次 TCP 连接探测。

    reachable=True 表示境内能连上；GFW 拦截通常表现为超时或连接重置。
    """
    t0 = time.monotonic()
    sock = None
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        latency = int((time.monotonic() - t0) * 1000)
        return {"reachable": True, "latency_ms": latency, "error": ""}
    except socket.timeout:
        return {"reachable": False, "latency_ms": None, "error": "timeout"}
    except ConnectionRefusedError:
        # 境内主机主动拒绝：说明没被墙，只是服务没监听 —— 仍视为"境内可达"
        return {"reachable": True, "latency_ms": int((time.monotonic() - t0) * 1000),
                "error": "refused"}
    except socket.gaierror as e:
        return {"reachable": False, "latency_ms": None, "error": f"dns:{e.strerror or e}"}
    except OSError as e:
        err = getattr(e, "strerror", None) or str(e)
        # Connection reset 在 GFW 场景下是典型的被墙特征
        return {"reachable": False, "latency_ms": None, "error": f"os:{err}"}
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


class ProbeHandler(BaseHTTPRequestHandler):
    server_version = "cn-probe/1.0"

    def handle_one_request(self):
        """吞掉客户端断开导致的 ConnectionResetError/BrokenPipeError。

        端口暴露在公网必然有扫描器来握手，这些噪音会淹没真实错误日志。
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True
        except socket.timeout:
            self.close_connection = True

    def log_message(self, fmt, *args):
        try:
            line = fmt % args
        except Exception:
            line = str(args)
        sys.stderr.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), line))
        try:
            sys.stderr.flush()
        except Exception:
            pass

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            self.close_connection = True

    def _authorized(self) -> bool:
        if not PROBE_KEY:
            return False
        got = self.headers.get("X-Probe-Key", "") or ""
        # 常量时间比较，避免时序侧信道
        import hmac
        return hmac.compare_digest(got, PROBE_KEY)

    def do_GET(self):
        if self.path.startswith("/health"):
            self._send_json(200, {
                "ok": True,
                "service": "cn-probe",
                "egress_ip": get_egress_ip(),
                "authenticated": self._authorized(),
                "time": int(time.time()),
            })
            return
        self._send_json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/check"):
            self._send_json(404, {"ok": False, "error": "not found"})
            return
        if not self._authorized():
            self._send_json(401, {"ok": False, "error": "invalid probe key"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY:
            self._send_json(400, {"ok": False, "error": "invalid body size"})
            return
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"ok": False, "error": f"bad json: {e}"})
            return

        targets = data.get("targets") or []
        if not isinstance(targets, list):
            self._send_json(400, {"ok": False, "error": "targets must be a list"})
            return
        if len(targets) > MAX_TARGETS:
            self._send_json(400, {"ok": False, "error": f"too many targets (max {MAX_TARGETS})"})
            return

        try:
            timeout = float(data.get("timeout", DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(0.5, min(timeout, MAX_TIMEOUT))

        # 去重保序
        t_start = time.monotonic()
        seen = set()
        uniq = []
        for t in targets:
            if not isinstance(t, str):
                continue
            if t in seen:
                continue
            seen.add(t)
            uniq.append(t)

        parsed = []
        results = []
        for t in uniq:
            host, port = parse_target(t)
            if host is None:
                results.append({"target": t, "reachable": False, "latency_ms": None,
                                "error": "bad target format"})
            else:
                parsed.append((t, host, port))
                results.append(None)  # 占位，稍后按顺序填

        if parsed:
            with ThreadPoolExecutor(max_workers=min(64, max(1, len(parsed)))) as pool:
                futures = [pool.submit(probe_one, h, p, timeout) for t, h, p in parsed]
                done = [f.result() for f in futures]
            ordered = [{"target": t, **r} for (t, h, p), r in zip(parsed, done)]
            it = iter(ordered)
            final = []
            for r in results:
                final.append(next(it) if r is None else r)
            results = final

        reachable = sum(1 for r in results if r.get("reachable"))
        self._send_json(200, {
            "ok": True,
            "results": results,
            "summary": {"total": len(results), "reachable": reachable,
                        "blocked": len(results) - reachable},
            "egress_ip": get_egress_ip(),
            "elapsed_ms": int((time.monotonic() - t_start) * 1000),
        })


def main():
    global PROBE_KEY

    ap = argparse.ArgumentParser(description="cn-probe: 中国境内 TCP 连通性探针")
    ap.add_argument("--host", "--bind", dest="host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--key", default=PROBE_KEY, help="共享密钥（也可用环境变量 CN_PROBE_KEY）")
    args = ap.parse_args()

    PROBE_KEY = args.key or ""
    if not PROBE_KEY:
        sys.stderr.write("ERROR: no probe key set. use --key or env CN_PROBE_KEY\n")
        sys.exit(1)

    srv = ThreadingHTTPServer((args.host, args.port), ProbeHandler)
    srv.daemon_threads = True
    egress = get_egress_ip()
    safe_print(f"cn-probe listening on {args.host}:{args.port}")
    safe_print(f"  egress IP: {egress or '(unavailable)'}")
    safe_print("  (if the egress IP is in mainland China, the probe is correctly placed)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        safe_print("stopped")
        srv.shutdown()


if __name__ == "__main__":
    main()
