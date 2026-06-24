"""L6.E — End-to-end Director → Executor → AdapterRouter wiring 集成测试.

模拟完整 KUN 路径:

  1. 用户 NL "在 Shopify 创建一个 mug 产品"
  2. IntentInterpreter 用 stub LLMRouter 解析 → parsed dict (含 automation 字段)
  3. extract_automation_action(parsed) → Action
  4. AutomationRunner.run(action) → AdapterRouter.route_and_execute()
  5. ShopifyAPIAdapter (用 fake http_caller) 跑成功 → ActionResult.status="ok"
  6. event 流: action.started → action.completed
  7. action_result_to_artifact 转 Phase 1 风格

零 LLM 实调 / 零 HTTP / 零 Playwright — 所有外部依赖 DI 替身.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from kun.agents.director.automation_intent import extract_automation_action
from kun.agents.executor.automation_runner import (
    AutomationRunner,
    action_result_to_artifact,
)
from kun.interface.automation import AdapterRegistry, AdapterRouter
from kun.interface.automation.shopify import (
    ShopifyAPIAdapter,
    ShopifyBrowserAdapter,
)

pytestmark = pytest.mark.e2e

# ---- Phase 1 fakes ----


class _StubLLMResponse:
    """Mimic LLMResponse shape used by IntentInterpreter."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.cost_usd_equivalent = 0.0


class _StubLLMRouter:
    """LLM Router stub returning canned JSON for intent parsing."""

    def __init__(self, response_json: dict[str, Any]) -> None:
        self.invocations: list[Any] = []
        self._response = _StubLLMResponse(json.dumps(response_json))

    async def invoke(self, request: Any, **_: Any) -> _StubLLMResponse:
        self.invocations.append(request)
        return self._response


# ---- Phase 2 fakes ----


class _FakeHttpCaller:
    def __init__(
        self, *, default: dict[str, Any] | None = None
    ) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self._default = default or {"_status_code": 201, "product": {"id": 999}}

    async def __call__(
        self, method: str, url: str, body: dict | None
    ) -> dict[str, Any]:
        self.calls.append((method, url, body))
        return self._default


class _FakePage:
    def __init__(self) -> None:
        self.closed = False

    async def goto(self, url: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        pass

    async def fill(self, selector: str, value: str) -> None:
        pass

    async def click(self, selector: str) -> None:
        pass

    async def wait_for_selector(
        self, selector: str, *, timeout: float | None = None  # noqa: ASYNC109
    ) -> None:
        pass

    async def screenshot(self, *, path: str | None = None) -> bytes:
        return b"png"

    async def close(self) -> None:
        self.closed = True


async def _fake_page_factory() -> _FakePage:
    return _FakePage()


# ---- Helpers ----


def _build_router_with_shopify(
    http_caller: _FakeHttpCaller,
) -> AdapterRouter:
    reg = AdapterRegistry()
    reg.register(ShopifyAPIAdapter(http_caller=http_caller, access_token="tok"))
    reg.register(ShopifyBrowserAdapter(page_factory=_fake_page_factory))
    return AdapterRouter(reg)


# ---- Tests ----


@pytest.mark.asyncio
async def test_e2e_director_to_router_happy_path() -> None:
    """User NL → parsed intent → Action → Router → Shopify API → ActionResult."""
    # Step 1: User's natural language (simulated — we skip IntentInterpreter
    # itself and start from the parsed JSON it would produce).
    parsed_intent = {
        "task_type": "automation.ecommerce.shopify.create_product",
        "goal_detail": "create a mug product in Shopify",
        "automation": {
            "target_platform": "shopify",
            "operation": "create_product",
            "payload": {"title": "Test Mug", "vendor": "ACME"},
            "requested_kind": "api",  # 冷启动期强制 API path (eval pattern)
        },
    }

    # Step 2: Director extracts Action
    action = extract_automation_action(parsed_intent, tenant_id="t-acme")
    assert action is not None
    assert action.target_platform == "shopify"
    assert action.operation == "create_product"
    assert action.tenant_id == "t-acme"
    assert action.requested_kind == "api"

    # Step 3: Executor wires Router
    http = _FakeHttpCaller(
        default={
            "_status_code": 201,
            "product": {"id": 12345, "title": "Test Mug"},
        }
    )
    router = _build_router_with_shopify(http)

    # Step 4: Track events
    events: list[tuple[str, dict[str, Any]]] = []

    async def emitter(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    runner = AutomationRunner(router, emitter=emitter)

    # Step 5: Execute
    decision = await runner.run(action)

    # Step 6: Verify Shopify API called
    assert len(http.calls) == 1
    method, url, body = http.calls[0]
    assert method == "POST"
    assert "products.json" in url
    assert body is not None
    assert body["product"]["title"] == "Test Mug"

    # Step 7: Result is ok
    assert decision.result is not None
    assert decision.result.status == "ok"
    assert decision.result.kind_used == "api"
    assert decision.fallback_used is False

    # Step 8: Events emitted
    event_kinds = [k for k, _ in events]
    assert event_kinds == ["action.started", "action.completed"]
    assert events[0][1]["target_platform"] == "shopify"
    assert events[0][1]["operation"] == "create_product"
    assert events[0][1]["tenant_id"] == "t-acme"
    assert events[1][1]["status"] == "ok"

    # Step 9: Convert to Phase 1 artifact
    artifact = action_result_to_artifact(decision.result)
    assert artifact["status"] == "ok"
    assert artifact["kind_used"] == "api"
    assert artifact["action_id"] == action.action_id


@pytest.mark.asyncio
async def test_e2e_api_rate_limited_falls_back_to_browser() -> None:
    """Shopify API 429 → Router fallback browser → 仍 ok."""
    parsed_intent = {
        "task_type": "automation.ecommerce.shopify.create_product",
        "automation": {
            "target_platform": "shopify",
            "operation": "create_product",
            "payload": {"title": "Test"},
            "requested_kind": "api",
        },
    }
    action = extract_automation_action(parsed_intent, tenant_id="t-acme")
    assert action is not None

    http = _FakeHttpCaller(
        default={"_status_code": 429, "errors": "Throttled"}
    )
    router = _build_router_with_shopify(http)
    runner = AutomationRunner(router)

    decision = await runner.run(action)
    assert decision.result is not None
    # Browser fallback engaged
    assert decision.fallback_used is True
    assert decision.result.kind_used == "browser"
    assert decision.result.status == "ok"


@pytest.mark.asyncio
async def test_e2e_non_automation_intent_returns_none() -> None:
    """普通 coding 任务无 automation 字段 → extract 返回 None."""
    parsed = {
        "task_type": "coding.python.fastapi",
        "goal_detail": "write a fastapi endpoint",
        # NO automation key
    }
    action = extract_automation_action(parsed, tenant_id="t-acme")
    assert action is None


@pytest.mark.asyncio
async def test_e2e_unknown_platform_no_adapter() -> None:
    """unknown platform → Router 返回无 adapter, decision.result is None."""
    parsed_intent = {
        "automation": {
            "target_platform": "fictional_platform",
            "operation": "do_thing",
            "payload": {},
        }
    }
    action = extract_automation_action(parsed_intent, tenant_id="t-acme")
    assert action is not None

    http = _FakeHttpCaller()
    router = _build_router_with_shopify(http)  # only knows shopify
    runner = AutomationRunner(router)

    decision = await runner.run(action)
    assert decision.result is None  # no adapter registered
    # http NOT called
    assert http.calls == []


@pytest.mark.asyncio
async def test_e2e_router_picks_browser_at_cold_start() -> None:
    """无 requested_kind, 冷启动 → Router pick browser (API score=0.5 < 0.7)."""
    parsed_intent = {
        "automation": {
            "target_platform": "shopify",
            "operation": "create_product",
            "payload": {"title": "Cold Start"},
            # NO requested_kind → router 自由选
        }
    }
    action = extract_automation_action(parsed_intent, tenant_id="t-acme")
    assert action is not None
    assert action.requested_kind is None

    http = _FakeHttpCaller()
    router = _build_router_with_shopify(http)
    runner = AutomationRunner(router)

    decision = await runner.run(action)
    assert decision.result is not None
    # Cold start prefers browser (与 L6.D-Shopify integration test 一致)
    assert decision.result.kind_used == "browser"
    assert decision.fallback_used is False
    # HTTP API 不被调用
    assert http.calls == []


@pytest.mark.asyncio
async def test_e2e_industry_agnostic_meta_ads_path_shape() -> None:
    """投放 (Meta Ads) 走同一 wiring — extract 出 Action 形态正确."""
    parsed_intent = {
        "automation": {
            "target_platform": "meta_ads",
            "operation": "create_ad",
            "payload": {"budget_usd": 50, "creative": "image-1"},
        }
    }
    action = extract_automation_action(parsed_intent, tenant_id="t-brand")
    assert action is not None
    assert action.target_platform == "meta_ads"
    assert action.operation == "create_ad"
    assert action.payload == {"budget_usd": 50, "creative": "image-1"}
    # 当 Meta Ads adapter 写好后, AutomationRunner 直接服务无需任何改动
