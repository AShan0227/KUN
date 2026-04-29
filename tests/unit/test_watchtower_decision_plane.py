from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from kun.context.assets import LayeredAsset
from kun.context.packer import ContextPack
from kun.context.storage import InMemoryAssetStore
from kun.core.state_ledger import StateLedger
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.orchestrator import Orchestrator
from kun.interface.llm import LLMRouter
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.router import set_router
from kun.interface.llm.stub_provider import StubProvider
from kun.watchtower.decision_plane import WatchtowerDecisionPlane
from kun.watchtower.memory_reuse import MemoryReuseAdvisor


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
        self.boost_asset_ids: list[list[str]] = []

    async def pack(
        self,
        task_ref: TaskRef,
        *,
        tenant_id: str,
        limit: int,
        boost_asset_ids=None,
    ) -> ContextPack:
        self.limits.append(limit)
        self.boost_asset_ids.append(list(boost_asset_ids or []))
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
@pytest.mark.asyncio
async def test_memory_reuse_hint_can_switch_sparse_strategy_pack() -> None:
    store = InMemoryAssetStore()
    prior = LayeredAsset.build(
        "methodology",
        "u-sylvan",
        metadata={
            "memory_layer": "meta_decision",
            "task_id": "task-old",
            "task_type": "custom.workflow",
            "strategy_pack_id": "education",
            "skill_hints": ["lesson_planner"],
            "execution_mode": "SMART",
            "reason": "历史上这类自定义流程实际按教育路径成功",
        },
        summary="元决策: custom.workflow 走 education 策略，使用 lesson_planner。",
        tags=["v3", "meta_decision", "custom.workflow", "education"],
    )
    await store.put(prior)
    task_ref = _task_ref(task_type="custom.workflow", text="给新人设计一套入门训练流程")

    hint = await MemoryReuseAdvisor(store=store).suggest(task_ref, tenant_id="u-sylvan")
    decision = WatchtowerDecisionPlane().decide(task_ref, reuse_hint=hint)

    assert hint.recommended_strategy_pack_id == "education"
    assert hint.reuse_asset_ids == [prior.asset_id]
    assert decision.strategy_pack_id == "education"
    assert "lesson_planner" in decision.skill_hints
    assert decision.metadata["reuse_applied"] is True
    assert decision.metadata["reuse_asset_ids"] == [prior.asset_id]


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


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_feeds_memory_reuse_into_decision_and_context(monkeypatch) -> None:
    monkeypatch.setattr("kun.engineering.orchestrator.session_scope", _fake_session_scope)
    providers = {
        "top": _RoutingStub(tier="top"),
        "cheap": _RoutingStub(tier="cheap"),
        "coding": _RoutingStub(tier="coding"),
        "fallback": _RoutingStub(tier="fallback"),
    }
    set_router(LLMRouter(providers))
    store = InMemoryAssetStore()
    prior = LayeredAsset.build(
        "methodology",
        "u-sylvan",
        metadata={
            "memory_layer": "meta_decision",
            "task_id": "task-old",
            "task_type": "education.lesson",
            "strategy_pack_id": "education",
            "skill_hints": ["lesson_planner", "quiz_generator"],
            "execution_mode": "SMART",
        },
        summary="上次教育任务成功路径：先 lesson_planner，再 quiz_generator。",
        tags=["v3", "meta_decision", "education.lesson", "education"],
    )
    await store.put(prior)
    packer = _RecordingContextPacker()

    orch = Orchestrator(
        context_packer=packer,
        decision_plane=WatchtowerDecisionPlane(),
        memory_reuse_advisor=MemoryReuseAdvisor(store=store),
        output_translator=_identity_translator,
    )
    events = []
    async for ev in orch.stream("帮我设计一节复习课"):
        events.append(ev)

    watchtower_event = next(
        ev
        for ev in events
        if ev.kind == "action_plan" and ev.data.get("stage") == "watchtower_decision"
    )
    assert watchtower_event.data["memory_reuse_hint"]["reuse_asset_ids"] == [prior.asset_id]
    assert packer.boost_asset_ids == [[prior.asset_id]]


async def _identity_translator(**kwargs: object) -> str:
    payload = kwargs["payload"]
    assert isinstance(payload, dict)
    return str(payload["answer"])
