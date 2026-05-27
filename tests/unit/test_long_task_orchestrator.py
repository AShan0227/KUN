"""LongTaskOrchestrator — composition class for the LT pipeline.

These tests exercise the *composition* layer: that the 6 LT services wire
together correctly and that LoopResult.status maps cleanly to the unified
OrchestratorEvent stream (kind, data). The underlying services are unit-tested
elsewhere; here we use fakes for the *external* boundary (LLM / tools / DB)
and the real LT services in between.
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
from kun.engineering.long_task_orchestrator import (
    LongTaskOrchestrator,
    LongTaskRunOutcome,
    OrchestratorEvent,
)

# --------------------------------------------------------------------------- #
# Stubs                                                                       #
# --------------------------------------------------------------------------- #


def _final_response(text: str = "done", *, cost: float = 0.01) -> LLMStepResponse:
    return LLMStepResponse(
        content=text,
        tool_calls=[],
        finish_reason="end_turn",
        cost_usd=cost,
        usage_tokens=50,
    )


def _tool_response(
    tool_name: str = "search",
    tool_id: str = "t-1",
    *,
    cost: float = 0.002,
) -> LLMStepResponse:
    return LLMStepResponse(
        content="thinking...",
        tool_calls=[ToolCall(tool_id=tool_id, name=tool_name, arguments={})],
        finish_reason="tool_use",
        cost_usd=cost,
        usage_tokens=30,
    )


class _FakeLLM:
    """Returns responses in order; clamps to last response when exhausted."""

    def __init__(self, responses: list[LLMStepResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, Any]]] = []
        self._idx = 0

    async def __call__(self, messages: list[dict[str, Any]]) -> LLMStepResponse:
        self.calls.append([dict(m) for m in messages])
        if self._idx < len(self.responses):
            r = self.responses[self._idx]
            self._idx += 1
            return r
        return self.responses[-1]


class _RaisingLLM:
    """LLM that always raises."""

    async def __call__(self, messages: list[dict[str, Any]]) -> LLMStepResponse:
        raise RuntimeError("simulated llm explosion")


async def _passthrough_tools(calls: list[ToolCall]) -> list[ToolResult]:
    return [ToolResult(tool_id=c.tool_id, content="ok") for c in calls]


class _ExpensiveLLM:
    """Each call costs more than max_budget_usd to force budget_exceeded."""

    async def __call__(self, messages: list[dict[str, Any]]) -> LLMStepResponse:
        return LLMStepResponse(
            content="thinking...",
            tool_calls=[ToolCall(tool_id="t-x", name="ping", arguments={})],
            finish_reason="tool_use",
            cost_usd=5.0,
            usage_tokens=10,
        )


class _CheckpointStore:
    """In-memory writer + reader + status_marker for the checkpoint service."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.status_marks: list[tuple[str, str, CheckpointStatus]] = []

    async def writer(self, row: dict[str, Any]) -> None:
        self.rows.append(dict(row))

    async def reader(
        self, tenant_id: str, task_id: str
    ) -> TaskCheckpoint | None:
        return None  # resume not exercised in unit tests

    async def marker(
        self, tenant_id: str, cp_id: str, status: CheckpointStatus
    ) -> None:
        self.status_marks.append((tenant_id, cp_id, status))
        for r in self.rows:
            if r["tenant_id"] == tenant_id and r["checkpoint_id"] == cp_id:
                r["status"] = status


class _EventCollector:
    """Collects emitted events for later assertion."""

    def __init__(self) -> None:
        self.events: list[OrchestratorEvent] = []

    async def __call__(self, ev: OrchestratorEvent) -> None:
        self.events.append(ev)

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def first(self, kind: str) -> OrchestratorEvent | None:
        for e in self.events:
            if e.kind == kind:
                return e
        return None

    def all_of(self, kind: str) -> list[OrchestratorEvent]:
        return [e for e in self.events if e.kind == kind]


# --------------------------------------------------------------------------- #
# Fixture helpers                                                             #
# --------------------------------------------------------------------------- #


def _owner() -> Owner:
    return Owner(tenant_id="t-1", user_id="u-1")


def _meta(
    *,
    task_id: str | None = None,
    success_criteria_short: str = "ship the long task",
) -> TaskMeta:
    owner = _owner()
    kwargs: dict[str, Any] = {
        "fingerprint": TaskMeta.compute_fingerprint("test long task", owner),
        "task_type": "coding.python.fastapi",
        "risk_level": "high",
        "complexity": "complex",
        "estimated_steps": 8,
        "estimated_duration_sec": 1200.0,
        "owner": owner,
        "success_criteria_short": success_criteria_short,
    }
    if task_id is not None:
        kwargs["task_id"] = task_id
    return TaskMeta(**kwargs)


def _spec(
    *,
    goal_detail: str = "implement OAuth2 login with PKCE and session refresh",
    subtasks_hint: list[str] | None = None,
) -> TaskSpec:
    return TaskSpec(
        goal_detail=goal_detail,
        success_metrics=["login flow works", "token refresh works"],
        subtasks_hint=subtasks_hint or [],
    )


def _ref_with_anchor(
    *,
    task_id: str | None = None,
    spec: TaskSpec | None = None,
    goal_statement: str = "implement OAuth2 login",
    success_criteria: list[str] | None = None,
) -> TaskRef:
    meta = _meta(task_id=task_id)
    ref = TaskRef(meta=meta, spec=spec or _spec())
    ref.goal_anchor = GoalAnchor(
        task_id=meta.task_id,
        goal_statement=goal_statement,
        success_criteria=success_criteria
        or ["login flow works", "token refresh works"],
        out_of_scope=["payment integration"],
        invariants=["never log user passwords"],
    )
    return ref


def _ref_without_anchor() -> TaskRef:
    return TaskRef(meta=_meta(), spec=_spec())


def _make_orch(
    llm: Any,
    *,
    store: _CheckpointStore | None = None,
    max_steps: int = 30,
    max_budget_usd: float = 1.0,
    max_wall_seconds: float = 1800.0,
    max_consecutive_tool_failures: int = 3,
    enable_recursive_planner: bool = True,
) -> tuple[LongTaskOrchestrator, _CheckpointStore]:
    s = store or _CheckpointStore()
    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=s.writer,
        checkpoint_reader=s.reader,
        checkpoint_status_marker=s.marker,
        max_steps=max_steps,
        max_budget_usd=max_budget_usd,
        max_wall_seconds=max_wall_seconds,
        max_consecutive_tool_failures=max_consecutive_tool_failures,
        enable_recursive_planner=enable_recursive_planner,
    )
    return orch, s


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


async def test_happy_path_final_answer_emits_full_event_stream() -> None:
    """final answer → started + plan_tree + answer + completed + done."""
    llm = _FakeLLM([_final_response("here is the oauth implementation")])
    orch, _store = _make_orch(llm)
    ref = _ref_with_anchor()
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert isinstance(outcome, LongTaskRunOutcome)
    assert outcome.loop_result.status == "final"
    assert outcome.loop_result.final_text == "here is the oauth implementation"
    kinds = sink.kinds()
    assert "long_task.started" in kinds
    assert "long_task.plan_tree" in kinds
    assert "answer" in kinds
    assert "long_task.completed" in kinds
    assert "done" in kinds
    # answer payload propagates final_text
    answer_ev = sink.first("answer")
    assert answer_ev is not None
    assert answer_ev.data["text"] == "here is the oauth implementation"
    assert answer_ev.data["status"] == "final"
    # done is last
    assert sink.events[-1].kind == "done"
    assert sink.events[-1].data["cancelled"] is False


async def test_on_event_none_does_not_crash() -> None:
    """Default no-op sink: run completes, outcome returned, no exception."""
    llm = _FakeLLM([_final_response("ok")])
    orch, _store = _make_orch(llm)
    ref = _ref_with_anchor()

    outcome = await orch.run_long_task(ref, on_event=None)

    assert outcome.loop_result.status == "final"
    # events_emitted counts internal emissions even with noop sink
    assert outcome.events_emitted > 0


async def test_max_steps_emits_guard_intervention_with_stage_max_steps() -> None:
    """Loop that never returns a final → max_steps → guard_intervention."""
    # Every step uses tools (never final), forcing the loop to hit max_steps.
    llm = _FakeLLM([_tool_response(tool_id=f"t-{i}") for i in range(20)])
    orch, _store = _make_orch(llm, max_steps=2)
    ref = _ref_with_anchor()
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert outcome.loop_result.status == "max_steps"
    guard = sink.first("guard_intervention")
    assert guard is not None
    assert guard.data["stage"] == "max_steps"
    assert guard.data["level"] == "exceeded"
    assert sink.events[-1].kind == "done"
    assert sink.events[-1].data["cancelled"] is False


async def test_budget_exceeded_emits_guard_intervention_with_stage_budget() -> None:
    """Single expensive step → budget_exceeded → guard_intervention/budget."""
    orch, _store = _make_orch(
        _ExpensiveLLM(),
        max_budget_usd=1.0,  # one $5 LLM call blows past this
        max_steps=10,
    )
    ref = _ref_with_anchor()
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert outcome.loop_result.status == "budget_exceeded"
    guard = sink.first("guard_intervention")
    assert guard is not None
    assert guard.data["stage"] == "budget"
    assert sink.events[-1].kind == "done"


async def test_failed_status_emits_guard_intervention_with_stage_error() -> None:
    """LLM raises → status='failed' → guard_intervention/error."""
    orch, _store = _make_orch(_RaisingLLM())
    ref = _ref_with_anchor()
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert outcome.loop_result.status == "failed"
    guard = sink.first("guard_intervention")
    assert guard is not None
    assert guard.data["stage"] == "error"
    assert "simulated llm explosion" in guard.data["error"]
    assert sink.events[-1].kind == "done"


async def test_plan_tree_event_carries_node_count() -> None:
    """RecursivePlanner runs by default → plan_tree event has node_count > 0."""
    llm = _FakeLLM([_final_response()])
    orch, _store = _make_orch(llm)
    ref = _ref_with_anchor(spec=_spec(subtasks_hint=["s1", "s2"]))
    sink = _EventCollector()

    await orch.run_long_task(ref, on_event=sink)

    pt = sink.first("long_task.plan_tree")
    assert pt is not None
    assert pt.data["node_count"] > 0
    # leaves count should be at least the number of root_steps
    assert pt.data["leaves"] >= 2


async def test_subtasks_hint_drives_root_steps_count() -> None:
    """task_ref.spec.subtasks_hint with 3 entries → 3 top-level plan nodes."""
    llm = _FakeLLM([_final_response()])
    orch, _store = _make_orch(llm)
    ref = _ref_with_anchor(spec=_spec(subtasks_hint=["a", "b", "c"]))
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert outcome.plan_tree is not None
    # root + 3 children at minimum (all 'a'/'b'/'c' are short ≤12 chars → atomic
    # by default heuristic, so they become leaves directly)
    children = outcome.plan_tree.children_of(outcome.plan_tree.root_id)
    assert len(children) == 3


async def test_missing_goal_anchor_emits_warning_and_degrades() -> None:
    """No goal_anchor attached → warning event + run still completes."""
    llm = _FakeLLM([_final_response("done anyway")])
    orch, _store = _make_orch(llm)
    ref = _ref_without_anchor()
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert outcome.loop_result.status == "final"
    warning = sink.first("long_task.warning")
    assert warning is not None
    assert warning.data["stage"] == "goal_anchor_resolution"
    # started event records anchor_id=None
    started = sink.first("long_task.started")
    assert started is not None
    assert started.data["anchor_id"] is None


async def test_final_checkpoint_marked_via_marker_callback() -> None:
    """ExecutorLoop saves a 'final' status checkpoint on the answering step."""
    llm = _FakeLLM([_final_response("final answer here")])
    orch, store = _make_orch(llm)
    ref = _ref_with_anchor()

    outcome = await orch.run_long_task(ref, on_event=None)

    # Ensure at least one row was written with status='final'
    final_rows = [r for r in store.rows if r["status"] == "final"]
    assert len(final_rows) >= 1
    # outcome.final_checkpoint_id should match the latest checkpoint id
    assert outcome.final_checkpoint_id is not None
    assert outcome.final_checkpoint_id == final_rows[-1]["checkpoint_id"]


async def test_long_task_outcome_carries_loop_result_and_plan_tree() -> None:
    """LongTaskRunOutcome aggregates loop_result + plan_tree + checkpoint id."""
    llm = _FakeLLM([_final_response("answer")])
    orch, _store = _make_orch(llm)
    ref = _ref_with_anchor()
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert outcome.loop_result is not None
    assert outcome.plan_tree is not None
    assert outcome.final_checkpoint_id is not None
    assert outcome.events_emitted == len(sink.events)
    # outcome is frozen dataclass — cannot be mutated
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        outcome.events_emitted = 999  # type: ignore[misc]


async def test_recursive_planner_can_be_disabled() -> None:
    """enable_recursive_planner=False → no plan_tree event, outcome.plan_tree=None."""
    llm = _FakeLLM([_final_response("done")])
    orch, _store = _make_orch(llm, enable_recursive_planner=False)
    ref = _ref_with_anchor()
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert outcome.plan_tree is None
    assert sink.first("long_task.plan_tree") is None
    # started event still emitted
    assert sink.first("long_task.started") is not None
    started = sink.first("long_task.started")
    assert started is not None
    assert started.data["plan_tree_size"] == 0


async def test_initial_messages_include_anchor_render() -> None:
    """First LLM call sees the GoalAnchor pinned in the system message."""
    llm = _FakeLLM([_final_response()])
    orch, _store = _make_orch(llm)
    ref = _ref_with_anchor(goal_statement="implement OAuth2 login")
    sink = _EventCollector()

    await orch.run_long_task(ref, on_event=sink)

    assert llm.calls, "expected at least one LLM invocation"
    first_call_messages = llm.calls[0]
    system = first_call_messages[0]
    assert system["role"] == "system"
    assert "GOAL ANCHOR" in system["content"]
    assert "implement OAuth2 login" in system["content"]
    user = first_call_messages[1]
    assert user["role"] == "user"
    assert "OAuth2" in user["content"]


async def test_done_event_carries_common_fields() -> None:
    """done event payload contains task_id, status, cost, steps."""
    llm = _FakeLLM([_final_response("ok", cost=0.013)])
    orch, _store = _make_orch(llm)
    ref = _ref_with_anchor()
    sink = _EventCollector()

    await orch.run_long_task(ref, on_event=sink)

    done = sink.first("done")
    assert done is not None
    assert done.data["task_id"] == ref.meta.task_id
    assert done.data["status"] == "final"
    assert done.data["steps_taken"] >= 1
    assert done.data["total_cost_usd"] == pytest.approx(0.013, abs=1e-6)
    assert "rationale" in done.data


async def test_event_count_matches_events_emitted_outcome() -> None:
    """outcome.events_emitted equals number of emissions to the sink."""
    llm = _FakeLLM([_final_response()])
    orch, _store = _make_orch(llm)
    ref = _ref_with_anchor()
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert outcome.events_emitted == len(sink.events)
    # We expect: started, plan_tree, answer, long_task.completed, done = 5
    # (no plan_tree if recursion disabled; here it's enabled)
    assert outcome.events_emitted == 5
