"""KUN-Lab benchmark suites (BATCH10 C40)."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    "LabBenchmarkDataset",
    "LabBenchmarkItem",
    "LabBenchmarkReport",
    "StrategyWinRate",
    "benchmark_report_from_log",
    "benchmark_report_from_results",
    "dataset_path",
    "list_benchmark_datasets",
    "load_benchmark_dataset",
    "run_benchmark_suite",
]
