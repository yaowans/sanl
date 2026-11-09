"""
运行时配置中心

之前 GFW 检测的探针地址/密钥只能写死在 docker-compose 环境变量里
（CN_PROBE_URL / CN_PROBE_KEY / CN_PROBE_ENABLED），改一次要重启容器，
管理面板也无法展示当前生效值，更没法让用户自助配置。

本模块把配置拆成两层：
- **静态来源**：app.yaml 的 gfw 段 + 环境变量（部署期兜底，仍兼容）
- **运行时覆盖**：管理面板保存到 app_settings 表（DB > 环境变量 > app.yaml）

优先级：DB 覆盖 > 环境变量 > app.yaml > 内置默认值。
覆盖项在进程内缓存，保存后立即失效重载，无需重启。
"""
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from .config import get_gfw_config  # noqa: F401  (兼容旧调用方)

logger = logging.getLogger(__name__)

# 覆盖项缓存：(ts, dict)；保存/清除后 invalidate 立即失效
_CACHE: Dict[str, Any] = {"ts": 0.0, "data": {}}
_CACHE_TTL = 30.0
_LOCK = threading.Lock()

# ===== GFW 配置字段定义 =====
# 前端配置页据此自动渲染表单 + 说明文案，后端是唯一事实来源，
# 避免前后端各写一份字段列表导致漂移。
GFW_FIELDS: List[dict] = [
    {
        "key": "gfw.enabled",
        "label": "启用 GFW 检测",
        "type": "bool",
        "default": False,
        "env": "CN_PROBE_ENABLED",
        "yaml": "gfw.enabled",
        "help": "总开关。关闭后测速流程不再调用境内探针，订阅输出也不再按封锁结论过滤。",
    },
    {
        "key": "gfw.probe_url",
        "label": "探针地址",
        "type": "text",
        "default": "",
        "env": "CN_PROBE_URL",
        "yaml": "gfw.probe_url",
        "placeholder": "http://43.136.58.195:8899",
        "help": "境内探针（cn-probe）的完整地址，不要带 /check 后缀。探针必须部署在中国大陆境内 VPS，"
                "否则检测结果无法反映 GFW 拦截。",
    },
    {
        "key": "gfw.probe_key",
        "label": "探针密钥",
        "type": "password",
        "default": "",
        "env": "CN_PROBE_KEY",
        "yaml": "gfw.probe_key",
        "placeholder": "X-Probe-Key 请求头使用的共享密钥",
        "help": "与探针启动参数 --key 一致，用于鉴权防止探针被滥用。留空则探针不可用。",
    },
    {
        "key": "gfw.timeout",
        "label": "单目标超时（秒）",
        "type": "number",
        "default": 3,
        "min": 0.5,
        "max": 10,
        "step": 0.5,
        "yaml": "gfw.timeout",
        "help": "探针对单个 IP:端口 的 TCP 连接等待时间。GFW 拦截典型表现为超时，"
                "适当调大可减少误判，但会拉长整轮检测耗时。",
    },
    {
        "key": "gfw.batch_size",
        "label": "单批目标数",
        "type": "number",
        "default": 100,
        "min": 1,
        "max": 500,
        "step": 10,
        "yaml": "gfw.batch_size",
        "help": "每次 POST /check 携带的目标数量。探针单次上限 500，"
                "批太大容易触发探针超时，太小则往返次数多。",
    },
    {
        "key": "gfw.check_limit",
        "label": "单轮检测节点上限",
        "type": "number",
        "default": 5000,
        "min": 100,
        "max": 50000,
        "step": 500,
        "yaml": "gfw.check_limit",
        "help": "每轮测速后最多复核多少个 active 节点。节点池很大时限制上限可避免探针被压垮。",
    },
    {
        "key": "gfw.auto_check",
        "label": "测速后自动复核",
        "type": "bool",
        "default": True,
        "yaml": "gfw.auto_check",
        "help": "开启后，每轮测速结束会自动调用境内探针复核存活节点，"
                "把「境外连通但境内被墙」的节点降级为 inactive。",
    },
    {
        "key": "gfw.filter_subscription",
        "label": "订阅输出过滤被墙节点",
        "type": "bool",
        "default": True,
        "yaml": "gfw.filter_subscription",
        "help": "开启后，默认订阅只输出探针确认境内可达的节点。"
                "关闭则订阅只按测速结果输出，不做境内复核。",
    },
    {
        "key": "gfw.downgrade_blocked",
        "label": "被墙节点降级为不可用",
        "type": "bool",
        "default": True,
        "yaml": "gfw.downgrade_blocked",
        "help": "开启后，复核判定境内不可达的节点会被标记为 inactive（不参与默认订阅），"
                "但不会硬删除节点——随时可以在节点列表里手动删。",
    },
]

_FIELD_BY_KEY = {f["key"]: f for f in GFW_FIELDS}

# 环境变量 → 配置键 的反向映射（仅带 CN_PROBE_ 前缀的老配置）
_ENV_KEYS = {f["env"]: f["key"] for f in GFW_FIELDS if f.get("env")}


def invalidate_settings_cache():
    """配置变更后清缓存（下次读取立即生效）"""
    with _LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["data"] = {}


def _load_overrides() -> Dict[str, str]:
    """读取 DB 覆盖项（带 30s 进程内缓存，减轻高频读取压力）"""
    now = time.time()
    with _LOCK:
        if now - _CACHE["ts"] < _CACHE_TTL and _CACHE["data"]:
            return dict(_CACHE["data"])
    try:
        from .schema.repository import get_app_settings
        data = get_app_settings()
    except Exception as e:
        logger.warning("读取 app_settings 失败: %s", e)
        data = {}
    with _LOCK:
        _CACHE["ts"] = now
        _CACHE["data"] = dict(data)
    return dict(data)


def _env_value(field: dict):
    """环境变量取值（带类型转换）"""
    env_name = field.get("env")
    if not env_name:
        return None
    raw = os.environ.get(env_name)
    if raw is None or raw == "":
        return None
    return _coerce(field, raw)


def _yaml_value(field: dict):
    """app.yaml 取值（yaml 路径相对 app.yaml 根，如 gfw.probe_url）"""
    yaml_path = field.get("yaml")
    if not yaml_path:
        return None
    try:
        from .config import load_config
        node = load_config()
        for part in yaml_path.split("."):
            if not isinstance(node, dict):
                return None
            node = node.get(part)
        if node is None:
            return None
        return _coerce(field, node)
    except Exception:
        return None


def _coerce(field: dict, value):
    """按字段类型转换值"""
    t = field.get("type", "text")
    try:
        if t == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on")
        if t == "number":
            return float(value)
        return str(value)
    except (TypeError, ValueError):
        return field.get("default")


def resolve_gfw_config() -> Dict[str, Any]:
    """解析当前生效的 GFW 配置（DB 覆盖 > 环境变量 > app.yaml > 默认值）"""
    overrides = _load_overrides()
    out: Dict[str, Any] = {}
    for f in GFW_FIELDS:
        key = f["key"]
        if key in overrides:
            val = _coerce(f, overrides[key])
            src = "db"
        else:
            val = _env_value(f)
            src = "env"
            if val is None:
                val = _yaml_value(f)
                src = "yaml"
            if val is None:
                val = f.get("default")
                src = "default"
        out[key] = val
        out[f"{key}__source"] = src
    return out


def get_gfw_value(key: str, default=None):
    """按配置键读取生效值"""
    return resolve_gfw_config().get(key, default)


def gfw_probe_enabled() -> bool:
    """探针是否启用且配置完整（地址 + 密钥都在）"""
    cfg = resolve_gfw_config()
    return bool(cfg.get("gfw.enabled")) and bool(cfg.get("gfw.probe_url")) \
        and bool(cfg.get("gfw.probe_key"))


def gfw_settings_payload() -> dict:
    """配置页数据：字段元数据 + 当前值 + 来源标记（密钥脱敏）"""
    cfg = resolve_gfw_config()
    items = []
    for f in GFW_FIELDS:
        key = f["key"]
        val = cfg.get(key)
        src = cfg.get(f"{key}__source", "default")
        if f["type"] == "password" and val:
            val = "••••••" + str(val)[-4:] if len(str(val)) > 4 else "••••"
        items.append({
            "key": key,
            "label": f["label"],
            "type": f["type"],
            "value": val,
            "source": src,
            "default": f.get("default"),
            "min": f.get("min"),
            "max": f.get("max"),
            "step": f.get("step"),
            "placeholder": f.get("placeholder", ""),
            "help": f.get("help", ""),
        })
    return {
        "items": items,
        "overrides": sorted(_load_overrides().keys()),
        "probe_ready": gfw_probe_enabled(),
    }


def save_gfw_settings(values: Dict[str, Any]) -> Dict[str, Any]:
    """保存 GFW 配置（只接受白名单键；空字符串表示清除该项覆盖）"""
    from .schema.repository import set_app_settings, delete_app_settings

    to_set: Dict[str, str] = {}
    to_clear: List[str] = []
    for raw_key, raw_val in (values or {}).items():
        key = str(raw_key).strip()
        f = _FIELD_BY_KEY.get(key)
        if not f:
            continue
        if raw_val is None:
            to_clear.append(key)
            continue
        if f["type"] == "bool":
            val = "true" if (raw_val is True or str(raw_val).strip().lower()
                             in ("1", "true", "yes", "on")) else "false"
        elif f["type"] == "number":
            try:
                num = float(raw_val)
            except (TypeError, ValueError):
                continue
            lo, hi = f.get("min"), f.get("max")
            if lo is not None and num < lo:
                num = lo
            if hi is not None and num > hi:
                num = hi
            val = str(num)
        else:
            val = str(raw_val).strip()
            if not val:
                # 空值 = 清除覆盖，回落到环境变量/app.yaml
                to_clear.append(key)
                continue
        to_set[key] = val

    if to_clear:
        delete_app_settings(to_clear)
    if to_set:
        set_app_settings(to_set)
    invalidate_settings_cache()
    return gfw_settings_payload()


def reset_gfw_settings(keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """清除覆盖项（全部或指定），回落到环境变量 / app.yaml"""
    from .schema.repository import delete_app_settings

    targets = [k for k in (keys or []) if k in _FIELD_BY_KEY] or list(_FIELD_BY_KEY.keys())
    delete_app_settings(targets)
    invalidate_settings_cache()
    return gfw_settings_payload()
