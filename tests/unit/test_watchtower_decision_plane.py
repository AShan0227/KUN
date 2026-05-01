from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from kun.context.assets import AssetLayer, LayeredAsset
from kun.context.packer import ContextPack
from kun.context.storage import InMemoryAssetStore
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
from kun.memory.similar_task_recall import (
    SimilarTaskExperience,
    recall_similar_task_experiences,
    summarize_execution_process_experiences,
    summarize_strategy_votes,
)
from kun.watchtower.decision_plane import WatchtowerDecisionPlane, load_qi_shadow_strategy_packs


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

    async def pack(
        self, task_ref: TaskRef, *, tenant_id: str, limit: int, **_: object
    ) -> ContextPack:
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
            },
            tenant_id="u-sylvan",
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
@pytest.mark.asyncio
async def test_similar_task_recall_extracts_strategy_votes_from_memory() -> None:
    store = InMemoryAssetStore()
    task_ref = _task_ref(
        task_type="business.growth",
        text="给产品做商业化增长方案",
        complexity=0.5,
    )
    await store.put(
        LayeredAsset.build(
            "memory",
            "u-sylvan",
            metadata={
                "memory_layer": "task_result",
                "task_type": "business.growth",
                "status": "done",
                "validation_outcome": "pass",
                "score_overall": 0.92,
                "strategy_pack_id": "commercialization",
            },
            summary="任务结果: 商业化增长方案成功, 转化路径清晰。",
            layer=AssetLayer.L2_PROJECT,
            tags=["v3", "task_result", "business.growth", "commercialization"],
        )
    )

    experiences = await recall_similar_task_experiences(
        tenant_id="u-sylvan",
        task_ref=task_ref,
        store=store,
    )

    assert experiences
    assert experiences[0].strategy_pack_id == "commercialization"
    assert experiences[0].positive_weight > 0
    assert summarize_strategy_votes(experiences)["commercialization"] > 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_similar_task_recall_returns_execution_process_experience() -> None:
    store = InMemoryAssetStore()
    task_ref = _task_ref(
        task_type="coding.python.pytest",
        text="修复 pytest 失败并输出回归报告",
    )
    process = LayeredAsset.build(
        "memory",
        "u-sylvan",
        metadata={
            "memory_layer": "execution_process",
            "task_type": "coding.python.pytest",
            "step_id": 2,
            "skill_used": "coding-pytest",
            "provider": "stub",
            "model": "gpt-test",
            "tier": "cheap",
            "cost_usd": 0.02,
        },
        summary="执行过程: step=2; skill=coding-pytest; 先复现 pytest 失败，再最小修复并回归。",
        layer=AssetLayer.L1_TASK,
        tags=["v3", "execution_process", "coding.python.pytest", "coding-pytest", "pytest"],
    )
    await store.put(process)

    experiences = await recall_similar_task_experiences(
        tenant_id="u-sylvan",
        task_ref=task_ref,
        store=store,
    )

    assert experiences[0].memory_layer == "execution_process"
    assert experiences[0].skill_used == "coding-pytest"
    assert experiences[0].model == "gpt-test"
    process_summaries = summarize_execution_process_experiences(experiences)
    assert process_summaries
    assert "先复现 pytest 失败" in process_summaries[0]


@pytest.mark.unit
def test_decision_plane_uses_similar_experience_as_moe_signal() -> None:
    task_ref = _task_ref(task_type="general", text="帮我想一个新业务方案")
    decision = WatchtowerDecisionPlane().decide(
        task_ref,
        similar_experiences=[
            SimilarTaskExperience(
                asset_id="mm-1",
                memory_layer="task_result",
                task_type="business.growth",
                summary="上一轮类似任务商业化策略效果最好。",
                strategy_pack_id="commercialization",
                validation_outcome="pass",
                score_overall=0.95,
                similarity_score=0.9,
                reason="text_overlap",
            )
        ],
    )

    assert decision.strategy_pack_id == "commercialization"
    assert decision.metadata["similar_experience_count"] == 1
    assert decision.metadata["similar_strategy_votes"]["commercialization"] > 0
    assert "similar_experience=commercialization" in decision.reason


@pytest.mark.unit
def test_decision_plane_turns_execution_process_memory_into_skill_hint() -> None:
    task_ref = _task_ref(task_type="coding.python.pytest", text="修复 pytest 失败并回归")
    decision = WatchtowerDecisionPlane().decide(
        task_ref,
        similar_experiences=[
            SimilarTaskExperience(
                asset_id="proc-good",
                memory_layer="execution_process",
                task_type="coding.python.pytest",
                summary="上一轮先复现 pytest，再最小修复。",
                skill_used="coding-pytest",
                validation_outcome="pass",
                score_overall=0.9,
                similarity_score=0.95,
                step_id=2,
            ),
            SimilarTaskExperience(
                asset_id="proc-failed",
                memory_layer="execution_process",
                task_type="coding.python.pytest",
                summary="失败路径不应该强化。",
                skill_used="broad-refactor",
                validation_outcome="fail",
                score_overall=0.1,
                similarity_score=1.0,
                step_id=1,
            ),
        ],
    )

    assert "coding-pytest" in decision.skill_hints
    assert "broad-refactor" not in decision.skill_hints
    assert decision.metadata["process_experience_skill_hints"] == ["coding-pytest"]
    assert "process_skill_hints=coding-pytest" in decision.reason


@pytest.mark.unit
@pytest.mark.asyncio
async def test_qi_shadow_strategy_pack_is_observed_not_applied() -> None:
    store = InMemoryAssetStore()
    await store.put(
        LayeredAsset.build(
            "methodology",
            "u-sylvan",
            metadata={
                "source": "qi.idle_replay.strategy_pack_draft",
                "draft_id": "draft-growth",
                "proposed_pack_id": "qi_growth_variant",
                "qi_review_status": "ready_for_human_review",
                "qi_rollout_plan_status": "shadow_plan",
                "production_action": False,
                "strategy_pack_draft": {
                    "proposed_pack_id": "qi_growth_variant",
                    "display_name": "增长影子策略",
                    "task_type_patterns": ["marketing*", "growth*"],
                    "keyword_triggers": ["转化", "获客"],
                    "skill_hints": ["ad_writer"],
                    "metric_dimensions": ["conversion_lift"],
                    "risk_watch": ["overclaim"],
                    "default_execution_mode": "MAX",
                    "reward_weights": {"quality": 0.5, "cost": 0.1},
                },
            },
            summary="启生成的增长策略影子候选",
            layer=AssetLayer.L2_PROJECT,
            tags=["strategy_pack_draft", "qi_rollout:shadow_plan"],
        )
    )
    task_ref = _task_ref(
        task_type="marketing.campaign",
        text="写一个获客转化方案",
    )

    shadow_packs = await load_qi_shadow_strategy_packs(tenant_id="u-sylvan", store=store)
    decision = WatchtowerDecisionPlane().decide(task_ref, shadow_packs=shadow_packs)

    assert decision.strategy_pack_id == "commercialization"
    assert "ad_writer" not in decision.skill_hints
    candidates = decision.metadata["qi_shadow_strategy_candidates"]
    assert candidates
    assert candidates[0]["pack_id"] == "qi_shadow:qi_growth_variant"
    assert candidates[0]["shadow_only"] is True
    assert candidates[0]["production_action"] is False
    assert candidates[0]["would_execution_mode"] == "MAX"


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
