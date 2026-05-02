from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api import session as session_api
from kun.core.orm import TenantMemberRow, TenantTokenIssueRow
from kun.core.tenancy import TenantContext, tenant_scope
from kun.ops.account_registry import hash_bearer_token
from kun.ops.account_sessions import (
    SessionTokenError,
    issue_session_token_pair,
    issue_websocket_ticket,
    refresh_session_access_token,
)
from kun.security.auth import sign_auth_token, verify_bearer_token


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
    def __init__(self, results: list[Any] | None = None) -> None:
        self.results = results or []
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        if self.results:
            return self.results.pop(0)
        return object()


class _FakeScope:
    def __init__(self, session: object | None = None) -> None:
        self.session = session or object()

    async def __aenter__(self) -> object:
        return self.session

    async def __aexit__(self, *_args: object) -> None:
        return None


def _api_app() -> FastAPI:
    app = FastAPI()
    app.include_router(session_api.router)
    return app


@pytest.mark.unit
async def test_issue_session_token_pair_mints_access_and_refresh_tokens() -> None:
    secret = "s" * 40
    fake_session = _FakeSession()

    pair = await issue_session_token_pair(
        fake_session,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        user_id="user-a",
        secret=secret,
        scopes=["chat:write", "world:approve"],
        access_ttl_sec=300,
        refresh_ttl_sec=3600,
    )

    access_claims = verify_bearer_token(f"Bearer {pair.access_token}", secret)
    refresh_claims = verify_bearer_token(f"Bearer {pair.refresh_token}", secret)
    assert access_claims.tenant_id == "tenant-a"
    assert access_claims.token_id == pair.access_token_id
    assert access_claims.token_type == "access"
    assert refresh_claims.token_id == pair.refresh_token_id
    assert refresh_claims.token_type == "refresh"
    assert len(fake_session.statements) == 2
    assert any("不是完整自助注册" in item for item in pair.honest_limits)


@pytest.mark.unit
async def test_issue_websocket_ticket_mints_short_ws_token() -> None:
    secret = "s" * 40
    fake_session = _FakeSession()

    ticket = await issue_websocket_ticket(
        fake_session,  # type: ignore[arg-type]
        tenant_id="tenant-a",
        user_id="user-a",
        secret=secret,
        scopes=["chat:write"],
        ttl_sec=60,
    )

    claims = verify_bearer_token(f"Bearer {ticket.ticket}", secret)
    assert claims.tenant_id == "tenant-a"
    assert claims.user_id == "user-a"
    assert claims.token_type == "ws"
    assert ticket.ticket_id.startswith("wst-")
    assert ticket.expires_at - int(datetime.now(UTC).timestamp()) <= 60


@pytest.mark.unit
async def test_refresh_session_access_token_uses_ledger_backed_refresh_row() -> None:
    secret = "s" * 40
    refresh_token = sign_auth_token(
        {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "scopes": ["chat:write"],
            "audience": "expert",
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "rfr-existing",
            "token_type": "refresh",
        },
        secret,
    )
    row = TenantTokenIssueRow(
        tenant_id="tenant-a",
        token_id="rfr-existing",
        token_hash=hash_bearer_token(refresh_token),
        user_id="user-a",
        audience="expert",
        scopes=["chat:write"],
        status="issued",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        metadata_json={"kind": "refresh"},
    )
    fake_session = _FakeSession([_ScalarResult(row)])

    result = await refresh_session_access_token(
        fake_session,  # type: ignore[arg-type]
        refresh_token=refresh_token,
        auth_secrets=[secret],
        access_ttl_sec=300,
    )

    claims = verify_bearer_token(f"Bearer {result.access_token}", secret)
    assert result.tenant_id == "tenant-a"
    assert result.refresh_token_id == "rfr-existing"
    assert result.scopes == ["chat:write"]
    assert claims.token_type == "access"
    assert claims.audience == "expert"
    assert len(fake_session.statements) == 2


@pytest.mark.unit
async def test_refresh_session_access_token_rejects_access_token() -> None:
    secret = "s" * 40
    access_token = sign_auth_token(
        {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "acc-existing",
            "token_type": "access",
        },
        secret,
    )

    with pytest.raises(SessionTokenError, match="refresh token required"):
        await refresh_session_access_token(
            _FakeSession(),  # type: ignore[arg-type]
            refresh_token=access_token,
            auth_secrets=[secret],
        )


@pytest.mark.unit
async def test_refresh_session_access_token_rejects_revoked_refresh_row() -> None:
    secret = "s" * 40
    refresh_token = sign_auth_token(
        {
            "tenant_id": "tenant-a",
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "rfr-revoked",
            "token_type": "refresh",
        },
        secret,
    )
    row = TenantTokenIssueRow(
        tenant_id="tenant-a",
        token_id="rfr-revoked",
        token_hash=hash_bearer_token(refresh_token),
        user_id="user-a",
        audience="developer",
        scopes=[],
        status="revoked",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        metadata_json={"kind": "refresh"},
    )

    with pytest.raises(SessionTokenError, match="revoked"):
        await refresh_session_access_token(
            _FakeSession([_ScalarResult(row)]),  # type: ignore[arg-type]
            refresh_token=refresh_token,
            auth_secrets=[secret],
        )


@pytest.mark.unit
def test_refresh_session_api_accepts_authorization_refresh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "s" * 40
    refresh_token = sign_auth_token(
        {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "rfr-api",
            "token_type": "refresh",
        },
        secret,
    )

    class _FakeSettings:
        def auth_secret_candidates(self) -> list[str]:
            return [secret]

    class _FakeScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def fake_refresh(*_args: object, **_kwargs: object):
        return session_api.AccessTokenRefresh(
            tenant_id="tenant-a",
            user_id="user-a",
            audience="developer",
            scopes=["chat:write"],
            access_token_id="acc-api",
            access_token=sign_auth_token(
                {
                    "tenant_id": "tenant-a",
                    "user_id": "user-a",
                    "jti": "acc-api",
                    "token_type": "access",
                },
                secret,
            ),
            access_expires_at=int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
            refresh_token_id="rfr-api",
            honest_limits=["最小 refresh-token 生命周期"],
        )

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())
    monkeypatch.setattr(session_api, "session_scope", lambda **_kwargs: _FakeScope())
    monkeypatch.setattr(session_api, "refresh_session_access_token", fake_refresh)

    response = TestClient(_api_app()).post(
        "/api/auth/session/refresh",
        json={},
        headers={"Authorization": f"Bearer {refresh_token}"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["access_token_id"] == "acc-api"
    assert body["refresh_token_id"] == "rfr-api"


@pytest.mark.unit
def test_refresh_session_api_rejects_access_token(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "s" * 40
    access_token = sign_auth_token(
        {
            "tenant_id": "tenant-a",
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            "jti": "acc-api",
            "token_type": "access",
        },
        secret,
    )

    class _FakeSettings:
        def auth_secret_candidates(self) -> list[str]:
            return [secret]

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())

    response = TestClient(_api_app()).post(
        "/api/auth/session/refresh",
        json={"refresh_token": access_token},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "refresh token required"


@pytest.mark.unit
def test_signup_api_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSettings:
        self_signup_enabled = False
        self_signup_invite_code = None

        def auth_secret_candidates(self) -> list[str]:
            return ["s" * 40]

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())

    response = TestClient(_api_app()).post(
        "/api/auth/signup",
        json={
            "invite_code": "invite",
            "tenant_id": "tenant-a",
            "owner_user_id": "owner-a",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "self signup is disabled"


@pytest.mark.unit
def test_signup_api_creates_account_and_refreshable_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "s" * 40

    class _FakeSettings:
        self_signup_enabled = True
        self_signup_invite_code = "invite-123"

        def auth_secret_candidates(self) -> list[str]:
            return [secret]

    async def fake_upsert(*_args: object, **kwargs: object) -> session_api.TenantAccountRecord:
        assert kwargs["tenant_id"] == "tenant-a"
        assert kwargs["owner_user_id"] == "owner-a"
        assert kwargs["metadata"] == {"source": "api.auth.signup"}
        return session_api.TenantAccountRecord(
            tenant_id="tenant-a",
            organization_id="tenant-a",
            display_name="Tenant A",
            owner_user_id="owner-a",
            persisted=True,
        )

    async def fake_issue(*_args: object, **kwargs: object) -> session_api.SessionTokenPair:
        assert kwargs["tenant_id"] == "tenant-a"
        assert kwargs["user_id"] == "owner-a"
        return session_api.SessionTokenPair(
            tenant_id="tenant-a",
            user_id="owner-a",
            audience="developer",
            scopes=["chat:write"],
            access_token_id="acc-signup",
            access_token=sign_auth_token(
                {
                    "tenant_id": "tenant-a",
                    "user_id": "owner-a",
                    "jti": "acc-signup",
                    "token_type": "access",
                },
                secret,
            ),
            access_expires_at=456,
            refresh_token_id="rfr-signup",
            refresh_token=sign_auth_token(
                {
                    "tenant_id": "tenant-a",
                    "user_id": "owner-a",
                    "jti": "rfr-signup",
                    "token_type": "refresh",
                },
                secret,
            ),
            refresh_expires_at=789,
        )

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())
    monkeypatch.setattr(session_api, "session_scope", lambda **_kwargs: _FakeScope())
    monkeypatch.setattr(session_api, "upsert_tenant_account_member", fake_upsert)
    monkeypatch.setattr(session_api, "issue_session_token_pair", fake_issue)

    response = TestClient(_api_app()).post(
        "/api/auth/signup",
        json={
            "invite_code": "invite-123",
            "tenant_id": "tenant-a",
            "owner_user_id": "owner-a",
            "display_name": "Tenant A",
            "scopes": ["chat:write"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["access_token_id"] == "acc-signup"
    assert body["refresh_token_id"] == "rfr-signup"
    assert body["account_persisted"] is True
    assert any("不是密码登录" in item for item in body["honest_limits"])


@pytest.mark.unit
def test_signup_api_rejects_bad_invite(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSettings:
        self_signup_enabled = True
        self_signup_invite_code = "right"

        def auth_secret_candidates(self) -> list[str]:
            return ["s" * 40]

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())

    response = TestClient(_api_app()).post(
        "/api/auth/signup",
        json={
            "invite_code": "wrong",
            "tenant_id": "tenant-a",
            "owner_user_id": "owner-a",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "invalid invite code"


@pytest.mark.unit
def test_accept_invite_api_activates_member_and_issues_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "s" * 40

    class _FakeSettings:
        self_signup_enabled = True
        self_signup_invite_code = "invite-123"

        def auth_secret_candidates(self) -> list[str]:
            return [secret]

    async def fake_accept(*_args: object, **kwargs: object) -> session_api.TenantMemberAccepted:
        assert kwargs["tenant_id"] == "tenant-a"
        assert kwargs["user_id"] == "member-a"
        return session_api.TenantMemberAccepted(
            tenant_id="tenant-a",
            user_id="member-a",
            role="viewer",
            scopes=["account:read"],
            status="active",
        )

    async def fake_issue(*_args: object, **kwargs: object) -> session_api.SessionTokenPair:
        assert kwargs["scopes"] == ["account:read"]
        assert kwargs["metadata"] == {"source": "api.auth.invite_accept", "role": "viewer"}
        return session_api.SessionTokenPair(
            tenant_id="tenant-a",
            user_id="member-a",
            audience="developer",
            scopes=["account:read"],
            access_token_id="acc-invite",
            access_token=sign_auth_token(
                {
                    "tenant_id": "tenant-a",
                    "user_id": "member-a",
                    "jti": "acc-invite",
                    "token_type": "access",
                },
                secret,
            ),
            access_expires_at=456,
            refresh_token_id="rfr-invite",
            refresh_token=sign_auth_token(
                {
                    "tenant_id": "tenant-a",
                    "user_id": "member-a",
                    "jti": "rfr-invite",
                    "token_type": "refresh",
                },
                secret,
            ),
            refresh_expires_at=789,
        )

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())
    monkeypatch.setattr(session_api, "session_scope", lambda **_kwargs: _FakeScope())
    monkeypatch.setattr(session_api, "accept_tenant_member_invite", fake_accept)
    monkeypatch.setattr(session_api, "issue_session_token_pair", fake_issue)

    response = TestClient(_api_app()).post(
        "/api/auth/invite/accept",
        json={
            "invite_code": "invite-123",
            "tenant_id": "tenant-a",
            "user_id": "member-a",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "active"
    assert body["access_token_id"] == "acc-invite"
    assert body["refresh_token_id"] == "rfr-invite"
    assert any("没有设备指纹" in item for item in body["honest_limits"])


@pytest.mark.unit
def test_accept_invite_api_accepts_one_time_token_without_global_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "s" * 40

    class _FakeSettings:
        self_signup_enabled = True
        self_signup_invite_code = None

        def auth_secret_candidates(self) -> list[str]:
            return [secret]

    async def fake_accept(*_args: object, **kwargs: object) -> session_api.TenantMemberAccepted:
        assert kwargs["invite_token"] == "invite.raw"
        assert kwargs["auth_secrets"] == [secret]
        return session_api.TenantMemberAccepted(
            tenant_id="tenant-a",
            user_id="member-a",
            role="viewer",
            scopes=["account:read"],
            status="active",
        )

    async def fake_issue(*_args: object, **_kwargs: object) -> session_api.SessionTokenPair:
        return session_api.SessionTokenPair(
            tenant_id="tenant-a",
            user_id="member-a",
            audience="developer",
            scopes=["account:read"],
            access_token_id="acc-token",
            access_token=sign_auth_token(
                {"tenant_id": "tenant-a", "user_id": "member-a", "jti": "acc-token"},
                secret,
            ),
            access_expires_at=111,
            refresh_token_id="rfr-token",
            refresh_token=sign_auth_token(
                {"tenant_id": "tenant-a", "user_id": "member-a", "jti": "rfr-token"},
                secret,
            ),
            refresh_expires_at=222,
        )

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())
    monkeypatch.setattr(session_api, "session_scope", lambda **_kwargs: _FakeScope())
    monkeypatch.setattr(session_api, "accept_tenant_member_invite", fake_accept)
    monkeypatch.setattr(session_api, "issue_session_token_pair", fake_issue)

    response = TestClient(_api_app()).post(
        "/api/auth/invite/accept",
        json={
            "invite_token": "invite.raw",
            "tenant_id": "tenant-a",
            "user_id": "member-a",
        },
    )

    assert response.status_code == 200
    assert response.json()["refresh_token_id"] == "rfr-token"


@pytest.mark.unit
def test_accept_invite_api_rejects_missing_invite(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSettings:
        self_signup_enabled = True
        self_signup_invite_code = "invite-123"

        def auth_secret_candidates(self) -> list[str]:
            return ["s" * 40]

    async def fake_accept(*_args: object, **_kwargs: object) -> session_api.TenantMemberAccepted:
        raise ValueError("tenant member invitation not found")

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())
    monkeypatch.setattr(session_api, "session_scope", lambda **_kwargs: _FakeScope())
    monkeypatch.setattr(session_api, "accept_tenant_member_invite", fake_accept)

    response = TestClient(_api_app()).post(
        "/api/auth/invite/accept",
        json={
            "invite_code": "invite-123",
            "tenant_id": "tenant-a",
            "user_id": "missing",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "tenant member invitation not found"


@pytest.mark.unit
def test_current_session_reports_context() -> None:
    with tenant_scope(
        TenantContext(
            tenant_id="tenant-a",
            user_id="user-a",
            scopes=("chat:write",),
            audience="expert",
        )
    ):
        response = TestClient(_api_app()).get("/api/auth/session/me")

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["user_id"] == "user-a"
    assert body["scopes"] == ["chat:write"]
    assert body["audience"] == "expert"


@pytest.mark.unit
def test_websocket_ticket_api_uses_current_session(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "s" * 40

    class _FakeSettings:
        def auth_secret_candidates(self) -> list[str]:
            return [secret]

    async def fake_issue_ws_ticket(*_args: object, **kwargs: object) -> session_api.WebSocketTicket:
        assert kwargs["tenant_id"] == "tenant-a"
        assert kwargs["user_id"] == "user-a"
        assert kwargs["scopes"] == ["chat:write"]
        return session_api.WebSocketTicket(
            tenant_id="tenant-a",
            user_id="user-a",
            audience="developer",
            scopes=["chat:write"],
            ticket_id="wst-test",
            ticket=sign_auth_token(
                {
                    "tenant_id": "tenant-a",
                    "user_id": "user-a",
                    "jti": "wst-test",
                    "token_type": "ws",
                },
                secret,
            ),
            expires_at=int((datetime.now(UTC) + timedelta(minutes=1)).timestamp()),
            honest_limits=["短期 WebSocket 握手票据"],
        )

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())
    monkeypatch.setattr(session_api, "session_scope", lambda **_kwargs: _FakeScope())
    monkeypatch.setattr(session_api, "issue_websocket_ticket", fake_issue_ws_ticket)

    with tenant_scope(
        TenantContext(tenant_id="tenant-a", user_id="user-a", scopes=("chat:write",))
    ):
        response = TestClient(_api_app()).post("/api/auth/ws-ticket")

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["ticket_id"] == "wst-test"
    assert verify_bearer_token(f"Bearer {body['ticket']}", secret).token_type == "ws"


@pytest.mark.unit
def test_password_set_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSettings:
        password_login_enabled = False

        def auth_secret_candidates(self) -> list[str]:
            return ["s" * 40]

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="user-a")):
        response = TestClient(_api_app()).post(
            "/api/auth/password/set",
            json={"password": "correct horse battery"},
        )

    assert response.status_code == 403
    assert response.json()["detail"] == "password login is disabled"


@pytest.mark.unit
def test_password_set_calls_credential_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, Any] = {}

    class _FakeSettings:
        password_login_enabled = True

        def auth_secret_candidates(self) -> list[str]:
            return ["s" * 40]

    async def fake_set_password(*_args: object, **kwargs: object) -> None:
        called.update(kwargs)

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())
    monkeypatch.setattr(session_api, "session_scope", lambda **_kwargs: _FakeScope())
    monkeypatch.setattr(session_api, "set_password_credential", fake_set_password)

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="user-a")):
        response = TestClient(_api_app()).post(
            "/api/auth/password/set",
            json={"password": "correct horse battery"},
        )

    assert response.status_code == 200
    assert called["tenant_id"] == "tenant-a"
    assert called["user_id"] == "user-a"
    assert called["password"] == "correct horse battery"


@pytest.mark.unit
def test_password_login_issues_session_pair(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "s" * 40
    member = TenantMemberRow(
        tenant_id="tenant-a",
        user_id="user-a",
        role="owner",
        scopes=["chat:write"],
        status="active",
    )

    class _FakeSettings:
        password_login_enabled = True

        def auth_secret_candidates(self) -> list[str]:
            return [secret]

    async def fake_verify(*_args: object, **kwargs: object) -> bool:
        assert kwargs["tenant_id"] == "tenant-a"
        assert kwargs["user_id"] == "user-a"
        assert kwargs["password"] == "correct horse battery"
        return True

    async def fake_issue(*_args: object, **kwargs: object) -> session_api.SessionTokenPair:
        assert kwargs["tenant_id"] == "tenant-a"
        assert kwargs["user_id"] == "user-a"
        assert kwargs["scopes"] == ["chat:write"]
        return session_api.SessionTokenPair(
            tenant_id="tenant-a",
            user_id="user-a",
            audience="developer",
            scopes=["chat:write"],
            access_token_id="acc-password",
            access_token=sign_auth_token(
                {"tenant_id": "tenant-a", "jti": "acc-password", "token_type": "access"},
                secret,
            ),
            access_expires_at=int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
            refresh_token_id="rfr-password",
            refresh_token=sign_auth_token(
                {"tenant_id": "tenant-a", "jti": "rfr-password", "token_type": "refresh"},
                secret,
            ),
            refresh_expires_at=int((datetime.now(UTC) + timedelta(days=30)).timestamp()),
        )

    monkeypatch.setattr(session_api, "settings", lambda: _FakeSettings())
    monkeypatch.setattr(
        session_api,
        "session_scope",
        lambda **_kwargs: _FakeScope(_FakeSession([_ScalarResult(member)])),
    )
    monkeypatch.setattr(session_api, "verify_password_credential", fake_verify)
    monkeypatch.setattr(session_api, "issue_session_token_pair", fake_issue)

    response = TestClient(_api_app()).post(
        "/api/auth/password/login",
        json={
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "password": "correct horse battery",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant_id"] == "tenant-a"
    assert body["access_token_id"] == "acc-password"
    assert body["refresh_token_id"] == "rfr-password"


@pytest.mark.unit
def test_current_user_sessions_hide_token_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime.now(UTC)
    row = TenantTokenIssueRow(
        tenant_id="tenant-a",
        token_id="acc-a",
        token_hash="secret-hash",
        user_id="user-a",
        audience="developer",
        scopes=["chat:write"],
        status="issued",
        expires_at=now + timedelta(minutes=15),
        metadata_json={"kind": "access"},
    )

    monkeypatch.setattr(
        session_api,
        "session_scope",
        lambda **_kwargs: _FakeScope(_FakeSession([_ScalarsResult([row])])),
    )

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="user-a")):
        response = TestClient(_api_app()).get("/api/auth/session/tokens")

    assert response.status_code == 200
    body = response.json()
    assert body["tokens"][0]["token_id"] == "acc-a"
    assert body["tokens"][0]["token_kind"] == "access"
    assert "token_hash" not in response.text
    assert "secret-hash" not in response.text


@pytest.mark.unit
def test_revoke_own_session_rejects_other_user_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        session_api,
        "session_scope",
        lambda **_kwargs: _FakeScope(_FakeSession([_ScalarResult("other-user")])),
    )

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="user-a")):
        response = TestClient(_api_app()).post("/api/auth/session/tokens/tok-a/revoke")

    assert response.status_code == 403
    assert response.json()["detail"] == "cannot revoke another user's token"


@pytest.mark.unit
def test_revoke_own_session_calls_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_session = _FakeSession([_ScalarResult("user-a")])
    called: dict[str, Any] = {}

    async def fake_revoke_token_issue(
        _session: object,
        *,
        tenant_id: str,
        token_id: str,
        reason: str,
    ) -> bool:
        called.update({"tenant_id": tenant_id, "token_id": token_id, "reason": reason})
        return True

    monkeypatch.setattr(session_api, "session_scope", lambda **_kwargs: _FakeScope(fake_session))
    monkeypatch.setattr(session_api, "revoke_token_issue", fake_revoke_token_issue)

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="user-a")):
        response = TestClient(_api_app()).post("/api/auth/session/tokens/tok-a/revoke")

    assert response.status_code == 200
    assert response.json()["status"] == "revoked"
    assert called == {
        "tenant_id": "tenant-a",
        "token_id": "tok-a",
        "reason": "self_session_revoke",
    }
