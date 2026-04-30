from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from kun.context.packer import ContextPack
from kun.core.state_ledger import StateLedger
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.credit_assignment import (
    ResourceCreditDelta,
    get_contribution_tracker,
    reset_contribution_tracker,
)
from kun.engineering.orchestrator import Orchestrator
from kun.interface.llm import LLMRouter
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.router import set_router
from kun.interface.llm.stub_provider import StubProvider
from kun.watchtower.decision_plane import WatchtowerDecisionPlane


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

    async def flush(self):
        pass


@asynccontextmanager
async def _fake_session_scope(**_kwargs: object) -> AsyncIterator[_FakeSession]:
    yield _FakeSession()


class _RoutingStub(StubProvider):
    async def invoke(self, request):
        sys_text = " ".join(m.content for m in request.messages if m.role == "system")
        if "意图理解层" in sys_text:
            self._builder = lambda _request: LLMResponse(
                content=(
                    '{"task_type": "education.lesson", "risk_level": "low", '
                    '"complexity_score": 0.4, "estimated_cost_usd": 0.05, '
                    '"estimated_duration_sec": 20, '
                    '"success_criteria_short": "设计一节复习课", '
                    '"goal_detail": "给用户设计一节循序渐进的复习课", '
                    '"success_metrics": ["覆盖关键知识点"]}'
                ),
                usage=UsageInfo(input_tokens=5, output_tokens=30),
            )
        else:
            self._builder = lambda _request: LLMResponse(
                content="这是一节复习课方案。",
                usage=UsageInfo(input_tokens=10, output_tokens=6),
            )
        return await super().invoke(request)


class _RecordingContextPacker:
    def __init__(self) -> None:
        self.limits: list[int] = []

    async def pack(self, task_ref: TaskRef, *, tenant_id: str, limit: int) -> ContextPack:
        self.limits.append(limit)
        return ContextPack()

    async def pack_query(self, *args, **kwargs) -> ContextPack:
        return ContextPack()


def _owner() -> Owner:
    return Owner(tenant_id="u-sylvan", user_id="u-sylvan")


def _task_ref(
    *,
    task_type: str = "education.lesson",
    text: str = "设计学习计划",
    complexity: float = 0.4,
) -> TaskRef:
    owner = _owner()
    return TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint(text, owner),
            task_type=task_type,
            risk_level="low",
            complexity_score=complexity,
            owner=owner,
            success_criteria_short=text,
        ),
        spec=TaskSpec(goal_detail=text),
    )


@pytest.mark.unit
def test_decision_plane_selects_sparse_education_pack() -> None:
    task_ref = _task_ref(text="帮我设计一套数学学习计划和复习题")

    decision = WatchtowerDecisionPlane().decide(task_ref)

    assert decision.strategy_pack_id == "education"
    assert "understanding_depth" in decision.metric_dimensions
    assert "revenue_potential" not in decision.metric_dimensions
    assert decision.execution_mode == "SMART"


@pytest.mark.unit
def test_decision_plane_flags_domain_drift() -> None:
    task_ref = _task_ref(text="帮我设计课程, 但是先处理付款合同和报价")

    decision = WatchtowerDecisionPlane().decide(task_ref)

    assert decision.strategy_pack_id == "education"
    assert "education_task_contains_commercial_or_financial_terms" in decision.alert_flags


@pytest.mark.unit
def test_decision_plane_uses_mission_review_as_strategy_signal() -> None:
    task_ref = _task_ref(text="继续推进这个长期运营任务", complexity=0.1)

    decision = WatchtowerDecisionPlane().decide(
        task_ref,
        mission_strategy={
            "last_review": {
                "summary": "上一轮卡住且结果不确定",
                "budget_notes": "已经超预算，需要控制 burn",
                "risk_notes": "存在高风险外发和不可逆动作",
            }
        },
    )

    assert decision.execution_mode in {"MAX", "ENSEMBLE"}
    assert "budget_adherence" in decision.metric_dimensions
    assert "risk_followup" in decision.metric_dimensions
    assert "mission_review_budget_attention" in decision.alert_flags
    assert "mission_review_risk_attention" in decision.alert_flags
    assert decision.reward_weights["cost"] >= 0.10
    assert decision.reward_weights["risk"] > 0.05
    assert decision.metadata["mission_review_adjustments"]["reason"] == "budget+risk+uncertainty"


@pytest.mark.unit
def test_decision_plane_uses_strategy_credit_as_moe_tie_breaker() -> None:
    reset_contribution_tracker()
    try:
        get_contribution_tracker().update_from_deltas(
            {
                "strategy_pack:education": ResourceCreditDelta(
                    resource_key="strategy_pack:education",
                    resource_kind="strategy_pack",
                    resource_id="education",
                    used_count=2,
                    pass_count=2,
                    critical_count=2,
                    credit_total=2.0,
                )
            }
        )
        task_ref = _task_ref(
            task_type="general",
            text="帮我把这个学习产品做商业化方案",
        )

        decision = WatchtowerDecisionPlane().decide(task_ref)

        assert decision.strategy_pack_id == "education"
    finally:
        reset_contribution_tracker()


@pytest.mark.unit
def test_decision_plane_applies_skill_hints_to_task_spec() -> None:
    task_ref = _task_ref()
    plane = WatchtowerDecisionPlane()
    decision = plane.decide(task_ref)

    plane.apply(task_ref, decision)

    assert task_ref.meta.execution_mode == decision.execution_mode
    assert "lesson_planner" in task_ref.spec.required_skills
    assert "quiz_generator" in task_ref.spec.required_skills


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_consumes_watchtower_decision_plane(monkeypatch) -> None:
    monkeypatch.setattr("kun.engineering.orchestrator.session_scope", _fake_session_scope)
    providers = {
        "top": _RoutingStub(tier="top"),
        "cheap": _RoutingStub(tier="cheap"),
        "coding": _RoutingStub(tier="coding"),
        "fallback": _RoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))
    packer = _RecordingContextPacker()
    plane = WatchtowerDecisionPlane()
    ledger = StateLedger()

    orch = Orchestrator(
        context_packer=packer,
        decision_plane=plane,
        state_ledger=ledger,
        output_translator=_identity_translator,
    )
    events = []
    async for ev in orch.stream("帮我设计一节复习课"):
        events.append(ev)

    watchtower_events = [
        ev
        for ev in events
        if ev.kind == "action_plan" and ev.data.get("stage") == "watchtower_decision"
    ]
    assert watchtower_events
    assert watchtower_events[0].data["strategy_pack_id"] == "education"
    # education/SMART 只拉轻量上下文, 证明决策单被执行层消费, 不是只 emit 事件.
    assert packer.limits == [1]
    snapshot = ledger.snapshot(watchtower_events[0].data["task_id"])
    assert snapshot is not None
    assert snapshot.strategy_pack_id == "education"
    assert snapshot.context_limit == 1
    assert snapshot.status == "done"
    assert snapshot.current_step == snapshot.total_steps
    assert snapshot.current_step >= 1
    assert snapshot.current_model


async def _identity_translator(**kwargs: object) -> str:
    payload = kwargs["payload"]
    assert isinstance(payload, dict)
    return str(payload["answer"])
