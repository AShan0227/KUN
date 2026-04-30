from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from kun.api.nuo import health_panel
from kun.api.nuo.health_panel import SecretStoreSetRequest, set_secret_store_value
from kun.core.tenancy import TenantContext, tenant_scope
from kun.ops.secret_store import SECRET_STORE_FILE_ENV


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nuo_secret_store_set_writes_without_echoing_value(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "secrets.json"
    monkeypatch.setenv(SECRET_STORE_FILE_ENV, str(store))

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="owner-a")):
        response = await set_secret_store_value(
            SecretStoreSetRequest(
                name="KUN_WORLD_SMTP_PASSWORD",
                value="super-secret",
            )
        )

    assert response.tenant_id == "tenant-a"
    assert response.name == "KUN_WORLD_SMTP_PASSWORD"
    assert "super-secret" not in response.model_dump_json()
    assert json.loads(store.read_text(encoding="utf-8")) == {
        "global": {},
        "tenants": {"tenant-a": {"KUN_WORLD_SMTP_PASSWORD": "super-secret"}},
    }
    assert oct(store.stat().st_mode & 0o777) == "0o600"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nuo_secret_store_set_requires_configured_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SECRET_STORE_FILE_ENV, raising=False)

    with (
        tenant_scope(TenantContext(tenant_id="tenant-a", user_id="owner-a")),
        pytest.raises(HTTPException) as exc,
    ):
        await set_secret_store_value(
            SecretStoreSetRequest(name="KUN_WORLD_SMTP_HOST", value="smtp.example.com")
        )

    assert exc.value.status_code == 400
    assert "KUN_SECRET_STORE_FILE" in str(exc.value.detail)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nuo_secret_store_set_rejects_non_world_key(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SECRET_STORE_FILE_ENV, str(tmp_path / "secrets.json"))

    with (
        tenant_scope(TenantContext(tenant_id="tenant-a", user_id="owner-a")),
        pytest.raises(HTTPException) as exc,
    ):
        await set_secret_store_value(SecretStoreSetRequest(name="KUN_AUTH_SECRET", value="x" * 40))

    assert exc.value.status_code == 422
    assert "KUN_WORLD" in str(exc.value.detail)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_nuo_readiness_uses_current_tenant_and_light_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_readiness(**kwargs):
        calls.append(kwargs)
        return {"status": "warn", "tenant_id": kwargs["tenant_id"]}

    monkeypatch.setattr(health_panel, "run_readiness_report", fake_readiness)

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="owner-a")):
        result = await health_panel.readiness_report(
            include_dogfood=False,
            include_db_mission=False,
            include_db_account=False,
            include_db_state_ledger_repair=False,
            include_db_long_horizon_drill=False,
            run_alembic_heads=False,
        )

    assert result == {"status": "warn", "tenant_id": "tenant-a"}
    assert calls == [
        {
            "tenant_id": "tenant-a",
            "include_dogfood": False,
            "include_db_mission": False,
            "include_db_account": False,
            "include_db_state_ledger_repair": False,
            "include_db_long_horizon_drill": False,
            "run_alembic_heads": False,
        }
    ]
