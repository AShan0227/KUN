"""TenantContext — 多租户 ambient context.

ADR-007: schema-ready + runtime 单租户默认
- day 1 所有业务表 tenant_id 非空
- 应用层 ambient TenantContext, 默认 "u-sylvan"
- 未来从 auth token 解析, 业务代码零改动
"""

from __future__ import annotations

import os
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Self


@dataclass(frozen=True)
class TenantContext:
    """当前租户上下文."""

    tenant_id: str
    user_id: str | None = None
    project_id: str | None = None
    # 权限作用域; 空 = 继承默认
    scopes: tuple[str, ...] = ()


_DEFAULT_TENANT_ID = os.getenv("KUN_DEFAULT_TENANT_ID", "u-sylvan")
_DEFAULT = TenantContext(tenant_id=_DEFAULT_TENANT_ID)

_current: ContextVar[TenantContext] = ContextVar("tenant", default=_DEFAULT)


def current_tenant() -> TenantContext:
    """Return the tenant context for the current task / request."""
    return _current.get()


def default_tenant_id() -> str:
    """Return the hardcoded default tenant_id (ADR-007)."""
    return _DEFAULT_TENANT_ID


class tenant_scope:  # noqa: N801 — intentional lowercase for `with` usage
    """Override tenant for a block (mostly for tests / internal jobs).

    Usage:
        with tenant_scope(TenantContext(tenant_id="u-other")):
            ...
    """

    def __init__(self, ctx: TenantContext) -> None:
        self.ctx = ctx
        self._token: Token[TenantContext] | None = None

    def __enter__(self) -> Self:
        self._token = _current.set(self.ctx)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _current.reset(self._token)
