"""真实 TCP 握手延迟测量

背景：subs-check（Go 二进制）输出的节点名只带**速度**，不带延迟
（实测确认：speed 模式名形如 `🇺🇸US_1|684KB/s`，latency 模式名连段都没有）。
因此不能从它的输出里取延迟——旧代码用「速度反推延迟」的假公式
`max(50, 2048000 / speed)`，速度一快结果就是 0，被夹到 50，
导致**所有快节点延迟恒为 50ms**（用户报告的问题）。

本模块在测速回填后，对存活节点的 server:port 做一次并发 TCP 握手，
取真实 RTT 写入 nodes.latency。语义：TCP 握手延迟（从本服务器出发），
比"代理应用层延迟"略低，但远优于编造的常数。
"""
import asyncio
import logging
import socket
import time
from typing import Dict, Iterable, List, Optional, Tuple

from ..engine.tcplayer import resolve_host

logger = logging.getLogger(__name__)

# 单目标握手超时（超过即视为不可达，给 None）
_CONNECT_TIMEOUT = 3.0
# 并发上限（DNS 解析也有独立的限流，这里控制连接并发）
_SEM_LIMIT = 200


# CDN / 反代常见网段（Cloudflare 为主）：解析到这些 IP 时，
# TCP RTT 只是"到就近边缘节点"的延迟，不代表国内用户访问该节点的延迟
_CDN_PREFIXES = (
    "104.16.", "104.17.", "104.18.", "104.19.", "104.20.", "104.21.", "104.22.", "104.23.",
    "104.24.", "104.25.", "104.26.", "104.27.",
    "172.64.", "172.65.", "172.66.", "172.67.",
    "162.158.", "162.159.", "188.114.", "173.245.", "103.21.", "103.22.", "103.31.",
    "141.101.", "108.162.", "190.93.", "197.234.", "198.41.",
)


def is_cdn_ip(ip: str) -> bool:
    """IP 是否属于 CDN（Cloudflare 等）边缘网段"""
    return any(str(ip or "").startswith(p) for p in _CDN_PREFIXES)


async def _one(server: str, port: int, sem: asyncio.Semaphore,
               timeout: float) -> dict:
    """单目标：返回 {"rtt": int|None, "edge": bool}"""
    async with sem:
        ip = await resolve_host(server, timeout=timeout)
        if not ip:
            return {"rtt": None, "edge": False}
        edge = is_cdn_ip(ip)
        t0 = time.monotonic()
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=timeout)
            rtt = int((time.monotonic() - t0) * 1000)
            return {"rtt": max(1, rtt), "edge": edge}
        except Exception:
            return {"rtt": None, "edge": edge}
        finally:
            if writer is not None:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass


async def measure_tcp_latency(
        targets: Iterable[Tuple[int, str, int]],
        concurrency: int = _SEM_LIMIT,
        timeout: float = _CONNECT_TIMEOUT) -> Dict[int, dict]:
    """并发测量 TCP 握手 RTT。

    targets: [(node_id, server, port), ...]
    返回 {node_id: {"rtt": int|None, "edge": bool}}
      rtt=None  → 握手失败/域名解析失败
      edge=True → 解析到 CDN 边缘（如 Cloudflare），rtt 不代表国内访问延迟
    """
    items: List[Tuple[int, str, int]] = []
    for nid, server, port in targets:
        server = str(server or "").strip()
        try:
            port = int(port)
        except (TypeError, ValueError):
            continue
        if not server or not (0 < port < 65536):
            continue
        items.append((nid, server, port))
    if not items:
        return {}

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    results = await asyncio.gather(
        *[_one(server, port, sem, timeout) for _, server, port in items],
        return_exceptions=True)

    out: Dict[int, dict] = {}
    for (nid, _s, _p), r in zip(items, results):
        if isinstance(r, BaseException):
            out[nid] = {"rtt": None, "edge": False}
        else:
            out[nid] = r
    ok = sum(1 for v in out.values() if v.get("rtt") is not None)
    edge = sum(1 for v in out.values() if v.get("edge"))
    logger.info(f"[tcp-latency] 测量 {len(items)} 个目标，成功 {ok}（CDN 边缘 {edge}）")
    return out


def measure_tcp_latency_sync(targets: Iterable[Tuple[int, str, int]],
                             concurrency: int = _SEM_LIMIT,
                             timeout: float = _CONNECT_TIMEOUT) -> Dict[int, dict]:
    """同步包装（供非 async 上下文调用）"""
    try:
        return asyncio.run(measure_tcp_latency(targets, concurrency, timeout))
    except RuntimeError:
        # 已在事件循环内：新建循环线程跑
        import concurrent.futures as cf
        with cf.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                lambda: asyncio.run(measure_tcp_latency(targets, concurrency, timeout))
            ).result()
