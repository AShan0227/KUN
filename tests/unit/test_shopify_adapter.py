"""L6.D-Shopify — Shopify Adapter (API + Browser + Router 集成) 单测."""

from __future__ import annotations

from typing import Any

import pytest
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


class CapturingHttpCaller:
    """记录所有 HTTP call, 可注入特定 response."""

    def __init__(self, *, default_response: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self._default_response = default_response or {"_status_code": 200, "ok": True}
        self._next_response: dict[str, Any] | None = None

    def queue_next(self, response: dict[str, Any]) -> None:
        self._next_response = response

    async def __call__(
        self, method: str, url: str, body: dict | None
    ) -> dict[str, Any]:
        self.calls.append((method, url, body))
        if self._next_response is not None:
            r = self._next_response
            self._next_response = None
            return r
        return self._default_response


class FakePage:
    """Minimal BrowserPage fake."""

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
        self, selector: str, *, timeout: float | None = None  # noqa: ASYNC109
    ) -> None:
        self.actions.append(("wait", selector))

    async def screenshot(self, *, path: str | None = None) -> bytes:
        return b"png"

    async def close(self) -> None:
        self.closed = True


async def fake_page_factory() -> FakePage:
    return FakePage()


# ---- ShopifyAPIAdapter ----


def test_api_adapter_supported_operations() -> None:
    api = ShopifyAPIAdapter()
    assert "create_product" in api.supported_operations
    assert "list_orders" in api.supported_operations
    assert "get_product" in api.supported_operations
    assert api.platform == "shopify"


@pytest.mark.asyncio
async def test_api_adapter_create_product_wraps_body() -> None:
    """Shopify 要求 body wrap 在 {"product": {...}}."""
    http = CapturingHttpCaller(
        default_response={
            "_status_code": 201,
            "product": {"id": 12345, "title": "Test"},
        }
    )
    api = ShopifyAPIAdapter(http_caller=http, access_token="token-x")
    result = await api.execute(
        make_action(
            target_platform="shopify",
            operation="create_product",
            payload={"title": "Test Product", "vendor": "ACME"},
        )
    )
    assert result.status == "ok"
    assert len(http.calls) == 1
    method, url, body = http.calls[0]
    assert method == "POST"
    assert "products.json" in url
    # body 应该 wrap 在 product 键下
    assert "product" in body
    assert body["product"]["title"] == "Test Product"
    assert body["product"]["vendor"] == "ACME"


@pytest.mark.asyncio
async def test_api_adapter_list_orders_get_no_body() -> None:
    http = CapturingHttpCaller(
        default_response={"orders": [{"id": 1}, {"id": 2}]}
    )
    api = ShopifyAPIAdapter(http_caller=http)
    result = await api.execute(
        make_action(
            target_platform="shopify",
            operation="list_orders",
            payload={},
        )
    )
    assert result.status == "ok"
    method, _url, body = http.calls[0]
    assert method == "GET"
    assert body is None or body == {}
    assert "orders" in result.result_payload


@pytest.mark.asyncio
async def test_api_adapter_get_product_substitutes_id() -> None:
    """endpoint template {product_id} 由 payload 填充."""
    http = CapturingHttpCaller(default_response={"product": {"id": 999}})
    api = ShopifyAPIAdapter(http_caller=http)
    result = await api.execute(
        make_action(
            target_platform="shopify",
            operation="get_product",
            payload={"product_id": 999},
        )
    )
    assert result.status == "ok"
    _method, url, _body = http.calls[0]
    assert "products/999.json" in url


@pytest.mark.asyncio
async def test_api_adapter_unsupported_op_returns_failed() -> None:
    api = ShopifyAPIAdapter(http_caller=CapturingHttpCaller())
    result = await api.execute(
        make_action(
            target_platform="shopify",
            operation="this_does_not_exist",
            payload={},
        )
    )
    assert result.status == "failed"
    assert "unsupported operation" in (result.error or "")


@pytest.mark.asyncio
async def test_api_adapter_401_returns_auth_required() -> None:
    http = CapturingHttpCaller(
        default_response={"_status_code": 401, "errors": "Invalid API key"}
    )
    api = ShopifyAPIAdapter(http_caller=http)
    result = await api.execute(
        make_action(target_platform="shopify", operation="list_orders", payload={})
    )
    assert result.status == "auth_required"


@pytest.mark.asyncio
async def test_api_adapter_429_returns_rate_limited() -> None:
    http = CapturingHttpCaller(
        default_response={"_status_code": 429, "errors": "Throttled"}
    )
    api = ShopifyAPIAdapter(http_caller=http)
    result = await api.execute(
        make_action(target_platform="shopify", operation="list_orders", payload={})
    )
    assert result.status == "rate_limited"


@pytest.mark.asyncio
async def test_api_adapter_500_returns_failed() -> None:
    http = CapturingHttpCaller(
        default_response={"_status_code": 500, "errors": "Server error"}
    )
    api = ShopifyAPIAdapter(http_caller=http)
    result = await api.execute(
        make_action(target_platform="shopify", operation="list_orders", payload={})
    )
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_api_adapter_health_check_with_token() -> None:
    http = CapturingHttpCaller(default_response={"shop": {"id": 1}})
    api = ShopifyAPIAdapter(http_caller=http, access_token="t-1")
    assert await api.health_check() is True


@pytest.mark.asyncio
async def test_api_adapter_health_check_no_token() -> None:
    api = ShopifyAPIAdapter(http_caller=CapturingHttpCaller(), access_token=None)
    assert await api.health_check() is False


# ---- ShopifyBrowserAdapter ----


def test_browser_adapter_supported_operations() -> None:
    b = ShopifyBrowserAdapter()
    assert "create_product" in b.supported_operations
    assert "list_orders" in b.supported_operations
    assert "get_product" in b.supported_operations


@pytest.mark.asyncio
async def test_browser_adapter_create_product_drives_form() -> None:
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = ShopifyBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="shopify",
            operation="create_product",
            payload={"title": "Test", "body_html": "<p>desc</p>"},
        )
    )
    assert result.status == "ok"
    page = pages[0]
    # 应该 goto admin/products/new
    assert any(a[0] == "goto" and "products/new" in a[1] for a in page.actions)
    # 应该 fill title + body
    assert any(a[0] == "fill" and a[2] == "Test" for a in page.actions)
    # 应该 click save
    assert any(a[0] == "click" for a in page.actions)
    assert page.closed is True


@pytest.mark.asyncio
async def test_browser_adapter_list_orders_navigates() -> None:
    b = ShopifyBrowserAdapter(page_factory=fake_page_factory)
    result = await b.execute(
        make_action(
            target_platform="shopify",
            operation="list_orders",
            payload={},
        )
    )
    assert result.status == "ok"
    assert "orders" in result.result_payload.get("navigated_to", "")


@pytest.mark.asyncio
async def test_browser_adapter_get_product_substitutes_id() -> None:
    b = ShopifyBrowserAdapter(page_factory=fake_page_factory)
    result = await b.execute(
        make_action(
            target_platform="shopify",
            operation="get_product",
            payload={"product_id": "789"},
        )
    )
    assert result.status == "ok"
    assert "789" in result.result_payload["navigated_to"]


@pytest.mark.asyncio
async def test_browser_adapter_unsupported_op_returns_failed() -> None:
    b = ShopifyBrowserAdapter(page_factory=fake_page_factory)
    result = await b.execute(
        make_action(
            target_platform="shopify",
            operation="unknown_op",
            payload={},
        )
    )
    assert result.status == "failed"


@pytest.mark.asyncio
async def test_browser_adapter_health_check_round_trip() -> None:
    b = ShopifyBrowserAdapter(page_factory=fake_page_factory)
    assert await b.health_check() is True


@pytest.mark.asyncio
async def test_browser_adapter_health_check_no_factory() -> None:
    b = ShopifyBrowserAdapter(page_factory=None)
    assert await b.health_check() is False


# ---- Integration: Registry + Router + Shopify adapters ----


@pytest.mark.asyncio
async def test_router_picks_browser_when_api_cold_start() -> None:
    """无历史时, API score=0.5 < 0.7, 双 adapter 注册 → 选 browser."""
    reg = AdapterRegistry()
    http = CapturingHttpCaller(default_response={"product": {"id": 1}})
    api = ShopifyAPIAdapter(http_caller=http)
    browser = ShopifyBrowserAdapter(page_factory=fake_page_factory)
    reg.register(api)
    reg.register(browser)
    router = AdapterRouter(reg)
    decision = await router.route_and_execute(
        make_action(
            target_platform="shopify",
            operation="create_product",
            payload={"title": "Test"},
        )
    )
    assert decision.result is not None
    assert decision.result.kind_used == "browser"
    assert decision.fallback_used is False


@pytest.mark.asyncio
async def test_router_prefers_api_after_warmup() -> None:
    """API 跑 20 次成功 → score > 0.7 → 选 API."""
    reg = AdapterRegistry()
    http = CapturingHttpCaller(
        default_response={"_status_code": 201, "product": {"id": 1}}
    )
    api = ShopifyAPIAdapter(http_caller=http)
    browser = ShopifyBrowserAdapter(page_factory=fake_page_factory)
    reg.register(api)
    reg.register(browser)
    router = AdapterRouter(reg)
    # warmup API
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
            payload={"title": "Should be API"},
        )
    )
    assert decision.result is not None
    assert decision.result.kind_used == "api"


@pytest.mark.asyncio
async def test_router_fallback_browser_when_api_429() -> None:
    """API 限流 → fallback browser."""
    reg = AdapterRegistry()
    http = CapturingHttpCaller(
        default_response={"_status_code": 429, "errors": "Throttled"}
    )
    api = ShopifyAPIAdapter(http_caller=http)
    browser = ShopifyBrowserAdapter(page_factory=fake_page_factory)
    reg.register(api)
    reg.register(browser)
    router = AdapterRouter(reg)
    decision = await router.route_and_execute(
        make_action(
            target_platform="shopify",
            operation="create_product",
            payload={"title": "Test"},
            requested_kind="api",  # 强制走 API 触发 429
        )
    )
    # fallback 到 browser, status ok
    assert decision.fallback_used is True
    assert decision.result is not None
    assert decision.result.kind_used == "browser"
    assert decision.result.status == "ok"
