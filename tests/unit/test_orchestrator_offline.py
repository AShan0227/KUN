"""Orchestrator walking-skeleton test (mocks DB + uses stub router).

Runs the full event loop: intent → plan → route → execute → finalize.
Skips DB I/O by monkey-patching session_scope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.orchestrator import Orchestrator, TaskResult
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

    async def flush(self):
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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_duplicate_returns_cached_answer(monkeypatch):
    providers = {
        "top": _RoutingStub(tier="top"),
        "cheap": _RoutingStub(tier="cheap"),
        "coding": _RoutingStub(tier="coding"),
        "fallback": _RoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))

    async def fake_find_idempotent_result_ref(*args, **kwargs):
        return "task-existing"

    async def fake_load_cached_task_result(*, tenant_id: str, task_id: str):
        return TaskResult(
            task_id=task_id,
            status="done",
            answer="cached answer",
            cost_usd_equivalent=0.03,
            tokens_in=7,
            tokens_out=2,
        )

    async def fail_if_executed(*args, **kwargs):
        raise AssertionError("duplicate request should not execute a new step")

    monkeypatch.setattr(
        "kun.engineering.orchestrator._find_idempotent_result_ref",
        fake_find_idempotent_result_ref,
    )
    monkeypatch.setattr(
        "kun.engineering.orchestrator._load_cached_task_result",
        fake_load_cached_task_result,
    )
    monkeypatch.setattr(Orchestrator, "_execute_step", fail_if_executed)

    result = await Orchestrator().run("Please greet the world")

    assert result.task_id == "task-existing"
    assert result.status == "done"
    assert result.answer == "cached answer"
    assert result.cost_usd_equivalent == 0.03


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_pauses_side_effect_tasks_before_execution(monkeypatch):
    owner = Owner(tenant_id="u-sylvan", project_id="proj-main")
    task_ref = TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("send email", owner),
            task_type="ops.email",
            risk_level="medium",
            owner=owner,
            success_criteria_short="发送邮件给客户",
        ),
        spec=TaskSpec(
            goal_detail="发送邮件给客户",
            required_tools=["email_sender"],
            external_resources=["customer-list"],
        ),
    )

    async def fake_interpret(*args, **kwargs):
        return task_ref

    async def fail_if_executed(*args, **kwargs):
        raise AssertionError("side-effect task should pause before execution")

    orch = Orchestrator()
    monkeypatch.setattr(orch.intent, "interpret", fake_interpret)
    monkeypatch.setattr(orch, "_execute_step", fail_if_executed)

    events = []
    async for ev in orch.stream("send email"):
        events.append(ev)

    assert "guard_intervention" in [ev.kind for ev in events]
    done = next(ev for ev in events if ev.kind == "done")
    result = TaskResult.model_validate(done.data["result"])
    assert result.status == "paused"
    assert "等待确认" in result.answer
    assert "message.send" in result.answer
