"""V7 Phase X.I-3-FIX — production-entry diff consumer tests.

Before X.I-3-FIX, `TaskSpec.production_entry_changes_required` was a
schema-only orphan: defined + tested for round-trip, NOT consumed.

These tests prove the consumer now fires:
  1. Pure-function `ProductionEntryDiffChecker.check_against_actual`
     produces correct verdicts in every quadrant (declared/actual mix).
  2. LongTaskOrchestrator at run completion calls the checker, emits
     `long_task.production_entry_diff` event, and persists the verdict
     into X.H.TRACE working_state.
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
from kun.governance.production_entry_diff_check import (
    ProductionEntryDiffChecker,
    ProductionEntryDiffReport,
)

pytestmark = pytest.mark.asyncio


# ============================================================
# Pure-function checker
# ============================================================


def test_check_returns_no_declaration_when_both_empty() -> None:
    r = ProductionEntryDiffChecker.check_against_actual([], [])
    assert r.verdict == "no_declaration"
    assert r.has_drift is False


def test_check_returns_drift_when_declared_unchanged() -> None:
    """Director said we'd touch X but nothing actually changed —
    this is the X.I-3 / X.H failure mode."""
    r = ProductionEntryDiffChecker.check_against_actual(
        ["kun/engineering/orchestrator.py"], []
    )
    assert r.verdict == "drift"
    assert r.has_drift is True
    assert r.declared_but_unchanged == ["kun/engineering/orchestrator.py"]


def test_check_returns_match_when_lists_align() -> None:
    r = ProductionEntryDiffChecker.check_against_actual(
        ["kun/engineering/orchestrator.py"],
        ["kun/engineering/orchestrator.py"],
    )
    assert r.verdict == "match"
    assert r.has_drift is False
    assert r.matched == ["kun/engineering/orchestrator.py"]


def test_check_returns_drift_when_partially_matched() -> None:
    """Declared 2 entries, only 1 touched + 1 surprise change → drift."""
    r = ProductionEntryDiffChecker.check_against_actual(
        ["kun/engineering/orchestrator.py", "kun/control_plane/daemon.py"],
        ["kun/engineering/orchestrator.py", "kun/api/main.py"],
    )
    assert r.verdict == "drift"
    assert r.has_drift is True
    assert r.matched == ["kun/engineering/orchestrator.py"]
    assert r.declared_but_unchanged == ["kun/control_plane/daemon.py"]
    assert r.changed_but_undeclared == ["kun/api/main.py"]


def test_check_returns_undeclared_when_actual_without_declaration() -> None:
    """No declaration but files DID change — flagged for honest review."""
    r = ProductionEntryDiffChecker.check_against_actual(
        [], ["kun/api/main.py"]
    )
    assert r.verdict == "undeclared_changes"
    assert r.changed_but_undeclared == ["kun/api/main.py"]


def test_check_trims_whitespace_and_drops_blanks() -> None:
    r = ProductionEntryDiffChecker.check_against_actual(
        ["  kun/engineering/orchestrator.py  ", ""],
        ["kun/engineering/orchestrator.py"],
    )
    assert r.verdict == "match"


# ============================================================
# LongTaskOrchestrator consumer wiring
# ============================================================


class _StubLLM:
    async def __call__(self, _: list[dict[str, Any]]) -> LLMStepResponse:
        return LLMStepResponse(
            content="done",
            tool_calls=[],
            finish_reason="end_turn",
            cost_usd=0.0,
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


def _ref(declared: list[str] | None = None) -> TaskRef:
    owner = Owner(tenant_id="t-xi3", user_id="u-xi3")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("xi3 wiring", owner),
        task_type="coding.python.fastapi",
        risk_level="medium",
        complexity="medium",
        estimated_steps=1,
        estimated_duration_sec=10.0,
        owner=owner,
        success_criteria_short="x.i-3 consumer fires",
    )
    ref = TaskRef(
        meta=meta,
        spec=TaskSpec(
            goal_detail="prove production_entry_changes_required is consumed",
            production_entry_changes_required=declared or [],
        ),
    )
    ref.goal_anchor = GoalAnchor(
        task_id=meta.task_id,
        goal_statement="x.i-3 consumer",
        success_criteria=["report emitted"],
        out_of_scope=[],
        invariants=[],
    )
    return ref


class _EventCollector:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def __call__(self, ev: Any) -> None:
        self.events.append(ev)


async def test_orchestrator_emits_diff_event_on_match() -> None:
    """Declared = actual → verdict='match'."""
    store = _Store()
    orch = LongTaskOrchestrator(
        llm_invoker=_StubLLM(),
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()
    await orch.run_long_task(
        _ref(declared=["kun/engineering/orchestrator.py"]),
        on_event=sink,
        actual_production_entry_changes=["kun/engineering/orchestrator.py"],
    )
    diff_events = [
        e for e in sink.events if e.kind == "long_task.production_entry_diff"
    ]
    assert len(diff_events) == 1
    assert diff_events[0].data["verdict"] == "match"
    assert diff_events[0].data["has_drift"] is False


async def test_orchestrator_emits_drift_event_when_declared_unchanged() -> None:
    """Declared but nothing actually changed → verdict='drift' — exactly
    the X.H/X.I-3 'wired class but no production change' failure mode."""
    store = _Store()
    orch = LongTaskOrchestrator(
        llm_invoker=_StubLLM(),
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()
    await orch.run_long_task(
        _ref(declared=["kun/engineering/orchestrator.py"]),
        on_event=sink,
        actual_production_entry_changes=[],
    )
    diff_events = [
        e for e in sink.events if e.kind == "long_task.production_entry_diff"
    ]
    assert len(diff_events) == 1
    assert diff_events[0].data["verdict"] == "drift"
    assert diff_events[0].data["has_drift"] is True
    assert (
        "kun/engineering/orchestrator.py"
        in diff_events[0].data["declared_but_unchanged"]
    )


async def test_orchestrator_writes_diff_to_runtime_trace() -> None:
    """X.H.TRACE — the verdict lands in working_state.runtime_features_used."""
    store = _Store()
    orch = LongTaskOrchestrator(
        llm_invoker=_StubLLM(),
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        enable_recursive_planner=False,
    )
    await orch.run_long_task(
        _ref(declared=["kun/engineering/orchestrator.py"]),
        actual_production_entry_changes=["kun/engineering/orchestrator.py"],
    )
    trace = orch._snapshot_runtime_features()
    assert "production_entry_diff" in trace
    assert trace["production_entry_diff"]["verdict"] == "match"
    assert trace["production_entry_diff"]["declared_count"] == 1


async def test_orchestrator_emits_no_declaration_when_field_empty() -> None:
    """When the task didn't declare and didn't change anything — honest
    'no_declaration' verdict, not silent skip."""
    store = _Store()
    orch = LongTaskOrchestrator(
        llm_invoker=_StubLLM(),
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()
    await orch.run_long_task(_ref(declared=None), on_event=sink)
    diff_events = [
        e for e in sink.events if e.kind == "long_task.production_entry_diff"
    ]
    assert len(diff_events) == 1
    assert diff_events[0].data["verdict"] == "no_declaration"


def test_report_dataclass_is_frozen() -> None:
    r = ProductionEntryDiffReport()
    with pytest.raises(Exception):
        r.verdict = "tampered"  # type: ignore[misc]
