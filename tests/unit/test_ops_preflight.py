from __future__ import annotations

import os
from pathlib import Path

import pytest
from kun.cli import app
from kun.core.config import Settings
from kun.ops import dogfood as dogfood_module
from kun.ops.dogfood import run_v4_dogfood
from kun.ops.preflight import run_preflight
from kun.ops.secret_audit import audit_runtime_secrets
from kun.ops.tenant_onboarding import create_tenant_onboarding_pack
from kun.security.auth import verify_bearer_token
from typer.testing import CliRunner


def _safe_prod_settings() -> Settings:
    return Settings(
        env="production",
        default_tenant_id=None,
        auth_secret=None,
        auth_secrets=(
            "new-prod-key-7f1b9c2d4e6a8b0c9d3e5f7a," "old-prod-key-8a2c4e6f0b1d3f5a7c9e0d2b"
        ),
        pg_dsn="postgresql+asyncpg://kun_app:prod-app-pw@db.internal:5432/kun",
        pg_admin_dsn="postgresql+asyncpg://kun_admin:prod-admin-pw@db.internal:5432/kun",
        s3_endpoint="https://objects.internal",
        s3_access_key="prod-access",
        s3_secret_key="prod-secret",
    )


@pytest.mark.unit
def test_preflight_blocks_unsafe_production_config(tmp_path: Path) -> None:
    cfg = Settings(
        env="production",
        default_tenant_id="u-sylvan",
        auth_secret="short",
        pg_dsn="postgresql+asyncpg://kun:kun@localhost:55432/kun",
    )
    report = run_preflight(cfg=cfg, repo_root=tmp_path, run_alembic_heads=False)

    assert report.status == "block"
    details = " ".join(check.detail for check in report.checks)
    assert "KUN_DEFAULT_TENANT_ID" in details
    assert "KUN_AUTH_SECRET" in details
    assert "KUN_PG_DSN" in details
    assert any(check.check_id == "backup_script" for check in report.blockers)
    assert any(check.check_id.startswith("secret_audit:") for check in report.blockers)


@pytest.mark.unit
def test_preflight_passes_core_checks_for_safe_config(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "backup_postgres.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "restore_postgres_smoke.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    report = run_preflight(
        cfg=_safe_prod_settings(),
        repo_root=tmp_path,
        run_alembic_heads=False,
    )

    assert report.status == "pass"
    assert report.blockers == []


@pytest.mark.unit
def test_preflight_accepts_auth_secret_rotation_list(tmp_path: Path) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "backup_postgres.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "restore_postgres_smoke.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    cfg = _safe_prod_settings().model_copy(
        update={
            "auth_secret": None,
            "auth_secrets": (
                "new-prod-key-7f1b9c2d4e6a8b0c9d3e5f7a," "old-prod-key-8a2c4e6f0b1d3f5a7c9e0d2b"
            ),
        }
    )

    report = run_preflight(cfg=cfg, repo_root=tmp_path, run_alembic_heads=False)

    assert report.status == "pass"
    assert cfg.auth_secret_candidates()[0].startswith("new-")


@pytest.mark.unit
def test_preflight_blocks_half_enabled_world_gateway_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "backup_postgres.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "restore_postgres_smoke.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setenv("KUN_WORLD_EMAIL_SEND_ENABLED", "true")
    monkeypatch.delenv("KUN_WORLD_SMTP_HOST", raising=False)
    monkeypatch.delenv("KUN_WORLD_SMTP_FROM", raising=False)

    report = run_preflight(
        cfg=_safe_prod_settings(),
        repo_root=tmp_path,
        run_alembic_heads=False,
    )

    assert report.status == "block"
    assert any(check.check_id == "world_handler_config:email.send" for check in report.blockers)
    details = " ".join(check.detail for check in report.blockers)
    assert "KUN_WORLD_SMTP_HOST" in details
    assert "KUN_WORLD_SMTP_FROM" in details


@pytest.mark.unit
def test_preflight_accepts_enabled_world_gateway_handler_with_required_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "backup_postgres.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "restore_postgres_smoke.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    monkeypatch.setenv("KUN_WORLD_EMAIL_SEND_ENABLED", "true")
    monkeypatch.setenv("KUN_WORLD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("KUN_WORLD_SMTP_FROM", "kun@example.com")

    report = run_preflight(
        cfg=_safe_prod_settings(),
        repo_root=tmp_path,
        run_alembic_heads=False,
    )

    assert report.status == "pass"
    assert any(
        check.check_id == "world_handler_config:email.send" and check.severity == "ok"
        for check in report.checks
    )


@pytest.mark.unit
def test_secret_audit_blocks_default_database_credentials() -> None:
    cfg = Settings(
        env="production",
        default_tenant_id=None,
        auth_secret=None,
        auth_secrets=(
            "new-prod-key-7f1b9c2d4e6a8b0c9d3e5f7a," "old-prod-key-8a2c4e6f0b1d3f5a7c9e0d2b"
        ),
        pg_dsn="postgresql+asyncpg://kun_app:kun_app@db.internal:5432/kun",
        pg_admin_dsn="postgresql+asyncpg://kun:kun@db.internal:5432/kun",
        s3_endpoint="https://objects.internal",
        s3_access_key="prod-access",
        s3_secret_key="prod-secret",
    )

    report = audit_runtime_secrets(cfg=cfg, environ={})

    assert report.status == "block"
    ids = {item.item_id for item in report.blockers}
    assert "database.app_default_credential" in ids
    assert "database.admin_default_credential" in ids


@pytest.mark.unit
def test_secret_audit_detects_partial_enterprise_api_auth() -> None:
    report = audit_runtime_secrets(
        cfg=_safe_prod_settings(),
        environ={
            "KUN_WORLD_API_POST_ENABLED": "true",
            "KUN_WORLD_API_ALLOWED_HOSTS": "api.example.com",
            "KUN_WORLD_API_AUTH_HEADER": "Authorization",
        },
    )

    assert report.status == "block"
    assert any(
        item.item_id == "world_gateway.enterprise_api.partial_auth" for item in report.blockers
    )


@pytest.mark.unit
def test_tenant_onboarding_pack_mints_verifiable_token() -> None:
    secret = "s" * 40
    pack = create_tenant_onboarding_pack(
        tenant_id="tenant-a",
        user_id="user-a",
        scopes=["world:approve", "world:dispatch"],
        audience="expert",
        ttl_sec=3600,
        secret=secret,
    )

    claims = verify_bearer_token(f"Bearer {pack.bearer_token}", secret)

    assert claims.tenant_id == "tenant-a"
    assert claims.user_id == "user-a"
    assert claims.audience == "expert"
    assert claims.scopes == ("world:approve", "world:dispatch")
    assert any("完整账号体系" in item for item in pack.missing_full_product)


@pytest.mark.unit
def test_ops_onboard_tenant_cli_outputs_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_AUTH_SECRET", "z" * 40)

    result = CliRunner().invoke(
        app,
        [
            "ops",
            "onboard-tenant",
            "--tenant",
            "tenant-cli",
            "--user",
            "user-cli",
            "--scopes",
            "world:approve",
        ],
    )

    assert result.exit_code == 0
    assert "tenant-cli" in result.output
    assert "bearer_token" in result.output


@pytest.mark.unit
def test_ops_preflight_cli_can_skip_alembic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_ENV", "dev")
    # Make sure a real shell env secret does not influence this smoke.
    monkeypatch.delenv("KUN_AUTH_SECRET", raising=False)

    result = CliRunner().invoke(
        app,
        ["ops", "preflight", "--skip-alembic", "--no-fail-on-blocker"],
        env={**os.environ, "KUN_ENV": "dev"},
    )

    assert result.exit_code == 0
    assert "KUN production preflight" in result.output


@pytest.mark.unit
async def test_v4_dogfood_smoke_runs_without_external_services() -> None:
    report = await run_v4_dogfood(tenant_id="tenant-dogfood")

    assert report.status in {"pass", "warn"}
    assert report.blockers == []
    ids = {item.scenario_id for item in report.scenarios}
    assert "tenant_onboarding_token" in ids
    assert "world_gateway_low_risk_handler" in ids


@pytest.mark.unit
async def test_v4_dogfood_can_include_db_mission_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_mission_scenario(*, tenant_id: str) -> dogfood_module.DogfoodScenarioResult:
        assert tenant_id == "tenant-dogfood"
        return dogfood_module.DogfoodScenarioResult(
            scenario_id="mission_resume_db",
            status="pass",
            summary="fake mission pass",
        )

    monkeypatch.setattr(dogfood_module, "_scenario_mission_resume_db", fake_mission_scenario)

    report = await dogfood_module.run_v4_dogfood(
        tenant_id="tenant-dogfood",
        include_db_mission=True,
    )

    assert any(item.scenario_id == "mission_resume_db" for item in report.scenarios)


@pytest.mark.unit
def test_ops_dogfood_cli_outputs_scenarios() -> None:
    result = CliRunner().invoke(
        app,
        ["ops", "dogfood", "--tenant", "tenant-cli", "--json", "--no-fail-on-blocker"],
    )

    assert result.exit_code == 0
    assert '"status"' in result.output
    assert "world_gateway_low_risk_handler" in result.output


@pytest.mark.unit
def test_ops_dogfood_cli_can_request_db_mission_scenario(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    async def fake_run_v4_dogfood(
        *,
        tenant_id: str = "u-sylvan",
        repo_root: Path | None = None,
        secret: str = "dogfood-secret-" + "x" * 32,
        include_db_mission: bool = False,
    ) -> dogfood_module.DogfoodReport:
        assert tenant_id == "tenant-cli"
        assert repo_root is None
        assert secret.startswith("dogfood-secret-")
        calls.append(include_db_mission)
        return dogfood_module.DogfoodReport(
            status="pass",
            scenarios=[
                dogfood_module.DogfoodScenarioResult(
                    scenario_id="mission_resume_db",
                    status="pass",
                    summary="fake mission pass",
                )
            ],
        )

    monkeypatch.setattr(dogfood_module, "run_v4_dogfood", fake_run_v4_dogfood)

    result = CliRunner().invoke(
        app,
        [
            "ops",
            "dogfood",
            "--tenant",
            "tenant-cli",
            "--include-db-mission",
            "--json",
            "--no-fail-on-blocker",
        ],
    )

    assert result.exit_code == 0
    assert calls == [True]
    assert "mission_resume_db" in result.output


@pytest.mark.unit
def test_ops_delivery_status_cli_outputs_honest_boundaries() -> None:
    result = CliRunner().invoke(app, ["ops", "delivery-status", "--json"])

    assert result.exit_code == 0
    assert '"capability_id": "production_deployment"' in result.output
    assert '"status": "not_ready"' in result.output
    assert '"validation_issues": []' in result.output


@pytest.mark.unit
def test_ops_delivery_status_can_fail_release_gate_on_not_ready() -> None:
    result = CliRunner().invoke(app, ["ops", "delivery-status", "--fail-on-not-ready"])

    assert result.exit_code == 3
    assert "KUN delivery status" in result.output


@pytest.mark.unit
def test_ops_secret_audit_cli_outputs_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_ENV", "dev")
    monkeypatch.delenv("KUN_AUTH_SECRET", raising=False)
    monkeypatch.delenv("KUN_AUTH_SECRETS", raising=False)

    result = CliRunner().invoke(
        app,
        ["ops", "secret-audit", "--json", "--no-fail-on-blocker"],
        env={**os.environ, "KUN_ENV": "dev"},
    )

    assert result.exit_code == 0
    assert '"status"' in result.output
    assert '"items"' in result.output
