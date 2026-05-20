"""外部 agent benchmark 引擎。

用于傩判断内部/外部 agent 在一组标准任务上的表现。当前实现只做纯工程
评分：精确匹配、Python 代码可编译、关键词 rubric 分，不调 LLM。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

ExpectedKind = Literal["code_compile", "exact_match", "rubric_score"]
AgentInvoke = Callable[[str], Awaitable[str]]
CostEstimator = Callable[["BenchmarkTask", str], float]


@dataclass(frozen=True)
class BenchmarkTask:
    task_id: str
    task_type: str
    prompt: str
    expected_kind: ExpectedKind
    expected: Any


@dataclass(frozen=True)
class AgentBenchmarkResult:
    agent_ref: str
    task_id: str
    task_type: str
    success: bool
    score: float
    cost_usd: float
    duration_sec: float


async def run_benchmark(
    *,
    agent_invoke: AgentInvoke,
    tasks: list[BenchmarkTask],
    agent_ref: str = "agent",
    cost_estimator: CostEstimator | None = None,
) -> list[AgentBenchmarkResult]:
    """按顺序跑一组 benchmark 任务。"""
    estimator = cost_estimator or _default_cost_estimator
    results: list[AgentBenchmarkResult] = []
    for task in tasks:
        started = time.perf_counter()
        output = ""
        try:
            output = await agent_invoke(task.prompt)
            score = score_output(output=output, task=task)
            success = _success_from_score(score=score, task=task)
        except Exception:
            score = 0.0
            success = False
        cost_usd = _estimate_cost(estimator=estimator, task=task, response=output)
        results.append(
            AgentBenchmarkResult(
                agent_ref=agent_ref,
                task_id=task.task_id,
                task_type=task.task_type,
                success=success,
                score=score,
                cost_usd=cost_usd,
                duration_sec=time.perf_counter() - started,
            )
        )
    return results


def score_output(*, output: str, task: BenchmarkTask) -> float:
    if task.expected_kind == "exact_match":
        return 1.0 if output.strip() == str(task.expected).strip() else 0.0
    if task.expected_kind == "code_compile":
        return _score_code_compile(output)
    if task.expected_kind == "rubric_score":
        return _score_rubric(output=output, expected=task.expected)
    raise ValueError(f"unsupported benchmark expected_kind: {task.expected_kind}")


def aggregate_results(results: list[AgentBenchmarkResult]) -> dict[str, float]:
    """给 NUO 面板用的简单汇总。"""
    if not results:
        return {"task_count": 0.0, "success_rate": 0.0, "avg_score": 0.0, "cost_usd": 0.0}
    return {
        "task_count": float(len(results)),
        "success_rate": sum(1 for r in results if r.success) / len(results),
        "avg_score": sum(r.score for r in results) / len(results),
        "cost_usd": sum(r.cost_usd for r in results),
    }


def sample_benchmark_tasks() -> list[BenchmarkTask]:
    """给 API smoke test 和冷启动用的 5 个示例任务。"""
    return [
        BenchmarkTask(
            task_id="exact-hello",
            task_type="exact_match",
            prompt="Return exactly: hello",
            expected_kind="exact_match",
            expected="hello",
        ),
        BenchmarkTask(
            task_id="python-compile",
            task_type="code.python",
            prompt="Return a Python function named add(a, b).",
            expected_kind="code_compile",
            expected=None,
        ),
        BenchmarkTask(
            task_id="rubric-tenant",
            task_type="security.review",
            prompt="Explain why tenant isolation matters.",
            expected_kind="rubric_score",
            expected={"signals": ["tenant", "isolation"], "min_score": 0.5},
        ),
        BenchmarkTask(
            task_id="rubric-cost",
            task_type="cost.review",
            prompt="Explain why budget tracking matters.",
            expected_kind="rubric_score",
            expected={"signals": ["budget", "cost"], "min_score": 0.5},
        ),
        BenchmarkTask(
            task_id="exact-json",
            task_type="format.json",
            prompt='Return exactly: {"ok":true}',
            expected_kind="exact_match",
            expected='{"ok":true}',
        ),
    ]


def _success_from_score(*, score: float, task: BenchmarkTask) -> bool:
    if task.expected_kind == "rubric_score" and isinstance(task.expected, dict):
        threshold = float(task.expected.get("min_score", 0.7))
        return score >= threshold
    return score >= 1.0


def _default_cost_estimator(task: BenchmarkTask, response: str) -> float:
    in_tokens = len(task.prompt) // 4
    out_tokens = len(response) // 4
    return (in_tokens * 0.001 + out_tokens * 0.005) / 1000


def _estimate_cost(
    *,
    estimator: CostEstimator,
    task: BenchmarkTask,
    response: str,
) -> float:
    try:
        return max(0.0, estimator(task, response))
    except Exception:
        return 0.0


def _score_code_compile(output: str) -> float:
    try:
        compile(output, "<agent-output>", "exec")
    except SyntaxError:
        return 0.0
    return 1.0


def _score_rubric(*, output: str, expected: Any) -> float:
    if not isinstance(expected, dict):
        return 0.0
    signals = [str(s).lower() for s in expected.get("signals", [])]
    if not signals:
        return 0.0
    lowered = output.lower()
    matched = sum(1 for signal in signals if signal in lowered)
    return matched / len(signals)


__all__ = [
    "AgentBenchmarkResult",
    "AgentInvoke",
    "BenchmarkTask",
    "CostEstimator",
    "aggregate_results",
    "run_benchmark",
    "sample_benchmark_tasks",
    "score_output",
]
