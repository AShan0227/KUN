"""LongTaskOrchestrator — composition class for the LT pipeline (ADR-022).

This wires the six long-task service modules into a single runnable entry
that the main `Orchestrator` can branch to when `_is_long_task(task_ref.meta)`
is True. The result mirrors the short-task `Orchestrator.stream(...)` shape so
the WebSocket protocol stays unified: every step emits an
``OrchestratorEvent(kind, data)`` exactly like the short path.

Architecture (ADR-022 Layer 2-5):
  Layer 2 (anchor pinning)      — GoalAnchor.render_for_system_prompt() at top of system prompt
  Layer 3 (input routing)       — LongTaskInputRouter (used by callers before route() entry)
  Layer 4 (plan-review heartbeat) — PlanReviewHeartbeat + PlanReviewService
  Layer 5 (anti-sycophancy)     — embedded in ExecutorLoop's system prompt (caller side)

Pipeline:
  1. Build initial messages = [system(anchor render), user(goal_detail)]
  2. (Optional) RecursivePlanner.expand(...) → PlanTree; emit "long_task.plan_tree"
  3. Emit "long_task.started"
  4. ExecutorLoop.run(...) — multi-step agent loop with checkpoint + compaction +
     plan-review already integrated. Returns LoopResult.
  5. Map LoopResult.status → terminal OrchestratorEvent(s)
  6. Return LongTaskRunOutcome (loop_result + plan_tree + final_checkpoint_id +
     events_emitted)

DI throughout — every external concern (LLM, tools, DB writers, External
Supervisor, summarizer, sub_planner, atomic_decider) is an injected callback.
Defaults are exposed as module-level constants matching ADR-022.

OrchestratorEvent strategy: a local lightweight ``OrchestratorEvent`` (pydantic
BaseModel with identical ``kind: str, data: dict`` shape as
``kun.engineering.orchestrator.OrchestratorEvent``) is defined here. This
avoids pulling in the orchestrator's heavy import graph (sqlalchemy + watchtower
+ skills) while keeping the on-wire contract identical. Integration (LT.INT-F)
will swap to the real type or define a converter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from kun.agents.director.recursive_planner import (
    AtomicDecider,
    PlanStepInput,
    PlanTree,
    RecursivePlanner,
    SubPlanner,
)
from kun.agents.executor.checkpoint import (
    CheckpointReader,
    CheckpointStatusMarker,
    CheckpointWriter,
    TaskCheckpointService,
)
from kun.agents.executor.compaction import ConversationCompactor, Summarizer
from kun.agents.executor.exec_loop import (
    ExecutorLoop,
    LLMInvoker,
    LoopResult,
    ToolExecutor,
)
from kun.agents.supervisor.plan_review_heartbeat import PlanReviewHeartbeat
from kun.agents.supervisor.plan_review_service import (
    ExternalSupervisorVerify,
    PlanReviewService,
)
from kun.core.logging import get_logger
from kun.datamodel.task import TaskRef

log = get_logger("kun.engineering.long_task_orchestrator")


# ---- ADR-022 defaults (Layer 4 heartbeat cadence) ----

DEFAULT_STEP_INTERVAL = 3
"""Plan-review every N executor steps (ADR-022 Layer 4)."""

DEFAULT_TIME_INTERVAL_SEC = 300.0
"""Plan-review every M seconds even without step progress (ADR-022 Layer 4)."""


# ---- OrchestratorEvent (local lightweight; identical shape) ----


class OrchestratorEvent(BaseModel):
    """One event yielded during long-task execution.

    Identical shape to ``kun.engineering.orchestrator.OrchestratorEvent``
    (kind: str, data: dict). Defined locally to avoid the heavy import graph
    of the short-task orchestrator. LT.INT-F will reconcile.
    """

    kind: str
    data: dict[str, Any] = Field(default_factory=dict)


# ---- Outcome ----


@dataclass(frozen=True)
class LongTaskRunOutcome:
    """Final outcome of a long-task run.

    Caller (the main Orchestrator branch) inspects this to decide whether to
    persist a TaskResult / emit downstream events / trigger any follow-on.
    """

    loop_result: LoopResult
    plan_tree: PlanTree | None
    final_checkpoint_id: str | None
    events_emitted: int


# ---- Event emitter type ----

EventSink = Callable[[OrchestratorEvent], Awaitable[None]]
"""Optional async sink for each emitted OrchestratorEvent."""


async def _noop_event_sink(_ev: OrchestratorEvent) -> None:
    """Default sink when caller passes on_event=None."""
    return None


# ---- LongTaskOrchestrator ----


class LongTaskOrchestrator:
    """Composition class — assembles the 6 LT services into one pipeline.

    All dependencies are injected; construction wires:
      - PlanReviewHeartbeat (step_interval=3, time_interval_sec=300)
        + PlanReviewService (with optional ExternalSupervisorVerify)
      - TaskCheckpointService (writer/reader/marker callbacks)
      - ConversationCompactor (with optional summarizer)
      - RecursivePlanner (with optional atomic_decider + sub_planner)
      - ExecutorLoop wiring all three services above
    """

    def __init__(
        self,
        *,
        llm_invoker: LLMInvoker,
        tool_executor: ToolExecutor,
        checkpoint_writer: CheckpointWriter,
        checkpoint_reader: CheckpointReader,
        checkpoint_status_marker: CheckpointStatusMarker | None = None,
        external_supervisor_verify: ExternalSupervisorVerify | None = None,
        compactor_summarizer: Summarizer | None = None,
        recursive_sub_planner: SubPlanner | None = None,
        atomic_decider: AtomicDecider | None = None,
        # ExecutorLoop tuning
        max_steps: int = 30,
        max_budget_usd: float = 1.0,
        max_wall_seconds: float = 1800.0,
        max_consecutive_tool_failures: int = 3,
        # ConversationCompactor tuning
        compaction_token_threshold: int = 30000,
        compaction_keep_last_k: int = 6,
        compaction_protect_first_n: int = 1,
        # PlanReviewHeartbeat tuning
        plan_review_step_interval: int = DEFAULT_STEP_INTERVAL,
        plan_review_time_interval_sec: float = DEFAULT_TIME_INTERVAL_SEC,
        # RecursivePlanner tuning
        recursive_max_depth: int = 3,
        recursive_max_breadth: int = 8,
        enable_recursive_planner: bool = True,
    ) -> None:
        # --- Layer 4: heartbeat + service ---
        self._heartbeat = PlanReviewHeartbeat(
            step_interval=plan_review_step_interval,
            time_interval_sec=plan_review_time_interval_sec,
        )
        self._plan_review = PlanReviewService(
            heartbeat=self._heartbeat,
            external_supervisor_verify=external_supervisor_verify,
        )

        # --- LT.C: checkpoint service ---
        self._checkpoint = TaskCheckpointService(
            writer=checkpoint_writer,
            reader=checkpoint_reader,
            status_marker=checkpoint_status_marker,
        )

        # --- LT.D: compactor ---
        self._compactor = ConversationCompactor(
            token_threshold=compaction_token_threshold,
            keep_last_k=compaction_keep_last_k,
            protect_first_n=compaction_protect_first_n,
            summarizer=compactor_summarizer,
        )

        # --- LT.F: recursive planner (opt-in) ---
        self._enable_recursive = enable_recursive_planner
        self._recursive_planner = RecursivePlanner(
            max_depth=recursive_max_depth,
            max_breadth_per_node=recursive_max_breadth,
            atomic_decider=atomic_decider,
            sub_planner=recursive_sub_planner,
        )

        # --- LT.E: executor loop wiring all of the above ---
        self._executor = ExecutorLoop(
            llm_invoker=llm_invoker,
            tool_executor=tool_executor,
            max_steps=max_steps,
            max_budget_usd=max_budget_usd,
            max_wall_seconds=max_wall_seconds,
            max_consecutive_tool_failures=max_consecutive_tool_failures,
            checkpoint_service=self._checkpoint,
            plan_review_service=self._plan_review,
            compactor=self._compactor,
        )

    # ----------------------------- public entry -----------------------------

    async def run_long_task(
        self,
        task_ref: TaskRef,
        *,
        on_event: EventSink | None = None,
    ) -> LongTaskRunOutcome:
        """Run the long-task pipeline end-to-end.

        Args:
          task_ref: Director-built TaskRef; ``goal_anchor`` should be attached
            (long-task mode), but absence is tolerated with a warning event.
          on_event: optional async sink; ``await on_event(ev)`` is called for
            each emitted ``OrchestratorEvent``. ``None`` → no-op sink.

        Returns:
          ``LongTaskRunOutcome`` carrying the underlying ``LoopResult``,
          the optional ``PlanTree``, the final checkpoint id (if any), and
          a count of events emitted to the sink.
        """
        sink: EventSink = on_event if on_event is not None else _noop_event_sink
        events_emitted = 0

        async def _emit(kind: str, data: dict[str, Any]) -> None:
            nonlocal events_emitted
            ev = OrchestratorEvent(kind=kind, data=data)
            try:
                await sink(ev)
                events_emitted += 1
            except Exception as e:  # pragma: no cover - sink errors must not crash run
                log.warning(
                    "long_task_orchestrator.sink_failed",
                    kind=kind,
                    error=str(e),
                )

        task_id = task_ref.meta.task_id
        tenant_id = task_ref.meta.owner.tenant_id

        # ---- 1. Resolve goal anchor + initial messages ----
        goal_anchor = getattr(task_ref, "goal_anchor", None)
        anchor_id: str | None = None
        anchor_dict: dict[str, Any] | None = None
        system_prompt: str

        if goal_anchor is None:
            # Should not happen for long-task mode (Director attaches it), but
            # we degrade gracefully: emit a warning event and continue without
            # the anchor pin.
            await _emit(
                "long_task.warning",
                {
                    "task_id": task_id,
                    "stage": "goal_anchor_resolution",
                    "message": (
                        "task_ref.goal_anchor missing — degrading to system prompt "
                        "without anchor pinning"
                    ),
                },
            )
            system_prompt = (
                "你正在执行一个长任务. 完成 user 的 goal, 严格按 success criteria 推进."
            )
        else:
            anchor_id = getattr(goal_anchor, "anchor_id", None)
            system_prompt = goal_anchor.render_for_system_prompt()
            anchor_dict = {
                "goal_statement": getattr(goal_anchor, "goal_statement", ""),
                "success_criteria": list(
                    getattr(goal_anchor, "success_criteria", []) or []
                ),
                "out_of_scope": list(getattr(goal_anchor, "out_of_scope", []) or []),
                "invariants": list(getattr(goal_anchor, "invariants", []) or []),
            }

        # User-side prompt: prefer goal_detail (TaskSpec L2), fall back to L1
        # success_criteria_short. Both come from Director.intent.
        user_text = ""
        if task_ref.spec is not None and task_ref.spec.goal_detail:
            user_text = task_ref.spec.goal_detail
        elif task_ref.meta.success_criteria_short:
            user_text = task_ref.meta.success_criteria_short
        else:
            user_text = "(no goal detail provided)"

        initial_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        # ---- 2. Optional: RecursivePlanner.expand → PlanTree ----
        plan_tree: PlanTree | None = None
        if self._enable_recursive:
            root_steps = _build_root_steps(task_ref)
            root_description = user_text
            try:
                plan_tree = await self._recursive_planner.expand(
                    root_description=root_description,
                    root_steps=root_steps,
                )
                await _emit(
                    "long_task.plan_tree",
                    {
                        "task_id": task_id,
                        "node_count": plan_tree.node_count(),
                        "leaves": len(plan_tree.leaves()),
                        "depth": plan_tree.depth(),
                        "root_description": plan_tree.root().description,
                    },
                )
            except Exception as e:
                log.warning(
                    "long_task_orchestrator.recursive_planner_failed",
                    task_id=task_id,
                    error=str(e),
                )
                # PlanTree is opt-in / advisory; ExecutorLoop runs without it.
                plan_tree = None

        # ---- 3. Emit started ----
        await _emit(
            "long_task.started",
            {
                "task_id": task_id,
                "anchor_id": anchor_id,
                "plan_tree_size": plan_tree.node_count() if plan_tree else 0,
            },
        )

        # ---- 4. ExecutorLoop.run (workhorse) ----
        loop_result = await self._executor.run(
            task_id=task_id,
            tenant_id=tenant_id,
            initial_messages=initial_messages,
            goal_anchor_id=anchor_id,
            anchor_dict=anchor_dict,
        )

        # ---- 5. Map LoopResult.status → OrchestratorEvent(s) ----
        await self._emit_terminal_events(loop_result, task_id, _emit)

        # ---- 6. Return outcome ----
        return LongTaskRunOutcome(
            loop_result=loop_result,
            plan_tree=plan_tree,
            final_checkpoint_id=loop_result.last_checkpoint_id,
            events_emitted=events_emitted,
        )

    # ----------------------------- internals --------------------------------

    async def _emit_terminal_events(
        self,
        result: LoopResult,
        task_id: str,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Translate LoopResult.status into the unified WS event stream."""
        common: dict[str, Any] = {
            "task_id": task_id,
            "status": result.status,
            "steps_taken": result.steps_taken,
            "total_cost_usd": round(result.total_cost_usd, 6),
            "total_tokens": result.total_tokens,
            "elapsed_seconds": round(result.elapsed_seconds, 3),
            "last_checkpoint_id": result.last_checkpoint_id,
            "rationale": result.rationale,
        }

        status = result.status
        if status == "final":
            await emit(
                "answer",
                {
                    **common,
                    "text": result.final_text,
                },
            )
            await emit("long_task.completed", {**common})
            await emit("done", {**common, "cancelled": False})
            return

        if status == "max_steps":
            await emit(
                "guard_intervention",
                {**common, "stage": "max_steps", "level": "exceeded"},
            )
            await emit("done", {**common, "cancelled": False})
            return

        if status == "budget_exceeded":
            await emit(
                "guard_intervention",
                {**common, "stage": "budget", "level": "exceeded"},
            )
            await emit("done", {**common, "cancelled": False})
            return

        if status == "wall_clock_exceeded":
            await emit(
                "task.timed_out",
                {**common, "stage": "wall_clock"},
            )
            await emit("done", {**common, "cancelled": False})
            return

        if status == "stuck":
            await emit(
                "insight",
                {**common, "stage": "stuck", "level": "alarm"},
            )
            await emit("done", {**common, "cancelled": False})
            return

        if status == "failed":
            await emit(
                "guard_intervention",
                {
                    **common,
                    "stage": "error",
                    "level": "exceeded",
                    "error": result.error or "",
                },
            )
            await emit("done", {**common, "cancelled": False})
            return

        if status == "user_cancelled":
            await emit("done", {**common, "cancelled": True})
            return

        # Defensive: future LoopStatus values should still get a "done" so the
        # WS stream terminates.
        await emit("done", {**common, "cancelled": False})  # pragma: no cover


def _build_root_steps(task_ref: TaskRef) -> list[PlanStepInput]:
    """Top-level steps for the RecursivePlanner.

    Precedence:
      1. task_ref.spec.subtasks_hint — one PlanStepInput per hint (preferred)
      2. fallback — single PlanStepInput with the L2 goal_detail or L1
         success_criteria_short
    """
    if task_ref.spec is not None and task_ref.spec.subtasks_hint:
        return [PlanStepInput(description=h) for h in task_ref.spec.subtasks_hint]

    fallback = (
        (task_ref.spec.goal_detail if task_ref.spec is not None else "")
        or task_ref.meta.success_criteria_short
        or "complete the long task"
    )
    return [PlanStepInput(description=fallback)]


__all__ = [
    "DEFAULT_STEP_INTERVAL",
    "DEFAULT_TIME_INTERVAL_SEC",
    "EventSink",
    "LongTaskOrchestrator",
    "LongTaskRunOutcome",
    "OrchestratorEvent",
]
