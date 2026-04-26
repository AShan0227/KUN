"""KUN-Lab CLI 子命令单测 (Wire 22)."""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from kun.cli import app
from kun.lab import (
    EnsembleConfig,
    EnsemblePathResult,
    EnsembleResult,
    reset_experiment_log,
)
from typer.testing import CliRunner

runner = CliRunner()


def _fake_result() -> EnsembleResult:
    return EnsembleResult(
        experiment_id="exp-cli-1",
        config=EnsembleConfig(n_paths=2),
        path_results=[
            EnsemblePathResult(
                path_idx=0,
                config={"strategy": "tier_top_low_temp", "tier": "top"},
                output="winning_text",
                score=0.9,
                cost_usd=0.05,
                latency_sec=0.1,
            ),
            EnsemblePathResult(
                path_idx=1,
                config={"strategy": "tier_cheap_high_temp", "tier": "cheap"},
                output="other_text",
                score=0.4,
                cost_usd=0.01,
                latency_sec=0.05,
            ),
        ],
        winning_path_idx=0,
        winning_output="winning_text",
        total_cost_usd=0.06,
        total_latency_sec=0.1,
        selection_reason="best_score:0.90",
    )


@pytest.fixture(autouse=True)
def _isolate_lab_state():
    """每个测试 reset ExperimentLog singleton + 干净 env + 修复 default event loop.

    asyncio.run() 在 lab CLI 内会关闭 default loop, 后续测试若用 deprecated
    asyncio.get_event_loop() 会 raise. yield 后 set_event_loop(new_event_loop())
    隔离, 不影响其他测试.
    """
    reset_experiment_log()
    saved_env = os.environ.pop("KUN_LAB_MODE", None)
    yield
    reset_experiment_log()
    if saved_env is not None:
        os.environ["KUN_LAB_MODE"] = saved_env
    else:
        os.environ.pop("KUN_LAB_MODE", None)
    with contextlib.suppress(Exception):
        asyncio.set_event_loop(asyncio.new_event_loop())


# ---- kun lab (no args) ----


def test_lab_help_shows_subcommands() -> None:
    result = runner.invoke(app, ["lab", "--help"])
    assert result.exit_code == 0
    assert "run" in result.output
    assert "stats" in result.output
    assert "promote" in result.output


# ---- kun lab run ----


def test_lab_run_without_enable_blocks() -> None:
    """没 KUN_LAB_MODE=1 + 没 --enable → exit code 2."""
    result = runner.invoke(app, ["lab", "run", "test prompt"])
    assert result.exit_code == 2
    assert "未启用" in result.output


def test_lab_run_with_enable_flag_works() -> None:
    """--enable → set env + 跑 + 输出实验表格."""
    fake_result = _fake_result()

    fake_executor = AsyncMock()
    fake_executor.run = AsyncMock(return_value=fake_result)

    with (
        patch("kun.lab.EnsembleExecutor", return_value=fake_executor),
        patch("kun.lab.make_default_adapter"),
    ):
        result = runner.invoke(
            app, ["lab", "run", "Q4 plan", "--enable", "--paths", "2", "--no-emit"]
        )

    assert result.exit_code == 0
    assert "exp-cli-1" in result.output
    assert "winning_text" in result.output
    assert "tier_top" in result.output  # strategy 前缀 (Rich 80-col 可能截断)


def test_lab_run_records_into_experiment_log() -> None:
    """跑完 ExperimentLog singleton 应该有 1 条."""
    fake_result = _fake_result()

    fake_executor = AsyncMock()
    fake_executor.run = AsyncMock(return_value=fake_result)

    with (
        patch("kun.lab.EnsembleExecutor", return_value=fake_executor),
        patch("kun.lab.make_default_adapter"),
    ):
        result = runner.invoke(
            app, ["lab", "run", "Q4 plan", "--enable", "--paths", "2", "--no-emit"]
        )

    assert result.exit_code == 0
    from kun.lab import get_experiment_log

    assert len(get_experiment_log().list_all()) == 1


def test_lab_run_passes_config_correctly() -> None:
    """--paths --selection --task-type 都正确传 EnsembleConfig."""
    fake_result = _fake_result()
    captured_kwargs: dict[str, Any] = {}

    async def fake_run(prompt, config=None, **kwargs):
        captured_kwargs["prompt"] = prompt
        captured_kwargs["config"] = config
        captured_kwargs.update(kwargs)
        return fake_result

    fake_executor = AsyncMock()
    fake_executor.run = fake_run  # type: ignore[assignment]

    with (
        patch("kun.lab.EnsembleExecutor", return_value=fake_executor),
        patch("kun.lab.make_default_adapter"),
    ):
        result = runner.invoke(
            app,
            [
                "lab", "run", "test", "--enable",
                "--paths", "3",
                "--selection", "majority_vote",
                "--task-type", "ad_creative",
                "--no-emit",
            ],
        )

    assert result.exit_code == 0
    cfg = captured_kwargs["config"]
    assert cfg.n_paths == 3
    assert cfg.selection_method == "majority_vote"
    assert captured_kwargs["task_type"] == "ad_creative"


# ---- kun lab stats ----


def test_lab_stats_empty_log_shows_message() -> None:
    result = runner.invoke(app, ["lab", "stats"])
    assert result.exit_code == 0
    assert "empty" in result.output.lower()


def test_lab_stats_displays_recipe_stats() -> None:
    """log 有数据 → 表格输出 (Rich truncate 在 80-col, 用短前缀检查)."""
    from kun.lab import get_experiment_log

    log = get_experiment_log()
    log.record(task_type="ad", ensemble_result=_fake_result())

    result = runner.invoke(app, ["lab", "stats"])
    assert result.exit_code == 0
    assert "tier_top" in result.output  # strategy 前缀 (Rich 可能截断)
    assert "1/1" in result.output  # win 1, total 1
    assert "1.00" in result.output  # win_rate
    assert "n_experiments=1" in result.output


def test_lab_stats_filters_by_task_type() -> None:
    from kun.lab import get_experiment_log

    log = get_experiment_log()
    log.record(task_type="ad", ensemble_result=_fake_result())
    log.record(task_type="biz", ensemble_result=_fake_result())

    result = runner.invoke(app, ["lab", "stats", "--task-type", "biz"])
    assert result.exit_code == 0
    assert "biz" in result.output


# ---- kun lab promote ----


def test_lab_promote_empty_log() -> None:
    result = runner.invoke(app, ["lab", "promote"])
    assert result.exit_code == 0
    assert "empty" in result.output.lower()


def test_lab_promote_dry_run_no_emit() -> None:
    """默认 --dry-run → 列 eligible + 不 emit."""
    from kun.lab import get_experiment_log

    log = get_experiment_log()
    for i in range(12):
        result_obj = EnsembleResult(
            experiment_id=f"e{i}",
            config=EnsembleConfig(n_paths=2),
            path_results=[
                EnsemblePathResult(
                    path_idx=0, config={"strategy": "tier_top_low_temp"}, score=0.9
                ),
            ],
            winning_path_idx=0,
        )
        log.record(task_type="ad", ensemble_result=result_obj)

    result = runner.invoke(app, ["lab", "promote", "--min-total", "10"])
    assert result.exit_code == 0
    assert "tier_top" in result.output  # strategy 前缀
    assert "dry_run" in result.output  # 提示


def test_lab_promote_apply_invokes_emitter() -> None:
    """--apply → 真调 promote_eligible (我们 mock LabEventEmitter)."""
    from kun.lab import get_experiment_log

    log = get_experiment_log()
    for i in range(12):
        log.record(
            task_type="ad",
            ensemble_result=EnsembleResult(
                experiment_id=f"e{i}",
                config=EnsembleConfig(n_paths=2),
                path_results=[
                    EnsemblePathResult(
                        path_idx=0, config={"strategy": "tier_top_low_temp"}, score=0.9
                    ),
                ],
                winning_path_idx=0,
            ),
        )

    fake_emit = AsyncMock(return_value=True)
    with patch("kun.lab.events.LabEventEmitter.on_recipe_promoted", new=fake_emit):
        result = runner.invoke(app, ["lab", "promote", "--min-total", "10", "--apply"])

    assert result.exit_code == 0
    assert "推升" in result.output or "promoted" in result.output.lower()
    # emit 至少 call 1 次
    assert fake_emit.await_count >= 1
