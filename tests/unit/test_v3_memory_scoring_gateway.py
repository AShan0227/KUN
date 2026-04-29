"""V3 remaining core loop tests: memory writeback, scoring, World Gateway."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kun.context.packer import ContextPacker
from kun.context.storage import InMemoryAssetStore
from kun.datamodel.runtime import RuntimeState, StepRecord
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.orchestrator import Orchestrator
from kun.interface.llm import LLMRouter
from kun.interface.llm.base import LLMResponse, UsageInfo
from kun.interface.llm.router import set_router
from kun.interface.llm.stub_provider import StubProvider
from kun.memory.writeback import MemoryWriteback, MemoryWritebackResult
from kun.watchtower.decision_plane import WatchtowerDecisionPlane
from kun.watchtower.scoring import UnifiedScoringSystem
from kun.world.gateway import WorldAction, WorldGateway


class _FakeSession:
    async def execute(self, *_args: object, **_kwargs: object) -> Any:
        class R:
            rowcount = 1

            def scalar_one_or_none(self) -> object | None:
                return None

            def scalar_one(self) -> int:
                return 0

            def all(self) -> list[object]:
                return []

            def one_or_none(self) -> object | None:
                return None

            def scalars(self) -> _FakeSession:
                return self

        return R()

    def add(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def flush(self) -> None:
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
                    '"goal_detail": "给用户设计一节复习课", '
                    '"success_metrics": ["覆盖关键知识点"]}'
                ),
                usage=UsageInfo(input_tokens=5, output_tokens=25),
            )
        else:
            self._builder = lambda _request: LLMResponse(
                content="复习课方案已完成。",
                usage=UsageInfo(input_tokens=10, output_tokens=6),
                model="stub-v3",
                provider="stub",
                tier="cheap",
            )
        return await super().invoke(request)


class _RecordingMemoryWriteback:
    def __init__(self) -> None:
        self.layers: list[str] = []

    async def record_meta_decision(self, **_kwargs: object) -> MemoryWritebackResult:
        self.layers.append("meta_decision")
        return MemoryWritebackResult(
            asset_id="mm-meta",
            memory_layer="meta_decision",
            asset_kind="methodology",
            summary="meta",
        )

    async def record_process_step(self, **_kwargs: object) -> MemoryWritebackResult:
        self.layers.append("execution_process")
        return MemoryWritebackResult(
            asset_id="mm-process",
            memory_layer="execution_process",
            asset_kind="memory",
            summary="process",
        )

    async def record_task_result(self, **_kwargs: object) -> MemoryWritebackResult:
        self.layers.append("task_result")
        return MemoryWritebackResult(
            asset_id="mm-result",
            memory_layer="task_result",
            asset_kind="memory",
            summary="result",
        )


def _task_ref() -> TaskRef:
    owner = Owner(tenant_id="tenant-v3", user_id="u")
    return TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("设计学习计划", owner),
            task_type="education.lesson",
            risk_level="low",
            complexity_score=0.4,
            owner=owner,
            estimated_cost_usd=0.1,
            estimated_duration_sec=30,
            success_criteria_short="设计学习计划",
        ),
        spec=TaskSpec(
            goal_detail="给用户设计学习计划",
            required_skills=["lesson_planner"],
            success_metrics=["覆盖知识点"],
        ),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_memory_writeback_assets_are_retrievable_by_context_packer() -> None:
    store = InMemoryAssetStore()
    writeback = MemoryWriteback(store=store)
    task_ref = _task_ref()
    runtime = RuntimeState(task_ref=task_ref.meta.task_id, status="done")
    step = StepRecord(step_id=1, skill_used="lesson_planner", cost_usd_equivalent=0.02)
    runtime.accumulate_step(step)

    await writeback.record_process_step(
        tenant_id="tenant-v3",
        task_ref=task_ref,
        step=step,
        answer="学习计划包括复习、练习和测验。",
        provider="stub",
        model="stub",
        tier="cheap",
    )
    await writeback.record_task_result(
        tenant_id="tenant-v3",
        task_ref=task_ref,
        status="done",
        answer="学习计划已完成。",
        runtime=runtime,
        validation_outcome="pass",
        validation_score=0.9,
        surprise_score=0.2,
        score_overall=0.88,
    )

    pack = await ContextPacker(store=store).pack(task_ref, tenant_id="tenant-v3", limit=5)

    assert {item.asset_kind for item in pack.items} == {"memory"}
    assert any("任务结果" in item.summary for item in pack.items)
    assert any("执行过程" in item.summary for item in pack.items)


@pytest.mark.unit
def test_unified_scorecard_uses_real_runtime_signals() -> None:
    task_ref = _task_ref()
    runtime = RuntimeState(task_ref=task_ref.meta.task_id, status="done")
    runtime.accumulate_step(
        StepRecord(step_id=1, skill_used="lesson_planner", cost_usd_equivalent=0.02)
    )
    decision = WatchtowerDecisionPlane().decide(task_ref)

    scorecard = UnifiedScoringSystem().score_task(
        task_ref=task_ref,
        runtime=runtime,
        status="done",
        validation_outcome="pass",
        validation_score=0.9,
        surprise_score=0.3,
        decision=decision,
    )

    assert scorecard.strategy_pack_id == "education"
    assert scorecard.overall > 0.7
    assert set(scorecard.metrics) >= {"success_rate", "cost", "risk", "reuse_value"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_orchestrator_consumes_memory_writeback_and_scorecard(monkeypatch) -> None:
    monkeypatch.setattr("kun.engineering.orchestrator.session_scope", _fake_session_scope)
    stub = _RoutingStub(tier="top", latency_ms=0)
    set_router(LLMRouter({"top": stub, "cheap": stub, "fallback": stub, "coding": stub}))
    memory = _RecordingMemoryWriteback()
    orch = Orchestrator(
        decision_plane=WatchtowerDecisionPlane(),
        memory_writeback=memory,
        scoring_system=UnifiedScoringSystem(),
        output_translator=_identity_translator,
    )

    events = []
    async for event in orch.stream("帮我设计一节复习课"):
        events.append(event)

    assert {"meta_decision", "execution_process", "task_result"} <= set(memory.layers)
    assert any(event.kind == "scorecard" for event in events)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_world_gateway_records_audit_without_fake_external_dispatch() -> None:
    gateway = WorldGateway()

    result = await gateway.execute_approved(
        WorldAction(
            action_id="act-1",
            task_ref="task-1",
            action_type="message.send",
            target_ref="customer:1",
            risk_level="high",
            payload={"body": "hello"},
        )
    )

    assert result.external_dispatched is False
    assert result.requires_handler is True
    assert result.audit["target"] == "api"
    assert "no delivery handler" in result.audit["reason"]


async def _identity_translator(**kwargs: object) -> str:
    payload = kwargs["payload"]
    assert isinstance(payload, dict)
    return str(payload["answer"])
