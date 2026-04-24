"""TenantContext — 多租户 ambient context.

ADR-007: schema-ready + runtime tenant context
- day 1 所有业务表 tenant_id 非空
- dev/staging 可用默认租户简化本地开发
- production 必须由请求/任务显式带 tenant，不能静默落到默认租户
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Self

from kun.core.config import settings


@dataclass(frozen=True)
class TenantContext:
    """当前租户上下文."""

    tenant_id: str
    user_id: str | None = None
    project_id: str | None = None
    # 权限作用域; 空 = 继承默认
    scopes: tuple[str, ...] = ()


class MissingTenantContextError(RuntimeError):
    """Raised when production code tries to run without explicit tenant context."""


_current: ContextVar[TenantContext | None] = ContextVar("tenant", default=None)


def current_tenant() -> TenantContext:
    """Return the tenant context for the current task / request."""
    ctx = _current.get()
    if ctx is not None:
        return ctx
    tenant_id = default_tenant_id()
    if tenant_id is None:
        raise MissingTenantContextError("explicit tenant context is required")
    return TenantContext(tenant_id=tenant_id)


def default_tenant_id() -> str | None:
    """Return the configured dev/staging fallback tenant, never in production."""
    cfg = settings()
    if cfg.env == "production":
        return None
    tenant_id = (cfg.default_tenant_id or "").strip()
    return tenant_id or None


def resolve_tenant_id(explicit_tenant_id: str | None) -> str:
    """Resolve tenant from an explicit value or the non-production fallback."""
    explicit = (explicit_tenant_id or "").strip()
    if explicit:
        return explicit
    tenant_id = default_tenant_id()
    if tenant_id is None:
        raise MissingTenantContextError("explicit tenant id is required")
    return tenant_id


class tenant_scope:  # noqa: N801 — intentional lowercase for `with` usage
    """Override tenant for a block (mostly for tests / internal jobs).

    Usage:
        with tenant_scope(TenantContext(tenant_id="u-other")):
            ...
    """

    def __init__(self, ctx: TenantContext) -> None:
        self.ctx = ctx
        self._token: Token[TenantContext | None] | None = None

    def __enter__(self) -> Self:
        self._token = _current.set(self.ctx)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _current.reset(self._token)
