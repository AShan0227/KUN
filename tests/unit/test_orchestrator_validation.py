"""End-to-end scenario — orchestrator + validation + capability writeback.

Verifies:
  - validation pipeline runs when task has high complexity or high risk
  - aggregated verdict is emitted as `insight` event
  - capability writeback runs (we stub the DB path so it just no-ops cleanly)
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
    async def execute(self, *a, **kw):
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

    def add(self, *a, **kw):
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
    monkeypatch.setattr("kun.engineering.orchestrator.session_scope", _fake_session_scope)
    # Also patch capability writeback's session scope
    monkeypatch.setattr("kun.engineering.capability_writeback.session_scope", _fake_session_scope)


def _high_complexity_intent(request):
    # returns an intent JSON that flips tier to tier1 (high complexity, low risk)
    return LLMResponse(
        content=(
            '{"task_type": "coding.python.complex", "risk_level": "low",'
            ' "complexity_score": 0.8, "estimated_cost_usd": 0.02,'
            ' "estimated_duration_sec": 5, "success_criteria_short": "do thing"}'
        ),
        usage=UsageInfo(input_tokens=5, output_tokens=20),
    )


def _good_exec(request):
    return LLMResponse(content="Result OK", usage=UsageInfo(input_tokens=8, output_tokens=3))


def _pass_judge(request):
    return LLMResponse(
        content='{"pass": true, "score": 0.85, "reason": "ok"}',
        usage=UsageInfo(input_tokens=20, output_tokens=15),
    )


class _MultiBuilderStub(StubProvider):
    """Pick response by what system prompt says."""

    async def invoke(self, request):
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            self._builder = _high_complexity_intent  # type: ignore[assignment]
        elif "评估判官" in sys_text:
            self._builder = _pass_judge  # type: ignore[assignment]
        else:
            self._builder = _good_exec  # type: ignore[assignment]
        return await super().invoke(request)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validation_triggers_for_high_complexity():
    providers = {
        "top": _MultiBuilderStub(tier="top"),
        "cheap": _MultiBuilderStub(tier="cheap"),
        "coding": _MultiBuilderStub(tier="coding"),
        "fallback": _MultiBuilderStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))

    orch = Orchestrator()
    kinds: list[str] = []
    async for ev in orch.stream("Do a complex thing"):
        kinds.append(ev.kind)

    # Walking skeleton emits: thinking, action_plan, action_plan (skills),
    # action, cost_tick, answer, done  PLUS insight for validation verdict
    assert "thinking" in kinds
    assert "insight" in kinds  # validation verdict emitted
    assert "answer" in kinds
    assert "done" in kinds
