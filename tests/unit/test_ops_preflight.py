from __future__ import annotations

import os
from pathlib import Path

import pytest
from kun.cli import app
from kun.core.config import Settings
from kun.ops.dogfood import run_v4_dogfood
from kun.ops.preflight import run_preflight
from kun.ops.tenant_onboarding import create_tenant_onboarding_pack
from kun.security.auth import verify_bearer_token
from typer.testing import CliRunner


def _safe_prod_settings() -> Settings:
    return Settings(
        env="production",
        default_tenant_id=None,
        auth_secret="x" * 40,
        pg_dsn="postgresql+asyncpg://kun_app:kun_app@localhost:55432/kun",
        pg_admin_dsn="postgresql+asyncpg://kun:kun@localhost:55432/kun",
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
            "auth_secrets": "new-" + "x" * 40 + ",old-" + "y" * 40,
        }
    )

    report = run_preflight(cfg=cfg, repo_root=tmp_path, run_alembic_heads=False)

    assert report.status == "pass"
    assert cfg.auth_secret_candidates()[0].startswith("new-")


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
def test_ops_dogfood_cli_outputs_scenarios() -> None:
    result = CliRunner().invoke(
        app,
        ["ops", "dogfood", "--tenant", "tenant-cli", "--json", "--no-fail-on-blocker"],
    )

    assert result.exit_code == 0
    assert '"status"' in result.output
    assert "world_gateway_low_risk_handler" in result.output
