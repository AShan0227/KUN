from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api import session as session_api
from kun.core.orm import TenantTokenIssueRow
from kun.ops.account_registry import hash_bearer_token
from kun.ops.account_sessions import (
    SessionTokenError,
    issue_session_token_pair,
    refresh_session_access_token,
)
from kun.security.auth import sign_auth_token, verify_bearer_token


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
