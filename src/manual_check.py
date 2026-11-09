"""
节点手工测速 / 信息刷新

与全量测速（checker.run_check）的区别：
- **只测用户指定的节点**（按 id），不拉订阅、不扫全池
- 结果只回填这些节点，**不影响其他节点的状态**
- 附带补齐 GeoIP 地区信息（国家/国家码/城市）与规范化命名

实现路径：把选中的节点序列化成协议链接 → 起一个**进程内本地 HTTP 订阅服务**
喂给 subs-check → 解析结果按指纹回填（作用域限定，不影响其他节点）。

为什么复用 subs-check 而不是自研引擎：
生产环境 check_backend=subs-check，且 mihomo 内核未安装（/app/bin/mihomo 缺失），
自研引擎的 KernelManager 无法启动。subs-check 是容器内唯一可用的真实链路测速器。

⚠ 已验证（生产实测）：subs-check 只认 HTTP 订阅（sub-urls），
`source.subscriptions` 指向本地文件会被忽略（订阅链接数量 本地=0）。
所以这里用 127.0.0.1 上的临时 HTTP 服务提供内容。
"""
import asyncio
import http.server
import json
import logging
import os
import signal
import socketserver
import subprocess
import threading
import time
import yaml
from typing import Callable, Dict, List, Optional, Set

from .schema import repository
from .schema.repository import node_fingerprint

logger = logging.getLogger(__name__)

ProgressCb = Optional[Callable[[str, int, int, str], None]]

# 手工测速的工作目录（与全量测速的 ./output 隔离，避免结果文件互相覆盖）
WORKDIR = "./output/manual"
# 本地订阅服务端口（避开 subs-check 的 8199 与内核的 7891+）
LOCAL_SUB_PORT = 8765
# 域名 → IP 解析缓存（命名用；进程内共享）
_dns_cache: Dict[str, str] = {}


def build_links(nodes) -> List[str]:
    """节点列表 → 协议链接列表（复用 subscribe 模块的转换，去重）"""
    from .api.subscribe import generate_links
    try:
        raw = generate_links(list(nodes))
    except Exception as e:
        logger.warning(f"[manual] 链接生成失败: {e}")
        return []
    out, seen = [], set()
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line in seen:
            continue
        seen.add(line)
        out.append(line)
    return out


def build_clash_yaml(nodes) -> str:
    """节点列表 → Clash.Meta YAML（无损：reality-opts 等复杂字段完整保留）

    ⚠ 必须用 clash-meta 而非基础 clash：基础版会**直接跳过 hysteria2/hysteria/tuic**
      协议（generate_clash 里过滤），导致这些节点根本不进测速（实测存活 0、无延迟）。
    ⚠ 不能用协议链接代替：vless:// 链接的 reality 参数在往返中会丢，
      Reality 节点必然握手失败（实测存活=0）。Clash YAML 是原始字段，无损。
    """
    from .api.subscribe import generate_by_format
    try:
        return generate_by_format("clash-meta", list(nodes))
    except Exception as e:
        logger.warning(f"[manual] Clash YAML 生成失败: {e}")
        return ""


def scope_fingerprints(nodes) -> Set[str]:
    """作用域指纹集合——回填时限定只影响这些节点"""
    fps = set()
    for n in nodes:
        d = n.node_data if isinstance(n.node_data, dict) else {}
        fps.add(node_fingerprint(n.node_type, d))
    return fps


async def fill_geo_for_nodes(nodes) -> int:
    """为指定节点补齐国家/国家码/城市（复用 geoip 模块，支持域名解析）"""
    from .geoip import lookup_servers, _emoji_flag
    if not nodes:
        return 0
    servers = []
    for n in nodes:
        d = n.node_data if isinstance(n.node_data, dict) else {}
        s = d.get("server")
        if s:
            servers.append(s)
    if not servers:
        return 0
    try:
        geo = await lookup_servers(list(set(servers)))
    except Exception as e:
        logger.warning(f"[manual] GeoIP 查询失败: {e}")
        return 0
    updated = 0
    for n in nodes:
        d = n.node_data if isinstance(n.node_data, dict) else {}
        info = geo.get(d.get("server"))
        if not info:
            continue
        code = info.get("country_code", "")
        flag = _emoji_flag(code)
        city = info.get("city", "")
        if flag and (n.country != flag
                     or getattr(n, "country_code", None) != code
                     or not getattr(n, "city", None)):
            repository.update_node_geo(n.id, flag, code, city)
            updated += 1
    return updated


def normalize_manual_names(nodes, include_ip: bool = True) -> int:
    """规范化手动节点命名：{国旗}{国家码}_{序号}|{IP}|{速度}

    示例：🇯🇵JP_1|49.212.231.67|452KB/s（速度来自最近一次测速，无则省略该段）
    IP 采用解析后的真实出口（域名先转 IP）；带延迟时格式为
    {国旗}{国家码}_{序号}|{IP}|{延迟}ms|{速度}。

    只处理**没有用户自定义名**的节点（custom_name 优先，不覆盖用户命名）。
    """
    import re as _re
    from .engine.namer import flag_of
    counter: Dict[str, int] = {}
    updated = 0

    def _resolve(server: str) -> str:
        """域名 → IP（带缓存；失败返回原值）"""
        import ipaddress
        try:
            ipaddress.ip_address(server)
            return server
        except (ValueError, TypeError):
            pass
        cached = _dns_cache.get(server)
        if cached:
            return cached
        try:
            import socket
            ip = socket.getaddrinfo(server, None, proto=socket.IPPROTO_TCP)[0][4][0]
            _dns_cache[server] = ip
            return ip
        except Exception:
            _dns_cache[server] = server
            return server

    for n in nodes:
        d = n.node_data if isinstance(n.node_data, dict) else {}
        if d.get("custom_name"):
            continue   # 用户自定义名优先，不动
        code = (getattr(n, "country_code", None) or "").strip().upper()
        if not code:
            # 从现有名字里猜（可能是 emoji 国旗或 ISO 码）
            try:
                from .engine.namer import guess_country
                from .engine.models import Candidate
                code = guess_country(Candidate(proxy={"name": n.node_name or ""}))
            except Exception:
                code = ""
        if not code:
            continue
        key = code
        counter[key] = counter.get(key, 0) + 1
        flag = flag_of(code) or "🌐"
        parts = [f"{flag}{code}_{counter[key]}"]
        if include_ip:
            server = str(d.get("server") or "").strip()
            if server:
                parts.append(_resolve(server))
        # 延迟/速度段（测速后回填的值；subs-check 回填解析按 | 分段提取 KB/s、ms）
        if n.latency:
            parts.append(f"{int(n.latency)}ms")
        elif getattr(n, "download_speed", None):
            sp = int(n.download_speed)
            parts.append(f"{sp/1024:.0f}KB/s" if sp < 1048576 else f"{sp/1048576:.1f}MB/s")
        new_name = "|".join(parts)
        if new_name == (n.node_name or ""):
            continue
        # ⚠ 用 set_node_auto_name 而非 rename_node：自动名不能盖 custom_name 戳，
        #   否则下一次规范化会把自动名当"用户自定义名"跳过，名字永远停在旧值
        if repository.set_node_auto_name(n.id, new_name):
            updated += 1
    return updated


# ===== subs-check 子进程封装 =====

class _SubHandler(http.server.BaseHTTPRequestHandler):
    """把订阅内容喂给 subs-check 的本地 HTTP 服务"""
    payload = b""
    content_type = "text/plain; charset=utf-8"

    def do_GET(self):
        data = self.payload
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


def _start_local_sub_server(body: str, port: int):
    """起本地订阅服务，返回 (server, thread)"""
    _SubHandler.payload = body.encode("utf-8")
    # Clash YAML 要用 yaml MIME，否则 subs-check 可能按纯文本解析
    _SubHandler.content_type = ("text/yaml; charset=utf-8"
                                if body.lstrip().startswith("proxies:")
                                else "text/plain; charset=utf-8")
    try:
        srv = socketserver.TCPServer(("127.0.0.1", port), _SubHandler)
    except OSError as e:
        raise RuntimeError(f"本地订阅端口 {port} 占用: {e}")
    srv.allow_reuse_address = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def _kill_proc(proc):
    """杀掉 subs-check 子进程组"""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    except Exception:
        pass


def _parse_subs_output(path: str) -> List[dict]:
    """解析 subs-check 产出的 clash proxies → 回填用的 results 列表"""
    import re
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"[manual] 解析结果失败 {path}: {e}")
        return []
    proxies = data.get("proxies", []) if isinstance(data, dict) else []
    results = []
    for proxy in proxies:
        if not isinstance(proxy, dict) or not proxy.get("server"):
            continue
        name = str(proxy.get("name", ""))
        ptype = str(proxy.get("type", "unknown")).lower()
        download_speed, latency, country = None, None, None
        if "|" in name:
            parts = [p.strip() for p in name.split("|")]
            m = re.search(r"[\U0001F1E6-\U0001F1FF]{2}", parts[0])
            if m:
                country = m.group(0)
            for p in parts[1:5]:  # 段位兼容：{旗}{CC}_{i}|{IP}|{延迟}ms|{速度}
                if p.endswith("KB/s"):
                    try:
                        download_speed = int(float(p[:-4]) * 1024)
                        break
                    except ValueError:
                        pass
                elif p.endswith("MB/s"):
                    try:
                        download_speed = int(float(p[:-4]) * 1024 * 1024)
                        break
                    except ValueError:
                        pass
            for p in parts[1:5]:  # 段位兼容：IP 段可能挤后延迟/速度位置
                digits = p.replace("ms", "").strip()
                if digits.isdigit():
                    latency = int(digits)
                    break
        if latency is None and download_speed is not None:
            latency = max(50, int(2048000 / (download_speed or 256)))
        elif latency is None:
            latency = 500
        node_data = dict(proxy)
        node_data.pop("name", None)
        results.append({
            "node_type": ptype,
            "node_data": node_data,
            "node_name": name,
            "download_speed": download_speed,
            "latency": latency,
            "country": country,
        })
    return results


async def run_manual_check(node_ids: List[int], *, mode: str = "latency",
                           progress_cb: ProgressCb = None,
                           timeout_sec: int = 300) -> dict:
    """手工测速：只测指定节点。

    返回 {checked, alive, blocked, geo_updated, renamed, error}
    """
    def prog(phase, done, total, detail=""):
        if progress_cb:
            try:
                progress_cb(phase, done, total, detail)
            except Exception:
                pass

    nodes = repository.get_nodes_by_ids(node_ids)
    if not nodes:
        return {"checked": 0, "alive": 0, "blocked": 0, "error": "节点不存在"}

    # ⚠ 用 Clash YAML 而非协议链接：链接往返会丢 reality 参数 → Reality 节点必失败
    body = build_clash_yaml(nodes)
    if not body:
        links = build_links(nodes)
        body = "\n".join(links) + "\n"
    if not body.strip():
        return {"checked": len(nodes), "alive": 0, "blocked": 0,
                "error": "所选节点无法转换为可测速的内容"}

    prog("prepare", 0, len(nodes), f"准备 {len(nodes)} 个节点")
    scope = scope_fingerprints(nodes)

    # ---- 0.5. 修复 Reality 参数字段名（存量数据 public-key/short-id → pbk/sid）----
    try:
        from .api.importer import repair_reality_opts
        fixed = repair_reality_opts(nodes)
        if fixed:
            logger.info(f"[manual] 修复 {fixed} 个节点的 Reality 参数")
            nodes = repository.get_nodes_by_ids(node_ids)
            body = build_clash_yaml(nodes) or body
    except Exception as e:
        logger.warning(f"[manual] Reality 修复失败: {e}")

    # ---- 1. 补 GeoIP 地区信息（测速前补，测速结果也能带上国旗）----
    geo_updated = 0
    try:
        geo_updated = await fill_geo_for_nodes(nodes)
        if geo_updated:
            prog("geo", len(nodes), len(nodes), f"补齐 {geo_updated} 个节点的地区信息")
            nodes = repository.get_nodes_by_ids(node_ids)   # 重新取，带上新 geo
    except Exception as e:
        logger.warning(f"[manual] GeoIP 补全失败: {e}")

    # ---- 2. 起本地订阅服务 + 跑 subs-check ----
    workdir = os.path.abspath(WORKDIR)
    os.makedirs(workdir, exist_ok=True)
    out_dir = os.path.join(workdir, "out")
    os.makedirs(out_dir, exist_ok=True)
    # 清理上一轮产物
    for f in ("all.yaml",):
        fp = os.path.join(out_dir, f)
        if os.path.exists(fp):
            try:
                os.remove(fp)
            except OSError:
                pass

    srv = None
    proc = None
    log_path = os.path.join(workdir, "run.log")
    try:
        srv, _ = _start_local_sub_server(body, LOCAL_SUB_PORT)
    except Exception as e:
        return {"checked": len(nodes), "alive": 0, "blocked": 0, "geo_updated": geo_updated,
                "error": f"本地订阅服务启动失败: {e}"}

    cfg = {
        # ⚠ 必须带 alive-test-url / node-prefix 等全量配置的键，否则 subs-check
        #   缺少测活端点配置会导致节点全部判死（实测：缺该项时 2 个可用节点存活=0）
        "alive-test-url": "http://gstatic.com/generate_204",
        "check-interval": 30,
        "concurrent": 240,
        "download-mb": 1,
        "download-timeout": 6,
        "filter": [],
        "listen-port": ":8299",
        "media-check": False,
        "media-concurrent": 20,
        "min-speed": 0,
        "node-prefix": "",
        "output-dir": out_dir,
        "print-progress": False,
        "rename-node": True,
        "save-method": "local",
        "source": {"subscriptions": [], "url-subscriptions": []},
        "speed-concurrent": 100,
        "speed-test-url": "http://cachefly.cachefly.net/10mb.test",
        "sub-urls": [f"http://127.0.0.1:{LOCAL_SUB_PORT}/sub"],
        "sub-urls-concurrent": 40,
        "sub-urls-retry": 3,
        "timeout": 5000,
        "total-speed-limit": 0,
    }
    # speed 模式才测速
    if mode not in ("speed", "full"):
        cfg["enable-speedtest"] = False
    cfg_path = os.path.join(workdir, "cfg.yaml")
    binary = os.path.abspath("./subs-check")
    try:
        with open(cfg_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        with open(log_path, "w", encoding="utf-8") as logf:
            proc = subprocess.Popen(
                [binary, "-f", cfg_path],
                stdout=logf, stderr=subprocess.STDOUT,
                cwd=os.path.abspath("."), start_new_session=True)
    except Exception as e:
        srv.shutdown()
        return {"checked": len(nodes), "alive": 0, "blocked": 0, "geo_updated": geo_updated,
                "error": f"subs-check 启动失败: {e}"}

    # ---- 3. 等待完成 ----
    t0 = time.time()
    done = False
    try:
        while time.time() - t0 < timeout_sec:
            await asyncio.sleep(1.5)
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    txt = f.read()
            except OSError:
                txt = ""
            if "检测完成" in txt:
                done = True
                break
            prog("probe", int(time.time() - t0), timeout_sec, "测速中…")
    finally:
        _kill_proc(proc)
        try:
            srv.shutdown()
        except Exception:
            pass

    result_file = os.path.join(out_dir, "all.yaml")
    results = _parse_subs_output(result_file) if done else []
    alive = len(results)

    # ---- 4. 回填（作用域限定，不影响其他节点）----
    if results:
        stats = repository.apply_check_results(results, scope_fingerprints=scope)
        alive = stats.get("alive", alive)
    else:
        # 没存活节点 → 作用域内的节点标记 inactive（其他节点不受影响）
        repository.mark_missing_inactive(set(), scope_fingerprints=scope)

    # ---- 5. 规范化命名（地区补齐后才有意义）----
    renamed = 0
    try:
        nodes = repository.get_nodes_by_ids(node_ids)
        renamed = normalize_manual_names(nodes)
    except Exception as e:
        logger.warning(f"[manual] 规范化命名失败: {e}")

    blocked = len(nodes) - alive
    prog("done", len(nodes), len(nodes), f"完成：{alive} 存活 / {blocked} 不可用")

    from .api.cache import invalidate_all
    try:
        invalidate_all()
    except Exception:
        pass

    logger.info(f"[manual] 手工测速完成: {len(nodes)} 个节点 → 存活 {alive}，"
                f"GeoIP 补 {geo_updated}，重命名 {renamed}")
    return {
        "checked": len(nodes),
        "alive": alive,
        "blocked": blocked,
        "geo_updated": geo_updated,
        "renamed": renamed,
        "timed_out": not done,
    }
