"""V7 Phase X.I-0a — EngineeringDisciplineEnforcer wired into LongTaskOrchestrator.

Before X.I-0a, kun/governance/engineering_discipline.py was a TOTAL
ORPHAN: defined but called by ZERO production code paths. The cockpit
endpoint /discipline/recent returned a hardcoded empty list.

This file asserts:
  1. When the enforcer is wired, a `long_task.discipline_report` event
     fires at run completion
  2. Without a wired enforcer, the event does NOT fire
  3. The runtime_features_trace.discipline_report field carries the
     overall score so the X.H trace path surfaces it
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.director.anchor import GoalAnchor
from kun.agents.executor.checkpoint import CheckpointStatus, TaskCheckpoint
from kun.agents.executor.exec_loop import (
    LLMStepResponse,
    ToolCall,
    ToolResult,
)
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.long_task_orchestrator import LongTaskOrchestrator
from kun.governance.engineering_discipline import (
    EngineeringDisciplineEnforcer,
)

pytestmark = pytest.mark.asyncio


class _StubLLM:
    async def __call__(self, _: list[dict[str, Any]]) -> LLMStepResponse:
        return LLMStepResponse(
            content="discipline check fixture done",
            tool_calls=[],
            finish_reason="end_turn",
            cost_usd=0.001,
            usage_tokens=10,
        )


async def _passthrough_tools(calls: list[ToolCall]) -> list[ToolResult]:
    return [ToolResult(tool_id=c.tool_id, content="ok") for c in calls]


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def writer(self, row: dict[str, Any]) -> None:
        self.rows.append(dict(row))

    async def reader(self, _t: str, _tk: str) -> TaskCheckpoint | None:
        return None

    async def marker(self, _t: str, _cp: str, _st: CheckpointStatus) -> None:
        return None


def _ref() -> TaskRef:
    owner = Owner(tenant_id="t-disc", user_id="u-disc")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("discipline wiring", owner),
        task_type="coding.python.fastapi",
        risk_level="medium",
        complexity="medium",
        estimated_steps=1,
        estimated_duration_sec=10.0,
        owner=owner,
        success_criteria_short="discipline enforcer fires",
    )
    ref = TaskRef(meta=meta, spec=TaskSpec(goal_detail="discipline fires"))
    ref.goal_anchor = GoalAnchor(
        task_id=meta.task_id,
        goal_statement="discipline wiring",
        success_criteria=["enforcer attached", "report emitted"],
        out_of_scope=[],
        invariants=[],
    )
    return ref


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, ev: Any) -> None:
        self.events.append(ev)


async def test_discipline_enforcer_emits_report_at_completion() -> None:
    """When wired, the enforcer runs after ExecutorLoop and emits the
    long_task.discipline_report event with overall_score + failed_disciplines."""
    store = _Store()
    enforcer = EngineeringDisciplineEnforcer()
    orch = LongTaskOrchestrator(
        llm_invoker=_StubLLM(),
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        discipline_enforcer=enforcer,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()
    outcome = await orch.run_long_task(_ref(), on_event=sink)

    assert outcome.loop_result.status == "final"
    reports = [
        e for e in sink.events if e.kind == "long_task.discipline_report"
    ]
    assert len(reports) == 1
    r = reports[0]
    assert "overall_score" in r.data
    assert "n_total" in r.data
    assert "failed_disciplines" in r.data
    assert isinstance(r.data["failed_disciplines"], list)


async def test_no_enforcer_means_no_discipline_report() -> None:
    """Without enforcer wired, the production path stays silent — backward
    compat with all pre-X.I-0 callers."""
    store = _Store()
    orch = LongTaskOrchestrator(
        llm_invoker=_StubLLM(),
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        # No discipline_enforcer
        enable_recursive_planner=False,
    )
    sink = _EventCollector()
    await orch.run_long_task(_ref(), on_event=sink)

    reports = [
        e for e in sink.events if e.kind == "long_task.discipline_report"
    ]
    assert reports == []


async def test_discipline_report_lands_in_runtime_features_trace() -> None:
    """X.H.TRACE field receives the discipline summary so future
    checkpoint rows know which disciplines failed."""
    store = _Store()
    enforcer = EngineeringDisciplineEnforcer()
    orch = LongTaskOrchestrator(
        llm_invoker=_StubLLM(),
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        discipline_enforcer=enforcer,
        enable_recursive_planner=False,
    )
    await orch.run_long_task(_ref())

    trace = orch._snapshot_runtime_features()
    assert "discipline_report" in trace
    assert "overall_score" in trace["discipline_report"]
    assert "failed_disciplines" in trace["discipline_report"]
