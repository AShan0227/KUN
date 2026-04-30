"""Operator-managed tenant/account registry.

This is deliberately not a full SaaS signup/billing system yet.  It gives KUN
a durable source of truth for tenant ownership, member scopes, and issued token
inventory so production onboarding is auditable instead of being a one-off JSON
blob copied from a terminal.
"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kun.core.orm import TenantAccountRow, TenantMemberRow, TenantTokenIssueRow
from kun.security.auth import sign_auth_token

Audience = Literal["novice", "developer", "expert"]
MemberRole = Literal["owner", "admin", "member", "viewer"]


class TenantAccountBootstrap(BaseModel):
    """Result returned by ops account bootstrap."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    organization_id: str
    display_name: str
    owner_user_id: str
    plan: str = "dev"
    billing_status: str = "manual"
    token_id: str
    token_hash: str
    bearer_token: str
    scopes: list[str] = Field(default_factory=list)
    expires_at: int
    persisted: bool
    honest_limits: list[str] = Field(default_factory=list)


class TenantAccountRecord(BaseModel):
    """Tenant account/member upsert result without issuing a bearer token."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    organization_id: str
    display_name: str
    owner_user_id: str
    role: MemberRole = "owner"
    plan: str = "dev"
    billing_status: str = "manual"
    persisted: bool = True
    honest_limits: list[str] = Field(default_factory=list)


class TenantMemberInvite(BaseModel):
    """Durable member invitation ledger result."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str
    role: MemberRole
    scopes: list[str] = Field(default_factory=list)
    status: str = "invited"
    honest_limits: list[str] = Field(default_factory=list)


def hash_bearer_token(token: str) -> str:
    """Hash a bearer token for audit correlation without storing the secret."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_bootstrap_token(
    *,
    tenant_id: str,
    secret: str,
    user_id: str,
    scopes: list[str],
    audience: Audience = "developer",
    ttl_sec: int = 86400,
    token_id: str | None = None,
) -> tuple[str, str, int]:
    """Mint a signed token and return ``(token_id, token, expires_at)``."""

    cleaned_tenant = tenant_id.strip()
    cleaned_user = user_id.strip()
    if not cleaned_tenant:
        raise ValueError("tenant_id is required")
    if not cleaned_user:
        raise ValueError("user_id is required")
    if len(secret) < 32:
        raise ValueError("secret must be at least 32 characters")
    cleaned_scopes = [scope.strip() for scope in scopes if scope.strip()]
    expires_at = int(time.time()) + ttl_sec
    stable_token_id = token_id or _token_id(cleaned_tenant, cleaned_user, expires_at)
    token = sign_auth_token(
        {
            "tenant_id": cleaned_tenant,
            "user_id": cleaned_user,
            "scopes": cleaned_scopes,
            "audience": audience,
            "exp": expires_at,
            "jti": stable_token_id,
        },
        secret,
    )
    return stable_token_id, token, expires_at


async def bootstrap_tenant_account(
    session: AsyncSession,
    *,
    tenant_id: str,
    organization_id: str,
    display_name: str,
    owner_user_id: str,
    secret: str,
    scopes: list[str],
    audience: Audience = "developer",
    role: MemberRole = "owner",
    plan: str = "dev",
    billing_status: str = "manual",
    ttl_sec: int = 86400,
    metadata: dict[str, Any] | None = None,
) -> TenantAccountBootstrap:
    """Upsert tenant account/member rows and store a token issuance record."""

    cleaned_tenant = _required("tenant_id", tenant_id)
    cleaned_org = _required("organization_id", organization_id)
    cleaned_name = _required("display_name", display_name)
    cleaned_owner = _required("owner_user_id", owner_user_id)
    cleaned_scopes = [scope.strip() for scope in scopes if scope.strip()]
    token_id, token, expires_at = build_bootstrap_token(
        tenant_id=cleaned_tenant,
        secret=secret,
        user_id=cleaned_owner,
        scopes=cleaned_scopes,
        audience=audience,
        ttl_sec=ttl_sec,
    )
    token_hash = hash_bearer_token(token)
    now = datetime.now(UTC)

    await upsert_tenant_account_member(
        session,
        tenant_id=cleaned_tenant,
        organization_id=cleaned_org,
        display_name=cleaned_name,
        owner_user_id=cleaned_owner,
        scopes=cleaned_scopes,
        role=role,
        plan=plan,
        billing_status=billing_status,
        metadata=metadata,
        now=now,
    )
    token_stmt = (
        pg_insert(TenantTokenIssueRow)
        .values(
            tenant_id=cleaned_tenant,
            token_id=token_id,
            token_hash=token_hash,
            user_id=cleaned_owner,
            audience=audience,
            scopes=cleaned_scopes,
            status="issued",
            expires_at=datetime.fromtimestamp(expires_at, UTC),
            metadata_json={"source": "ops.account_bootstrap"},
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[TenantTokenIssueRow.tenant_id, TenantTokenIssueRow.token_id],
            set_={
                "token_hash": token_hash,
                "user_id": cleaned_owner,
                "audience": audience,
                "scopes": cleaned_scopes,
                "status": "issued",
                "expires_at": datetime.fromtimestamp(expires_at, UTC),
                "revoked_at": None,
                "metadata_json": {"source": "ops.account_bootstrap"},
                "updated_at": now,
            },
        )
    )
    await session.execute(token_stmt)
    return TenantAccountBootstrap(
        tenant_id=cleaned_tenant,
        organization_id=cleaned_org,
        display_name=cleaned_name,
        owner_user_id=cleaned_owner,
        plan=plan.strip() or "dev",
        billing_status=billing_status.strip() or "manual",
        token_id=token_id,
        token_hash=token_hash,
        bearer_token=token,
        scopes=cleaned_scopes,
        expires_at=expires_at,
        persisted=True,
        honest_limits=_honest_limits(),
    )


async def upsert_tenant_account_member(
    session: AsyncSession,
    *,
    tenant_id: str,
    organization_id: str,
    display_name: str,
    owner_user_id: str,
    scopes: list[str],
    role: MemberRole = "owner",
    plan: str = "dev",
    billing_status: str = "manual",
    metadata: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> TenantAccountRecord:
    """Upsert tenant account + owner/member rows without minting a token."""

    cleaned_tenant = _required("tenant_id", tenant_id)
    cleaned_org = _required("organization_id", organization_id)
    cleaned_name = _required("display_name", display_name)
    cleaned_owner = _required("owner_user_id", owner_user_id)
    cleaned_scopes = [scope.strip() for scope in scopes if scope.strip()]
    timestamp = now or datetime.now(UTC)
    cleaned_plan = plan.strip() or "dev"
    cleaned_billing = billing_status.strip() or "manual"
    account_stmt = (
        pg_insert(TenantAccountRow)
        .values(
            tenant_id=cleaned_tenant,
            organization_id=cleaned_org,
            display_name=cleaned_name,
            owner_user_id=cleaned_owner,
            plan=cleaned_plan,
            billing_status=cleaned_billing,
            metadata_json=metadata or {},
            updated_at=timestamp,
        )
        .on_conflict_do_update(
            index_elements=[TenantAccountRow.tenant_id],
            set_={
                "organization_id": cleaned_org,
                "display_name": cleaned_name,
                "owner_user_id": cleaned_owner,
                "plan": cleaned_plan,
                "billing_status": cleaned_billing,
                "metadata_json": metadata or {},
                "updated_at": timestamp,
            },
        )
    )
    member_stmt = (
        pg_insert(TenantMemberRow)
        .values(
            tenant_id=cleaned_tenant,
            user_id=cleaned_owner,
            role=role,
            scopes=cleaned_scopes,
            status="active",
            updated_at=timestamp,
        )
        .on_conflict_do_update(
            index_elements=[TenantMemberRow.tenant_id, TenantMemberRow.user_id],
            set_={
                "role": role,
                "scopes": cleaned_scopes,
                "status": "active",
                "updated_at": timestamp,
            },
        )
    )
    await session.execute(account_stmt)
    await session.execute(member_stmt)
    return TenantAccountRecord(
        tenant_id=cleaned_tenant,
        organization_id=cleaned_org,
        display_name=cleaned_name,
        owner_user_id=cleaned_owner,
        role=role,
        plan=cleaned_plan,
        billing_status=cleaned_billing,
        honest_limits=[
            "这里只创建账号/成员账本，不签发裸 bearer token。",
            "会话 token 需要通过 account-session 或 invite signup 单独签发并入账。",
        ],
    )


async def invite_tenant_member(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    role: MemberRole = "member",
    scopes: list[str],
) -> TenantMemberInvite:
    """Create/update an invited tenant member without sending external email."""

    cleaned_tenant = _required("tenant_id", tenant_id)
    cleaned_user = _required("user_id", user_id)
    cleaned_scopes = [scope.strip() for scope in scopes if scope.strip()]
    existing = (
        await session.execute(
            select(TenantMemberRow.status).where(
                TenantMemberRow.tenant_id == cleaned_tenant,
                TenantMemberRow.user_id == cleaned_user,
            )
        )
    ).scalar_one_or_none()
    status = "active" if existing == "active" else "invited"
    now = datetime.now(UTC)
    stmt = (
        pg_insert(TenantMemberRow)
        .values(
            tenant_id=cleaned_tenant,
            user_id=cleaned_user,
            role=role,
            scopes=cleaned_scopes,
            status=status,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[TenantMemberRow.tenant_id, TenantMemberRow.user_id],
            set_={
                "role": role,
                "scopes": cleaned_scopes,
                "status": status,
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)
    return TenantMemberInvite(
        tenant_id=cleaned_tenant,
        user_id=cleaned_user,
        role=role,
        scopes=cleaned_scopes,
        status=status,
        honest_limits=[
            "这里只写入成员邀请账本，不会自动发送邮件。",
            "被邀请成员仍需要管理员签发 session 或后续接入接受邀请流程。",
        ],
    )


async def is_token_revoked(
    session: AsyncSession,
    *,
    tenant_id: str,
    token_hash: str,
) -> bool:
    """Return whether a bearer token hash has been revoked for a tenant.

    Missing rows are allowed so older operator-minted tokens do not break
    immediately after the ledger migration. Once a token is issued through the
    account ledger, revocation becomes enforceable.
    """

    result = await session.execute(
        select(TenantTokenIssueRow.status).where(
            TenantTokenIssueRow.tenant_id == tenant_id,
            TenantTokenIssueRow.token_hash == token_hash,
        )
    )
    status = result.scalar_one_or_none()
    return status == "revoked"


async def revoke_token_issue(
    session: AsyncSession,
    *,
    tenant_id: str,
    token_id: str,
    reason: str = "",
) -> bool:
    """Mark an issued tenant token as revoked."""

    now = datetime.now(UTC)
    result = await session.execute(
        update(TenantTokenIssueRow)
        .where(
            TenantTokenIssueRow.tenant_id == tenant_id,
            TenantTokenIssueRow.token_id == token_id,
            TenantTokenIssueRow.status == "issued",
        )
        .values(
            status="revoked",
            revoked_at=now,
            updated_at=now,
            metadata_json={"revoked_reason": reason.strip()} if reason.strip() else {},
        )
    )
    return bool(getattr(result, "rowcount", 0))


def build_unpersisted_bootstrap(
    *,
    tenant_id: str,
    organization_id: str,
    display_name: str,
    owner_user_id: str,
    secret: str,
    scopes: list[str],
    audience: Audience = "developer",
    plan: str = "dev",
    billing_status: str = "manual",
    ttl_sec: int = 86400,
) -> TenantAccountBootstrap:
    """Build the same account pack without touching the database."""

    token_id, token, expires_at = build_bootstrap_token(
        tenant_id=tenant_id,
        secret=secret,
        user_id=owner_user_id,
        scopes=scopes,
        audience=audience,
        ttl_sec=ttl_sec,
    )
    return TenantAccountBootstrap(
        tenant_id=_required("tenant_id", tenant_id),
        organization_id=_required("organization_id", organization_id),
        display_name=_required("display_name", display_name),
        owner_user_id=_required("owner_user_id", owner_user_id),
        plan=plan.strip() or "dev",
        billing_status=billing_status.strip() or "manual",
        token_id=token_id,
        token_hash=hash_bearer_token(token),
        bearer_token=token,
        scopes=[scope.strip() for scope in scopes if scope.strip()],
        expires_at=expires_at,
        persisted=False,
        honest_limits=_honest_limits(),
    )


def _token_id(tenant_id: str, user_id: str, expires_at: int) -> str:
    raw = f"{tenant_id}:{user_id}:{expires_at}:{time.time_ns()}"
    return "tok-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _required(field: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"{field} is required")
    return cleaned


def _honest_limits() -> list[str]:
    return [
        "这是账号/组织/token 签发账本的第一版，不是完整自助注册系统。",
        "生产请求中间件会校验已撤销 token；refresh token 续期已有最小闭环，但还没有完整登录/OAuth/设备会话。",
        "账单仍是 manual/trial/active 等状态记录，还没有真实支付闭环。",
    ]


__all__ = [
    "Audience",
    "MemberRole",
    "TenantAccountBootstrap",
    "TenantAccountRecord",
    "TenantMemberInvite",
    "bootstrap_tenant_account",
    "build_bootstrap_token",
    "build_unpersisted_bootstrap",
    "hash_bearer_token",
    "invite_tenant_member",
    "is_token_revoked",
    "revoke_token_issue",
    "upsert_tenant_account_member",
]
