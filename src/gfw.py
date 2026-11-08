"""
GFW 拦截检测客户端

境外服务器测速只能确认"服务器能连上"，但 GFW 会让境内用户连不上这些节点。
本模块通过调用部署在中国大陆境内的探针服务（cn-probe），从境内视角复核
IP:端口 是否可达，从而把"境外连通但境内被墙"的节点识别出来。

探针服务见 cn_probe/ 目录（零依赖，单文件）。
"""
import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

import httpx

from .config import get_gfw_config

logger = logging.getLogger(__name__)

# 批量探测分批大小（探针单次上限 500，这里留余量）
_BATCH_SIZE = 100
# 单批超时（含探针自身的连接等待）
_BATCH_TIMEOUT = 40.0


def _probe_config() -> dict:
    """读取探针配置：enabled / url / key / timeout"""
    gfw = get_gfw_config()
    return {
        "enabled": bool(gfw.get("enabled", False)),
        "url": (gfw.get("probe_url") or "").rstrip("/"),
        "key": gfw.get("probe_key") or "",
        "timeout": float(gfw.get("timeout", 3)),
        "batch_size": int(gfw.get("batch_size", _BATCH_SIZE)),
    }


def probe_enabled() -> bool:
    """探针是否已启用且配置完整"""
    c = _probe_config()
    return c["enabled"] and bool(c["url"]) and bool(c["key"])


def extract_target(node_data: str) -> Optional[Tuple[str, int]]:
    """从节点的 Clash 格式数据中取出 (server, port)。

    node_data 存的就是 clash proxies 的原始字段，server/port 一定在最外层。
    """
    import json
    try:
        d = json.loads(node_data) if isinstance(node_data, str) else (node_data or {})
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict):
        return None
    server = d.get("server")
    port = d.get("port")
    if not server:
        return None
    try:
        port = int(port)
    except (TypeError, ValueError):
        return None
    if not (0 < port < 65536):
        return None
    server = str(server).strip()
    if not server:
        return None
    return server, port


def format_target(server: str, port: int) -> str:
    """IPv6 需要方括号包裹"""
    if ":" in server and not server.startswith("["):
        return f"[{server}]:{port}"
    return f"{server}:{port}"


async def _probe_batch(http: httpx.AsyncClient, targets: List[str],
                      timeout: float) -> Dict[str, dict]:
    """探测一批目标，返回 {target: result}"""
    c = _probe_config()
    try:
        r = await http.post(
            f"{c['url']}/check",
            json={"targets": targets, "timeout": timeout},
            headers={"X-Probe-Key": c["key"]},
            timeout=_BATCH_TIMEOUT,
        )
        if r.status_code != 200:
            logger.warning("gfw probe HTTP %s: %s", r.status_code, r.text[:200])
            return {}
        data = r.json()
    except Exception as e:
        logger.warning("gfw probe request failed: %s", e)
        return {}

    out = {}
    for item in data.get("results", []):
        tgt = item.get("target")
        if not tgt:
            continue
        out[tgt] = {
            "reachable": bool(item.get("reachable")),
            "latency_ms": item.get("latency_ms"),
            "error": item.get("error", ""),
        }
    return out


async def check_targets(targets: List[str]) -> Dict[str, dict]:
    """批量探测一组 'ip:port' 目标。

    返回 {target: {"reachable":bool, "latency_ms":int|None, "error":str}}
    探针不可用时返回空 dict（调用方应据此跳过过滤，而不是把所有节点判死）。
    """
    if not probe_enabled() or not targets:
        return {}

    c = _probe_config()
    timeout = c["timeout"]
    batch_size = max(1, c["batch_size"])
    results: Dict[str, dict] = {}

    async with httpx.AsyncClient() as http:
        for i in range(0, len(targets), batch_size):
            batch = targets[i:i + batch_size]
            try:
                part = await _probe_batch(http, batch, timeout)
            except Exception as e:
                logger.warning("gfw probe batch failed: %s", e)
                part = {}
            results.update(part)
            if i + batch_size < len(targets):
                # 轻微节流，避免把探针打爆
                await asyncio.sleep(0.3)

    return results


async def check_nodes(nodes: List[dict]) -> Dict[int, dict]:
    """对节点列表做境内可达性检测。

    nodes: [{"id":int, "node_data":str}, ...]
    返回 {node_id: {"server","port","blocked","reachable","latency_ms","error"}}
    """
    if not probe_enabled():
        return {}

    # 组装待探测目标
    target_map: Dict[str, List[int]] = {}
    node_meta: Dict[int, Tuple[str, int]] = {}
    for n in nodes:
        parsed = extract_target(n.get("node_data") or "")
        if not parsed:
            continue
        server, port = parsed
        tgt = format_target(server, port)
        target_map.setdefault(tgt, []).append(n["id"])
        node_meta[n["id"]] = (server, port)

    if not target_map:
        return {}

    raw = await check_targets(list(target_map.keys()))
    if not raw:
        logger.warning("GFW 探针无响应，本次跳过封锁过滤")
        return {}

    out: Dict[int, dict] = {}
    for tgt, node_ids in target_map.items():
        r = raw.get(tgt)
        if r is None:
            # 该目标未返回结果：不做判定（避免误杀）
            continue
        for nid in node_ids:
            server, port = node_meta[nid]
            out[nid] = {
                "server": server,
                "port": port,
                "reachable": r["reachable"],
                "blocked": not r["reachable"],
                "latency_ms": r.get("latency_ms"),
                "error": r.get("error", ""),
            }
    return out


async def probe_health() -> dict:
    """探针健康检查（供 API / 前端展示用）"""
    if not probe_enabled():
        return {"enabled": False, "ok": False, "error": "探针未启用或配置不完整"}
    c = _probe_config()
    try:
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{c['url']}/health",
                               headers={"X-Probe-Key": c["key"]}, timeout=10)
            if r.status_code != 200:
                return {"enabled": True, "ok": False, "error": f"HTTP {r.status_code}"}
            d = r.json()
    except Exception as e:
        return {"enabled": True, "ok": False, "error": str(e)[:200]}

    return {
        "enabled": True,
        "ok": bool(d.get("ok")),
        "egress_ip": d.get("egress_ip", ""),
        "authenticated": bool(d.get("authenticated")),
        "url": c["url"],
    }
