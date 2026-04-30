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
    assert body["tokens"][0]["token_id"] == "tok-a"
    assert body["tokens"][0]["status"] == "issued"
    assert "token_hash" not in body["tokens"][0]
    assert "secret-hash-that-must-not-leak" not in response.text
    assert body["counts"]["issued_tokens"] == 1


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
