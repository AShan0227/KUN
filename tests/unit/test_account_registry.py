from __future__ import annotations

from typing import Any

import pytest
from kun.core.orm import TenantMemberRow
from kun.ops.account_registry import (
    accept_tenant_member_invite,
    build_bootstrap_token,
    hash_bearer_token,
    invite_tenant_member,
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


class _SequenceSession:
    def __init__(self, results: list[_ScalarResult]) -> None:
        self.results = results
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        return self.results.pop(0)


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


@pytest.mark.unit
async def test_invite_tenant_member_keeps_existing_active_status() -> None:
    fake_session = _SequenceSession([_ScalarResult("active"), _ScalarResult(rowcount=1)])

    invited = await invite_tenant_member(
        fake_session,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        user_id="member-a",
        role="admin",
        scopes=["account:read"],
    )

    assert invited.status == "active"
    assert invited.role == "admin"
    assert len(fake_session.statements) == 2
    assert any("不会自动发送邮件" in item for item in invited.honest_limits)


@pytest.mark.unit
async def test_accept_tenant_member_invite_marks_member_active() -> None:
    row = TenantMemberRow(
        tenant_id="tenant-a",
        user_id="member-a",
        role="viewer",
        scopes=["account:read"],
        status="invited",
    )
    fake_session = _SequenceSession([_ScalarResult(row), _ScalarResult(rowcount=1)])

    accepted = await accept_tenant_member_invite(
        fake_session,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        user_id="member-a",
    )

    assert accepted.status == "active"
    assert accepted.role == "viewer"
    assert accepted.scopes == ["account:read"]
    assert len(fake_session.statements) == 2


@pytest.mark.unit
async def test_accept_tenant_member_invite_rejects_missing_invite() -> None:
    with pytest.raises(ValueError, match="not found"):
        await accept_tenant_member_invite(
            _SequenceSession([_ScalarResult(None)]),  # type: ignore[arg-type]
            tenant_id="tenant-a",
            user_id="missing",
        )
