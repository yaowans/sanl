"""账号鉴权 API —— 登录 / 登出 / 当前用户 / 修改密码

面板类接口（管理面板 + 写操作）需要登录；订阅短链 /sub/{token}/{fmt} 不受影响，
订阅客户端继续走 Token 机制。
"""
import logging
import secrets
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import auth
from ..schema import repository

logger = logging.getLogger(__name__)

router = APIRouter()


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class ChangePasswordRequest(BaseModel):
    old_password: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=256)


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    password: str = Field(..., min_length=8, max_length=256)
    role: str = Field("user", pattern="^(admin|user)$")


@router.post("/login")
async def login(req: LoginRequest, request: Request, response: Response):
    """账号密码登录，成功下发 HttpOnly 会话 Cookie"""
    allowed, reason = auth.check_login_allowed(request, req.username)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    user = repository.get_user_by_username(req.username)
    # 用户不存在与密码错误返回同一提示，避免用户名枚举
    if not user or not user.is_active:
        auth.record_login_fail(request, req.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    if not auth.verify_password(req.password, user.password_hash):
        auth.record_login_fail(request, req.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")

    auth.clear_login_fail(request, req.username)

    # 轮数升级时重算哈希
    if auth.needs_rehash(user.password_hash):
        try:
            repository.update_user_password(user.id, auth.hash_password(req.password))
        except Exception as e:
            logger.warning("password rehash failed: %s", e)

    session_id = auth.create_session(user.id, user.username, user.role)
    auth.set_session_cookie(response, session_id)
    logger.info("login ok: user=%s role=%s", user.username, user.role)
    return {"status": "ok", "user": {"id": user.id, "username": user.username,
                                     "role": user.role}}


@router.post("/logout")
async def logout(request: Request, response: Response):
    """登出并清除会话 Cookie"""
    sid = auth.read_session_cookie(request)
    if sid:
        auth.destroy_session(sid)
    auth.clear_session_cookie(response)
    return {"status": "ok"}


@router.get("/me")
async def me(request: Request):
    """当前登录状态；未登录返回 authenticated=False（前端据此跳登录页）"""
    user = await auth.current_user(request)
    if not user:
        return {"authenticated": False, "user": None}
    return {"authenticated": True, "user": user}


@router.post("/change-password")
async def change_password(req: ChangePasswordRequest, request: Request, response: Response):
    """修改当前用户密码；改后踢掉该用户所有会话"""
    user = await auth.current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")

    db_user = repository.get_user_by_username(user["username"])
    if not db_user or not auth.verify_password(req.old_password, db_user.password_hash):
        raise HTTPException(status_code=400, detail="原密码错误")

    repository.update_user_password(db_user.id, auth.hash_password(req.new_password))
    repository.delete_user_sessions(db_user.id)
    auth.clear_session_cookie(response)
    return {"status": "ok", "message": "密码已修改，请重新登录"}


# ===== 用户管理（仅管理员）=====

@router.get("/users")
async def list_users(request: Request):
    await auth.require_admin(request)
    return repository.list_users()


@router.post("/users")
async def create_user(req: CreateUserRequest, request: Request):
    await auth.require_admin(request)
    pwd_hash = auth.hash_password(req.password)
    u = repository.create_user(req.username, pwd_hash, req.role)
    if not u:
        raise HTTPException(status_code=409, detail="用户名已存在")
    return {"status": "ok", "id": u.id, "username": u.username, "role": u.role}


@router.put("/users/{user_id}/role")
async def set_user_role(user_id: int, request: Request, role: str = Body(..., embed=True)):
    await auth.require_admin(request)
    if role not in ("admin", "user"):
        raise HTTPException(status_code=400, detail="role must be admin|user")
    repository.update_user_role(user_id, role)
    # 角色变了，旧会话里的角色是快照，需踢掉
    repository.delete_user_sessions(user_id)
    return {"status": "ok"}


@router.put("/users/{user_id}/active")
async def set_user_active(user_id: int, request: Request, is_active: int = Body(..., embed=True)):
    await auth.require_admin(request)
    repository.toggle_user_active(user_id, 1 if is_active else 0)
    if not is_active:
        repository.delete_user_sessions(user_id)
    return {"status": "ok"}


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, request: Request):
    admin = await auth.require_admin(request)
    if admin["id"] == user_id:
        raise HTTPException(status_code=400, detail="不能删除当前登录账号")
    repository.delete_user_sessions(user_id)
    with repository.get_connection() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"status": "ok"}
