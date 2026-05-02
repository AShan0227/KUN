"""KUN-Lab benchmark suite tests (BATCH10 C40)."""

from __future__ import annotations

import contextlib
import os
from typing import Any
from unittest.mock import patch

import pytest
from kun.cli import app
from kun.core.metrics import lab_benchmark_run_total, lab_benchmark_winrate
from kun.lab import (
    BenchmarkRunOptions,
    EnsembleConfig,
    EnsemblePathResult,
    EnsembleResult,
    HistoricalTaskReplayTarget,
    LabReplayReport,
    get_experiment_log,
    list_benchmark_datasets,
    load_benchmark_dataset,
    replay_historical_task,
    reset_experiment_log,
    run_benchmark_suite,
)
from typer.testing import CliRunner


def _metric_sample(collector, sample_name: str, **labels: str) -> float:
    for metric in collector.collect():
        for sample in metric.samples:
            if sample.name != sample_name:
                continue
            if all(sample.labels.get(key) == value for key, value in labels.items()):
                return float(sample.value)
    return 0.0


def _fake_result(experiment_id: str = "exp-bench") -> EnsembleResult:
    return EnsembleResult(
        experiment_id=experiment_id,
        config=EnsembleConfig(n_paths=2),
        path_results=[
            EnsemblePathResult(
                path_idx=0,
                config={"strategy": "tier_top_low_temp", "tier": "top"},
                output="AI 会议 客户 成本 安全",
                score=0.9,
                cost_usd=0.03,
                latency_sec=0.1,
            ),
            EnsemblePathResult(
                path_idx=1,
                config={"strategy": "tier_cheap_high_temp", "tier": "cheap"},
                output="ok",
                score=0.2,
                cost_usd=0.01,
                latency_sec=0.05,
            ),
        ],
        winning_path_idx=0,
        winning_output="AI 会议 客户 成本 安全",
        total_cost_usd=0.04,
        total_latency_sec=0.1,
    )


class _FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def run(self, prompt, config=None, scoring_fn=None, task_type=None):
        self.calls.append(
            {
                "prompt": prompt,
                "config": config,
                "scoring_fn": scoring_fn,
                "task_type": task_type,
            }
        )
        return _fake_result(f"exp-{len(self.calls)}")


@pytest.fixture(autouse=True)
def _clean_lab_env():
    reset_experiment_log()
    saved = os.environ.pop("KUN_LAB_MODE", None)
    yield
    reset_experiment_log()
    if saved is None:
        os.environ.pop("KUN_LAB_MODE", None)
    else:
        os.environ["KUN_LAB_MODE"] = saved


def test_builtin_benchmark_datasets_have_expected_sizes() -> None:
    assert set(list_benchmark_datasets()) >= {
        "marketing_copy",
        "code_refactor",
        "decision_analysis",
    }
    assert len(load_benchmark_dataset("marketing_copy").items) == 20
    assert len(load_benchmark_dataset("code_refactor").items) == 20
    assert len(load_benchmark_dataset("decision_analysis").items) == 15


@pytest.mark.asyncio
async def test_run_benchmark_suite_records_and_emits_metrics() -> None:
    dataset = load_benchmark_dataset("marketing_copy")
    executor = _FakeExecutor()
    before = _metric_sample(
        lab_benchmark_run_total,
        "kun_lab_benchmark_run_total",
        dataset="marketing_copy",
    )

    report = await run_benchmark_suite(
        dataset,
        executor=executor,  # type: ignore[arg-type]
        experiment_log=get_experiment_log(),
        options=BenchmarkRunOptions(limit=2, paths=2),
    )

    assert report.dataset == "marketing_copy"
    assert report.total_items == 2
    assert len(executor.calls) == 2
    assert len(get_experiment_log().by_task_type("lab_benchmark.marketing_copy")) == 2
    assert report.strategy_stats[0].strategy == "tier_top_low_temp"
    after = _metric_sample(
        lab_benchmark_run_total,
        "kun_lab_benchmark_run_total",
        dataset="marketing_copy",
    )
    assert after == before + 1
    assert (
        _metric_sample(
            lab_benchmark_winrate,
            "kun_lab_benchmark_winrate",
            dataset="marketing_copy",
            strategy="tier_top_low_temp",
        )
        == 1.0
    )


def test_lab_benchmark_suite_cli_blocks_when_disabled() -> None:
    result = CliRunner().invoke(app, ["lab", "benchmark", "suite", "marketing_copy"])

    assert result.exit_code == 2
    assert "未启用" in result.output


def test_lab_benchmark_suite_cli_smoke() -> None:
    fake_executor = _FakeExecutor()
    with (
        patch("kun.lab.EnsembleExecutor", return_value=fake_executor),
        patch("kun.lab.make_default_adapter", return_value=object()),
    ):
        result = CliRunner().invoke(
            app,
            [
                "lab",
                "benchmark",
                "suite",
                "marketing_copy",
                "--limit",
                "1",
                "--paths",
                "2",
                "--enable",
            ],
        )

    assert result.exit_code == 0
    assert "lab benchmark" in result.output
    assert "tier_top" in result.output


def test_lab_benchmark_report_cli_reads_experiment_log() -> None:
    log = get_experiment_log()
    log.record(task_type="lab_benchmark.marketing_copy", ensemble_result=_fake_result())

    result = CliRunner().invoke(app, ["lab", "benchmark", "report", "--dataset", "marketing_copy"])

    assert result.exit_code == 0
    assert "lab benchmark" in result.output
    assert "tier_top" in result.output


@pytest.mark.asyncio
async def test_replay_historical_task_compares_winner_strategy() -> None:
    executor = _FakeExecutor()

    async def fake_loader(task_id: str, *, tenant_id: str) -> HistoricalTaskReplayTarget:
        return HistoricalTaskReplayTarget(
            task_id=task_id,
            tenant_id=tenant_id,
            task_type="writing.creative",
            prompt="write launch copy",
            original_winning_strategy="tier_top_low_temp",
        )

    report = await replay_historical_task(
        "task-1",
        tenant_id="u-sylvan",
        executor=executor,  # type: ignore[arg-type]
        experiment_log=get_experiment_log(),
        options=BenchmarkRunOptions(paths=2),
        task_loader=fake_loader,
    )

    assert report.task_id == "task-1"
    assert report.task_type == "writing.creative"
    assert report.original_winning_strategy == "tier_top_low_temp"
    assert report.replay_winning_strategy == "tier_top_low_temp"
    assert report.matches_original is True
    assert executor.calls[0]["prompt"] == "write launch copy"
    assert executor.calls[0]["task_type"] == "lab_replay.writing.creative"
    assert len(get_experiment_log().by_task_type("lab_replay.writing.creative")) == 1


@pytest.mark.asyncio
async def test_replay_historical_task_unknown_baseline_is_not_failure() -> None:
    executor = _FakeExecutor()

    async def fake_loader(task_id: str, *, tenant_id: str) -> HistoricalTaskReplayTarget:
        return HistoricalTaskReplayTarget(
            task_id=task_id,
            tenant_id=tenant_id,
            task_type="analysis",
            prompt="analyze",
            original_winning_strategy=None,
        )

    report = await replay_historical_task(
        "task-2",
        tenant_id="u-sylvan",
        executor=executor,  # type: ignore[arg-type]
        task_loader=fake_loader,
    )

    assert report.original_winning_strategy is None
    assert report.matches_original is None


def test_lab_benchmark_replay_cli_smoke() -> None:
    async def fake_replay(*_args: Any, **_kwargs: Any) -> LabReplayReport:
        return LabReplayReport(
            task_id="task-1",
            task_type="writing.creative",
            experiment_id="exp-replay",
            original_winning_strategy="tier_top_low_temp",
            replay_winning_strategy="tier_top_low_temp",
            matches_original=True,
            winning_output_preview="ok",
            total_cost_usd=0.04,
        )

    with (
        patch("kun.lab.EnsembleExecutor", return_value=object()),
        patch("kun.lab.make_default_adapter", return_value=object()),
        patch("kun.lab.replay_historical_task", side_effect=fake_replay),
    ):
        result = CliRunner().invoke(
            app,
            [
                "lab",
                "benchmark",
                "replay",
                "--task-id",
                "task-1",
                "--enable",
                "--paths",
                "2",
            ],
        )

    assert result.exit_code == 0
    assert "lab replay" in result.output
    assert "tier_top_low_temp" in result.output
    assert "yes" in result.output


def test_keyword_scorer_is_used_by_real_executor_path() -> None:
    from kun.lab.benchmark import _keyword_scorer

    async def _run() -> None:
        score = await _keyword_scorer(["AI", "会议"])("这是 AI 会议工具", "prompt")
        assert score == 1.0

    import asyncio

    with contextlib.suppress(RuntimeError):
        asyncio.run(_run())
