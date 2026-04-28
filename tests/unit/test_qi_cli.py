"""V2.3 `kun qi` CLI 子命令 status / start / stop."""

from __future__ import annotations

import os
from unittest.mock import patch

from kun.cli import app
from kun.qi import reset_qi_budget
from typer.testing import CliRunner


def test_qi_status_disabled_default() -> None:
    runner = CliRunner()
    reset_qi_budget()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_QI_ENABLED", None)
        os.environ.pop("KUN_QI_FORCE_ACTIVE", None)
        result = runner.invoke(app, ["qi", "status"])
    assert result.exit_code == 0
    assert "启 (Qi) status" in result.output
    assert "window_active" in result.output


def test_qi_status_enabled_force_active() -> None:
    runner = CliRunner()
    reset_qi_budget()
    with patch.dict(os.environ, {"KUN_QI_ENABLED": "1", "KUN_QI_FORCE_ACTIVE": "1"}, clear=False):
        result = runner.invoke(app, ["qi", "status"])
    assert result.exit_code == 0
    assert "yes" in result.output  # window_active = yes


def test_qi_start_sets_force_active() -> None:
    runner = CliRunner()
    reset_qi_budget()
    with patch.dict(os.environ, {}, clear=False):
        result = runner.invoke(app, ["qi", "start", "--duration", "30", "--budget", "2.5"])
    assert result.exit_code == 0
    assert "force-active" in result.output
    # Note: env mutation only affects parent in real shell; we just verify exit code


def test_qi_stop_clears_force_active() -> None:
    runner = CliRunner()
    reset_qi_budget()
    with patch.dict(os.environ, {"KUN_QI_FORCE_ACTIVE": "1"}, clear=False):
        result = runner.invoke(app, ["qi", "stop"])
    assert result.exit_code == 0
    assert "stopped" in result.output


def test_qi_help_lists_subcommands() -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["qi", "--help"])
    assert result.exit_code == 0
    assert "status" in result.output
    assert "start" in result.output
    assert "stop" in result.output
