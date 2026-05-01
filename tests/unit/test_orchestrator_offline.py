"""Orchestrator walking-skeleton test (mocks DB + uses stub router).

Runs the full event loop: intent → plan → route → execute → finalize.
Skips DB I/O by monkey-patching session_scope.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from kun.context.packer import ContextPack, PackedContextItem
from kun.core.emergent_solution import (
    EmergentSolution,
    EmergentSolutionLibrary,
    EmergentSource,
)
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.emergent_switch import EmergentSwitchManager
from kun.engineering.orchestrator import Orchestrator, TaskResult, _context_resource_ids
from kun.interface.llm import LLMRouter
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.router import set_router
from kun.interface.llm.stub_provider import StubProvider
from kun.skills.dispatcher import autoload_builtins
from kun.watchtower.engine import RuleEngine
from kun.watchtower.rules import GuardRule, RuleAction, RuleTrigger


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
async def _fake_session_scope(**_kwargs: object) -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


@pytest.fixture(autouse=True)
def _patch_db(monkeypatch):
    """Bypass DB so tests run without Postgres."""
    monkeypatch.setattr("kun.engineering.orchestrator.session_scope", _fake_session_scope)


async def _identity_translator(**kwargs: object) -> str:
    payload = kwargs["payload"]
    assert isinstance(payload, dict)
    return str(payload["answer"])


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


def _costly_exec_builder(request):
    return LLMResponse(
        content="Hello, world!",
        usage=UsageInfo(input_tokens=10, output_tokens=4),
        cost_usd_equivalent=0.03,
    )


def test_context_resource_ids_preserve_asset_kind() -> None:
    pack = ContextPack(
        items=[
            PackedContextItem(
                asset_id="m-1",
                asset_kind="memory",
                relevance_score=0.9,
            ),
            PackedContextItem(
                asset_id="method-1",
                asset_kind="methodology",
                relevance_score=0.8,
            ),
            PackedContextItem(
                asset_id="knowledge-1",
                asset_kind="knowledge",
                relevance_score=0.7,
            ),
        ]
    )

    assert _context_resource_ids(pack) == [
        "memory:m-1",
        "methodology:method-1",
        "knowledge:knowledge-1",
    ]


def _judge_builder(request):
    return LLMResponse(
        content='{"pass": true, "score": 0.9, "reason": "ok"}',
        usage=UsageInfo(input_tokens=12, output_tokens=8),
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


class _CostlyRoutingStub(_RoutingStub):
    async def invoke(self, request):
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            self._builder = _intent_builder  # type: ignore[assignment]
        else:
            self._builder = _costly_exec_builder  # type: ignore[assignment]
        return await StubProvider.invoke(self, request)


class _WorldRequestRoutingStub(_RoutingStub):
    async def invoke(self, request):
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            self._builder = _intent_builder  # type: ignore[assignment]
        else:
            self._builder = lambda _request: LLMResponse(
                content=(
                    '<skill name="world-request">'
                    '{"action_type":"email.send","target_ref":"ops@example.com",'
                    '"risk_level":"medium","payload":{"to":"ops@example.com",'
                    '"subject":"Need approval","body":"Please review."}}'
                    "</skill>"
                ),
                usage=UsageInfo(input_tokens=10, output_tokens=22),
            )
        return await StubProvider.invoke(self, request)


class _PlanningRoutingStub(StubProvider):
    planning_calls: int

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.planning_calls = 0

    async def invoke(self, request):
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            self._builder = lambda _request: LLMResponse(
                content=json.dumps(
                    {
                        "task_type": "coding.python.complex",
                        "risk_level": "low",
                        "complexity_score": 0.8,
                        "estimated_cost_usd": 0.02,
                        "estimated_duration_sec": 5,
                        "success_criteria_short": "完成复杂任务",
                        "goal_detail": "完成复杂任务并验证结果",
                        "success_metrics": ["结果通过"],
                        "required_skills": ["code-review"],
                    },
                    ensure_ascii=False,
                ),
                usage=UsageInfo(input_tokens=5, output_tokens=20),
            )
        elif "任务拆解层" in sys_text:
            self.planning_calls += 1
            self._builder = lambda _request: LLMResponse(
                content=json.dumps(
                    {
                        "steps": [
                            {
                                "step_id": 1,
                                "description": "核对边界",
                                "skill_hint": "task.boundary_check",
                                "depends_on": [],
                            },
                            {
                                "step_id": 2,
                                "description": "执行复杂任务",
                                "skill_hint": "code-review",
                                "depends_on": [1],
                            },
                        ]
                    },
                    ensure_ascii=False,
                ),
                usage=UsageInfo(input_tokens=12, output_tokens=30),
            )
        elif "评估判官" in sys_text:
            self._builder = _judge_builder  # type: ignore[assignment]
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

    orch = Orchestrator(output_translator=_identity_translator)
    events = []
    async for ev in orch.stream("Please greet the world"):
        events.append(ev)
    events_seen = [ev.kind for ev in events]

    # Minimum contract: we see thinking → action_plan → action → cost_tick → answer → done
    assert "thinking" in events_seen
    assert "action_plan" in events_seen
    assert "action" in events_seen
    assert "cost_tick" in events_seen
    assert "answer" in events_seen
    assert "done" in events_seen
    ooda_stages = [ev.data.get("stage") for ev in events if ev.kind == "action_plan"]
    assert "ooda_orient" in ooda_stages
    assert "ooda_decide" in ooda_stages
    assert "ooda_reflect" in ooda_stages
    assert "ooda_finalize" in ooda_stages


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_uses_llm_planner_for_complex_task():
    top = _PlanningRoutingStub(tier="top")
    providers = {
        "top": top,
        "cheap": _PlanningRoutingStub(tier="cheap"),
        "coding": _PlanningRoutingStub(tier="coding"),
        "fallback": _PlanningRoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))

    events_seen = []
    async for ev in Orchestrator().stream("Do a complex thing"):
        events_seen.append(ev.kind)

    assert top.planning_calls == 1
    assert "answer" in events_seen
    assert "done" in events_seen


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_pauses_out_of_scope_task_before_planning(monkeypatch) -> None:
    shared = _PlanningRoutingStub(tier="top")
    providers = {
        "top": shared,
        "cheap": shared,
        "coding": shared,
        "fallback": shared,
    }
    set_router(LLMRouter(providers))
    monkeypatch.setenv(
        "KUN_TASK_BOUNDARY_SCOPE_JSON",
        json.dumps(
            {
                "default": {
                    "role_id": "marketing-agent",
                    "role_name": "marketing copywriter",
                    "allowed_task_types": ["marketing.*"],
                    "forbidden_task_types": ["coding.*"],
                    "boundary_strict_mode": True,
                    "out_of_scope_redirect": "coding-agent",
                }
            }
        ),
    )

    events = []
    async for ev in Orchestrator(output_translator=_identity_translator).stream("Fix this bug"):
        events.append(ev)

    assert shared.planning_calls == 0
    guard_events = [
        ev
        for ev in events
        if ev.kind == "guard_intervention" and ev.data["stage"] == "task_boundary_guard"
    ]
    assert guard_events
    assert guard_events[0].data["level"] == "blocked"
    done = next(ev for ev in events if ev.kind == "done")
    result = TaskResult.model_validate(done.data["result"])
    assert result.status == "paused"
    assert "当前角色边界不覆盖" in result.answer
    assert "coding-agent" in result.answer


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_applies_emergent_switch_to_tail_plan():
    top = _PlanningRoutingStub(tier="top")
    providers = {
        "top": top,
        "cheap": _PlanningRoutingStub(tier="cheap"),
        "coding": _PlanningRoutingStub(tier="coding"),
        "fallback": _PlanningRoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))

    library = EmergentSolutionLibrary()
    library.add(
        EmergentSolution(
            task_type="coding",
            discovered_by="capability_card_query",
            source=EmergentSource(kind="internal_history", snippet="历史路径更好"),
            description="先重排后续执行，再补一次验证",
            estimated_outcome_delta=0.35,
            estimated_cost_delta=-0.05,
            status="stable",
            applies_when=["复杂任务第一步后发现更优后续路径"],
        )
    )
    emergent_switch = EmergentSwitchManager(library, switch_threshold=0.05)

    events = []
    async for ev in Orchestrator(emergent_switch_manager=emergent_switch).stream(
        "Do a complex thing"
    ):
        events.append(ev)

    committed = [ev for ev in events if ev.kind == "emergent_switch_committed"]
    assert committed
    assert committed[0].data["replan"]["replacement_step_ids"]
    replan_events = [
        ev
        for ev in events
        if ev.kind == "action_plan" and ev.data.get("stage") == "emergent_replan_applied"
    ]
    assert replan_events
    assert replan_events[0].data["replacement_step_ids"]
    actions = [ev.data.get("description") for ev in events if ev.kind == "action"]
    assert "按涌现方案调整后续执行: 先重排后续执行，再补一次验证" in actions
    assert "按调整后的方案重新验证结果并交付" in actions


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
    orch = Orchestrator(output_translator=_identity_translator)
    result = await orch.run("Say hi")
    assert result.status == "done"
    assert result.answer == "Hello, world!"
    assert result.task_id.startswith("tk-")
    assert result.cost_usd_equivalent == 0.0  # stub has zero prices


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_emits_budget_policy_when_task_cost_crosses_warning():
    providers = {
        "top": _CostlyRoutingStub(tier="top"),
        "cheap": _CostlyRoutingStub(tier="cheap"),
        "coding": _CostlyRoutingStub(tier="coding"),
        "fallback": _CostlyRoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))

    events = []
    async for ev in Orchestrator(output_translator=_identity_translator).stream("Say hi"):
        events.append(ev)

    budget_events = [ev for ev in events if ev.data.get("stage") == "budget_policy"]

    assert budget_events
    assert budget_events[0].data["level"] == "CRITICAL"
    assert budget_events[0].data["decision_ticket"]["decision_point"] == "budget_policy"


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

    result = await Orchestrator(output_translator=_identity_translator).run(
        "Please greet the world"
    )

    assert result.task_id == "task-existing"
    assert result.status == "done"
    assert result.answer == "cached answer"
    assert result.cost_usd_equivalent == 0.03


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_duplicate_orphan_is_marked_failed(monkeypatch):
    providers = {
        "top": _RoutingStub(tier="top"),
        "cheap": _RoutingStub(tier="cheap"),
        "coding": _RoutingStub(tier="coding"),
        "fallback": _RoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))
    persisted: list[TaskResult] = []

    async def fake_find_idempotent_result_ref(*args, **kwargs):
        return "task-orphan"

    async def fake_load_cached_task_result(*, tenant_id: str, task_id: str):
        return None

    async def fake_load_task_progress(*, tenant_id: str, task_id: str):
        return None, None

    async def fake_persist_task_result(_session, *, tenant_id: str, result: TaskResult):
        persisted.append(result)

    async def fail_if_executed(*args, **kwargs):
        raise AssertionError("orphan duplicate should not execute a new step")

    monkeypatch.setattr(
        "kun.engineering.orchestrator._find_idempotent_result_ref",
        fake_find_idempotent_result_ref,
    )
    monkeypatch.setattr(
        "kun.engineering.orchestrator._load_cached_task_result",
        fake_load_cached_task_result,
    )
    monkeypatch.setattr(
        "kun.engineering.orchestrator._load_task_progress",
        fake_load_task_progress,
    )
    monkeypatch.setattr(
        "kun.engineering.orchestrator._persist_task_result",
        fake_persist_task_result,
    )
    monkeypatch.setattr(Orchestrator, "_execute_step", fail_if_executed)

    result = await Orchestrator(output_translator=_identity_translator).run(
        "Please greet the world"
    )

    assert result.task_id == "task-orphan"
    assert result.status == "failed"
    assert "stopped during initialization" in result.answer
    assert persisted and persisted[0].status == "failed"


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

    orch = Orchestrator(output_translator=_identity_translator)
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
    assert "email.draft" in result.answer


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watchtower_pause_rule_stops_hot_execution_loop(monkeypatch):
    providers = {
        "top": _CostlyRoutingStub(tier="top"),
        "cheap": _CostlyRoutingStub(tier="cheap"),
        "coding": _CostlyRoutingStub(tier="coding"),
        "fallback": _CostlyRoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))
    rule_engine = RuleEngine(
        rules=[
            GuardRule(
                id="unit_cost_runaway",
                kind="guard",
                description="pause when cost exceeds estimate",
                trigger=RuleTrigger(
                    event_type="task.step.completed",
                    when=(
                        "event['payload'].get('accumulated_cost_usd', 0) > "
                        "event['payload'].get('estimated_cost_usd', 0) * 1.2"
                    ),
                ),
                severity="high",
                actions=[RuleAction(handler="pause_task")],
            )
        ]
    )

    orch = Orchestrator(output_translator=_identity_translator, rule_engine=rule_engine)
    events = []
    async for ev in orch.stream("Please greet the world"):
        events.append(ev)

    kinds = [ev.kind for ev in events]
    assert "guard_intervention" in kinds
    done = next(ev for ev in events if ev.kind == "done")
    result = TaskResult.model_validate(done.data["result"])
    assert result.status == "paused"
    assert "守望暂停" in result.answer


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_request_skill_pauses_and_enqueues_pending_action(monkeypatch):
    autoload_builtins()
    providers = {
        "top": _WorldRequestRoutingStub(tier="top"),
        "cheap": _WorldRequestRoutingStub(tier="cheap"),
        "coding": _WorldRequestRoutingStub(tier="coding"),
        "fallback": _WorldRequestRoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))
    captured_actions = []

    async def fake_enqueue(_session, *, tenant_id, task_ref, actions):
        captured_actions.extend(actions)

    monkeypatch.setattr("kun.engineering.orchestrator.enqueue_pending_actions", fake_enqueue)

    events = []
    async for ev in Orchestrator(output_translator=_identity_translator).stream(
        "Please draft and send an approval email"
    ):
        events.append(ev)

    done = next(ev for ev in events if ev.kind == "done")
    result = TaskResult.model_validate(done.data["result"])
    assert result.status == "paused"
    assert "外部动作审批" in result.answer
    assert [action.action_type for action in captured_actions] == ["email.draft"]
    preflight = next(
        ev
        for ev in events
        if ev.kind == "action_plan" and ev.data.get("stage") == "preflight_guard"
    )
    assert preflight.data["decision_ticket"]["decision_point"] == "preflight_guard"
    assert preflight.data["decision_ticket"]["status"] == "blocked"
    guard = next(
        ev
        for ev in events
        if ev.kind == "guard_intervention" and ev.data["stage"] == "world_action_approval"
    )
    assert guard.data["pending_actions"][0]["action_type"] == "email.draft"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_translates_final_answer_for_output_kind():
    providers = {
        "top": _RoutingStub(tier="top"),
        "cheap": _RoutingStub(tier="cheap"),
        "coding": _RoutingStub(tier="coding"),
        "fallback": _RoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))
    seen: list[dict[str, object]] = []

    async def translator(**kwargs: object) -> str:
        seen.append(dict(kwargs))
        payload = kwargs["payload"]
        assert isinstance(payload, dict)
        return f"{kwargs['recipient_kind']}::{payload['answer']}"

    result = await Orchestrator(output_translator=translator).run(
        "Say hi",
        output_kind="a2a",
    )

    assert result.answer == "a2a::Hello, world!"
    assert seen[0]["recipient_kind"] == "a2a"
    assert seen[0]["context"] == {
        "tenant_id": "u-sylvan",
        "audience": "developer",
        "language": "zh",
    }
