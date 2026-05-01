"""Minimal password credential ledger.

This is intentionally small: PBKDF2-HMAC-SHA256, tenant-scoped rows, and no raw
password storage.  It is not OAuth, WebAuthn, device risk, or billing.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kun.core.orm import TenantPasswordCredentialRow

ALGORITHM = "pbkdf2_sha256"
ITERATIONS = 260_000
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 12


class PasswordAuthError(ValueError):
    """Raised when password setup or login cannot proceed."""


async def set_password_credential(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    password: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Create or replace one tenant member password credential."""

    cleaned_tenant = _required("tenant_id", tenant_id)
    cleaned_user = _required("user_id", user_id)
    _validate_password(password)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _derive(password, salt=salt, iterations=ITERATIONS)
    now = datetime.now(UTC)
    stmt = (
        pg_insert(TenantPasswordCredentialRow)
        .values(
            tenant_id=cleaned_tenant,
            user_id=cleaned_user,
            algorithm=ALGORITHM,
            iterations=ITERATIONS,
            salt_b64=_b64(salt),
            password_hash_b64=_b64(digest),
            status="active",
            failed_count=0,
            last_changed_at=now,
            metadata_json=metadata or {},
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=[
                TenantPasswordCredentialRow.tenant_id,
                TenantPasswordCredentialRow.user_id,
            ],
            set_={
                "algorithm": ALGORITHM,
                "iterations": ITERATIONS,
                "salt_b64": _b64(salt),
                "password_hash_b64": _b64(digest),
                "status": "active",
                "failed_count": 0,
                "last_changed_at": now,
                "metadata_json": metadata or {},
                "updated_at": now,
            },
        )
    )
    await session.execute(stmt)


async def verify_password_credential(
    session: AsyncSession,
    *,
    tenant_id: str,
    user_id: str,
    password: str,
) -> bool:
    """Verify a password and update the credential audit counters."""

    cleaned_tenant = _required("tenant_id", tenant_id)
    cleaned_user = _required("user_id", user_id)
    row = (
        await session.execute(
            select(TenantPasswordCredentialRow).where(
                TenantPasswordCredentialRow.tenant_id == cleaned_tenant,
                TenantPasswordCredentialRow.user_id == cleaned_user,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.status != "active" or row.algorithm != ALGORITHM:
        return False
    try:
        salt = _unb64(row.salt_b64)
        expected = _unb64(row.password_hash_b64)
        actual = _derive(password, salt=salt, iterations=int(row.iterations))
    except Exception:
        return False
    ok = hmac.compare_digest(actual, expected)
    now = datetime.now(UTC)
    if ok:
        await session.execute(
            update(TenantPasswordCredentialRow)
            .where(
                TenantPasswordCredentialRow.tenant_id == cleaned_tenant,
                TenantPasswordCredentialRow.user_id == cleaned_user,
            )
            .values(last_login_at=now, failed_count=0, updated_at=now)
        )
    else:
        await session.execute(
            update(TenantPasswordCredentialRow)
            .where(
                TenantPasswordCredentialRow.tenant_id == cleaned_tenant,
                TenantPasswordCredentialRow.user_id == cleaned_user,
            )
            .values(
                failed_count=TenantPasswordCredentialRow.failed_count + 1,
                updated_at=now,
            )
        )
    return ok


def _derive(password: str, *, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )


def _validate_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordAuthError("password must be at least 12 characters")
    if len(password) > 1024:
        raise PasswordAuthError("password is too long")


def _required(field: str, value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise PasswordAuthError(f"{field} is required")
    return cleaned


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


__all__ = [
    "PasswordAuthError",
    "set_password_credential",
    "verify_password_credential",
]
