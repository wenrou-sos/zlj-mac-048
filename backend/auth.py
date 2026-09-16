"""认证与授权：用户、岗位(角色)、牛舍范围、会话令牌、服务端鉴权。

设计要点：
- 一个用户可拥有多个岗位（兼岗），权限为所有岗位并集；
- 牛舍范围来自 UserShedAssignment，支持临时接管（valid_from/valid_to），
  调岗/停用/退出只需改岗位、改分配有效期或停用账号，下一次请求即生效（权限不缓存）；
- 会话令牌存数据库，停用用户/修改其岗位后可整体吊销，立即失效；
- 所有业务接口在服务端逐请求鉴权，前端隐藏按钮仅为体验，不能作为安全边界；
- 管理员/场长为"全场范围"，其余岗位仅能访问被分配牛舍内的牛只数据。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Optional, Set

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import or_
from sqlalchemy.orm import Session

from . import models
from .database import get_db

# ---------------- 岗位与权限矩阵 ----------------
ROLES = ["admin", "manager", "vet", "milker", "viewer"]
ROLE_LABELS = {
    "admin": "管理员",
    "manager": "场长",
    "vet": "兽医",
    "milker": "挤奶员",
    "viewer": "只读人员",
}

# 权限码：模块:动作
_ALL_BUSINESS = [
    "cow:read", "cow:write",
    "milking:read", "milking:write", "milking:discard",
    "health:read", "health:write",
    "medication:read", "medication:write",
    "estrus:read", "estrus:write",
    "drug:read", "drug:write",
    "report:read",
]
_ROLE_PERMS = {
    "admin": ["*"],
    "manager": _ALL_BUSINESS + ["shed:manage", "user:manage"],
    # 兽医：本舍内健康/用药/发情配种，可读档案、药品目录、看板提醒
    "vet": [
        "cow:read", "report:read", "drug:read",
        "health:read", "health:write",
        "medication:read", "medication:write",
        "estrus:read", "estrus:write",
        "milking:read", "milking:discard",
    ],
    # 挤奶员：本舍内挤奶记录（仅本人所录可改删）、休药期补标废弃
    "milker": [
        "cow:read", "report:read", "drug:read",
        "milking:read", "milking:write", "milking:discard",
        "health:read", "medication:read", "estrus:read",
    ],
    # 只读人员：本舍内一切只读
    "viewer": [
        "cow:read", "milking:read", "health:read", "medication:read",
        "estrus:read", "drug:read", "report:read",
    ],
}

# 哪些岗位默认拥有"全场范围"（不限牛舍）
GLOBAL_ROLES = {"admin", "manager"}

SESSION_TTL = timedelta(hours=12)
COOKIE_NAME = "dairy_session"


# ---------------- 密码哈希（标准库 PBKDF2，无需额外依赖） ----------------
def hash_password(password: str, *, iterations: int = 200_000) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), iterations)
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, want = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(dk.hex(), want)
    except (ValueError, TypeError):
        return False


# ---------------- 当前登录主体 ----------------
@dataclass
class Principal:
    user: models.User
    db: Session
    _perms: Optional[Set[str]] = None
    _shed_ids: Optional[Set[int]] = None

    # ---- 角色与权限 ----
    @property
    def roles(self) -> list:
        return [r for r in (self.user.roles or "").split(",") if r]

    @property
    def is_global(self) -> bool:
        return bool(GLOBAL_ROLES.intersection(self.roles))

    def permissions(self) -> Set[str]:
        if self._perms is None:
            perms: Set[str] = set()
            for r in self.roles:
                perms.update(_ROLE_PERMS.get(r, []))
            self._perms = perms
        return self._perms

    def can(self, code: str) -> bool:
        perms = self.permissions()
        return "*" in perms or code in perms

    def require(self, code: str) -> None:
        if not self.can(code):
            raise HTTPException(403, "没有执行该操作的权限")

    # ---- 牛舍范围 ----
    def shed_ids(self) -> Optional[Set[int]]:
        """返回可访问牛舍 id 集合；None 表示全场范围。每次请求实时计算。"""
        if self.is_global:
            return None
        if self._shed_ids is None:
            today = date.today()
            rows = self.db.query(models.UserShedAssignment.shed_id).filter(
                models.UserShedAssignment.user_id == self.user.id,
                or_(models.UserShedAssignment.valid_from.is_(None),
                    models.UserShedAssignment.valid_from <= today),
                or_(models.UserShedAssignment.valid_to.is_(None),
                    models.UserShedAssignment.valid_to >= today),
            ).all()
            self._shed_ids = {r[0] for r in rows}
        return self._shed_ids

    def cow_ids_query(self):
        """可访问牛只 id 的子查询（供 service 层过滤）；None 表示不限。"""
        ids = self.shed_ids()
        if ids is None:
            return None
        # 空集合时返回一个不可能命中的子查询，而不是"不加条件"导致越权看全场
        return self.db.query(models.Cow.id).filter(models.Cow.shed_id.in_(ids or {-1}))

    def can_access_cow(self, cow: Optional[models.Cow]) -> bool:
        if cow is None:
            return False
        ids = self.shed_ids()
        return ids is None or cow.shed_id in ids

    def require_cow(self, cow: Optional[models.Cow]) -> models.Cow:
        if cow is None:
            raise HTTPException(404, "未找到该牛")
        if not self.can_access_cow(cow):
            raise HTTPException(403, "该牛不在您负责的牛舍范围内")
        return cow

    def reset_scope_cache(self) -> None:
        self._shed_ids = None
        self._perms = None


# ---------------- 会话解析 ----------------
def _extract_token(request: Request, authorization: Optional[str]) -> Optional[str]:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip()
    return request.cookies.get(COOKIE_NAME)


def get_principal(
    request: Request,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Principal:
    """必须登录"""
    token = _extract_token(request, authorization)
    if not token:
        raise HTTPException(401, "未登录或登录已失效")
    sess = db.query(models.AuthSession).filter_by(token=token).first()
    now = datetime.utcnow()
    if not sess or not sess.active or (sess.expires_at and sess.expires_at < now):
        raise HTTPException(401, "登录已过期，请重新登录")
    user = db.get(models.User, sess.user_id)
    if not user or not user.active:
        # 停用/退出立即生效：直接拒绝并吊销该会话
        if sess:
            sess.active = False
            db.commit()
        raise HTTPException(401, "账号已停用，请联系管理员")
    return Principal(user=user, db=db)


def get_optional_principal(
    request: Request,
    authorization: Optional[str] = Header(None),
    db: Session = Depends(get_db),
) -> Optional[Principal]:
    token = _extract_token(request, authorization)
    if not token:
        return None
    sess = db.query(models.AuthSession).filter_by(token=token).first()
    now = datetime.utcnow()
    if not sess or not sess.active or (sess.expires_at and sess.expires_at < now):
        return None
    user = db.get(models.User, sess.user_id)
    if not user or not user.active:
        return None
    return Principal(user=user, db=db)


def create_session(db: Session, user: models.User) -> models.AuthSession:
    """登录成功：吊销该用户旧会话（保证调岗/改密后旧令牌不能继续用），签发新会话"""
    db.query(models.AuthSession).filter_by(user_id=user.id, active=True).update(
        {models.AuthSession.active: False}
    )
    sess = models.AuthSession(
        user_id=user.id,
        token=secrets.token_urlsafe(32),
        expires_at=datetime.utcnow() + SESSION_TTL,
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def revoke_sessions(db: Session, user_id: int) -> None:
    """管理员停用/重置密码后调用：全部会话立即失效"""
    db.query(models.AuthSession).filter_by(user_id=user_id, active=True).update(
        {models.AuthSession.active: False}
    )
    db.commit()


# ---------------- 路由依赖工厂 ----------------
def require_perm(code: str):
    def _dep(p: Principal = Depends(get_principal)) -> Principal:
        p.require(code)
        return p
    return _dep
