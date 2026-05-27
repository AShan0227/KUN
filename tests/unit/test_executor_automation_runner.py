"""L6.E — AutomationRunner 单测 (Executor 侧 Router 调用方)."""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.executor.automation_runner import (
    AutomationRunner,
    action_result_to_artifact,
)
from kun.interface.automation import (
    Action,
    ActionResult,
    AdapterRegistry,
    AdapterRouter,
    AutomationAdapter,
    make_action,
)

# ---- Fake adapters ----


class _FakeAdapter:
    """Stub adapter — returns canned ActionResult per platform/op."""

    def __init__(
        self,
        *,
        kind: str,
        platform: str = "shopify",
        ops: set[str] | None = None,
        status: str = "ok",
        result_payload: dict[str, Any] | None = None,
        health: bool = True,
    ) -> None:
        self.kind = kind
        self.platform = platform
        self.supported_operations = ops or {"create_product", "list_orders"}
        self._status = status
        self._result_payload = result_payload or {"echo": "ok"}
        self._health = health
        self.executions: list[Action] = []

    async def execute(self, action: Action) -> ActionResult:
        self.executions.append(action)
        return ActionResult(
            action_id=action.action_id,
            status=self._status,  # type: ignore[arg-type]
            kind_used=self.kind,  # type: ignore[arg-type]
            result_payload=self._result_payload,
            latency_ms=12.5,
            rationale=f"fake {self.kind}",
            error=None if self._status == "ok" else "stub error",
        )

    async def health_check(self) -> bool:
        return self._health


def _make_router(adapters: list[AutomationAdapter]) -> AdapterRouter:
    reg = AdapterRegistry()
    for a in adapters:
        reg.register(a)
    return AdapterRouter(reg)


# ---- Tests ----


@pytest.mark.asyncio
async def test_runs_action_and_returns_decision() -> None:
    api = _FakeAdapter(kind="api")
    runner = AutomationRunner(_make_router([api]))
    action = make_action(
        target_platform="shopify",
        operation="create_product",
        payload={"title": "Test"},
        requested_kind="api",
    )
    decision = await runner.run(action)
    assert decision.result is not None
    assert decision.result.status == "ok"
    assert decision.result.kind_used == "api"
    assert len(api.executions) == 1


@pytest.mark.asyncio
async def test_emits_start_and_completed_events() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def capture(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    api = _FakeAdapter(kind="api")
    runner = AutomationRunner(_make_router([api]), emitter=capture)
    action = make_action(
        target_platform="shopify",
        operation="create_product",
        payload={"title": "Test"},
        requested_kind="api",
    )
    await runner.run(action)
    kinds = [k for k, _ in events]
    assert kinds == ["action.started", "action.completed"]
    started_payload = events[0][1]
    assert started_payload["action_id"] == action.action_id
    assert started_payload["target_platform"] == "shopify"
    completed_payload = events[1][1]
    assert completed_payload["status"] == "ok"
    assert completed_payload["kind_used"] == "api"
    assert "latency_ms" in completed_payload


@pytest.mark.asyncio
async def test_emits_failed_event_when_status_not_ok() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def capture(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    api = _FakeAdapter(kind="api", status="failed")
    runner = AutomationRunner(_make_router([api]), emitter=capture)
    action = make_action(
        target_platform="shopify",
        operation="create_product",
        payload={},
        requested_kind="api",
    )
    await runner.run(action)
    kinds = [k for k, _ in events]
    assert kinds == ["action.started", "action.failed"]
    assert events[1][1]["status"] == "failed"
    assert events[1][1]["error"] == "stub error"


@pytest.mark.asyncio
async def test_emit_failure_does_not_break_main_path() -> None:
    """state-accumulation emit raise 被吞, run 仍返回 decision."""

    async def bad_emitter(kind: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("kafka down")

    api = _FakeAdapter(kind="api")
    runner = AutomationRunner(_make_router([api]), emitter=bad_emitter)
    action = make_action(
        target_platform="shopify",
        operation="create_product",
        payload={},
        requested_kind="api",
    )
    decision = await runner.run(action)
    assert decision.result is not None
    assert decision.result.status == "ok"


@pytest.mark.asyncio
async def test_no_adapter_registered_emits_failed() -> None:
    events: list[tuple[str, dict[str, Any]]] = []

    async def capture(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    # Empty registry — no adapters
    router = AdapterRouter(AdapterRegistry())
    runner = AutomationRunner(router, emitter=capture)
    action = make_action(
        target_platform="unknown",
        operation="x",
        payload={},
    )
    decision = await runner.run(action)
    assert decision.result is None
    kinds = [k for k, _ in events]
    assert kinds == ["action.started", "action.failed"]
    assert events[1][1]["reason"] == "no_adapter_registered"


@pytest.mark.asyncio
async def test_fallback_used_propagates_to_event() -> None:
    """API 429 → fallback browser; event 应反映 fallback_used=True."""
    events: list[tuple[str, dict[str, Any]]] = []

    async def capture(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, payload))

    api = _FakeAdapter(kind="api", status="rate_limited")
    browser = _FakeAdapter(kind="browser", status="ok")
    runner = AutomationRunner(_make_router([api, browser]), emitter=capture)
    action = make_action(
        target_platform="shopify",
        operation="create_product",
        payload={"title": "Test"},
        requested_kind="api",
    )
    decision = await runner.run(action)
    assert decision.result is not None
    assert decision.result.status == "ok"
    assert decision.result.kind_used == "browser"
    assert decision.fallback_used is True
    # event 也应该带 fallback_used
    assert events[1][1]["fallback_used"] is True


@pytest.mark.asyncio
async def test_default_noop_emitter_does_not_raise() -> None:
    """没传 emitter 时, 默认 noop, 不应 raise."""
    api = _FakeAdapter(kind="api")
    runner = AutomationRunner(_make_router([api]))  # 无 emitter
    action = make_action(
        target_platform="shopify",
        operation="create_product",
        payload={},
        requested_kind="api",
    )
    decision = await runner.run(action)
    assert decision.result is not None
    assert decision.result.status == "ok"


# ---- action_result_to_artifact ----


def test_artifact_helper_round_trip() -> None:
    result = ActionResult(
        action_id="act-x",
        status="ok",
        kind_used="api",
        result_payload={"product": {"id": 1}},
        latency_ms=42.0,
        rationale="api succeeded",
        artifact_refs=("s3://bucket/file",),
        cost_usd=0.0001,
    )
    art = action_result_to_artifact(result)
    assert art["action_id"] == "act-x"
    assert art["status"] == "ok"
    assert art["kind_used"] == "api"
    assert art["result_payload"] == {"product": {"id": 1}}
    assert art["latency_ms"] == 42.0
    assert art["rationale"] == "api succeeded"
    assert art["artifact_refs"] == ["s3://bucket/file"]
    assert art["cost_usd"] == 0.0001
    assert art["error"] is None


def test_artifact_helper_carries_error() -> None:
    result = ActionResult(
        action_id="act-x",
        status="failed",
        kind_used="api",
        result_payload={},
        latency_ms=1.0,
        rationale="",
        error="server 500",
    )
    art = action_result_to_artifact(result)
    assert art["status"] == "failed"
    assert art["error"] == "server 500"
