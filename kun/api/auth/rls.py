"""Postgres RLS binding — `SET LOCAL app.tenant_id = :tid` 包装.

Phase 1 RLS policy 写死成 `current_setting('app.default_tenant_id', true)`,
全局所有连接 visible 同一租户. Phase 2 中期 ADR-019 切换路径:

  1. 改 alembic migration 让 RLS policy 用 `current_setting('app.tenant_id')`
     (不带 true → 缺失即报错, fail-closed)
  2. 在每个 transaction 入口调 bind_tenant_to_session(session, tenant_id)
  3. 任何遗漏的 path 都会被 RLS 阻断 (postgres 拒绝 row)

设计要点:
  - SET LOCAL 限当前 transaction, autocommit 模式下不生效 — caller 必须用
    session_scope (已经在 kun.core.db 全程用了).
  - tenant_id 用 bind parameter 防 SQL injection (postgres 不允许 SET 的值
    直接 parameterize, 所以白名单校验 + double-quote escape).
"""

from __future__ import annotations

import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# tenant_id 字符白名单 — 我们的 id 都是 [a-z0-9_-] 风格 ("u-sylvan", "t-acme").
# 这里宽松一点允许大小写 + 点 (容纳 UUID / email-style 等).
_TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")


class InvalidTenantIdError(ValueError):
    """Raised when tenant_id fails the safety regex."""


async def bind_tenant_to_session(
    session: AsyncSession, tenant_id: str
) -> None:
    """Bind tenant_id to current Postgres transaction for RLS.

    Idempotent within a transaction. Must be called *before* any data query.

    Raises:
      InvalidTenantIdError if tenant_id contains characters that could escape
        the SET LOCAL string (defense in depth — caller should also validate).
    """
    if not isinstance(tenant_id, str) or not tenant_id:
        raise InvalidTenantIdError("tenant_id must be a non-empty string")
    if not _TENANT_ID_PATTERN.match(tenant_id):
        raise InvalidTenantIdError(
            f"tenant_id contains unsupported characters: {tenant_id!r}"
        )
    # SET cannot use bind parameters in PG; we've validated the input above.
    # Use single-quote literal which the regex precludes from containing '.
    await session.execute(text(f"SET LOCAL app.tenant_id = '{tenant_id}'"))


__all__ = [
    "InvalidTenantIdError",
    "bind_tenant_to_session",
]
