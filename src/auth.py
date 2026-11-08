"""
账号密码鉴权模块

设计：
- 密码哈希：PBKDF2-HMAC-SHA256（标准库实现，无需额外依赖），每用户独立随机盐
- 会话：HttpOnly Cookie 承载不透明会话 ID，服务端存表校验，可撤销
- 令牌：订阅 Token 与账号会话相互独立，订阅客户端不受面板登录影响
"""
import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Optional

from fastapi import Request, Response

from .schema import repository

logger = logging.getLogger(__name__)

# PBKDF2 参数：OWASP 建议 sha256 + 600k 轮；200k 在登录 QPS 下兼顾耗时与体验
_PBKDF2_ROUNDS = 200_000
_SESSION_COOKIE = "sanl_session"
_SESSION_TTL = 7 * 24 * 3600  # 会话有效期 7 天
_LOGIN_MAX_FAIL = 10           # 连续失败上限
_LOGIN_LOCK_SECONDS = 300      # 触发后锁定 5 分钟


def hash_password(password: str) -> str:
    """生成密码哈希：pbkdf2_sha256$轮数$盐(hex)$哈希(hex)"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码；哈希格式非法或轮数不符返回 False（不抛异常，避免泄露信息）"""
    try:
        algo, rounds, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError, TypeError):
        return False


def needs_rehash(stored: str) -> bool:
    """哈希轮数低于当前标准时需在登录成功路径上重算"""
    try:
        algo, rounds, _, _ = stored.split("$")
        return algo == "pbkdf2_sha256" and int(rounds) < _PBKDF2_ROUNDS
    except (ValueError, AttributeError, TypeError):
        return True


# ===== 会话 =====

def create_session(user_id: int, username: str, role: str) -> str:
    """创建会话并落库，返回 session id"""
    session_id = secrets.token_urlsafe(32)
    repository.create_session(session_id, user_id, username, role, int(time.time()) + _SESSION_TTL)
    return session_id


def destroy_session(session_id: str):
    repository.delete_session(session_id)


def get_session_user(session_id: str) -> Optional[dict]:
    """校验会话并返回用户信息；过期/无效返回 None"""
    if not session_id:
        return None
    sess = repository.get_session(session_id)
    if not sess:
        return None
    if sess.get("expires_at", 0) < int(time.time()):
        repository.delete_session(session_id)
        return None
    user = repository.get_user_by_id(sess["user_id"])
    if not user or not user.is_active:
        return None
    return {"id": user.id, "username": user.username, "role": user.role}


def set_session_cookie(response: Response, session_id: str):
    response.set_cookie(
        _SESSION_COOKIE, session_id,
        max_age=_SESSION_TTL, httponly=True, samesite="lax",
        secure=False,  # 部署为纯 HTTP；启用 HTTPS 反代后改为 True
        path="/",
    )


def clear_session_cookie(response: Response):
    response.delete_cookie(_SESSION_COOKIE, path="/")


def read_session_cookie(request: Request) -> str:
    return request.cookies.get(_SESSION_COOKIE, "") or ""


# ===== 登录限流（按用户名+IP 维度，防撞库）=====

def _login_key(request: Request, username: str) -> str:
    ip = request.client.host if request.client else "unknown"
    return f"{ip}|{username.lower()}"


def check_login_allowed(request: Request, username: str) -> tuple[bool, str]:
    """检查是否允许登录；返回 (是否允许, 原因)"""
    state = repository.get_login_fail_state(_login_key(request, username))
    if state and state["fail_count"] >= _LOGIN_MAX_FAIL:
        left = _LOGIN_LOCK_SECONDS - (int(time.time()) - state["last_fail_at"])
        if left > 0:
            return False, f"登录尝试过多，请 {left} 秒后重试"
    return True, ""


def record_login_fail(request: Request, username: str):
    repository.record_login_fail(_login_key(request, username))


def clear_login_fail(request: Request, username: str):
    repository.clear_login_fail(_login_key(request, username))


# ===== FastAPI 依赖 =====

async def current_user(request: Request) -> Optional[dict]:
    """当前登录用户；未登录返回 None。用于 Depends() 挂到管理接口"""
    return get_session_user(read_session_cookie(request))


async def require_admin(request: Request) -> dict:
    """要求管理员身份，否则 403"""
    user = await current_user(request)
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    if user.get("role") != "admin":
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user
