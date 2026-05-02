"""KUN-Lab benchmark suites (BATCH10 C40)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field

from kun.lab.ensemble_executor import EnsembleConfig, EnsembleExecutor, EnsembleResult
from kun.lab.experiment_log import ExperimentLog


class LabBenchmarkItem(BaseModel):
    item_id: str
    prompt: str
    task_type: str = "kun_lab.benchmark"
    expected_keywords: list[str] = Field(default_factory=list)


class LabBenchmarkDataset(BaseModel):
    name: str
    description: str = ""
    items: list[LabBenchmarkItem]


class StrategyWinRate(BaseModel):
    strategy: str
    wins: int = 0
    total: int = 0
    win_rate: float = 0.0
    avg_score: float = 0.0
    avg_cost_usd: float = 0.0


class LabBenchmarkReport(BaseModel):
    dataset: str
    total_items: int
    experiments: int
    total_cost_usd: float
    strategy_stats: list[StrategyWinRate]


class HistoricalTaskReplayTarget(BaseModel):
    """A production task converted into a lab replay target."""

    task_id: str
    tenant_id: str = "u-sylvan"
    task_type: str = "kun_lab.replay"
    prompt: str
    original_answer: str = ""
    original_winning_strategy: str | None = None
    result_metadata: dict[str, Any] = Field(default_factory=dict)


class LabReplayReport(BaseModel):
    """Replay result for one historical task."""

    task_id: str
    task_type: str
    experiment_id: str
    original_winning_strategy: str | None = None
    replay_winning_strategy: str = ""
    matches_original: bool | None = None
    winning_output_preview: str = ""
    total_cost_usd: float = 0.0


@dataclass(frozen=True)
class BenchmarkRunOptions:
    limit: int | None = None
    paths: int = 5
    selection_method: Literal["best_score", "majority_vote", "judge_picks"] = "best_score"
    cost_budget_total_usd: float = 1.0


def benchmarks_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "lab_benchmarks"


def dataset_path(name_or_path: str | Path) -> Path:
    raw = Path(name_or_path)
    if raw.exists():
        return raw
    name = str(name_or_path).replace("suite/", "").removesuffix(".yaml")
    candidate = benchmarks_dir() / f"{name}.yaml"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"benchmark dataset not found: {name_or_path}")


def load_benchmark_dataset(name_or_path: str | Path) -> LabBenchmarkDataset:
    path = dataset_path(name_or_path)
    data = yaml.safe_load(path.read_text()) or {}
    return LabBenchmarkDataset.model_validate(data)


def list_benchmark_datasets() -> list[str]:
    root = benchmarks_dir()
    if not root.exists():
        return []
    return sorted(path.stem for path in root.glob("*.yaml"))


async def run_benchmark_suite(
    dataset: LabBenchmarkDataset,
    *,
    executor: EnsembleExecutor,
    experiment_log: ExperimentLog | None = None,
    options: BenchmarkRunOptions | None = None,
) -> LabBenchmarkReport:
    opts = options or BenchmarkRunOptions()
    items = dataset.items[: opts.limit] if opts.limit else list(dataset.items)
    results: list[EnsembleResult] = []
    for item in items:
        config = EnsembleConfig(
            n_paths=opts.paths,
            selection_method=opts.selection_method,
            cost_budget_total_usd=opts.cost_budget_total_usd,
            metadata={
                "benchmark_dataset": dataset.name,
                "benchmark_item_id": item.item_id,
                "prompt": item.prompt,
            },
        )
        result = await executor.run(
            item.prompt,
            config=config,
            scoring_fn=_keyword_scorer(item.expected_keywords),
            task_type=f"lab_benchmark.{dataset.name}",
        )
        results.append(result)
        if experiment_log is not None:
            experiment_log.record(
                task_type=f"lab_benchmark.{dataset.name}",
                ensemble_result=result,
                notes=f"benchmark_item_id={item.item_id}",
            )

    report = benchmark_report_from_results(dataset.name, results)
    _emit_benchmark_metrics(report)
    return report


async def load_historical_task_for_replay(
    task_id: str,
    *,
    tenant_id: str = "u-sylvan",
) -> HistoricalTaskReplayTarget:
    """Load a persisted task/result pair and turn it into a replay target."""

    from sqlalchemy import select

    from kun.core.db import session_scope
    from kun.core.orm import TaskResultRow, TaskRow

    async with session_scope(tenant_id=tenant_id) as session:
        task = (
            await session.execute(
                select(TaskRow).where(
                    TaskRow.tenant_id == tenant_id,
                    TaskRow.task_id == task_id,
                )
            )
        ).scalar_one_or_none()
        if task is None:
            raise LookupError(f"task not found for replay: {tenant_id}/{task_id}")
        result = (
            await session.execute(
                select(TaskResultRow).where(
                    TaskResultRow.tenant_id == tenant_id,
                    TaskResultRow.task_id == task_id,
                )
            )
        ).scalar_one_or_none()

    result_json = dict(result.result_json or {}) if result is not None else {}
    answer = str(result.answer or "") if result is not None else ""
    return HistoricalTaskReplayTarget(
        task_id=task.task_id,
        tenant_id=tenant_id,
        task_type=task.task_type,
        prompt=_prompt_from_task_row(task),
        original_answer=answer,
        original_winning_strategy=_extract_winning_strategy(result_json),
        result_metadata=result_json,
    )


async def replay_historical_task(
    task_id: str,
    *,
    tenant_id: str = "u-sylvan",
    executor: EnsembleExecutor,
    experiment_log: ExperimentLog | None = None,
    options: BenchmarkRunOptions | None = None,
    task_loader: Callable[..., Awaitable[HistoricalTaskReplayTarget]] | None = None,
) -> LabReplayReport:
    """Replay one historical task through lab ensemble and compare winners."""

    opts = options or BenchmarkRunOptions()
    loader = task_loader or load_historical_task_for_replay
    target = await loader(task_id, tenant_id=tenant_id)
    config = EnsembleConfig(
        n_paths=opts.paths,
        selection_method=opts.selection_method,
        cost_budget_total_usd=opts.cost_budget_total_usd,
        metadata={
            "replay_task_id": target.task_id,
            "replay_tenant_id": target.tenant_id,
            "original_winning_strategy": target.original_winning_strategy or "",
        },
    )
    result = await executor.run(
        target.prompt,
        config=config,
        task_type=f"lab_replay.{target.task_type}",
    )
    if experiment_log is not None:
        experiment_log.record(
            task_type=f"lab_replay.{target.task_type}",
            ensemble_result=result,
            notes=f"replay_task_id={target.task_id}",
        )
    replay_strategy = _winning_strategy(result)
    original = target.original_winning_strategy
    return LabReplayReport(
        task_id=target.task_id,
        task_type=target.task_type,
        experiment_id=result.experiment_id,
        original_winning_strategy=original,
        replay_winning_strategy=replay_strategy,
        matches_original=None if original is None else replay_strategy == original,
        winning_output_preview=result.winning_output[:240],
        total_cost_usd=result.total_cost_usd,
    )


def benchmark_report_from_results(
    dataset_name: str,
    results: list[EnsembleResult],
) -> LabBenchmarkReport:
    buckets: dict[str, dict[str, float]] = defaultdict(
        lambda: {"wins": 0, "total": 0, "score": 0.0, "cost": 0.0}
    )
    for result in results:
        for path_result in result.path_results:
            if path_result.error:
                continue
            strategy = str(path_result.config.get("strategy", "unknown"))
            bucket = buckets[strategy]
            bucket["total"] += 1
            bucket["score"] += path_result.score
            bucket["cost"] += path_result.cost_usd
            if path_result.path_idx == result.winning_path_idx:
                bucket["wins"] += 1

    stats: list[StrategyWinRate] = []
    for strategy, bucket in buckets.items():
        total = int(bucket["total"])
        wins = int(bucket["wins"])
        stats.append(
            StrategyWinRate(
                strategy=strategy,
                wins=wins,
                total=total,
                win_rate=wins / total if total else 0.0,
                avg_score=bucket["score"] / total if total else 0.0,
                avg_cost_usd=bucket["cost"] / total if total else 0.0,
            )
        )
    stats.sort(key=lambda item: (item.win_rate, item.avg_score), reverse=True)
    return LabBenchmarkReport(
        dataset=dataset_name,
        total_items=len(results),
        experiments=len(results),
        total_cost_usd=sum(result.total_cost_usd for result in results),
        strategy_stats=stats,
    )


def benchmark_report_from_log(log: ExperimentLog, dataset_name: str) -> LabBenchmarkReport:
    experiments = log.by_task_type(f"lab_benchmark.{dataset_name}")
    return benchmark_report_from_results(
        dataset_name,
        [experiment.ensemble_result for experiment in experiments],
    )


def _prompt_from_task_row(task: Any) -> str:
    spec = task.spec_json or {}
    if isinstance(spec, dict):
        for key in ("goal_detail", "original_message", "prompt", "message"):
            value = spec.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(task.success_criteria_short).strip()


def _winning_strategy(result: EnsembleResult) -> str:
    for path_result in result.path_results:
        if path_result.path_idx == result.winning_path_idx:
            return str(path_result.config.get("strategy") or "")
    return ""


def _extract_winning_strategy(payload: Any) -> str | None:
    """Best-effort extraction from persisted task_result JSON."""

    if isinstance(payload, dict):
        for key in ("winning_strategy", "winner_strategy", "selected_strategy"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for key in ("ensemble_result", "lab", "result", "metadata"):
            found = _extract_winning_strategy(payload.get(key))
            if found:
                return found
        path_results = payload.get("path_results")
        winner_idx = payload.get("winning_path_idx")
        if isinstance(path_results, list) and isinstance(winner_idx, int):
            for item in path_results:
                if not isinstance(item, dict):
                    continue
                if item.get("path_idx") == winner_idx:
                    config = item.get("config")
                    if isinstance(config, dict):
                        strategy = config.get("strategy")
                        if isinstance(strategy, str) and strategy:
                            return strategy
        for value in payload.values():
            found = _extract_winning_strategy(value)
            if found:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _extract_winning_strategy(item)
            if found:
                return found
    return None


def _keyword_scorer(expected_keywords: list[str]) -> Callable[[str, str], Awaitable[float]]:
    async def score(output: str, _prompt: str) -> float:
        if not expected_keywords:
            return 0.5
        lowered = output.lower()
        hits = sum(1 for keyword in expected_keywords if keyword.lower() in lowered)
        return hits / len(expected_keywords)

    return score


def _emit_benchmark_metrics(report: LabBenchmarkReport) -> None:
    try:
        from kun.core.metrics import lab_benchmark_run_total, lab_benchmark_winrate

        lab_benchmark_run_total.labels(dataset=report.dataset).inc()
        for stat in report.strategy_stats:
            lab_benchmark_winrate.labels(dataset=report.dataset, strategy=stat.strategy).set(
                stat.win_rate
            )
    except Exception:
        return


__all__ = [
    "BenchmarkRunOptions",
    "HistoricalTaskReplayTarget",
    "LabBenchmarkDataset",
    "LabBenchmarkItem",
    "LabBenchmarkReport",
    "LabReplayReport",
    "StrategyWinRate",
    "benchmark_report_from_log",
    "benchmark_report_from_results",
    "dataset_path",
    "list_benchmark_datasets",
    "load_benchmark_dataset",
    "load_historical_task_for_replay",
    "replay_historical_task",
    "run_benchmark_suite",
]
