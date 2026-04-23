"""Orchestrator walking-skeleton test (mocks DB + uses stub router).

Runs the full event loop: intent → plan → route → execute → finalize.
Skips DB I/O by monkey-patching session_scope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from kun.engineering.orchestrator import Orchestrator
from kun.interface.llm import LLMRouter
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.router import set_router
from kun.interface.llm.stub_provider import StubProvider


class _FakeSession:
    async def execute(self, *args, **kwargs):
        class R:
            def scalar_one_or_none(self):
                return None

            def scalar_one(self):
                return 0

            def all(self):
                return []

            def scalars(self):
                return self

        return R()

    def add(self, *args, **kwargs):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass


@asynccontextmanager
async def _fake_session_scope() -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Bypass DB so tests run without Postgres."""
    monkeypatch.setattr("kun.engineering.orchestrator.session_scope", _fake_session_scope)


def _intent_builder(request):
    """Return a JSON that the intent interpreter can parse."""
    return LLMResponse(
        content=(
            '{"task_type": "writing.greeting", "risk_level": "low", '
            '"complexity_score": 0.1, "estimated_cost_usd": 0.01, '
            '"estimated_duration_sec": 5, "success_criteria_short": "say hello"}'
        ),
        usage=UsageInfo(input_tokens=5, output_tokens=20),
    )


def _exec_builder(request):
    return LLMResponse(
        content="Hello, world!",
        usage=UsageInfo(input_tokens=10, output_tokens=4),
    )


class _RoutingStub(StubProvider):
    """Stub that picks the right response based on 'system' content.

    Intent prompts contain '意图理解层' in system, exec prompts contain '执行角色'.
    """

    async def invoke(self, request):
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            self._builder = _intent_builder  # type: ignore[assignment]
        else:
            self._builder = _exec_builder  # type: ignore[assignment]
        return await super().invoke(request)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_runs_end_to_end():
    providers = {
        "top": _RoutingStub(tier="top"),
        "cheap": _RoutingStub(tier="cheap"),
        "coding": _RoutingStub(tier="coding"),
        "fallback": _RoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))

    orch = Orchestrator()
    events_seen = []
    async for ev in orch.stream("Please greet the world"):
        events_seen.append(ev.kind)

    # Minimum contract: we see thinking → action_plan → action → cost_tick → answer → done
    assert "thinking" in events_seen
    assert "action_plan" in events_seen
    assert "action" in events_seen
    assert "cost_tick" in events_seen
    assert "answer" in events_seen
    assert "done" in events_seen


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_run_returns_result():
    providers = {
        "top": _RoutingStub(tier="top"),
        "cheap": _RoutingStub(tier="cheap"),
        "coding": _RoutingStub(tier="coding"),
        "fallback": _RoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))
    orch = Orchestrator()
    result = await orch.run("Say hi")
    assert result.status == "done"
    assert result.answer == "Hello, world!"
    assert result.task_id.startswith("tk-")
    assert result.cost_usd_equivalent == 0.0  # stub has zero prices
