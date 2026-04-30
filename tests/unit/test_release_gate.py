from __future__ import annotations

from pathlib import Path

import pytest
from kun.cli import app
from kun.ops.release_gate import run_release_gate
from typer.testing import CliRunner


def _write_release_files(root: Path) -> None:
    docs = root / "docs" / "ops"
    docs.mkdir(parents=True)
    (docs / "release-checklist-v4.md").write_text(
        "tag rollback hotfix backup restore",
        encoding="utf-8",
    )
    scripts = root / "scripts"
    scripts.mkdir()
    (scripts / "backup_postgres.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "restore_postgres_smoke.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (scripts / "backup_restore_drill.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    (scripts / "check_legal_guard.py").write_text("print('ok')\n", encoding="utf-8")


@pytest.mark.unit
def test_release_gate_blocks_bad_tag(tmp_path: Path) -> None:
    _write_release_files(tmp_path)

    report = run_release_gate(
        release_tag="bad",
        repo_root=tmp_path,
        run_git=False,
        run_alembic_heads=False,
        require_ready=False,
    )

    assert report.status == "block"
    assert any(item.check_id == "tag_shape" for item in report.blockers)


@pytest.mark.unit
def test_release_gate_checks_v4_checklist(tmp_path: Path) -> None:
    _write_release_files(tmp_path)

    report = run_release_gate(
        release_tag="v4.0.0",
        repo_root=tmp_path,
        run_git=False,
        run_alembic_heads=False,
        require_ready=False,
    )

    ids = {item.check_id for item in report.checks}
    assert "release_checklist_v4" in ids
    assert "legal_guard" in ids
    assert not any(item.check_id == "release_checklist_v4" for item in report.blockers)


@pytest.mark.unit
def test_ops_release_check_cli_json_can_skip_git_and_alembic() -> None:
    result = CliRunner().invoke(
        app,
        [
            "ops",
            "release-check",
            "--tag",
            "v4.0.0-test",
            "--skip-git",
            "--skip-alembic",
            "--no-fail-on-blocker",
            "--json",
        ],
    )

    assert result.exit_code == 0
    assert "release_checklist_v4" in result.output
    assert "v4.0.0-test" in result.output
