from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kun.core.orm import TenantPasswordCredentialRow
from kun.ops import password_auth
from kun.ops.password_auth import (
    PasswordAuthError,
    set_password_credential,
    verify_password_credential,
)


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _FakeSession:
    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = results or []
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        if self.results:
            return self.results.pop(0)
        return object()


@pytest.mark.unit
async def test_set_password_rejects_short_password() -> None:
    with pytest.raises(PasswordAuthError, match="at least 12"):
        await set_password_credential(
            _FakeSession(),  # type: ignore[arg-type]
            tenant_id="tenant-a",
            user_id="user-a",
            password="short",
        )


@pytest.mark.unit
async def test_set_password_stores_only_hash_material() -> None:
    fake = _FakeSession()

    await set_password_credential(
        fake,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        user_id="user-a",
        password="correct horse battery",
    )

    assert len(fake.statements) == 1
    assert "correct horse battery" not in str(fake.statements[0])


@pytest.mark.unit
async def test_verify_password_accepts_matching_hash() -> None:
    salt = b"1234567890abcdef"
    digest = password_auth._derive(
        "correct horse battery",
        salt=salt,
        iterations=password_auth.ITERATIONS,
    )
    row = TenantPasswordCredentialRow(
        tenant_id="tenant-a",
        user_id="user-a",
        algorithm=password_auth.ALGORITHM,
        iterations=password_auth.ITERATIONS,
        salt_b64=password_auth._b64(salt),
        password_hash_b64=password_auth._b64(digest),
        status="active",
        failed_count=1,
        last_changed_at=datetime.now(UTC) - timedelta(days=1),
    )
    fake = _FakeSession([_ScalarResult(row)])

    ok = await verify_password_credential(
        fake,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        user_id="user-a",
        password="correct horse battery",
    )

    assert ok is True
    assert len(fake.statements) == 2


@pytest.mark.unit
async def test_verify_password_rejects_mismatch() -> None:
    salt = b"1234567890abcdef"
    digest = password_auth._derive(
        "correct horse battery",
        salt=salt,
        iterations=password_auth.ITERATIONS,
    )
    row = TenantPasswordCredentialRow(
        tenant_id="tenant-a",
        user_id="user-a",
        algorithm=password_auth.ALGORITHM,
        iterations=password_auth.ITERATIONS,
        salt_b64=password_auth._b64(salt),
        password_hash_b64=password_auth._b64(digest),
        status="active",
        failed_count=0,
        last_changed_at=datetime.now(UTC) - timedelta(days=1),
    )
    fake = _FakeSession([_ScalarResult(row)])

    ok = await verify_password_credential(
        fake,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        user_id="user-a",
        password="wrong horse battery",
    )

    assert ok is False
    assert len(fake.statements) == 2
