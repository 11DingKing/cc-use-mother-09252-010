"""认证与授权：令牌换主体、按 scope + 项目粒度鉴权。"""
from __future__ import annotations

from typing import Optional

from ..domain import models as m
from ..domain.errors import AuthenticationError, AuthorizationError
from ..persistence.uow import Database


class AccessControl:
    def __init__(self, db: Database) -> None:
        self._db = db

    def authenticate(self, token: Optional[str]) -> m.Principal:
        if not token:
            raise AuthenticationError("缺少令牌")
        for p in self._db.principals.values():
            if token in p.tokens:
                return p
        raise AuthenticationError("令牌无效")

    def require(self, principal: m.Principal, scope: str,
                project_id: Optional[str] = None) -> None:
        if not principal.can(scope, project_id):
            raise AuthorizationError(
                f"主体 {principal.id} 缺少 {scope} 权限"
                + (f"（项目 {project_id}）" if project_id else ""))

    def require_scope(self, principal: m.Principal, scope: str) -> None:
        """仅要求持有该粒度（任意项目即可）；项目粒度由处理器按资源归属校验。"""
        if principal.can(m.SCOPE_ADMIN):
            return
        if not any(s == scope for s, _ in principal.grants):
            raise AuthorizationError(f"主体 {principal.id} 缺少 {scope} 粒度")
