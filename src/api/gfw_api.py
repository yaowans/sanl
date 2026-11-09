"""GFW 境内探针 API —— 探针健康检查 / 手动检测 / 被墙节点列表 / 结论缓存管理

探针服务（cn-probe）部署在中国大陆境内 VPS，sanl（境外）调用它复核
IP:端口 的境内可达性，用于识别"境外连通但境内被墙"的节点。
"""
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Body
from pydantic import BaseModel, Field

from .. import auth
from ..gfw import (check_nodes, check_targets, probe_health, probe_enabled,
                   extract_target, format_target)
from ..schema import repository
from ..settings import (gfw_settings_payload, save_gfw_settings, reset_gfw_settings,
                        resolve_gfw_config)

logger = logging.getLogger(__name__)

router = APIRouter()


class CheckTargetsRequest(BaseModel):
    targets: List[str] = Field(..., description="'ip:port' 列表")
    timeout: Optional[float] = Field(None, ge=0.5, le=10)


class GfwConfigRequest(BaseModel):
    """保存 GFW 配置；只接受白名单键（见 settings.GFW_FIELDS）"""
    values: Dict[str, Any] = Field(default_factory=dict,
                                   description="配置键值对，如 {'gfw.enabled': true, 'gfw.probe_url': 'http://...'}")


class GfwConfigResetRequest(BaseModel):
    keys: Optional[List[str]] = Field(None, description="要清除覆盖的键；为空表示全部复位")


@router.get("/config")
async def gfw_get_config(request: Request):
    """获取 GFW 配置（字段元数据 + 当前生效值 + 来源标记）"""
    await auth.require_admin(request)
    return gfw_settings_payload()


@router.post("/config")
async def gfw_save_config(req: GfwConfigRequest, request: Request):
    """保存 GFW 配置（落库，立即生效，无需重启）"""
    await auth.require_admin(request)
    if not req.values:
        raise HTTPException(status_code=400, detail="values 不能为空")
    payload = save_gfw_settings(req.values)
    logger.info("GFW 配置已更新: %s", sorted(req.values.keys()))
    return {"status": "ok", **payload}


@router.post("/config/reset")
async def gfw_reset_config(req: GfwConfigResetRequest, request: Request):
    """清除配置覆盖，回落到环境变量 / app.yaml"""
    await auth.require_admin(request)
    payload = reset_gfw_settings(req.keys)
    return {"status": "ok", **payload}


@router.post("/config/test")
async def gfw_test_config(req: GfwConfigRequest, request: Request):
    """用给定配置试连探针（不落库），返回连通性与出口 IP

    用于用户填完地址还没保存时先验证，避免保存后才发现填错。
    """
    await auth.require_admin(request)
    if not req.values:
        raise HTTPException(status_code=400, detail="values 不能为空")

    # 合并：当前生效值 + 传入的临时覆盖
    base = resolve_gfw_config()
    merged = {}
    for k, v in req.values.items():
        merged[k] = v
    url = str(merged.get("gfw.probe_url", base.get("gfw.probe_url", "")) or "").rstrip("/")
    key = str(merged.get("gfw.probe_key", base.get("gfw.probe_key", "")) or "")
    enabled = merged.get("gfw.enabled", base.get("gfw.enabled", False))
    enabled = bool(enabled) and str(enabled).lower() not in ("false", "0", "no", "off")

    if not url:
        raise HTTPException(status_code=400, detail="请填写探针地址")
    if not key:
        raise HTTPException(status_code=400, detail="请填写探针密钥")

    import httpx
    try:
        async with httpx.AsyncClient() as http:
            r = await http.get(f"{url}/health", headers={"X-Probe-Key": key}, timeout=12)
            if r.status_code != 200:
                return {"status": "error", "ok": False, "enabled": enabled,
                        "error": f"HTTP {r.status_code}: {r.text[:200]}"}
            d = r.json()
    except Exception as e:
        return {"status": "error", "ok": False, "enabled": enabled,
                "error": f"{type(e).__name__}: {str(e)[:200]}"}

    egress = d.get("egress_ip", "")
    # 出口 IP 判定：仅 IPv4 且明显不是境外常见段时提示（启发式，页面只做提示）
    return {
        "status": "ok",
        "ok": bool(d.get("ok")),
        "enabled": enabled,
        "egress_ip": egress,
        "authenticated": bool(d.get("authenticated")),
        "url": url,
    }


@router.get("/config/probe-script")
async def gfw_probe_script(request: Request):
    """下载境内探针（cn-probe）部署脚本

    探针是零依赖单文件 Python，直接扔到国内小鸡上跑即可。
    """
    await auth.require_admin(request)
    path = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "cn_probe.py")
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="探针脚本未随程序分发")
    return {
        "status": "ok",
        "filename": "cn_probe.py",
        "content": content,
        "usage": [
            "python3 cn_probe.py --host 0.0.0.0 --port 8899 --key <你的密钥>",
            "# systemd 自启动示例：",
            "[Unit]",
            "Description=cn-probe GFW 境内连通性探针",
            "After=network.target",
            "",
            "[Service]",
            "ExecStart=/usr/bin/python3 /opt/cn_probe/cn_probe.py --port 8899 --key <你的密钥>",
            "Restart=always",
            "",
            "[Install]",
            "WantedBy=multi-user.target",
        ],
    }


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
async def gfw_check_nodes(request: Request, limit: int = Query(500, le=50000),
                          apply: bool = Query(False, description="是否把被墙节点降级为 inactive")):
    """对库内节点批量复核；apply=true 时把境内不可达的节点标记为 inactive"""
    await auth.require_admin(request)
    if not probe_enabled():
        raise HTTPException(status_code=400, detail="探针未启用或配置不完整")

    from ..gfw import _probe_config
    cfg = _probe_config()
    # 配置里的单轮上限生效（显式传入的 limit 取较小者）
    effective_limit = min(limit, int(cfg.get("check_limit", 5000)))
    # 是否降级被墙节点由配置决定（apply=true 只是显式请求）
    downgrade = apply and cfg.get("downgrade_blocked", True)

    nodes = repository.list_nodes(status="active", limit=effective_limit)
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

    if downgrade and blocked_ids:
        repository.mark_nodes_status(blocked_ids, "inactive")
        from ..schema.repository import update_node_scores
        update_node_scores()

    return {
        "checked": len(results),
        "blocked": len(blocked_ids),
        "applied": bool(downgrade and blocked_ids),
        "downgraded": len(blocked_ids) if downgrade else 0,
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
