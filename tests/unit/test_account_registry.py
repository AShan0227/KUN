from __future__ import annotations

from typing import Any

import pytest
from kun.ops.account_registry import (
    build_bootstrap_token,
    hash_bearer_token,
    is_token_revoked,
    revoke_token_issue,
)
from kun.security.auth import verify_bearer_token


class _ScalarResult:
    def __init__(self, value: str | None = None, rowcount: int = 0) -> None:
        self._value = value
        self.rowcount = rowcount

    def scalar_one_or_none(self) -> str | None:
        return self._value


class _FakeSession:
    def __init__(self, result: _ScalarResult) -> None:
        self.result = result
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        return self.result


@pytest.mark.unit
def test_bootstrap_token_carries_jti_for_audit() -> None:
    secret = "x" * 40
    token_id, token, _expires_at = build_bootstrap_token(
        tenant_id="tenant-a",
        user_id="owner-a",
        scopes=["world:approve"],
        secret=secret,
        token_id="tok-test",
    )

    claims = verify_bearer_token(f"Bearer {token}", secret)

    assert token_id == "tok-test"
    assert claims.token_id == "tok-test"
    assert hash_bearer_token(token) == hash_bearer_token(token)


@pytest.mark.unit
async def test_is_token_revoked_only_blocks_revoked_status() -> None:
    revoked = await is_token_revoked(
        _FakeSession(_ScalarResult("revoked")),  # type: ignore[arg-type]
        tenant_id="tenant-a",
        token_hash="h",
    )
    issued = await is_token_revoked(
        _FakeSession(_ScalarResult("issued")),  # type: ignore[arg-type]
        tenant_id="tenant-a",
        token_hash="h",
    )
    unknown = await is_token_revoked(
        _FakeSession(_ScalarResult(None)),  # type: ignore[arg-type]
        tenant_id="tenant-a",
        token_hash="h",
    )

    assert revoked is True
    assert issued is False
    assert unknown is False


@pytest.mark.unit
async def test_revoke_token_issue_returns_rowcount() -> None:
    ok = await revoke_token_issue(
        _FakeSession(_ScalarResult(rowcount=1)),  # type: ignore[arg-type]
        tenant_id="tenant-a",
        token_id="tok-a",
        reason="leaked",
    )
    missing = await revoke_token_issue(
        _FakeSession(_ScalarResult(rowcount=0)),  # type: ignore[arg-type]
        tenant_id="tenant-a",
        token_id="tok-a",
    )

    assert ok is True
    assert missing is False
