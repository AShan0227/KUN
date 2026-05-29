"""LongTaskOrchestrator — composition class for the LT pipeline.

These tests exercise the *composition* layer: that the 6 LT services wire
together correctly and that LoopResult.status maps cleanly to the unified
OrchestratorEvent stream (kind, data). The underlying services are unit-tested
elsewhere; here we use fakes for the *external* boundary (LLM / tools / DB)
and the real LT services in between.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
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


# --------------------------------------------------------------------------- #
# LT.INT integration wiring tests                                             #
# (external_supervisor_service auto-wire + plan_review_writer_factory)        #
# --------------------------------------------------------------------------- #


class _StubExternalSupervisorService:
    """Stub of ExternalSupervisorService — same shape as the integration
    test's stub. Captures analyze_observation calls."""

    def __init__(self, verdict: str = "ok") -> None:
        self.verdict = verdict
        self.calls: list[dict[str, Any]] = []

    async def analyze_observation(
        self,
        *,
        obs_kind: str,
        observation_payload: dict[str, Any],
        anchor: dict[str, Any] | None = None,
        target_task_id: str | None = None,
        target_anchor_id: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append(
            {
                "obs_kind": obs_kind,
                "observation_payload": observation_payload,
                "anchor": anchor,
                "target_task_id": target_task_id,
                "target_anchor_id": target_anchor_id,
            }
        )
        return SimpleNamespace(
            observation_id="ev_l-stub-1",
            observed_at=datetime.now(UTC),
            obs_kind=obs_kind,
            target_task_id=target_task_id,
            target_anchor_id=target_anchor_id,
            verdict=self.verdict,
            rationale="stub rationale",
            recommended_action=None,
            raw_llm_content="{}",
            model_used="stub",
            extras={},
        )


async def test_external_supervisor_service_auto_wires_verify() -> None:
    """When external_supervisor_service is passed (no verify), the orchestrator
    auto-builds external_supervisor_verify via the integration adapter and
    threads it into PlanReviewService."""
    llm = _FakeLLM([_final_response()])
    store = _CheckpointStore()
    service = _StubExternalSupervisorService(verdict="concerning")

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=service,  # type: ignore[arg-type]
    )

    # Auto-wire: orchestrator should now have a non-None external verify
    # callable, and PlanReviewService should have inherited it.
    assert orch._external_supervisor_verify is not None
    assert orch._plan_review._external_verify is not None

    # Smoke: invoking the verify callable forwards to the service.
    result = await orch._external_supervisor_verify({"step": 1}, {"goal_statement": "ship it"})
    assert result.verdict == "concerning"
    assert len(service.calls) == 1
    assert service.calls[0]["obs_kind"] == "drift_check"  # adapter default

    # End-to-end run still completes cleanly (no DI conflicts).
    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=None)
    assert outcome.loop_result.status == "final"


async def test_explicit_verify_wins_over_service() -> None:
    """When both external_supervisor_verify AND external_supervisor_service
    are passed, the explicit verify wins (no auto-wiring)."""
    llm = _FakeLLM([_final_response()])
    store = _CheckpointStore()
    service = _StubExternalSupervisorService()

    async def explicit_verify(
        _self_report: dict[str, Any], _anchor: dict[str, Any] | None
    ) -> SimpleNamespace:
        return SimpleNamespace(verdict="ok", rationale="explicit")

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_verify=explicit_verify,
        external_supervisor_service=service,  # type: ignore[arg-type]
    )

    # The explicit verify is preserved (identity check).
    assert orch._external_supervisor_verify is explicit_verify

    # Calling the wired verify hits the explicit one, NOT the service.
    result = await orch._external_supervisor_verify({"x": 1}, None)
    assert result.rationale == "explicit"
    assert service.calls == []


# ---- plan_review_writer_factory wiring ---- #


class _FactoryRecorder:
    """Records factory invocations and the writer's invocations.

    factory(tenant_id, task_id, anchor_id) → writer
    writer(outcome, *, triggered_at_step, self_report=None) → review_id
    """

    def __init__(self, raise_on_write: bool = False) -> None:
        self.factory_calls: list[dict[str, Any]] = []
        self.writer_calls: list[dict[str, Any]] = []
        self._raise_on_write = raise_on_write

    def __call__(self, *, tenant_id: str, task_id: str, anchor_id: str) -> Any:
        self.factory_calls.append(
            {"tenant_id": tenant_id, "task_id": task_id, "anchor_id": anchor_id}
        )

        async def _writer(
            outcome: Any,
            *,
            triggered_at_step: int,
            self_report: dict[str, Any] | None = None,
        ) -> str:
            if self._raise_on_write:
                raise RuntimeError("simulated db write failure")
            self.writer_calls.append(
                {
                    "outcome": outcome,
                    "triggered_at_step": triggered_at_step,
                    "self_report": self_report,
                }
            )
            return f"plan_review-{len(self.writer_calls)}"

        return _writer


async def test_plan_review_writer_factory_called_with_tenant_task_anchor() -> None:
    """When plan_review_writer_factory is set, run_long_task calls it with
    (tenant_id, task_id, anchor_id) at task start."""
    llm = _FakeLLM([_final_response()])
    store = _CheckpointStore()
    recorder = _FactoryRecorder()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        plan_review_writer_factory=recorder,
        # No need for heartbeat triggers in this test — just verify factory call
    )
    ref = _ref_with_anchor(task_id="task-zzz")

    outcome = await orch.run_long_task(ref, on_event=None)

    assert outcome.loop_result.status == "final"
    assert len(recorder.factory_calls) == 1
    call = recorder.factory_calls[0]
    assert call["tenant_id"] == ref.meta.owner.tenant_id
    assert call["task_id"] == "task-zzz"
    assert call["anchor_id"] == ref.goal_anchor.anchor_id


async def test_plan_review_writer_factory_not_called_without_anchor() -> None:
    """No goal_anchor → no anchor_id → factory should NOT be invoked (we don't
    know what anchor_id to bind to)."""
    llm = _FakeLLM([_final_response()])
    store = _CheckpointStore()
    recorder = _FactoryRecorder()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        plan_review_writer_factory=recorder,
    )

    outcome = await orch.run_long_task(_ref_without_anchor(), on_event=None)

    assert outcome.loop_result.status == "final"
    assert recorder.factory_calls == []
    assert recorder.writer_calls == []


async def test_heartbeat_triggers_invoke_plan_review_writer() -> None:
    """With step_interval=1, every observe call triggers heartbeat, which
    fires the per-task emitter, which calls the writer."""
    # Two tool-step responses then final — gives at least 2 LLM iterations
    # (the tool step counts as a step; observe is called at top of each iter).
    llm = _FakeLLM(
        [
            _tool_response(tool_id="t-1"),
            _final_response("all done"),
        ]
    )
    store = _CheckpointStore()
    recorder = _FactoryRecorder()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        plan_review_writer_factory=recorder,
        plan_review_step_interval=1,
        # keep time interval large so step-threshold is the deterministic one
        plan_review_time_interval_sec=3600.0,
        enable_recursive_planner=False,  # keep test tight
    )
    ref = _ref_with_anchor(task_id="task-hb-1")

    outcome = await orch.run_long_task(ref, on_event=None)

    assert outcome.loop_result.status == "final"
    # observe runs at the top of EVERY iteration, so we expect ≥ 2 writes
    # (one per iteration with step_interval=1).
    assert len(recorder.writer_calls) >= 2
    # Each call carries a PlanReviewOutcome with internal_verdict + action.
    for call in recorder.writer_calls:
        out = call["outcome"]
        assert out.triggered is True
        assert out.internal_verdict in {"aligned", "drifting", "off_track"}
        assert out.final_verdict == out.internal_verdict
        # external_verdict is None (we don't have a real verify path here)
        assert out.external_verdict is None
        assert out.action in {
            "continue",
            "pause_for_anchor_recheck",
            "trigger_rcdh_level_0",
        }
        assert call["triggered_at_step"] >= 1


async def test_plan_review_writer_failure_does_not_break_main_path() -> None:
    """Writer raising should be logged but never propagate out of the loop."""
    llm = _FakeLLM([_final_response("done despite writer failure")])
    store = _CheckpointStore()
    recorder = _FactoryRecorder(raise_on_write=True)

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        plan_review_writer_factory=recorder,
        plan_review_step_interval=1,
        plan_review_time_interval_sec=3600.0,
        enable_recursive_planner=False,
    )
    ref = _ref_with_anchor()
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    # Even though writer raised, the main loop still produces 'final'.
    assert outcome.loop_result.status == "final"
    assert outcome.loop_result.final_text == "done despite writer failure"
    # Writer was attempted (factory was called → emitter fired → writer raised)
    assert len(recorder.factory_calls) == 1
    # Standard terminal events still emitted
    assert sink.first("answer") is not None
    assert sink.events[-1].kind == "done"


async def test_per_task_heartbeat_restored_after_run() -> None:
    """The orchestrator's shared heartbeat / plan_review references should be
    restored to the originals after run_long_task returns, so a second run
    (without factory) still uses the no-emitter wiring."""
    llm = _FakeLLM([_final_response()])
    store = _CheckpointStore()
    recorder = _FactoryRecorder()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        plan_review_writer_factory=recorder,
        plan_review_step_interval=1,
        plan_review_time_interval_sec=3600.0,
        enable_recursive_planner=False,
    )
    original_heartbeat = orch._heartbeat
    original_plan_review = orch._plan_review

    await orch.run_long_task(_ref_with_anchor(), on_event=None)

    # After run, the shared instances are restored — heartbeat NOT the per-task one
    assert orch._heartbeat is original_heartbeat
    assert orch._plan_review is original_plan_review
    assert orch._executor._plan_review is original_plan_review
    # And the original heartbeat never had an emitter wired in
    assert original_heartbeat._emitter is None


# --------------------------------------------------------------------------- #
# Critique hook tests (LT.WIRE-2 — every-N-steps direct supervisor call)      #
# --------------------------------------------------------------------------- #


class _CriticServiceStub:
    """Stub ExternalSupervisorService that lets each test pick a verdict.

    Same shape as the existing ``_StubExternalSupervisorService`` but with a
    knob to inject raises and per-call verdict sequencing so we can simulate
    the alarming-drift path independently of the heartbeat code.
    """

    def __init__(
        self,
        *,
        verdicts: list[str] | None = None,
        raise_exc: BaseException | None = None,
    ) -> None:
        # Per-call verdicts; clamps to last entry once exhausted.
        self._verdicts = list(verdicts or ["ok"])
        self._idx = 0
        self._raise = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def analyze_observation(
        self,
        *,
        obs_kind: str,
        observation_payload: dict[str, Any],
        anchor: dict[str, Any] | None = None,
        target_task_id: str | None = None,
        target_anchor_id: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append(
            {
                "obs_kind": obs_kind,
                "observation_payload": observation_payload,
                "anchor": anchor,
                "target_task_id": target_task_id,
                "target_anchor_id": target_anchor_id,
            }
        )
        if self._raise is not None:
            raise self._raise
        if self._idx < len(self._verdicts):
            verdict = self._verdicts[self._idx]
            self._idx += 1
        else:
            verdict = self._verdicts[-1]
        return SimpleNamespace(
            observation_id=f"ev_l-crit-{self._idx}",
            observed_at=datetime.now(UTC),
            obs_kind=obs_kind,
            target_task_id=target_task_id,
            target_anchor_id=target_anchor_id,
            verdict=verdict,
            rationale=f"stub rationale ({verdict})",
            recommended_action="halt and reassess" if verdict == "alarming" else None,
            raw_llm_content="{}",
            model_used="stub-critic",
            extras={},
        )


def _multi_step_responses(n_tool_steps: int) -> list[LLMStepResponse]:
    """Helper: n_tool_steps tool-call responses then a final answer."""
    out: list[LLMStepResponse] = [
        _tool_response(tool_id=f"t-{i}") for i in range(n_tool_steps)
    ]
    out.append(_final_response("all done"))
    return out


async def test_critique_hook_fires_every_n_steps() -> None:
    """critique_every_n_steps=2 + 6 main-line steps → critique fires 3 times.

    The wrapped invoker counts each LLM call as a step, so 5 tool-call steps
    + 1 final = 6 calls total; critique fires at call 2, 4, 6.
    """
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=5))  # 5 tool + 1 final = 6 calls
    store = _CheckpointStore()
    critic = _CriticServiceStub(verdicts=["ok", "ok", "ok"])

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=critic,  # type: ignore[arg-type]
        critique_every_n_steps=2,
        max_steps=10,
        enable_recursive_planner=False,
    )
    ref = _ref_with_anchor(task_id="task-crit-1")
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert outcome.loop_result.status == "final"
    # 6 LLM calls → critique at calls 2, 4, 6 → 3 supervisor calls
    assert len(critic.calls) == 3
    # All three critiques were obs_kind='critique'
    assert all(c["obs_kind"] == "critique" for c in critic.calls)
    # Each call passed the anchor
    for c in critic.calls:
        assert c["anchor"] is not None
        assert c["anchor"]["goal_statement"]
    # Three long_task.critique events emitted (one per supervisor call)
    critiques = sink.all_of("long_task.critique")
    assert len(critiques) == 3
    # call_count increments by `every` between events
    counts = [ev.data["call_count"] for ev in critiques]
    assert counts == [2, 4, 6]


async def test_critique_hook_below_threshold_does_not_fire() -> None:
    """critique_every_n_steps=10 + only 3 LLM calls → no supervisor calls."""
    # 2 tool steps + 1 final = 3 LLM calls; below threshold 10
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=2))
    store = _CheckpointStore()
    critic = _CriticServiceStub()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=critic,  # type: ignore[arg-type]
        critique_every_n_steps=10,
        max_steps=10,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    assert outcome.loop_result.status == "final"
    # No critique calls — 3 LLM calls < threshold 10
    assert critic.calls == []
    assert sink.all_of("long_task.critique") == []
    assert sink.all_of("long_task.drift_alarm") == []


async def test_critique_hook_failure_does_not_break_main_path() -> None:
    """Supervisor raising during critique is logged + swallowed; loop still wins."""
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=3))  # 4 LLM calls
    store = _CheckpointStore()
    critic = _CriticServiceStub(raise_exc=RuntimeError("local LLM down"))

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=critic,  # type: ignore[arg-type]
        critique_every_n_steps=2,
        max_steps=10,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    # Even though critique raised, the main loop completed normally.
    assert outcome.loop_result.status == "final"
    # We DID attempt the supervisor (calls were recorded before raise) —
    # one attempt at call 2, one at call 4
    assert len(critic.calls) == 2
    # No critique / drift_alarm events emitted (the raise short-circuits before emit)
    assert sink.all_of("long_task.critique") == []
    assert sink.all_of("long_task.drift_alarm") == []
    # Terminal events still happen
    assert sink.first("answer") is not None
    assert sink.events[-1].kind == "done"


async def test_critique_verdict_alarming_emits_drift_alarm_event() -> None:
    """verdict='alarming' → emits long_task.drift_alarm with rationale."""
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=1))  # 2 LLM calls
    store = _CheckpointStore()
    critic = _CriticServiceStub(verdicts=["alarming"])

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=critic,  # type: ignore[arg-type]
        critique_every_n_steps=2,
        max_steps=10,
        enable_recursive_planner=False,
    )
    ref = _ref_with_anchor(task_id="task-alarm-1")
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)

    assert outcome.loop_result.status == "final"
    # Exactly one critique call at call 2, with verdict='alarming'
    assert len(critic.calls) == 1
    # Both events emitted: critique + drift_alarm
    critiques = sink.all_of("long_task.critique")
    alarms = sink.all_of("long_task.drift_alarm")
    assert len(critiques) == 1
    assert len(alarms) == 1
    assert critiques[0].data["verdict"] == "alarming"
    assert alarms[0].data["verdict"] == "alarming"
    assert alarms[0].data["task_id"] == "task-alarm-1"
    assert "alarming" in alarms[0].data["rationale"]
    # recommended_action threads through
    assert alarms[0].data["recommended_action"] == "halt and reassess"


async def test_critique_verdict_concerning_emits_critique_only() -> None:
    """verdict='concerning' → only long_task.critique, no drift_alarm."""
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=1))
    store = _CheckpointStore()
    critic = _CriticServiceStub(verdicts=["concerning"])

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=critic,  # type: ignore[arg-type]
        critique_every_n_steps=2,
        max_steps=10,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    assert outcome.loop_result.status == "final"
    assert len(sink.all_of("long_task.critique")) == 1
    assert sink.all_of("long_task.drift_alarm") == []


async def test_critique_backward_compat_default_disabled() -> None:
    """Not passing critique_every_n_steps → hook disabled, no supervisor calls,
    even when external_supervisor_service IS wired (used for plan_review verify)."""
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=4))  # 5 LLM calls
    store = _CheckpointStore()
    critic = _CriticServiceStub()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=critic,  # type: ignore[arg-type]
        # critique_every_n_steps deliberately omitted
        max_steps=10,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    assert outcome.loop_result.status == "final"
    # No critique-mode supervisor calls. The plan_review path may or may not
    # fire its OWN supervisor calls via _external_supervisor_verify — but
    # those have obs_kind='drift_check', NOT 'critique'.
    critique_calls = [c for c in critic.calls if c["obs_kind"] == "critique"]
    assert critique_calls == []
    assert sink.all_of("long_task.critique") == []


async def test_critique_invalid_cadence_raises_at_construction() -> None:
    """critique_every_n_steps=0 → ValueError at constructor."""
    with pytest.raises(ValueError, match="critique_every_n_steps"):
        LongTaskOrchestrator(
            llm_invoker=_FakeLLM([_final_response()]),
            tool_executor=_passthrough_tools,
            checkpoint_writer=_CheckpointStore().writer,
            checkpoint_reader=_CheckpointStore().reader,
            checkpoint_status_marker=_CheckpointStore().marker,
            critique_every_n_steps=0,
        )


async def test_critique_no_service_means_hook_disabled() -> None:
    """critique_every_n_steps set but no service → hook silently disabled.

    No crash, no events, loop runs normally. Useful when the supervisor
    process is offline but the orchestrator still ships.
    """
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=3))  # 4 LLM calls
    store = _CheckpointStore()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        # No external_supervisor_service wired
        critique_every_n_steps=2,
        max_steps=10,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    assert outcome.loop_result.status == "final"
    # No critique events emitted (no service to call)
    assert sink.all_of("long_task.critique") == []
    assert sink.all_of("long_task.drift_alarm") == []


async def test_critique_skipped_when_anchor_missing() -> None:
    """No goal_anchor → critique threshold still counts but supervisor is not called.

    The render rejects empty anchors; we degrade by skipping the call (the
    long_task.warning event already alerts the caller about the missing anchor).
    """
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=3))  # 4 LLM calls
    store = _CheckpointStore()
    critic = _CriticServiceStub()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=critic,  # type: ignore[arg-type]
        critique_every_n_steps=2,
        max_steps=10,
        enable_recursive_planner=False,
    )

    outcome = await orch.run_long_task(_ref_without_anchor(), on_event=None)

    assert outcome.loop_result.status == "final"
    # Anchor missing → supervisor never invoked
    assert critic.calls == []


async def test_critique_observation_payload_carries_last_steps() -> None:
    """The supervisor receives the last K step summaries inside observation_payload."""
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=3))  # 4 LLM calls
    store = _CheckpointStore()
    critic = _CriticServiceStub(verdicts=["ok", "ok"])

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=critic,  # type: ignore[arg-type]
        critique_every_n_steps=2,
        max_steps=10,
        enable_recursive_planner=False,
    )

    await orch.run_long_task(_ref_with_anchor(), on_event=None)

    # Two critique calls (at call_count 2 and 4)
    assert len(critic.calls) == 2
    for c in critic.calls:
        payload = c["observation_payload"]
        assert "critique_prompt" in payload
        assert "last_steps" in payload
        assert "call_count" in payload
        # The critique_prompt is the rendered template
        assert "EXTERNAL CRITIC" in payload["critique_prompt"]
        # last_steps slice = critique_every_n_steps entries
        assert len(payload["last_steps"]) == 2
    # Call counts grow
    assert critic.calls[0]["observation_payload"]["call_count"] == 2
    assert critic.calls[1]["observation_payload"]["call_count"] == 4


async def test_critique_llm_invoker_restored_after_run() -> None:
    """After run_long_task, the executor's _llm reference is restored to the
    original (un-wrapped) invoker — so a subsequent run with a different
    config doesn't accumulate wrappers."""
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=1))
    store = _CheckpointStore()
    critic = _CriticServiceStub()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=critic,  # type: ignore[arg-type]
        critique_every_n_steps=2,
        enable_recursive_planner=False,
    )
    original_llm = orch._executor._llm

    await orch.run_long_task(_ref_with_anchor(), on_event=None)

    # After run, the wrapper is gone — back to the original llm_invoker
    assert orch._executor._llm is original_llm


# --------------------------------------------------------------------------- #
# Trifecta wiring (V7 §12.4 — X.E.TRIFECTA-WIRING)                            #
# --------------------------------------------------------------------------- #


def _stub_trifecta_hooks(
    past_cost: float = 0.001,
    present_cost: float = 0.002,
    future_cost: float = 0.005,
) -> tuple[Any, Any, Any]:
    async def _past(
        _task_id: str, _recent: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        return ([{"finding": "past-stub"}], past_cost, None)

    async def _present(
        _task_id: str, _cur: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        return ([{"critique": "present-stub"}], present_cost, None)

    async def _future(
        _task_id: str, _plan: dict[str, Any], n: int
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        return (
            [{"candidate": f"f-{i}"} for i in range(n)],
            future_cost,
            None,
        )

    return _past, _present, _future


def _make_trifecta_coordinator(*, past=None, present=None, future=None):
    from kun.agents.trifecta import TrifectaCoordinator

    p, q, r = _stub_trifecta_hooks()
    return TrifectaCoordinator(
        past_hook=past or p,
        present_hook=present or q,
        future_hook=future or r,
    )


async def test_trifecta_hook_fires_every_n_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trifecta_every_n_steps=2 + 6 main-line steps → trifecta fires 3 times."""
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=5))  # 6 LLM calls total
    store = _CheckpointStore()
    coord = _make_trifecta_coordinator()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        trifecta_coordinator=coord,
        trifecta_every_n_steps=2,
        max_steps=10,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(_ref_with_anchor(task_id="task-tri-1"), on_event=sink)

    assert outcome.loop_result.status == "final"
    ticks = sink.all_of("long_task.trifecta_tick")
    assert len(ticks) == 3
    call_counts = [ev.data["call_count"] for ev in ticks]
    assert call_counts == [2, 4, 6]
    # All 3 lines fired and reported OK in the synthetic stubs
    for ev in ticks:
        assert ev.data["past_state"] == "ok"
        assert ev.data["present_state"] == "ok"
        assert ev.data["future_state"] == "ok"
        assert ev.data["n_findings"] == 1 + 1 + 3  # past + present + 3 future candidates
        assert ev.data["any_line_failed"] is False


async def test_trifecta_hook_below_threshold_does_not_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """trifecta_every_n_steps=10 + only 3 LLM calls → no trifecta tick fires."""
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=2))  # 3 LLM calls
    store = _CheckpointStore()
    coord = _make_trifecta_coordinator()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        trifecta_coordinator=coord,
        trifecta_every_n_steps=10,
        max_steps=10,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    assert outcome.loop_result.status == "final"
    assert sink.all_of("long_task.trifecta_tick") == []


async def test_trifecta_hook_failure_does_not_kill_main_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A line's hook raising → tick fires but reports FAILED state; main loop wins."""
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")

    async def _boom_past(
        _task_id: str, _recent: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        raise RuntimeError("simulated past-line crash")

    coord = _make_trifecta_coordinator(past=_boom_past)
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=1))  # 2 LLM calls
    store = _CheckpointStore()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        trifecta_coordinator=coord,
        trifecta_every_n_steps=2,
        max_steps=10,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    # Main loop completed
    assert outcome.loop_result.status == "final"
    # Trifecta tick still emitted, but flags failure
    ticks = sink.all_of("long_task.trifecta_tick")
    assert len(ticks) == 1
    assert ticks[0].data["past_state"] == "failed"
    assert ticks[0].data["any_line_failed"] is True
    # Line-failed event also emitted
    failed = sink.all_of("long_task.trifecta_line_failed")
    assert len(failed) == 1
    assert "simulated past-line crash" in (failed[0].data["past_error"] or "")


async def test_trifecta_env_master_off_does_not_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KUN_V7_TRIFECTA_ENABLED unset → coordinator runs all-DISABLED, no real fires."""
    monkeypatch.delenv("KUN_V7_TRIFECTA_ENABLED", raising=False)
    coord = _make_trifecta_coordinator()
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=3))  # 4 LLM calls
    store = _CheckpointStore()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        trifecta_coordinator=coord,
        trifecta_every_n_steps=2,
        max_steps=10,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    # Wrapper still calls coord.run(), but coord returns all-DISABLED
    # (master switch off), so emitted ticks all have state='disabled'
    assert outcome.loop_result.status == "final"
    ticks = sink.all_of("long_task.trifecta_tick")
    # 2 ticks (at calls 2 and 4), each with all 3 lines DISABLED
    assert len(ticks) == 2
    for ev in ticks:
        assert ev.data["past_state"] == "disabled"
        assert ev.data["present_state"] == "disabled"
        assert ev.data["future_state"] == "disabled"
        assert ev.data["n_findings"] == 0


async def test_trifecta_no_coordinator_means_no_wrapping() -> None:
    """trifecta_coordinator=None disables the hook entirely (no wrapping)."""
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=3))
    store = _CheckpointStore()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        trifecta_every_n_steps=2,  # cadence set, but no coordinator
        max_steps=10,
        enable_recursive_planner=False,
    )
    original_llm = orch._executor._llm
    sink = _EventCollector()

    await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    # No trifecta wrapping happened (coord=None short-circuits the activation)
    assert orch._executor._llm is original_llm
    assert sink.all_of("long_task.trifecta_tick") == []


async def test_trifecta_wrapper_restored_after_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After run_long_task, _executor._llm is back to the original (no wrapper leak)."""
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=2))  # 3 LLM calls
    store = _CheckpointStore()
    coord = _make_trifecta_coordinator()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        trifecta_coordinator=coord,
        trifecta_every_n_steps=2,
        enable_recursive_planner=False,
    )
    original_llm = orch._executor._llm

    await orch.run_long_task(_ref_with_anchor(), on_event=None)

    assert orch._executor._llm is original_llm


async def test_trifecta_invalid_every_n_steps_raises_at_construction() -> None:
    """trifecta_every_n_steps=0 → ValueError at __init__."""
    store = _CheckpointStore()
    llm = _FakeLLM([_final_response("done")])

    with pytest.raises(ValueError, match="trifecta_every_n_steps must be >= 1"):
        LongTaskOrchestrator(
            llm_invoker=llm,
            tool_executor=_passthrough_tools,
            checkpoint_writer=store.writer,
            checkpoint_reader=store.reader,
            checkpoint_status_marker=store.marker,
            trifecta_coordinator=_make_trifecta_coordinator(),
            trifecta_every_n_steps=0,
            enable_recursive_planner=False,
        )


async def test_trifecta_and_critique_can_stack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Both hooks active simultaneously: 6 LLM calls, critique@2,4,6 + trifecta@3,6."""
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")
    llm = _FakeLLM(_multi_step_responses(n_tool_steps=5))  # 6 LLM calls
    store = _CheckpointStore()
    critic = _CriticServiceStub(verdicts=["ok", "ok", "ok"])
    coord = _make_trifecta_coordinator()

    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        external_supervisor_service=critic,  # type: ignore[arg-type]
        critique_every_n_steps=2,
        trifecta_coordinator=coord,
        trifecta_every_n_steps=3,
        max_steps=10,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    assert outcome.loop_result.status == "final"
    # Critique at calls 2, 4, 6
    critiques = sink.all_of("long_task.critique")
    assert [ev.data["call_count"] for ev in critiques] == [2, 4, 6]
    # Trifecta at calls 3, 6
    ticks = sink.all_of("long_task.trifecta_tick")
    assert [ev.data["call_count"] for ev in ticks] == [3, 6]


# --------------------------------------------------------------------------- #
# Methodology runtime injection (V7 §12 RSI — X.G.RSI-CLOSED-LOOP)             #
# --------------------------------------------------------------------------- #


def _make_methodology_selector(tmp_dir):
    from kun.engineering.methodology_runtime_loader import (
        MethodologyRuntimeSelector,
        load_methodologies,
    )

    return MethodologyRuntimeSelector(load_methodologies(tmp_dir))


async def test_methodology_inject_appends_to_system_prompt_and_emits_event(
    tmp_path,
) -> None:
    """When a selector is wired + a yaml seed matches the task, the system
    prompt sent to the LLM contains the rendered methodology block AND
    we emit long_task.methodology_injected."""
    seeds = tmp_path / "methodologies"
    seeds.mkdir()
    (seeds / "test_seed.yaml").write_text(
        """
topic: testing-acceptance
title: real PG e2e proves wiring not just unit
description: chain real PG e2e tests to prove production wiring closure
trigger:
  - condition: new database-backed subsystem
action:
  - write one ultimate e2e that walks all subsystems
  - assert row counts after the run
applicability:
  - V7 Phase X.B / X.C wiring proofs
confidence: high
""",
        encoding="utf-8",
    )

    selector = _make_methodology_selector(seeds)
    assert selector.n_entries == 1

    llm = _FakeLLM([_final_response("done")])
    store = _CheckpointStore()
    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        methodology_selector=selector,
        methodology_top_k=1,
        enable_recursive_planner=False,
    )
    ref = _ref_with_anchor(
        goal_statement="prove production wiring closure via real PG e2e",
        success_criteria=["real PG row delta", "e2e wiring proof"],
    )
    sink = _EventCollector()

    outcome = await orch.run_long_task(ref, on_event=sink)
    assert outcome.loop_result.status == "final"

    # The first LLM call's system message must contain the injected block
    assert len(llm.calls) >= 1
    system_msg = next((m for m in llm.calls[0] if m.get("role") == "system"), None)
    assert system_msg is not None
    assert "RSI 进化产物" in system_msg["content"]
    assert "real PG e2e proves wiring not just unit" in system_msg["content"]

    # Event was emitted
    injected = sink.all_of("long_task.methodology_injected")
    assert len(injected) == 1
    assert injected[0].data["n_methodologies"] == 1
    assert injected[0].data["methodologies"][0]["title"].startswith("real PG")


async def test_methodology_inject_skipped_when_no_match(tmp_path) -> None:
    """If no methodology matches the task context, nothing is injected and
    no event is emitted — but main task still completes normally."""
    seeds = tmp_path / "methodologies"
    seeds.mkdir()
    (seeds / "unrelated.yaml").write_text(
        """
topic: quantum-physics
title: schrodinger wavefunction collapse pattern
description: handle wave function collapse in quantum experiments
trigger:
  - condition: building quantum interpreter
action:
  - do not use in classical computing
applicability:
  - quantum compilers
confidence: low
""",
        encoding="utf-8",
    )
    selector = _make_methodology_selector(seeds)

    llm = _FakeLLM([_final_response("done")])
    store = _CheckpointStore()
    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        methodology_selector=selector,
        methodology_top_k=3,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()
    outcome = await orch.run_long_task(_ref_with_anchor(), on_event=sink)

    assert outcome.loop_result.status == "final"
    # No injection event
    assert sink.all_of("long_task.methodology_injected") == []
    # System prompt did NOT contain the RSI header
    system_msg = next((m for m in llm.calls[0] if m.get("role") == "system"), None)
    assert system_msg is not None
    assert "RSI 进化产物" not in system_msg["content"]


async def test_methodology_top_k_zero_disables_inject(tmp_path) -> None:
    """methodology_top_k=0 disables injection even if selector is wired."""
    seeds = tmp_path / "methodologies"
    seeds.mkdir()
    (seeds / "match.yaml").write_text(
        """
topic: testing-acceptance
title: real PG wiring closure
description: real PG e2e test pattern
trigger:
  - condition: new test
action:
  - write e2e
applicability:
  - V7 Phase X.G
confidence: high
""",
        encoding="utf-8",
    )
    selector = _make_methodology_selector(seeds)
    assert selector.n_entries == 1

    llm = _FakeLLM([_final_response("done")])
    store = _CheckpointStore()
    orch = LongTaskOrchestrator(
        llm_invoker=llm,
        tool_executor=_passthrough_tools,
        checkpoint_writer=store.writer,
        checkpoint_reader=store.reader,
        checkpoint_status_marker=store.marker,
        methodology_selector=selector,
        methodology_top_k=0,
        enable_recursive_planner=False,
    )
    sink = _EventCollector()
    outcome = await orch.run_long_task(
        _ref_with_anchor(goal_statement="real PG wiring"), on_event=sink
    )
    assert outcome.loop_result.status == "final"
    assert sink.all_of("long_task.methodology_injected") == []


async def test_methodology_negative_top_k_raises_at_construction(tmp_path) -> None:
    store = _CheckpointStore()
    llm = _FakeLLM([_final_response("done")])
    with pytest.raises(ValueError, match="methodology_top_k must be >= 0"):
        LongTaskOrchestrator(
            llm_invoker=llm,
            tool_executor=_passthrough_tools,
            checkpoint_writer=store.writer,
            checkpoint_reader=store.reader,
            checkpoint_status_marker=store.marker,
            methodology_top_k=-1,
            enable_recursive_planner=False,
        )
