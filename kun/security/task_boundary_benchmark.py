"""TaskBoundaryGuard benchmark runner (BATCH9 C33).

The runner accepts an OffTopicEval-compatible JSONL dataset and reports whether
TaskBoundaryGuard rejects out-of-scope tasks while keeping in-scope tasks usable.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from kun.core.metrics import task_boundary_reject_rate
from kun.security.task_boundary_guard import ScopeConfig, TaskBoundaryGuard


class BoundaryBenchmarkCase(BaseModel):
    """One OffTopicEval-compatible task-boundary benchmark case."""

    case_id: str
    category: str = "off_topic"
    task_meta: dict[str, Any]
    scope: ScopeConfig | None = None
    expected_in_scope: bool


class BoundaryCaseResult(BaseModel):
    case_id: str
    category: str
    expected_in_scope: bool
    actual_in_scope: bool
    boundary_score: float
    reason: str
    matched_pattern: str = ""
    suggested_redirect: str = ""

    @property
    def passed(self) -> bool:
        return self.expected_in_scope == self.actual_in_scope


class BenchmarkReport(BaseModel):
    dataset: str
    total: int
    pass_count: int
    fail_count: int
    reject_count: int
    false_accept_count: int
    false_reject_count: int
    reject_rate: float = Field(ge=0.0, le=1.0)
    off_topic_reject_rate: float = Field(ge=0.0, le=1.0)
    accuracy: float = Field(ge=0.0, le=1.0)
    by_category: dict[str, dict[str, int]]
    results: list[BoundaryCaseResult]


@dataclass(frozen=True)
class DatasetBundle:
    name: str
    cases: list[BoundaryBenchmarkCase]


def load_default_dataset() -> DatasetBundle:
    """Load the bundled smoke dataset.

    Production/offline jobs can pass the full upstream OffTopicEval JSONL file to
    ``load_dataset``. The bundled file is intentionally small so unit tests and
    local CI stay fast.
    """
    fixture = resources.files("kun.security.fixtures").joinpath("offtopic_eval_smoke.jsonl")
    return DatasetBundle(name="offtopic_eval_smoke", cases=load_dataset(Path(str(fixture))))


def load_dataset(path: Path) -> list[BoundaryBenchmarkCase]:
    """Load a JSONL or JSON list dataset."""
    raw = path.read_text()
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        parsed = json.loads(raw)
        if isinstance(parsed, dict) and isinstance(parsed.get("cases"), list):
            rows = parsed["cases"]
        elif isinstance(parsed, list):
            rows = parsed
        else:
            raise ValueError("dataset JSON must be a list or {'cases': [...]}")
    return [_coerce_case(row) for row in rows]


async def run_benchmark(
    guard: TaskBoundaryGuard,
    dataset: Iterable[BoundaryBenchmarkCase],
    *,
    dataset_name: str = "offtopic_eval",
) -> BenchmarkReport:
    results: list[BoundaryCaseResult] = []
    by_category: dict[str, Counter[str]] = {}

    for case in dataset:
        decision = await guard.check(case.task_meta, scope=case.scope)
        result = BoundaryCaseResult(
            case_id=case.case_id,
            category=case.category,
            expected_in_scope=case.expected_in_scope,
            actual_in_scope=decision.in_scope,
            boundary_score=decision.boundary_score,
            reason=decision.reason,
            matched_pattern=decision.matched_pattern,
            suggested_redirect=decision.suggested_redirect,
        )
        results.append(result)
        bucket = by_category.setdefault(case.category, Counter())
        bucket["total"] += 1
        bucket["pass" if result.passed else "fail"] += 1
        if decision.in_scope:
            bucket["accept"] += 1
        else:
            bucket["reject"] += 1

    total = len(results)
    pass_count = sum(1 for result in results if result.passed)
    reject_count = sum(1 for result in results if not result.actual_in_scope)
    false_accept_count = sum(
        1
        for result in results
        if result.expected_in_scope is False and result.actual_in_scope is True
    )
    false_reject_count = sum(
        1
        for result in results
        if result.expected_in_scope is True and result.actual_in_scope is False
    )
    off_topic_total = sum(1 for result in results if result.expected_in_scope is False)
    off_topic_rejects = sum(
        1
        for result in results
        if result.expected_in_scope is False and result.actual_in_scope is False
    )
    reject_rate = reject_count / total if total else 0.0
    off_topic_reject_rate = off_topic_rejects / off_topic_total if off_topic_total else 0.0
    accuracy = pass_count / total if total else 0.0

    with suppress(Exception):
        task_boundary_reject_rate.labels(dataset=dataset_name).set(reject_rate)

    return BenchmarkReport(
        dataset=dataset_name,
        total=total,
        pass_count=pass_count,
        fail_count=total - pass_count,
        reject_count=reject_count,
        false_accept_count=false_accept_count,
        false_reject_count=false_reject_count,
        reject_rate=reject_rate,
        off_topic_reject_rate=off_topic_reject_rate,
        accuracy=accuracy,
        by_category={key: dict(counter) for key, counter in by_category.items()},
        results=results,
    )


def _coerce_case(row: dict[str, Any]) -> BoundaryBenchmarkCase:
    scope_raw = row.get("scope")
    scope = ScopeConfig.model_validate(scope_raw) if isinstance(scope_raw, dict) else None
    return BoundaryBenchmarkCase(
        case_id=str(row["case_id"]),
        category=str(row.get("category") or "off_topic"),
        task_meta=dict(row.get("task_meta") or {}),
        scope=scope,
        expected_in_scope=bool(row["expected_in_scope"]),
    )


__all__ = [
    "BenchmarkReport",
    "BoundaryBenchmarkCase",
    "BoundaryCaseResult",
    "DatasetBundle",
    "load_dataset",
    "load_default_dataset",
    "run_benchmark",
]
