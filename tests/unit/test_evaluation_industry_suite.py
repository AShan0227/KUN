"""L6.B — IndustryEvalSuite 单测."""

from __future__ import annotations

from typing import Any

import pytest
from kun.evaluation.industry_suite import (
    PASS_THRESHOLD,
    EvalReport,
    EvalResult,
    GoldenTask,
    IndustryEvalSuite,
    builtin_acceptance_metrics,
)


def _task(
    *,
    task_id: str,
    industry: str = "电商",
    target_platform: str = "shopify",
    operation: str = "create_product",
    golden_input: dict | None = None,
    golden_output: dict | None = None,
    metric_name: str = "exact_key_match",
) -> GoldenTask:
    return GoldenTask(
        task_id=task_id,
        industry=industry,
        target_platform=target_platform,
        operation=operation,
        description=f"Test task {task_id}",
        golden_input=golden_input or {"title": "P1"},
        golden_output=golden_output or {"status": "ok", "product_id": "prod-1"},
        metric_name=metric_name,
    )


# ---- Built-in metrics ----


def test_exact_key_match_full() -> None:
    m = builtin_acceptance_metrics()["exact_key_match"]
    score = m({"status": "ok", "id": "1"}, {"status": "ok", "id": "1"})
    assert score == 1.0


def test_exact_key_match_partial() -> None:
    m = builtin_acceptance_metrics()["exact_key_match"]
    # golden 有 2 key, actual 命中 1 个
    score = m({"status": "ok"}, {"status": "ok", "id": "1"})
    assert score == 0.5


def test_exact_key_match_empty_golden() -> None:
    m = builtin_acceptance_metrics()["exact_key_match"]
    assert m({"anything": "x"}, {}) == 1.0


def test_status_ok() -> None:
    m = builtin_acceptance_metrics()["status_ok"]
    assert m({"status": "ok"}, {}) == 1.0
    assert m({"status": "success"}, {}) == 1.0
    assert m({"status": "failed"}, {}) == 0.0
    assert m({"no_status": "x"}, {}) == 0.0


def test_keys_present() -> None:
    m = builtin_acceptance_metrics()["keys_present"]
    # golden 期望 2 key, actual 有 2 个 (value 不同也算)
    score = m({"a": "X", "b": "Y"}, {"a": "1", "b": "2"})
    assert score == 1.0
    # 只 1 个 key 在
    score = m({"a": "X"}, {"a": "1", "b": "2"})
    assert score == 0.5


def test_jaccard_payload() -> None:
    m = builtin_acceptance_metrics()["jaccard_payload"]
    # 完全相同
    assert m({"a": 1, "b": 2}, {"a": 1, "b": 2}) == 1.0
    # 部分重叠
    score = m({"a": 1, "c": 3}, {"a": 1, "b": 2})
    # actual: {a=1, c=3}, golden: {a=1, b=2}
    # intersection: {a=1}, union: {a=1, c=3, b=2}
    assert score == pytest.approx(1 / 3)


def test_jaccard_both_empty() -> None:
    m = builtin_acceptance_metrics()["jaccard_payload"]
    assert m({}, {}) == 1.0


# ---- IndustryEvalSuite ----


@pytest.mark.asyncio
async def test_suite_runs_all_passing() -> None:
    tasks = [
        _task(task_id="t1"),
        _task(task_id="t2", golden_output={"status": "ok", "product_id": "prod-2"}),
    ]
    suite = IndustryEvalSuite(industry="电商", tasks=tasks)

    async def executor(inp: dict) -> dict:
        # 完美返回 golden output
        return {"status": "ok", "product_id": "prod-1"}

    report = await suite.run(executor)
    assert isinstance(report, EvalReport)
    assert report.total_tasks == 2
    # t1 全匹配 → pass, t2 部分匹配 (score 0.5 < 0.8) → fail
    assert report.passed_tasks == 1
    assert report.failed_tasks == 1
    assert report.error_tasks == 0


@pytest.mark.asyncio
async def test_suite_status_ok_metric() -> None:
    tasks = [
        _task(task_id="t1", metric_name="status_ok"),
        _task(task_id="t2", metric_name="status_ok"),
    ]
    suite = IndustryEvalSuite(industry="电商", tasks=tasks)

    async def executor(inp: dict) -> dict:
        return {"status": "ok"}

    report = await suite.run(executor)
    assert report.passed_tasks == 2
    assert report.pass_rate == 1.0


@pytest.mark.asyncio
async def test_suite_executor_exception_recorded_as_error() -> None:
    tasks = [_task(task_id="t1"), _task(task_id="t2")]
    suite = IndustryEvalSuite(industry="电商", tasks=tasks)

    async def bad_executor(inp: dict) -> dict:
        raise RuntimeError("API down")

    report = await suite.run(bad_executor)
    assert report.passed_tasks == 0
    assert report.error_tasks == 2
    for r in report.results:
        assert r.error == "API down"


@pytest.mark.asyncio
async def test_suite_unknown_metric_always_fails() -> None:
    tasks = [_task(task_id="t1", metric_name="non_existent_metric")]
    suite = IndustryEvalSuite(industry="电商", tasks=tasks)

    async def executor(inp: dict) -> dict:
        return {"status": "ok", "product_id": "prod-1"}

    report = await suite.run(executor)
    assert report.passed_tasks == 0


@pytest.mark.asyncio
async def test_suite_custom_metric() -> None:
    """注入自定义 metric, 比 built-in 优先."""

    def my_custom_metric(actual: dict[str, Any], golden: dict[str, Any]) -> float:
        return 1.0 if actual.get("magic") == "yes" else 0.0

    tasks = [
        GoldenTask(
            task_id="t1",
            industry="电商",
            target_platform="shopify",
            operation="x",
            description="custom test",
            golden_input={},
            golden_output={},
            metric_name="my_magic",
        )
    ]
    suite = IndustryEvalSuite(
        industry="电商",
        tasks=tasks,
        custom_metrics={"my_magic": my_custom_metric},
    )

    async def executor(inp: dict) -> dict:
        return {"magic": "yes"}

    report = await suite.run(executor)
    assert report.passed_tasks == 1


@pytest.mark.asyncio
async def test_suite_by_platform_aggregation() -> None:
    tasks = [
        _task(task_id="t1", target_platform="shopify"),
        _task(task_id="t2", target_platform="shopify"),
        _task(task_id="t3", target_platform="meta_ads"),
    ]
    suite = IndustryEvalSuite(industry="电商", tasks=tasks)

    async def executor(inp: dict) -> dict:
        return {"status": "ok", "product_id": "prod-1"}

    report = await suite.run(executor)
    assert "shopify" in report.by_platform
    assert "meta_ads" in report.by_platform
    assert report.by_platform["shopify"]["pass"] == 2
    assert report.by_platform["meta_ads"]["pass"] == 1


@pytest.mark.asyncio
async def test_suite_by_operation_aggregation() -> None:
    tasks = [
        _task(task_id="t1", operation="create_product"),
        _task(task_id="t2", operation="list_orders"),
    ]
    suite = IndustryEvalSuite(industry="电商", tasks=tasks)

    async def executor(inp: dict) -> dict:
        return {"status": "ok", "product_id": "prod-1"}

    report = await suite.run(executor)
    assert "create_product" in report.by_operation
    assert "list_orders" in report.by_operation


def test_suite_stats() -> None:
    tasks = [
        _task(task_id="t1", target_platform="shopify", operation="create_product"),
        _task(
            task_id="t2",
            target_platform="meta_ads",
            operation="create_ad",
            metric_name="status_ok",
        ),
    ]
    suite = IndustryEvalSuite(industry="电商", tasks=tasks)
    s = suite.stats()
    assert s["task_count"] == 2
    assert set(s["platforms"]) == {"shopify", "meta_ads"}
    assert set(s["operations"]) == {"create_product", "create_ad"}
    assert s["metrics_used"] == {"exact_key_match": 1, "status_ok": 1}


@pytest.mark.asyncio
async def test_pass_threshold_at_boundary() -> None:
    """score == 0.8 应 pass (>= threshold)."""

    def metric_exactly_at_threshold(a: dict[str, Any], g: dict[str, Any]) -> float:
        return PASS_THRESHOLD  # exactly at threshold

    tasks = [
        GoldenTask(
            task_id="t1",
            industry="电商",
            target_platform="shopify",
            operation="x",
            description="t",
            golden_input={},
            golden_output={},
            metric_name="at_threshold",
        )
    ]
    suite = IndustryEvalSuite(
        industry="电商",
        tasks=tasks,
        custom_metrics={"at_threshold": metric_exactly_at_threshold},
    )

    async def executor(inp: dict) -> dict:
        return {}

    report = await suite.run(executor)
    assert report.passed_tasks == 1


@pytest.mark.asyncio
async def test_eval_result_carries_rationale_and_latency() -> None:
    tasks = [_task(task_id="t1", metric_name="status_ok")]
    suite = IndustryEvalSuite(industry="电商", tasks=tasks)

    async def executor(inp: dict) -> dict:
        return {"status": "ok"}

    report = await suite.run(executor)
    r = report.results[0]
    assert isinstance(r, EvalResult)
    assert "score=" in r.rationale
    assert r.latency_ms >= 0.0
