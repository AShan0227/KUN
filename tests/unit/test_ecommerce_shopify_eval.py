"""L6.D-EcomEval — 电商 Shopify 评测集 + Adapter Router e2e 单测."""

from __future__ import annotations

from typing import Any

import pytest
from kun.evaluation import (
    build_shopify_eval_suite,
    build_shopify_golden_tasks,
)
from kun.interface.automation import (
    AdapterRegistry,
    AdapterRouter,
    make_action,
)
from kun.interface.automation.shopify import (
    ShopifyAPIAdapter,
    ShopifyBrowserAdapter,
)

# ---- Fakes ----


async def fake_shopify_http(method: str, url: str, body: dict | None) -> dict[str, Any]:
    """Mock Shopify API 各 endpoint 返回."""
    if "products.json" in url and method == "POST":
        # create_product
        return {
            "_status_code": 201,
            "product": {
                "id": 99999,
                "title": (body or {}).get("product", {}).get("title", ""),
                "vendor": (body or {}).get("product", {}).get("vendor", ""),
            },
        }
    if "products/" in url and method == "GET" and "json" in url:
        # get_product
        # 简单解析 product_id (URL 末段)
        import re
        m = re.search(r"products/(\d+)\.json", url)
        product_id = m.group(1) if m else "unknown"
        return {
            "_status_code": 200,
            "product": {"id": int(product_id), "title": "Existing Product"},
        }
    if "orders.json" in url and method == "GET":
        # list_orders
        return {
            "_status_code": 200,
            "orders": [{"id": 1, "name": "#1001"}, {"id": 2, "name": "#1002"}],
        }
    if "shop.json" in url:
        # health check
        return {"_status_code": 200, "shop": {"id": 1}}
    return {"_status_code": 404, "error": "endpoint not found"}


class FakePage:
    def __init__(self) -> None:
        self.closed = False

    async def goto(self, url: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        pass

    async def fill(self, selector: str, value: str) -> None:
        pass

    async def click(self, selector: str) -> None:
        pass

    async def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        pass

    async def screenshot(self, *, path: str | None = None) -> bytes:
        return b"png"

    async def close(self) -> None:
        self.closed = True


async def fake_page_factory() -> FakePage:
    return FakePage()


def _build_router_with_shopify() -> AdapterRouter:
    """注册 Shopify API + Browser, 返回 Router."""
    reg = AdapterRegistry()
    reg.register(
        ShopifyAPIAdapter(http_caller=fake_shopify_http, access_token="test-token")
    )
    reg.register(ShopifyBrowserAdapter(page_factory=fake_page_factory))
    return AdapterRouter(reg)


# ---- Golden tasks structure ----


def test_golden_tasks_count_is_6() -> None:
    tasks = build_shopify_golden_tasks()
    assert len(tasks) == 6


def test_golden_tasks_cover_3_operations() -> None:
    tasks = build_shopify_golden_tasks()
    ops = {t.operation for t in tasks}
    assert ops == {"create_product", "list_orders", "get_product"}


def test_golden_tasks_industry_is_ecommerce() -> None:
    tasks = build_shopify_golden_tasks()
    assert all(t.industry == "电商" for t in tasks)
    assert all(t.target_platform == "shopify" for t in tasks)


def test_golden_tasks_each_has_input_output() -> None:
    for t in build_shopify_golden_tasks():
        assert "operation" in t.golden_input
        assert "payload" in t.golden_input
        # golden_output may be {} for edge cases — 不强制非空


def test_eval_suite_stats() -> None:
    suite = build_shopify_eval_suite()
    s = suite.stats()
    assert s["task_count"] == 6
    assert s["industry"] == "电商"
    assert s["platforms"] == ["shopify"]


# ---- End-to-end: Suite + Router 跑 6 tasks ----


@pytest.mark.asyncio
async def test_e2e_suite_runs_through_router() -> None:
    """Suite 把 task 喂 executor — executor 是 Router wrap, 调 Shopify adapter."""
    suite = build_shopify_eval_suite()
    router = _build_router_with_shopify()

    async def executor(inp: dict[str, Any]) -> dict[str, Any]:
        action = make_action(
            target_platform=inp["target_platform"],
            operation=inp["operation"],
            payload=inp["payload"],
            requested_kind="api",  # eval golden output 是 API shape, 显式用 API
        )
        decision = await router.route_and_execute(action)
        if decision.result is None:
            return {"error": "no result"}
        return decision.result.result_payload

    report = await suite.run(executor)
    # 6 task 都跑完
    assert report.total_tasks == 6
    # 应该有大多数通过 (keys_present + exact_key_match 比较宽松)
    assert report.passed_tasks >= 5
    assert report.error_tasks == 0


@pytest.mark.asyncio
async def test_e2e_create_product_succeeds_with_api() -> None:
    """warmup API 后, create_product 走 API 真返回 product."""
    router = _build_router_with_shopify()
    # warmup API 让 score > 0.7
    for _ in range(20):
        await router.route_and_execute(
            make_action(
                target_platform="shopify",
                operation="create_product",
                payload={"title": "Warmup"},
                requested_kind="api",
            )
        )
    decision = await router.route_and_execute(
        make_action(
            target_platform="shopify",
            operation="create_product",
            payload={"title": "Real Product", "vendor": "ACME"},
        )
    )
    assert decision.result is not None
    assert decision.result.kind_used == "api"
    assert decision.result.status == "ok"
    assert decision.result.result_payload["product"]["title"] == "Real Product"


@pytest.mark.asyncio
async def test_e2e_list_orders_returns_orders() -> None:
    router = _build_router_with_shopify()
    decision = await router.route_and_execute(
        make_action(
            target_platform="shopify",
            operation="list_orders",
            payload={},
            requested_kind="api",
        )
    )
    assert decision.result is not None
    assert decision.result.status == "ok"
    assert len(decision.result.result_payload["orders"]) == 2


@pytest.mark.asyncio
async def test_e2e_get_product_substitutes_id_through_router() -> None:
    router = _build_router_with_shopify()
    decision = await router.route_and_execute(
        make_action(
            target_platform="shopify",
            operation="get_product",
            payload={"product_id": "42"},
            requested_kind="api",
        )
    )
    assert decision.result is not None
    assert decision.result.status == "ok"
    assert decision.result.result_payload["product"]["id"] == 42


@pytest.mark.asyncio
async def test_e2e_pass_rate_above_threshold() -> None:
    """完整 6 task 评测 — pass_rate ≥ 0.8 视为冷启动 OK."""
    suite = build_shopify_eval_suite()
    router = _build_router_with_shopify()

    async def executor(inp: dict[str, Any]) -> dict[str, Any]:
        action = make_action(
            target_platform=inp["target_platform"],
            operation=inp["operation"],
            payload=inp["payload"],
            requested_kind="api",  # eval golden output 是 API shape
        )
        decision = await router.route_and_execute(action)
        return decision.result.result_payload if decision.result else {}

    report = await suite.run(executor)
    # 冷启动校准合格阈值
    assert report.pass_rate >= 0.8, (
        f"pass_rate={report.pass_rate} < 0.8, results: "
        f"{[(r.task_id, r.passed, r.score) for r in report.results]}"
    )


@pytest.mark.asyncio
async def test_e2e_per_operation_breakdown() -> None:
    """报告应该有 per operation 统计."""
    suite = build_shopify_eval_suite()
    router = _build_router_with_shopify()

    async def executor(inp: dict[str, Any]) -> dict[str, Any]:
        action = make_action(
            target_platform=inp["target_platform"],
            operation=inp["operation"],
            payload=inp["payload"],
            requested_kind="api",
        )
        decision = await router.route_and_execute(action)
        return decision.result.result_payload if decision.result else {}

    report = await suite.run(executor)
    # 应有 3 个 operation bucket
    assert "create_product" in report.by_operation
    assert "list_orders" in report.by_operation
    assert "get_product" in report.by_operation
