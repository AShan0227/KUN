"""IndustryEvalSuite — 行业评测集 (ADR-011 §冷启动校准 / ADR-026).

数据模型 + 跑评测 + 报告. industry-agnostic — 每个行业填具体 GoldenTask.

GoldenTask:
  - target_platform (shopify / meta_ads / wechat_mp / salesforce)
  - operation (create_product / create_ad / publish_article / log_call)
  - golden_input — 给 KUN 的输入 (用户意图描述)
  - golden_output — 期望输出 (key fields + 期望 status)
  - acceptance_metric — 判定函数 (built-in 或 custom)

跑流程:
  IndustryEvalSuite(industry="电商", tasks=[...]).run(kun_executor)
    → 对每个 task 调 executor(task.golden_input)
    → 用 acceptance_metric 判定通过 / 失败
    → 汇总 EvalReport (pass_rate / per-platform / per-operation 分布)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.evaluation.industry_suite")


# Acceptance metric: 看 actual output 是否达标 (返回 0.0-1.0)
AcceptanceMetric = Callable[[dict[str, Any], dict[str, Any]], float]
"""(actual_output, golden_output) → score in [0, 1]. ≥ 0.8 视为通过."""


# Executor: 接收 input dict, 返回 actual output dict
TaskExecutor = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


PASS_THRESHOLD = 0.8


@dataclass(frozen=True)
class GoldenTask:
    """单条评测任务."""

    task_id: str
    industry: str  # 电商 / 投放 / 内容分发 / CRM
    target_platform: str
    operation: str
    description: str
    golden_input: dict[str, Any]
    golden_output: dict[str, Any]
    metric_name: str = "exact_key_match"
    """metric 名 — 从 builtin_acceptance_metrics 查或 caller 自带"""
    timeout_sec: float = 60.0
    tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class EvalResult:
    """单 task 的评测结果."""

    task_id: str
    passed: bool
    score: float
    actual_output: dict[str, Any]
    rationale: str = ""
    error: str | None = None
    latency_ms: float = 0.0


@dataclass(frozen=True)
class EvalReport:
    """全 suite 评测报告."""

    industry: str
    total_tasks: int
    passed_tasks: int
    failed_tasks: int
    error_tasks: int
    pass_rate: float
    avg_score: float
    by_platform: dict[str, dict[str, int]]  # platform → {pass, fail, error}
    by_operation: dict[str, dict[str, int]]  # operation → {pass, fail, error}
    results: list[EvalResult]
    started_at: datetime
    completed_at: datetime


# ---- Built-in acceptance metrics ----


def _metric_exact_key_match(actual: dict[str, Any], golden: dict[str, Any]) -> float:
    """golden 中每个 key 都在 actual 中匹配 → 1.0; 部分匹配按比例."""
    if not golden:
        return 1.0
    matches = sum(
        1 for k, v in golden.items() if k in actual and actual[k] == v
    )
    return matches / len(golden)


def _metric_status_ok(actual: dict[str, Any], golden: dict[str, Any]) -> float:
    """只看 actual.status == 'ok' / 'success'."""
    status = actual.get("status", "")
    if status in ("ok", "success", "done", "completed"):
        return 1.0
    return 0.0


def _metric_keys_present(actual: dict[str, Any], golden: dict[str, Any]) -> float:
    """golden 中每个 key 在 actual 中存在 (不比 value) → 计数."""
    if not golden:
        return 1.0
    present = sum(1 for k in golden if k in actual)
    return present / len(golden)


def _metric_jaccard_payload(actual: dict[str, Any], golden: dict[str, Any]) -> float:
    """对 payload field set 做 Jaccard 相似度 (key + str 化的 value)."""
    a = {f"{k}={v}" for k, v in actual.items()}
    g = {f"{k}={v}" for k, v in golden.items()}
    if not a and not g:
        return 1.0
    if not a or not g:
        return 0.0
    return len(a & g) / len(a | g)


def builtin_acceptance_metrics() -> dict[str, AcceptanceMetric]:
    """返回 built-in metrics 字典 — caller 用 metric_name 查."""
    return {
        "exact_key_match": _metric_exact_key_match,
        "status_ok": _metric_status_ok,
        "keys_present": _metric_keys_present,
        "jaccard_payload": _metric_jaccard_payload,
    }


# ---- Eval Suite ----


@dataclass
class IndustryEvalSuite:
    """单行业评测集. industry-agnostic — caller 填具体 task."""

    industry: str
    tasks: list[GoldenTask]
    custom_metrics: dict[str, AcceptanceMetric] = field(default_factory=dict)
    """超出 built-in 的自定义 metric (按 metric_name 注册)"""

    def _resolve_metric(self, name: str) -> AcceptanceMetric:
        if name in self.custom_metrics:
            return self.custom_metrics[name]
        built = builtin_acceptance_metrics()
        if name in built:
            return built[name]
        # 未知 metric → 永远不通过 (fail-safe)
        log.warning("eval.metric_not_found", metric=name)
        return lambda a, g: 0.0

    async def run(self, executor: TaskExecutor) -> EvalReport:
        """跑全 suite. executor(input) → actual_output."""
        import time

        results: list[EvalResult] = []
        by_platform_counts: dict[str, dict[str, int]] = defaultdict(
            lambda: {"pass": 0, "fail": 0, "error": 0}
        )
        by_op_counts: dict[str, dict[str, int]] = defaultdict(
            lambda: {"pass": 0, "fail": 0, "error": 0}
        )
        started = datetime.now(UTC)

        for task in self.tasks:
            t0 = time.perf_counter()
            metric = self._resolve_metric(task.metric_name)
            try:
                actual = await executor(task.golden_input)
                latency = (time.perf_counter() - t0) * 1000
                score = metric(actual, task.golden_output)
                passed = score >= PASS_THRESHOLD
                results.append(
                    EvalResult(
                        task_id=task.task_id,
                        passed=passed,
                        score=score,
                        actual_output=actual,
                        rationale=(
                            f"metric={task.metric_name} score={score:.2f}"
                            f" {'>=' if passed else '<'} {PASS_THRESHOLD}"
                        ),
                        latency_ms=latency,
                    )
                )
                bucket = "pass" if passed else "fail"
                by_platform_counts[task.target_platform][bucket] += 1
                by_op_counts[task.operation][bucket] += 1
            except Exception as e:
                latency = (time.perf_counter() - t0) * 1000
                log.warning(
                    "eval.executor_raised",
                    task_id=task.task_id,
                    error=str(e),
                )
                results.append(
                    EvalResult(
                        task_id=task.task_id,
                        passed=False,
                        score=0.0,
                        actual_output={},
                        rationale=f"executor raised: {e}",
                        error=str(e),
                        latency_ms=latency,
                    )
                )
                by_platform_counts[task.target_platform]["error"] += 1
                by_op_counts[task.operation]["error"] += 1

        completed = datetime.now(UTC)
        passed_count = sum(1 for r in results if r.passed)
        error_count = sum(1 for r in results if r.error is not None)
        failed_count = len(results) - passed_count - error_count
        total = len(results) if results else 1
        avg_score = sum(r.score for r in results) / total

        return EvalReport(
            industry=self.industry,
            total_tasks=len(self.tasks),
            passed_tasks=passed_count,
            failed_tasks=failed_count,
            error_tasks=error_count,
            pass_rate=passed_count / total,
            avg_score=avg_score,
            by_platform=dict(by_platform_counts),
            by_operation=dict(by_op_counts),
            results=results,
            started_at=started,
            completed_at=completed,
        )

    def stats(self) -> dict[str, Any]:
        return {
            "industry": self.industry,
            "task_count": len(self.tasks),
            "platforms": sorted({t.target_platform for t in self.tasks}),
            "operations": sorted({t.operation for t in self.tasks}),
            "metrics_used": dict(
                Counter(t.metric_name for t in self.tasks)
            ),
        }


__all__ = [
    "PASS_THRESHOLD",
    "AcceptanceMetric",
    "EvalReport",
    "EvalResult",
    "GoldenTask",
    "IndustryEvalSuite",
    "TaskExecutor",
    "builtin_acceptance_metrics",
]
