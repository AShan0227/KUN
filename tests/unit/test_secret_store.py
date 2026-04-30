from __future__ import annotations

import json
from pathlib import Path

import pytest
from kun.ops.secret_audit import audit_runtime_secrets
from kun.ops.secret_store import (
    SECRET_STORE_FILE_ENV,
    secret_for_tenant,
    secret_store_has_required,
    secret_store_status,
)
from kun.world.gateway import BrowserExecuteHandler, EnterpriseApiPostHandler, WorldAction
from kun.world.tenant_env import env_for_tenant, missing_required_world_env


def _write_store(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "global": {"KUN_WORLD_SMTP_PORT": "2525"},
                "tenants": {
                    "tenant-a": {
                        "KUN_WORLD_SMTP_HOST": "smtp.tenant-a.example.com",
                        "KUN_WORLD_SMTP_FROM": "kun@tenant-a.example.com",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.unit
def test_secret_store_reads_tenant_scoped_values(tmp_path: Path) -> None:
    store = tmp_path / "secrets.json"
    _write_store(store)
    env = {SECRET_STORE_FILE_ENV: str(store)}

    assert (
        secret_for_tenant("tenant-a", "KUN_WORLD_SMTP_HOST", env=env) == "smtp.tenant-a.example.com"
    )
    assert env_for_tenant("tenant-a", "KUN_WORLD_SMTP_FROM", env=env) == (
        "kun@tenant-a.example.com"
    )
    assert env_for_tenant("tenant-a", "KUN_WORLD_SMTP_PORT", env=env) == "2525"


@pytest.mark.unit
def test_secret_store_satisfies_required_world_env(tmp_path: Path) -> None:
    store = tmp_path / "secrets.json"
    _write_store(store)
    env = {SECRET_STORE_FILE_ENV: str(store)}

    assert secret_store_has_required(
        ("KUN_WORLD_SMTP_HOST", "KUN_WORLD_SMTP_FROM"),
        tenant_id="tenant-a",
        env=env,
    )
    assert (
        missing_required_world_env(
            ("KUN_WORLD_SMTP_HOST", "KUN_WORLD_SMTP_FROM"),
            tenant_id="tenant-a",
            env=env,
        )
        == []
    )


@pytest.mark.unit
def test_secret_audit_reports_configured_secret_store(tmp_path: Path) -> None:
    store = tmp_path / "secrets.json"
    _write_store(store)

    report = audit_runtime_secrets(environ={SECRET_STORE_FILE_ENV: str(store)})
    item = next(i for i in report.items if i.item_id == "world_gateway.secret_store.configured")

    assert item.severity == "ok"
    assert "smtp.tenant-a.example.com" not in item.detail


@pytest.mark.unit
def test_secret_store_status_reports_bad_json(tmp_path: Path) -> None:
    store = tmp_path / "bad.json"
    store.write_text("not json", encoding="utf-8")

    status = secret_store_status(env={SECRET_STORE_FILE_ENV: str(store)})

    assert status.configured is True
    assert status.readable is False
    assert status.error
    assert (
        secret_for_tenant(
            "tenant-a", "KUN_WORLD_SMTP_HOST", env={SECRET_STORE_FILE_ENV: str(store)}
        )
        is None
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enterprise_api_handler_accepts_tenant_secret_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "secrets.json"
    store.write_text(
        json.dumps(
            {
                "tenants": {
                    "tenant-a": {
                        "KUN_WORLD_API_ALLOWED_HOSTS": "api.tenant.example.com",
                        "KUN_WORLD_API_AUTH_HEADER": "X-Api-Key",
                        "KUN_WORLD_API_AUTH_VALUE": "secret-token",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(SECRET_STORE_FILE_ENV, str(store))
    monkeypatch.delenv("KUN_WORLD_API_ALLOWED_HOSTS", raising=False)

    handler = EnterpriseApiPostHandler.from_env(tmp_path)
    result = await handler.preview(
        WorldAction(
            action_id="wa_1",
            tenant_id="tenant-a",
            task_ref="task-1",
            action_type="enterprise_api.post",
            target_ref="https://api.tenant.example.com/v1/run",
            risk_level="high",
            payload={"json": {"ok": True}},
        )
    )

    assert result.status == "preview"
    assert result.audit["tenant_scoped_config"] is True
    assert result.audit["auth_source"] == "tenant_override"
    assert "[redacted]" in result.rendered_payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_browser_handler_accepts_global_secret_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = tmp_path / "secrets.json"
    store.write_text(
        json.dumps({"global": {"KUN_WORLD_BROWSER_ALLOWED_HOSTS": "app.example.com"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(SECRET_STORE_FILE_ENV, str(store))
    monkeypatch.delenv("KUN_WORLD_BROWSER_ALLOWED_HOSTS", raising=False)

    handler = BrowserExecuteHandler.from_env(tmp_path)
    result = await handler.preview(
        WorldAction(
            action_id="wa_2",
            tenant_id="tenant-a",
            task_ref="task-1",
            action_type="browser.execute",
            target_ref="https://app.example.com/login",
            risk_level="high",
            payload={"steps": [{"kind": "goto"}]},
        )
    )

    assert result.status == "preview"
    assert result.audit["allowed_hosts"] == ["app.example.com"]
