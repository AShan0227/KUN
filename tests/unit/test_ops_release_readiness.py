from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from kun.cli import app
from kun.core.config import Settings
from kun.ops.release_readiness import run_release_readiness
from typer.testing import CliRunner


def _safe_prod_settings() -> Settings:
    return Settings(
        env="production",
        default_tenant_id=None,
        auth_secret=None,
        auth_secrets=(
            "new-prod-key-7f1b9c2d4e6a8b0c9d3e5f7a,old-prod-key-8a2c4e6f0b1d3f5a7c9e0d2b"
        ),
        pg_dsn="postgresql+asyncpg://kun_app:prod-app-pw@db.internal:5432/kun",
        pg_admin_dsn="postgresql+asyncpg://kun_admin:prod-admin-pw@db.internal:5432/kun",
        s3_endpoint="https://objects.internal",
        s3_access_key="prod-access",
        s3_secret_key="prod-secret",
    )


def _make_release_repo(root: Path) -> None:
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "backup_postgres.sh").write_text("#!/usr/bin/env bash\n")
    (root / "scripts" / "restore_postgres_smoke.sh").write_text("#!/usr/bin/env bash\n")
    (root / "scripts" / "backup_restore_drill.py").write_text("#!/usr/bin/env python3\n")
    (root / "docs" / "ops").mkdir(parents=True)
    (root / "docs" / "ops" / "release-checklist-v4.md").write_text(
        "tag rollback hotfix backup restore\n",
        encoding="utf-8",
    )
    (root / "alembic" / "versions").mkdir(parents=True)
    (root / "alembic" / "versions" / "0001_initial.py").write_text(
        'revision = "0001"\ndown_revision = None\n',
        encoding="utf-8",
    )
    (root / "CHANGELOG-v4.0.md").write_text("# v4.0\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


@pytest.mark.unit
def test_release_readiness_passes_for_clean_local_release_repo(tmp_path: Path) -> None:
    _make_release_repo(tmp_path)

    report = run_release_readiness(
        version="v4.0.0",
        repo_root=tmp_path,
        cfg=_safe_prod_settings(),
        run_alembic_heads=False,
        run_legal_guard=False,
    )

    assert report.status == "pass"
    ids = {check.check_id for check in report.checks if check.severity == "ok"}
    assert {
        "tag_name",
        "git_worktree_clean",
        "git_tag_available",
        "alembic_local_single_head",
        "backup_restore_prerequisites",
        "release_checklist_v4",
        "delivery_status_validation",
        "production_preflight",
    } <= ids


@pytest.mark.unit
def test_release_readiness_blocks_dirty_worktree_and_existing_tag(tmp_path: Path) -> None:
    _make_release_repo(tmp_path)
    subprocess.run(["git", "tag", "v4.0.0"], cwd=tmp_path, check=True)
    (tmp_path / "dirty.txt").write_text("not committed", encoding="utf-8")

    report = run_release_readiness(
        version="v4.0.0",
        repo_root=tmp_path,
        cfg=_safe_prod_settings(),
        run_alembic_heads=False,
        run_legal_guard=False,
    )

    blockers = {check.check_id for check in report.blockers}
    assert report.status == "block"
    assert "git_worktree_clean" in blockers
    assert "git_tag_available" in blockers


@pytest.mark.unit
def test_release_readiness_blocks_bad_tag_and_missing_rollback_notes(tmp_path: Path) -> None:
    _make_release_repo(tmp_path)
    (tmp_path / "docs" / "ops" / "release-checklist-v4.md").write_text(
        "tag only\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "break checklist"], cwd=tmp_path, check=True)

    report = run_release_readiness(
        version="4.0",
        repo_root=tmp_path,
        cfg=_safe_prod_settings(),
        run_alembic_heads=False,
        run_legal_guard=False,
    )

    blockers = {check.check_id for check in report.blockers}
    assert "tag_name" in blockers
    assert "release_checklist_v4" in blockers


@pytest.mark.unit
def test_ops_release_check_cli_outputs_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_ENV", "dev")
    result = CliRunner().invoke(
        app,
        [
            "ops",
            "release-check",
            "--version",
            "v4.0.0",
            "--skip-alembic",
            "--skip-legal-guard",
            "--allow-dirty",
            "--json",
            "--no-fail-on-blocker",
        ],
    )

    assert result.exit_code == 0
    assert '"version": "v4.0.0"' in result.output
    assert '"checks"' in result.output
