"""Wire (dogfood): kun lab dogfood CLI 子命令."""

from __future__ import annotations

import asyncio
import contextlib
import os

import pytest
from kun.cli import app
from kun.lab import reset_experiment_log, reset_recipe_registry
from typer.testing import CliRunner

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate():
    reset_experiment_log()
    reset_recipe_registry()
    saved_env = os.environ.pop("KUN_LAB_MODE", None)
    yield
    reset_experiment_log()
    reset_recipe_registry()
    if saved_env is not None:
        os.environ["KUN_LAB_MODE"] = saved_env
    else:
        os.environ.pop("KUN_LAB_MODE", None)
    with contextlib.suppress(Exception):
        asyncio.set_event_loop(asyncio.new_event_loop())


def test_dogfood_without_enable_blocks() -> None:
    """没 KUN_LAB_MODE=1 + 没 --enable → exit 2."""
    result = runner.invoke(app, ["lab", "dogfood"])
    assert result.exit_code == 2
    assert "未启用" in result.output


def test_dogfood_with_enable_runs_full_loop() -> None:
    """--enable → 跑完 4 步 + 输出 registry + classifier 决策."""
    result = runner.invoke(app, ["lab", "dogfood", "--enable", "--paths", "2", "--types", "test_a"])
    assert result.exit_code == 0, result.output
    # 4 个 step 标题
    assert "Step 1/4" in result.output
    assert "Step 2/4" in result.output
    assert "Step 3/4" in result.output
    assert "Step 4/4" in result.output
    # registry size > 0
    assert "registry size now" in result.output
    # classifier 决策表
    assert "classifier 决策" in result.output


def test_dogfood_writes_report_json() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(
            app,
            [
                "lab",
                "dogfood",
                "--enable",
                "--paths",
                "2",
                "--types",
                "test_a,test_b",
                "--report-path",
                "dogfood-report.json",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "dogfood report written" in result.output

        import json
        from pathlib import Path

        data = json.loads(Path("dogfood-report.json").read_text())
        assert data["task_types"] == ["test_a", "test_b"]
        assert data["experiment_count"] == 10
        assert data["registry"]
        assert data["classifier_decisions"]


def test_dogfood_custom_task_types() -> None:
    """--types 多 task_type 都跑."""
    result = runner.invoke(
        app,
        [
            "lab",
            "dogfood",
            "--enable",
            "--paths",
            "2",
            "--types",
            "type_a,type_b,type_c",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "type_a" in result.output
    assert "type_b" in result.output
    assert "type_c" in result.output


def test_dogfood_empty_types_rejected() -> None:
    result = runner.invoke(app, ["lab", "dogfood", "--enable", "--types", ""])
    assert result.exit_code == 2
