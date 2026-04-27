"""TaskBoundaryGuard benchmark tests (BATCH9 C33)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from kun.cli import app
from kun.core.metrics import task_boundary_reject_rate
from kun.engineering.idle_batch import TaskBoundaryEvalStep, list_steps
from kun.security.task_boundary_benchmark import (
    BoundaryBenchmarkCase,
    load_dataset,
    load_default_dataset,
    run_benchmark,
)
from kun.security.task_boundary_guard import ScopeConfig, TaskBoundaryGuard
from typer.testing import CliRunner


def _gauge_value(dataset: str) -> float:
    for metric in task_boundary_reject_rate.collect():
        for sample in metric.samples:
            if (
                sample.name == "kun_task_boundary_reject_rate"
                and sample.labels.get("dataset") == dataset
            ):
                return float(sample.value)
    return 0.0


def test_default_dataset_loads() -> None:
    bundle = load_default_dataset()

    assert bundle.name == "offtopic_eval_smoke"
    assert len(bundle.cases) >= 8
    assert any(case.expected_in_scope is False for case in bundle.cases)
    assert any(case.expected_in_scope is True for case in bundle.cases)


def test_load_dataset_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "boundary.jsonl"
    path.write_text(
        json.dumps(
            {
                "case_id": "x",
                "category": "off_topic",
                "task_meta": {"task_type": "coding.python"},
                "scope": {
                    "role_id": "marketing",
                    "allowed_task_types": ["marketing.*"],
                    "forbidden_task_types": ["coding.*"],
                },
                "expected_in_scope": False,
            }
        )
        + "\n"
    )

    cases = load_dataset(path)

    assert len(cases) == 1
    assert cases[0].scope is not None
    assert cases[0].scope.role_id == "marketing"


@pytest.mark.asyncio
async def test_run_benchmark_reports_accuracy_and_rates() -> None:
    scope = ScopeConfig(
        role_id="marketing",
        role_name="marketing copywriter",
        allowed_task_types=["marketing.*"],
        forbidden_task_types=["coding.*"],
    )
    cases = [
        BoundaryBenchmarkCase(
            case_id="in",
            category="in_scope",
            task_meta={"task_type": "marketing.ad"},
            scope=scope,
            expected_in_scope=True,
        ),
        BoundaryBenchmarkCase(
            case_id="out",
            category="off_topic",
            task_meta={"task_type": "coding.python"},
            scope=scope,
            expected_in_scope=False,
        ),
    ]

    report = await run_benchmark(TaskBoundaryGuard(), cases, dataset_name="unit_boundary")

    assert report.total == 2
    assert report.pass_count == 2
    assert report.false_accept_count == 0
    assert report.false_reject_count == 0
    assert report.reject_rate == 0.5
    assert report.off_topic_reject_rate == 1.0
    assert _gauge_value("unit_boundary") == 0.5


@pytest.mark.asyncio
async def test_run_benchmark_counts_false_accept() -> None:
    scope = ScopeConfig(role_id="open", role_name="general")
    cases = [
        BoundaryBenchmarkCase(
            case_id="miss",
            category="off_topic",
            task_meta={"task_type": "coding.python"},
            scope=scope,
            expected_in_scope=False,
        )
    ]

    report = await run_benchmark(TaskBoundaryGuard(), cases, dataset_name="false_accept_unit")

    assert report.false_accept_count == 1
    assert report.fail_count == 1


@pytest.mark.asyncio
async def test_task_boundary_eval_step_runs_default_dataset() -> None:
    summary = await TaskBoundaryEvalStep().run("u-test")

    assert summary["dataset"] == "offtopic_eval_smoke"
    assert summary["total"] >= 8
    assert 0.0 <= summary["reject_rate"] <= 1.0
    assert "results" not in summary


def test_task_boundary_eval_step_registered() -> None:
    assert "task_boundary_eval" in list_steps()


def test_security_task_boundary_benchmark_cli_smoke() -> None:
    result = CliRunner().invoke(app, ["security", "task-boundary-benchmark"])

    assert result.exit_code == 0
    assert "task-boundary benchmark" in result.stdout
    assert "offtopic_eval_smoke" in result.stdout


def test_security_task_boundary_benchmark_cli_json() -> None:
    result = CliRunner().invoke(app, ["security", "task-boundary-benchmark", "--json"])

    assert result.exit_code == 0
    assert '"dataset": "offtopic_eval_smoke"' in result.stdout
