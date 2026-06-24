"""L6.A — Automation Adapter framework 单测."""

from __future__ import annotations

from typing import ClassVar

import pytest
from kun.interface.automation import (
    Action,
    ActionResult,
    AdapterRegistry,
    AdapterRouter,
    APIAdapter,
    BrowserAdapter,
    make_action,
)
from kun.interface.automation.api_base import HttpCaller
from kun.interface.automation.browser_base import BrowserPage

# ---- Test fakes ----


class FakeAPIShopify(APIAdapter):
    platform = "shopify"
    supported_operations: ClassVar[set[str]] = {"create_product", "list_orders"}

    async def _do_execute(self, action: Action, *, http_caller: HttpCaller) -> ActionResult:
        # 调 fake http caller, 返回成功
        resp = await http_caller("POST", f"/api/{action.operation}", action.payload)
        return ActionResult(
            action_id=action.action_id,
            status="ok",
            kind_used="api",
            result_payload=resp,
            latency_ms=0.0,
            rationale=f"shopify api {action.operation} succeeded",
        )


class FakeBrowserShopify(BrowserAdapter):
    platform = "shopify"
    supported_operations: ClassVar[set[str]] = {"create_product", "list_orders"}

    async def _do_execute(self, action: Action, *, page: BrowserPage) -> ActionResult:
        await page.goto(f"https://shopify.com/admin/{action.operation}")
        return ActionResult(
            action_id=action.action_id,
            status="ok",
            kind_used="browser",
            result_payload={"navigated": True, "operation": action.operation},
            latency_ms=0.0,
            rationale=f"shopify browser {action.operation} succeeded",
        )


class FakeFailingAPIShopify(APIAdapter):
    platform = "shopify"
    supported_operations: ClassVar[set[str]] = {"create_product"}

    async def _do_execute(self, action: Action, *, http_caller: HttpCaller) -> ActionResult:
        raise RuntimeError("API rate limited")


class FakePage:
    """Minimal BrowserPage fake for tests."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, ...]] = []
        self.closed = False

    async def goto(self, url: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        self.actions.append(("goto", url))

    async def fill(self, selector: str, value: str) -> None:
        self.actions.append(("fill", selector, value))

    async def click(self, selector: str) -> None:
        self.actions.append(("click", selector))

    async def wait_for_selector(
        self,
        selector: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> None:
        self.actions.append(("wait", selector))

    async def screenshot(self, *, path: str | None = None) -> bytes:
        self.actions.append(("screenshot",))
        return b"fake-png"

    async def close(self) -> None:
        self.closed = True


async def fake_http_ok(method: str, url: str, body: dict | None) -> dict:
    return {"method": method, "url": url, "received": body}


async def fake_page_factory():
    return FakePage()


# ---- make_action helper ----


def test_make_action_auto_id() -> None:
    a = make_action(target_platform="shopify", operation="list_orders", payload={"status": "open"})
    assert a.target_platform == "shopify"
    assert a.operation == "list_orders"
    assert a.action_id.startswith("act-")


def test_make_action_explicit_id() -> None:
    a = make_action(
        target_platform="shopify",
        operation="x",
        payload={},
        action_id="act-explicit",
    )
    assert a.action_id == "act-explicit"


# ---- AdapterRegistry ----


def test_registry_register_and_lookup() -> None:
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    reg.register(api)
    assert reg.has(platform="shopify", operation="create_product")
    assert reg.has(platform="shopify", operation="list_orders")
    cands = reg.candidates_for(platform="shopify", operation="create_product")
    assert len(cands) == 1
    assert cands[0] is api


def test_registry_supports_multiple_adapters_per_op() -> None:
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    browser = FakeBrowserShopify(page_factory=fake_page_factory)
    reg.register(api)
    reg.register(browser)
    cands = reg.candidates_for(platform="shopify", operation="create_product")
    assert len(cands) == 2
    kinds = {c.kind for c in cands}
    assert kinds == {"api", "browser"}


def test_registry_unregister() -> None:
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    reg.register(api)
    reg.unregister(api)
    assert not reg.has(platform="shopify", operation="create_product")


def test_registry_no_dup_on_repeated_register() -> None:
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    reg.register(api)
    reg.register(api)  # 二次 register
    cands = reg.candidates_for(platform="shopify", operation="create_product")
    assert len(cands) == 1


def test_registry_stats() -> None:
    reg = AdapterRegistry()
    reg.register(FakeAPIShopify(http_caller=fake_http_ok))
    reg.register(FakeBrowserShopify(page_factory=fake_page_factory))
    s = reg.stats()
    assert s["platforms"] == 1
    assert s["operations"] == 2  # create_product + list_orders


def test_registry_lookup_missing_returns_empty() -> None:
    reg = AdapterRegistry()
    assert reg.candidates_for(platform="missing", operation="x") == []
    assert reg.has(platform="missing", operation="x") is False


# ---- AdapterRouter ----


def test_router_select_returns_none_when_no_adapter() -> None:
    reg = AdapterRegistry()
    router = AdapterRouter(reg)
    sel = router.select(
        make_action(target_platform="unknown", operation="x", payload={})
    )
    assert sel.selected is None
    assert "not registered" in sel.reason or "no adapter" in sel.reason


def test_router_prefers_api_by_default() -> None:
    """无历史时, API capability_score 默认 0.5 < 0.7 阈值 — 但单 API 时仍走 API."""
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    reg.register(api)
    router = AdapterRouter(reg)
    sel = router.select(
        make_action(target_platform="shopify", operation="create_product", payload={})
    )
    assert sel.selected is api
    assert sel.kind == "api"


def test_router_falls_back_to_browser_when_api_below_threshold() -> None:
    """API score < 0.7 且 browser 可用 → 选 browser."""
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    browser = FakeBrowserShopify(page_factory=fake_page_factory)
    reg.register(api)
    reg.register(browser)
    router = AdapterRouter(reg)
    # 无历史 — api 默认 0.5, 不达 0.7 → 选 browser
    sel = router.select(
        make_action(target_platform="shopify", operation="create_product", payload={})
    )
    assert sel.kind == "browser"
    assert sel.selected is browser
    # 注意: fallback_candidate None 因为 browser 已是兜底


def test_router_requested_kind_overrides_preference() -> None:
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    browser = FakeBrowserShopify(page_factory=fake_page_factory)
    reg.register(api)
    reg.register(browser)
    router = AdapterRouter(reg)
    action = make_action(
        target_platform="shopify",
        operation="create_product",
        payload={},
        requested_kind="api",
    )
    sel = router.select(action)
    assert sel.kind == "api"
    assert sel.selected is api


@pytest.mark.asyncio
async def test_router_route_and_execute_happy_path() -> None:
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    reg.register(api)
    router = AdapterRouter(reg)
    action = make_action(
        target_platform="shopify",
        operation="create_product",
        payload={"title": "Test Product"},
    )
    decision = await router.route_and_execute(action)
    assert decision.result is not None
    assert decision.result.status == "ok"
    assert decision.result.kind_used == "api"
    assert decision.fallback_used is False


@pytest.mark.asyncio
async def test_router_fallback_browser_when_api_raises() -> None:
    reg = AdapterRegistry()
    bad_api = FakeFailingAPIShopify(http_caller=fake_http_ok)
    browser = FakeBrowserShopify(page_factory=fake_page_factory)
    reg.register(bad_api)
    reg.register(browser)
    router = AdapterRouter(reg)
    action = make_action(
        target_platform="shopify",
        operation="create_product",
        payload={},
        requested_kind="api",  # 强制走 API 触发失败
    )
    decision = await router.route_and_execute(action)
    assert decision.fallback_used is True
    assert decision.result is not None
    assert decision.result.kind_used == "browser"
    assert decision.result.status == "ok"


@pytest.mark.asyncio
async def test_router_health_updates_after_executions() -> None:
    """成功执行 → success_count++; 看 damped_score 应该 > 0.5."""
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    reg.register(api)
    router = AdapterRouter(reg)
    for _ in range(20):
        await router.route_and_execute(
            make_action(
                target_platform="shopify",
                operation="create_product",
                payload={},
                requested_kind="api",
            )
        )
    snap = router.snapshot_health()
    api_entries = [e for e in snap["adapters"] if e["kind"] == "api"]
    assert len(api_entries) > 0
    e = api_entries[0]
    assert e["success_count"] == 20
    assert e["damped_score"] > 0.5  # 应该 boost 到接近 1.0


@pytest.mark.asyncio
async def test_router_after_api_warms_up_it_prefers_api() -> None:
    """API 跑 20 次成功后, score > 0.7 → 优先 API 而不是 browser."""
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    browser = FakeBrowserShopify(page_factory=fake_page_factory)
    reg.register(api)
    reg.register(browser)
    router = AdapterRouter(reg)
    # warm up API
    for _ in range(20):
        await router.route_and_execute(
            make_action(
                target_platform="shopify",
                operation="create_product",
                payload={},
                requested_kind="api",
            )
        )
    # 现在不显式 kind, router 应优先 API
    sel = router.select(
        make_action(target_platform="shopify", operation="create_product", payload={})
    )
    assert sel.kind == "api"


# ---- BrowserAdapter base ----


@pytest.mark.asyncio
async def test_browser_adapter_creates_and_closes_page() -> None:
    pages: list[FakePage] = []

    async def tracking_factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    adapter = FakeBrowserShopify(page_factory=tracking_factory)
    result = await adapter.execute(
        make_action(target_platform="shopify", operation="create_product", payload={})
    )
    assert result.status == "ok"
    assert pages[0].closed is True
    assert ("goto", "https://shopify.com/admin/create_product") in pages[0].actions


@pytest.mark.asyncio
async def test_browser_adapter_handles_factory_failure() -> None:
    """page_factory 抛异常 → execute 不抛, 返回 failed."""

    async def bad_factory() -> FakePage:
        raise RuntimeError("playwright not installed")

    adapter = FakeBrowserShopify(page_factory=bad_factory)
    result = await adapter.execute(
        make_action(target_platform="shopify", operation="create_product", payload={})
    )
    assert result.status == "failed"
    assert "playwright not installed" in (result.error or "")


@pytest.mark.asyncio
async def test_browser_adapter_no_factory_returns_failed() -> None:
    adapter = FakeBrowserShopify(page_factory=None)
    result = await adapter.execute(
        make_action(target_platform="shopify", operation="create_product", payload={})
    )
    assert result.status == "failed"
    assert "page_factory" in (result.error or "")


@pytest.mark.asyncio
async def test_browser_adapter_screenshot_on_exception() -> None:
    """子类 _do_execute 抛 → base 自动截图 + return failed + artifact_refs."""

    class BadBrowser(BrowserAdapter):
        platform = "shopify"
        supported_operations: ClassVar[set[str]] = {"x"}

        async def _do_execute(self, action: Action, *, page: BrowserPage) -> ActionResult:
            await page.goto("https://x.com")
            raise RuntimeError("element not found")

    adapter = BadBrowser(page_factory=fake_page_factory)
    result = await adapter.execute(
        make_action(target_platform="shopify", operation="x", payload={})
    )
    assert result.status == "failed"
    assert any(a.get("kind") == "failure_screenshot" for a in result.artifact_refs)


@pytest.mark.asyncio
async def test_browser_adapter_health_check_round_trip() -> None:
    adapter = FakeBrowserShopify(page_factory=fake_page_factory)
    assert await adapter.health_check() is True


@pytest.mark.asyncio
async def test_browser_adapter_health_check_no_factory_false() -> None:
    adapter = FakeBrowserShopify(page_factory=None)
    assert await adapter.health_check() is False


# ---- APIAdapter base ----


@pytest.mark.asyncio
async def test_api_adapter_execute_happy_path() -> None:
    api = FakeAPIShopify(http_caller=fake_http_ok)
    result = await api.execute(
        make_action(
            target_platform="shopify",
            operation="create_product",
            payload={"title": "P1"},
        )
    )
    assert result.status == "ok"
    assert result.result_payload["received"] == {"title": "P1"}


@pytest.mark.asyncio
async def test_api_adapter_no_http_caller_returns_failed() -> None:
    api = FakeAPIShopify(http_caller=None)
    result = await api.execute(
        make_action(target_platform="shopify", operation="create_product", payload={})
    )
    assert result.status == "failed"
    assert "http_caller" in (result.error or "")


@pytest.mark.asyncio
async def test_api_adapter_health_check_default_true() -> None:
    api = FakeAPIShopify(http_caller=fake_http_ok)
    assert await api.health_check() is True


# ---- Full integration: Registry + Router + adapters ----


@pytest.mark.asyncio
async def test_full_integration_pipeline() -> None:
    """完整 pipeline: Registry 注册 API + Browser → Router 选 → execute."""
    reg = AdapterRegistry()
    api = FakeAPIShopify(http_caller=fake_http_ok)
    browser = FakeBrowserShopify(page_factory=fake_page_factory)
    reg.register(api)
    reg.register(browser)
    router = AdapterRouter(reg)

    # 第一次 — API 没历史, score=0.5 < 0.7, 选 browser
    decision1 = await router.route_and_execute(
        make_action(target_platform="shopify", operation="create_product", payload={})
    )
    assert decision1.result is not None
    assert decision1.result.kind_used == "browser"
    # 显式让 API 跑 N 次让 score 升上去
    for _ in range(20):
        await router.route_and_execute(
            make_action(
                target_platform="shopify",
                operation="create_product",
                payload={},
                requested_kind="api",
            )
        )
    # 现在 API score > 0.7, router 应优先 API
    decision2 = await router.route_and_execute(
        make_action(target_platform="shopify", operation="create_product", payload={})
    )
    assert decision2.result is not None
    assert decision2.result.kind_used == "api"
