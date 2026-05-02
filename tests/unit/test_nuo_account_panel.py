from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.nuo import account_panel
from kun.api.nuo.account_panel import router
from kun.core.orm import TenantAccountRow, TenantMemberRow, TenantTokenIssueRow
from kun.core.tenancy import TenantContext, tenant_scope
from kun.ops.account_registry import TenantMemberInvite


class _ScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar_one_or_none(self) -> Any:
        return self.value


class _ScalarsResult:
    def __init__(self, values: list[Any]) -> None:
        self.values = values

    def scalars(self) -> list[Any]:
        return self.values


class _FakeSession:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return self.results.pop(0)


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.unit
def test_account_summary_hides_token_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    fake_session = _FakeSession(
        [
            _ScalarResult(
                TenantAccountRow(
                    tenant_id="tenant-a",
                    organization_id="org-a",
                    display_name="KUN Test",
                    owner_user_id="owner-a",
                    status="active",
                    plan="dev",
                    billing_status="manual",
                    created_at=now,
                    updated_at=now,
                )
            ),
            _ScalarsResult(
                [
                    TenantMemberRow(
                        tenant_id="tenant-a",
                        user_id="owner-a",
                        role="owner",
                        scopes=["account:read", "account:admin"],
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                ]
            ),
            _ScalarsResult(
                [
                    TenantTokenIssueRow(
                        tenant_id="tenant-a",
                        token_id="tok-a",
                        token_hash="secret-hash-that-must-not-leak",
                        user_id="owner-a",
                        audience="developer",
                        scopes=["account:read"],
                        status="issued",
                        expires_at=now + timedelta(hours=1),
                        revoked_at=None,
                        last_used_at=now,
                        last_ip_hash="ip-hash",
                        last_user_agent="pytest-agent",
                        use_count=3,
                        created_at=now,
                        updated_at=now,
                    )
                ]
            ),
        ]
    )

    @asynccontextmanager
    async def fake_scope(*_: Any, **__: Any):
        yield fake_session

    monkeypatch.setattr(account_panel, "session_scope", fake_scope)

    with tenant_scope(
        TenantContext(
            tenant_id="tenant-a",
            user_id="owner-a",
            scopes=("account:read",),
        )
    ):
        response = TestClient(_app()).get("/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["account"]["display_name"] == "KUN Test"
    assert body["members"][0]["role"] == "owner"
    assert body["current_user_id"] == "owner-a"
    assert body["current_member"]["role"] == "owner"
    assert body["membership_validated"] is True
    assert body["membership_warning"] == ""
    assert body["tokens"][0]["token_id"] == "tok-a"
    assert body["tokens"][0]["status"] == "issued"
    assert body["tokens"][0]["last_ip_hash"] == "ip-hash"
    assert body["tokens"][0]["last_user_agent"] == "pytest-agent"
    assert body["tokens"][0]["use_count"] == 3
    assert body["tokens"][0]["session_risk_level"] == "info"
    assert body["counts"]["session_risk_tokens"] == 0
    assert "token_hash" not in body["tokens"][0]
    assert "secret-hash-that-must-not-leak" not in response.text
    assert body["counts"]["issued_tokens"] == 1


@pytest.mark.unit
def test_account_summary_flags_unvalidated_current_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    fake_session = _FakeSession(
        [
            _ScalarResult(
                TenantAccountRow(
                    tenant_id="tenant-a",
                    organization_id="org-a",
                    display_name="KUN Test",
                    owner_user_id="owner-a",
                    status="active",
                    plan="dev",
                    billing_status="manual",
                    created_at=now,
                    updated_at=now,
                )
            ),
            _ScalarsResult(
                [
                    TenantMemberRow(
                        tenant_id="tenant-a",
                        user_id="owner-a",
                        role="owner",
                        scopes=["account:read", "account:admin"],
                        status="active",
                        created_at=now,
                        updated_at=now,
                    )
                ]
            ),
            _ScalarsResult([]),
        ]
    )

    @asynccontextmanager
    async def fake_scope(*_: Any, **__: Any):
        yield fake_session

    monkeypatch.setattr(account_panel, "session_scope", fake_scope)

    with tenant_scope(
        TenantContext(
            tenant_id="tenant-a",
            user_id="ghost-user",
            scopes=("account:read",),
        )
    ):
        response = TestClient(_app()).get("/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["current_user_id"] == "ghost-user"
    assert body["current_member"] is None
    assert body["membership_validated"] is False
    assert "不在当前租户成员账本" in body["membership_warning"]
    assert "跨租户服务端切换器" in body["honest_limits"][-1]


@pytest.mark.unit
def test_account_summary_flags_inactive_current_member(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(UTC)
    fake_session = _FakeSession(
        [
            _ScalarResult(None),
            _ScalarsResult(
                [
                    TenantMemberRow(
                        tenant_id="tenant-a",
                        user_id="viewer-a",
                        role="viewer",
                        scopes=["account:read"],
                        status="invited",
                        created_at=now,
                        updated_at=now,
                    )
                ]
            ),
            _ScalarsResult([]),
        ]
    )

    @asynccontextmanager
    async def fake_scope(*_: Any, **__: Any):
        yield fake_session

    monkeypatch.setattr(account_panel, "session_scope", fake_scope)

    with tenant_scope(
        TenantContext(
            tenant_id="tenant-a",
            user_id="viewer-a",
            scopes=("account:read",),
        )
    ):
        response = TestClient(_app()).get("/summary")

    assert response.status_code == 200
    body = response.json()
    assert body["current_member"]["status"] == "invited"
    assert body["membership_validated"] is False
    assert "不是 active" in body["membership_warning"]


@pytest.mark.unit
def test_token_summary_flags_minimal_session_risk() -> None:
    now = datetime.now(UTC)
    row = TenantTokenIssueRow(
        tenant_id="tenant-a",
        token_id="tok-risk",
        token_hash="hash",
        user_id="owner-a",
        audience="developer",
        scopes=["account:read"],
        status="issued",
        expires_at=now + timedelta(days=90),
        revoked_at=None,
        last_used_at=now,
        last_ip_hash=None,
        last_user_agent=None,
        use_count=2,
        created_at=now,
        updated_at=now,
    )

    item = account_panel._token_summary(row)

    assert item.session_risk_level == "warn"
    assert "token 有效期超过 30 天" in item.session_risk_reasons
    assert "已有调用但缺少 IP 指纹" in item.session_risk_reasons
    assert "已有调用但缺少 UA 摘要" in item.session_risk_reasons


@pytest.mark.unit
def test_revoke_token_requires_admin_scope() -> None:
    with tenant_scope(
        TenantContext(
            tenant_id="tenant-a",
            user_id="viewer-a",
            scopes=("account:read",),
        )
    ):
        response = TestClient(_app()).post("/tokens/tok-a/revoke", json={"reason": "leaked"})

    assert response.status_code == 403
    assert "account:admin" in response.text


@pytest.mark.unit
def test_invite_member_requires_admin_scope() -> None:
    with tenant_scope(
        TenantContext(
            tenant_id="tenant-a",
            user_id="viewer-a",
            scopes=("account:read",),
        )
    ):
        response = TestClient(_app()).post(
            "/members/invite",
            json={"user_id": "new-user", "role": "member", "scopes": ["chat:write"]},
        )

    assert response.status_code == 403
    assert "account:admin" in response.text


@pytest.mark.unit
def test_invite_member_writes_invitation_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}
    invite_expires_at = datetime.now(UTC) + timedelta(days=7)

    class _FakeSettings:
        env = "production"

        def auth_secret_candidates(self) -> list[str]:
            return ["s" * 40]

    @asynccontextmanager
    async def fake_scope(*_: Any, **__: Any):
        yield object()

    async def fake_invite_tenant_member(_session: object, **kwargs: Any):
        called.update(kwargs)
        return TenantMemberInvite(
            tenant_id=kwargs["tenant_id"],
            user_id=kwargs["user_id"],
            role=kwargs["role"],
            scopes=kwargs["scopes"],
            status="invited",
            acceptance_token_id="tok-invite",
            acceptance_token="invite.raw",
            invite_expires_at=invite_expires_at,
            honest_limits=["不会自动发送邮件"],
        )

    monkeypatch.setattr(account_panel, "settings", lambda: _FakeSettings())
    monkeypatch.setattr(account_panel, "session_scope", fake_scope)
    monkeypatch.setattr(account_panel, "invite_tenant_member", fake_invite_tenant_member)

    with tenant_scope(
        TenantContext(
            tenant_id="tenant-a",
            user_id="owner-a",
            scopes=("account:admin",),
        )
    ):
        response = TestClient(_app()).post(
            "/members/invite",
            json={"user_id": "new-user", "role": "viewer", "scopes": ["account:read"]},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "invited"
    assert body["role"] == "viewer"
    assert body["acceptance_token_id"] == "tok-invite"
    assert body["acceptance_token"] == "invite.raw"
    assert "尚未发送邮件" in body["message"]
    assert body["email_draft"]["delivery_status"] == "draft_only"
    assert "你被邀请加入 KUN 租户" in body["email_draft"]["subject"]
    assert "invite.raw" in body["email_draft"]["body"]
    assert "没有自动发送邮件" in body["email_draft"]["body"]
    assert called == {
        "tenant_id": "tenant-a",
        "user_id": "new-user",
        "role": "viewer",
        "scopes": ["account:read"],
        "invite_secret": "s" * 40,
        "invite_ttl_sec": 604800,
        "invited_by_user_id": "owner-a",
    }


@pytest.mark.unit
def test_revoke_token_calls_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_scope(*_: Any, **__: Any):
        yield object()

    async def fake_revoke_token_issue(
        _session: object,
        *,
        tenant_id: str,
        token_id: str,
        reason: str,
    ) -> bool:
        called.update({"tenant_id": tenant_id, "token_id": token_id, "reason": reason})
        return True

    monkeypatch.setattr(account_panel, "session_scope", fake_scope)
    monkeypatch.setattr(account_panel, "revoke_token_issue", fake_revoke_token_issue)

    with tenant_scope(
        TenantContext(
            tenant_id="tenant-a",
            user_id="owner-a",
            scopes=("account:admin",),
        )
    ):
        response = TestClient(_app()).post("/tokens/tok-a/revoke", json={"reason": "leaked"})

    assert response.status_code == 200
    assert response.json()["status"] == "revoked"
    assert called == {"tenant_id": "tenant-a", "token_id": "tok-a", "reason": "leaked"}
