"""GFW 境内探针 API —— 探针健康检查 / 手动检测 / 被墙节点列表 / 结论缓存管理

探针服务（cn-probe）部署在中国大陆境内 VPS，sanl（境外）调用它复核
IP:端口 的境内可达性，用于识别"境外连通但境内被墙"的节点。
"""
import asyncio
import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Body
from pydantic import BaseModel, Field

from .. import auth
from ..gfw import (check_nodes, check_targets, probe_health, probe_enabled,
                   extract_target, format_target)
from ..schema import repository

logger = logging.getLogger(__name__)

router = APIRouter()


class CheckTargetsRequest(BaseModel):
    targets: List[str] = Field(..., description="'ip:port' 列表")
    timeout: Optional[float] = Field(None, ge=0.5, le=10)


@router.get("/health")
async def gfw_health(request: Request):
    """探针健康状态（含出口 IP，便于确认探针确实在境内）"""
    await auth.require_admin(request)
    return await probe_health()


@router.post("/check")
async def gfw_check(req: CheckTargetsRequest, request: Request):
    """手动检测一批 ip:port 的境内可达性"""
    await auth.require_admin(request)
    if not probe_enabled():
        raise HTTPException(status_code=400, detail="探针未启用或配置不完整")
    if not req.targets:
        raise HTTPException(status_code=400, detail="targets 不能为空")
    if len(req.targets) > 500:
        raise HTTPException(status_code=400, detail="单次最多 500 个目标")

    results = await check_targets(req.targets)
    if not results:
        raise HTTPException(status_code=502, detail="探针无响应")
    reachable = sum(1 for r in results.values() if r.get("reachable"))
    return {
        "results": [{"target": t, **r} for t, r in results.items()],
        "summary": {"total": len(results), "reachable": reachable,
                    "blocked": len(results) - reachable},
    }


@router.post("/check-nodes")
async def gfw_check_nodes(request: Request, limit: int = Query(500, le=5000),
                          apply: bool = Query(False, description="是否把被墙节点降级为 inactive")):
    """对库内节点批量复核；apply=true 时把境内不可达的节点标记为 inactive"""
    await auth.require_admin(request)
    if not probe_enabled():
        raise HTTPException(status_code=400, detail="探针未启用或配置不完整")

    nodes = repository.list_nodes(status="active", limit=limit)
    if not nodes:
        return {"checked": 0, "blocked": 0, "message": "无 active 节点"}

    payload = [{"id": n.id, "node_data": n.node_data} for n in nodes]
    results = await check_nodes(payload)
    if not results:
        raise HTTPException(status_code=502, detail="探针无响应")

    blocked_ids = []
    for nid, r in results.items():
        repository.save_gfw_check(
            r["server"], r["port"], 1 if r["blocked"] else 0, 1,
            source="cn-probe", detail=r.get("error", ""), latency=r.get("latency_ms"),
        )
        repository.set_node_gfw_status(
            nid, r["server"], r["port"], 1 if r["blocked"] else 0, 1, "cn-probe"
        )
        if r["blocked"]:
            blocked_ids.append(nid)

    if apply and blocked_ids:
        repository.mark_nodes_status(blocked_ids, "inactive")
        from ..schema.repository import update_node_scores
        update_node_scores()

    return {
        "checked": len(results),
        "blocked": len(blocked_ids),
        "applied": bool(apply and blocked_ids),
        "blocked_nodes": blocked_ids[:100],
    }


@router.get("/blocked")
async def gfw_blocked(request: Request, limit: int = Query(200, le=1000)):
    """列出被判定为境内不可达的节点"""
    await auth.require_admin(request)
    return repository.list_gfw_blocked_nodes(limit=limit)


@router.get("/stats")
async def gfw_stats(request: Request):
    """GFW 检测统计"""
    await auth.require_admin(request)
    return repository.gfw_stats()


@router.delete("/cache")
async def gfw_clear_cache(request: Request):
    """清空 GFW 结论缓存（下次测速会重新探测）"""
    await auth.require_admin(request)
    repository.clear_gfw_status()
    return {"status": "ok"}
