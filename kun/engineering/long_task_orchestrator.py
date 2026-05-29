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
    LLMStepResponse,
    LoopResult,
    ToolExecutor,
)
from kun.agents.supervisor.plan_review_heartbeat import (
    PlanReviewHeartbeat,
    ReviewTrigger,
    derive_action,
)
from kun.agents.supervisor.plan_review_service import (
    ExternalSupervisorVerify,
    PlanReviewOutcome,
    PlanReviewService,
)
from kun.agents.trifecta import TrifectaCoordinator
from kun.core.logging import get_logger
from kun.datamodel.task import TaskRef
from kun.engineering.methodology_runtime_loader import (
    MethodologyRuntimeSelector,
    TaskContext,
    render_for_system_prompt,
)
from kun.external_supervisor.service import ExternalSupervisorService
from kun.governance.engineering_discipline import (
    EngineeringDisciplineEnforcer,
)
from kun.integration.external_supervisor import make_external_supervisor_verify
from kun.integration.external_supervisor_critique import render_critique_prompt
from kun.integration.plan_review_db import PlanReviewWriter

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


PlanReviewWriterFactory = Callable[..., PlanReviewWriter]
"""Factory producing a per-task PlanReviewWriter.

Call signature (kwargs)::

    factory(tenant_id=..., task_id=..., anchor_id=...) -> PlanReviewWriter

Matches ``kun.integration.plan_review_db.make_plan_review_writer``.
"""


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
        # LT.INT integration adapters (optional; backward-compatible)
        external_supervisor_service: ExternalSupervisorService | None = None,
        plan_review_writer_factory: PlanReviewWriterFactory | None = None,
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
        # External Supervisor critique cadence (ADR-023 cross-check).
        # When set + external_supervisor_service is wired, every N main-line
        # steps the orchestrator calls the supervisor directly with a
        # critique prompt (anchor + last N step summaries) and emits
        # long_task.critique / long_task.drift_alarm based on verdict.
        # None (default) = critique hook disabled — backward compatible.
        critique_every_n_steps: int | None = None,
        # V7 §12.4 Trifecta milestone cadence (X.E.TRIFECTA-WIRING).
        # When set + trifecta_coordinator is wired, every N main-line steps
        # the orchestrator fires TrifectaCoordinator.run() with the synthetic
        # mid-task state and emits long_task.trifecta_tick with per-line state
        # + cost multiplier + n_findings. Hooks are already baked into the
        # injected coordinator (production callers wire real-LLM hooks).
        # None (default) = trifecta hook disabled — backward compatible.
        trifecta_coordinator: TrifectaCoordinator | None = None,
        trifecta_every_n_steps: int | None = None,
        trifecta_n_future_candidates: int = 3,
        # V7 §12 RSI runtime methodology selector (X.G.RSI-CLOSED-LOOP).
        # When wired, at the start of each long-task run we score the
        # `seeds/methodologies/` entries against the task context and inject
        # the top-K rendered text as an extra system-prompt segment. This is
        # the read-side of the closed loop — promoted capabilities actually
        # influence subsequent tasks.
        # None (default) = no methodology injection — backward compatible.
        methodology_selector: MethodologyRuntimeSelector | None = None,
        methodology_top_k: int = 3,
        # V7 §4.3 + Phase F + X.I-0 — Claude Code 10-维 engineering discipline.
        # When wired, runs the enforcer at run completion over the final
        # answer + step summary; emits ``long_task.discipline_report``.
        # Before X.I-0 this was a TOTAL ORPHAN: defined in governance/, no
        # production caller. cockpit /discipline/recent returned [].
        discipline_enforcer: EngineeringDisciplineEnforcer | None = None,
    ) -> None:
        # --- Auto-wire external_supervisor_verify from service when not given ---
        # If both are passed, the explicit verify wins (caller is being deliberate).
        if external_supervisor_verify is None and external_supervisor_service is not None:
            external_supervisor_verify = make_external_supervisor_verify(
                external_supervisor_service
            )
        self._external_supervisor_verify = external_supervisor_verify

        # --- Critique hook (direct supervisor call every N steps) ---
        # Validation: critique_every_n_steps must be >= 1 when set; the hook
        # only fires when external_supervisor_service is wired AND the cadence
        # is set. critique_every_n_steps=None keeps the hook disabled
        # (backward compat with all pre-existing tests).
        if critique_every_n_steps is not None and critique_every_n_steps < 1:
            raise ValueError(
                "critique_every_n_steps must be >= 1 when set; got "
                f"{critique_every_n_steps!r}"
            )
        self._external_supervisor_service = external_supervisor_service
        self._critique_every_n_steps = critique_every_n_steps

        # --- V7 §12.4 Trifecta hook ---
        if trifecta_every_n_steps is not None and trifecta_every_n_steps < 1:
            raise ValueError(
                "trifecta_every_n_steps must be >= 1 when set; got "
                f"{trifecta_every_n_steps!r}"
            )
        self._trifecta_coordinator = trifecta_coordinator
        self._trifecta_every_n_steps = trifecta_every_n_steps
        self._trifecta_n_future_candidates = trifecta_n_future_candidates

        # --- V7 §12 RSI runtime methodology selector ---
        if methodology_top_k < 0:
            raise ValueError(
                f"methodology_top_k must be >= 0; got {methodology_top_k!r}"
            )
        self._methodology_selector = methodology_selector
        self._methodology_top_k = methodology_top_k

        # --- X.I-0: Engineering discipline enforcer (V7 §4.3 / Phase F) ---
        self._discipline_enforcer = discipline_enforcer

        # --- Stash heartbeat tuning so we can rebuild per-task when needed ---
        # When plan_review_writer_factory is provided, run_long_task rebuilds
        # heartbeat + PlanReviewService with a per-task emitter wired in at
        # construction time (PlanReviewHeartbeat only consults its emitter
        # attribute, never re-reads it post-init).
        self._plan_review_step_interval = plan_review_step_interval
        self._plan_review_time_interval_sec = plan_review_time_interval_sec
        self._plan_review_writer_factory = plan_review_writer_factory

        # --- Layer 4: heartbeat + service (default, no-emitter wiring) ---
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

        # V7 Phase X.H.TRACE — mutable dict accumulating which X.E / X.G /
        # DIST-D runtime features fired during this orchestrator instance.
        # Methodology injection / trifecta ticks / critique calls update it;
        # ExecutorLoop reads it via runtime_features_provider at each
        # checkpoint save so the PG row preserves causality.
        self._runtime_features_trace: dict[str, Any] = {}

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
            runtime_features_provider=self._snapshot_runtime_features,
        )

    def _snapshot_runtime_features(self) -> dict[str, Any]:
        """Read-side of the X.H.TRACE mutable dict — returns a shallow copy
        so the ExecutorLoop never mutates the orchestrator's bookkeeping."""
        return dict(self._runtime_features_trace)

    # ----------------------------- public entry -----------------------------

    async def run_long_task(
        self,
        task_ref: TaskRef,
        *,
        on_event: EventSink | None = None,
        extra_system_segments: list[str] | None = None,
    ) -> LongTaskRunOutcome:
        """Run the long-task pipeline end-to-end.

        Args:
          task_ref: Director-built TaskRef; ``goal_anchor`` should be attached
            (long-task mode), but absence is tolerated with a warning event.
          on_event: optional async sink; ``await on_event(ev)`` is called for
            each emitted ``OrchestratorEvent``. ``None`` → no-op sink.
          extra_system_segments: optional list of system-prompt segments to
            append after GoalAnchor + anti-sycophancy. LT.TOOLS-GAP fix —
            caller (Orchestrator) injects skill directive here so LLM sees
            tool schema. Each segment is joined by double newline.

        Returns:
          ``LongTaskRunOutcome`` carrying the underlying ``LoopResult``,
          the optional ``PlanTree``, the final checkpoint id (if any), and
          a count of events emitted to the sink.
        """
        sink: EventSink = on_event if on_event is not None else _noop_event_sink
        events_emitted = 0
        # X.H.TRACE — fresh trace dict per run so concurrent / sequential
        # runs don't leak features into each other's checkpoints.
        self._runtime_features_trace = {}

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

        # ---- 0. (Optional) Per-task heartbeat + plan_review_writer wiring ----
        # If the caller injected a plan_review_writer_factory, we rebuild the
        # heartbeat (with a per-task emitter that persists each trigger) plus
        # the PlanReviewService bound to that heartbeat, and reassign them on
        # the ExecutorLoop for this run. Heartbeat consults its emitter
        # attribute set at __init__, so we can't safely mutate the shared one
        # across concurrent tasks — we rebuild and restore.
        original_heartbeat = self._heartbeat
        original_plan_review = self._plan_review
        per_task_heartbeat: PlanReviewHeartbeat | None = None
        plan_review_writer: PlanReviewWriter | None = None
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

        # LT.TOOLS-GAP fix: append skill_directive / other extras to system
        # prompt so LLM sees tool schemas — without this, LLM has no idea
        # what tools exist and gives empty final answer.
        if extra_system_segments:
            for segment in extra_system_segments:
                if segment and segment.strip():
                    system_prompt = system_prompt + "\n\n" + segment

        # ---- 1c. (Optional) Inject runtime methodology context (V7 §12) ----
        # Pull from MethodologyRuntimeSelector if wired. Closes the RSI loop:
        # promoted seeds in seeds/methodologies/ actually reach the LLM.
        if (
            self._methodology_selector is not None
            and self._methodology_top_k > 0
        ):
            method_ctx = TaskContext(
                task_type=getattr(task_ref.meta, "task_type", "") or "",
                goal_statement=anchor_dict.get("goal_statement", "")
                if anchor_dict
                else "",
                goal_keywords=(anchor_dict or {}).get("success_criteria", []),
                extra_keywords=[user_text],
            )
            try:
                chosen = self._methodology_selector.select_for(
                    method_ctx, top_k=self._methodology_top_k
                )
                rendered = render_for_system_prompt(chosen)
                if rendered:
                    system_prompt = system_prompt + "\n\n" + rendered
                    # X.H.TRACE: record into the orchestrator's mutable trace
                    # so subsequent checkpoint saves carry the methodology
                    # IDs into PG.
                    self._runtime_features_trace["methodologies"] = [
                        {
                            "title": m.title,
                            "topic": m.topic,
                            "score": round(m.score, 3),
                            "file_path": m.file_path,
                        }
                        for m in chosen
                    ]
                    await _emit(
                        "long_task.methodology_injected",
                        {
                            "task_id": task_id,
                            "n_methodologies": len(chosen),
                            "methodologies": self._runtime_features_trace[
                                "methodologies"
                            ],
                        },
                    )
            except Exception as e:
                log.warning(
                    "long_task_orchestrator.methodology_inject_failed",
                    task_id=task_id,
                    error=f"{type(e).__name__}: {e}",
                )

        initial_messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text},
        ]

        # ---- 1b. Wire per-task plan_review writer + heartbeat emitter ----
        # Done after anchor resolution because the writer is pre-bound to
        # (tenant_id, task_id, anchor_id). We rebuild heartbeat + service so
        # the new emitter is in place at construction time, and reassign on
        # the shared ExecutorLoop. Restored in finally below.
        if self._plan_review_writer_factory is not None and anchor_id is not None:
            try:
                plan_review_writer = self._plan_review_writer_factory(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    anchor_id=anchor_id,
                )
            except Exception as e:
                log.warning(
                    "long_task_orchestrator.plan_review_writer_factory_failed",
                    task_id=task_id,
                    error=str(e),
                )
                plan_review_writer = None

            if plan_review_writer is not None:
                bound_writer = plan_review_writer

                async def _heartbeat_emitter(trigger: ReviewTrigger) -> None:
                    """Persist a placeholder PlanReviewOutcome on each trigger.

                    We do not yet parse the Executor's self_report JSON out of
                    the message stream (LT.INT-G+ work), so external_verdict
                    stays None. The internal verdict + drift_evidence come
                    from heartbeat's own rule-based evaluation, which is
                    surfaced via trigger.payload — re-use it to avoid making
                    the row look more informed than it is.
                    """
                    payload = trigger.payload or {}
                    internal_verdict = payload.get("supervisor_verdict", "aligned")
                    drift_evidence = list(payload.get("drift_evidence", []) or [])
                    self_report_payload = payload.get("executor_self_report") or None
                    if not isinstance(self_report_payload, dict):
                        self_report_payload = None

                    action = derive_action(internal_verdict)
                    outcome = PlanReviewOutcome(
                        triggered=True,
                        trigger=trigger,
                        internal_verdict=internal_verdict,  # type: ignore[arg-type]
                        external_verdict=None,
                        final_verdict=internal_verdict,  # type: ignore[arg-type]
                        drift_evidence=drift_evidence,
                        action=action,
                        rationale=(
                            f"heartbeat-triggered ({trigger.reason}) at "
                            f"step={trigger.triggered_at_step}; "
                            f"internal={internal_verdict}"
                        ),
                    )
                    try:
                        await bound_writer(
                            outcome,
                            triggered_at_step=trigger.triggered_at_step,
                            self_report=self_report_payload,
                        )
                    except Exception as e:
                        log.warning(
                            "long_task_orchestrator.plan_review_write_failed",
                            task_id=task_id,
                            review_id=trigger.review_id,
                            error=str(e),
                        )

                per_task_heartbeat = PlanReviewHeartbeat(
                    step_interval=self._plan_review_step_interval,
                    time_interval_sec=self._plan_review_time_interval_sec,
                    emitter=_heartbeat_emitter,
                )
                per_task_service = PlanReviewService(
                    heartbeat=per_task_heartbeat,
                    external_supervisor_verify=self._external_supervisor_verify,
                )
                self._heartbeat = per_task_heartbeat
                self._plan_review = per_task_service
                self._executor._plan_review = per_task_service

        # ---- 1c. Wire critique hook (direct supervisor cross-check every K steps) ----
        # We wrap the ExecutorLoop's LLM invoker with a counter + critique
        # caller. After every K LLM invocations, we ask
        # ExternalSupervisorService.analyze_observation to grade the last K
        # step summaries against the anchor. Verdict 'alarming' → emit
        # long_task.drift_alarm so the caller (main Orchestrator) can decide
        # to abort. We track step summaries in a closure list so compaction
        # of `messages` doesn't erase our look-back. Restored in finally.
        original_llm_invoker = self._executor._llm
        critique_hook_active = (
            self._critique_every_n_steps is not None
            and self._external_supervisor_service is not None
        )
        if critique_hook_active:
            # Local imports avoid pulling protocol types into module scope where
            # they would clash with the in-file `LLMInvoker` alias.
            wrapped_invoker = self._build_critique_wrapped_invoker(
                original_invoker=original_llm_invoker,
                emit=_emit,
                task_id=task_id,
                anchor_id=anchor_id,
                anchor_dict=anchor_dict,
            )
            self._executor._llm = wrapped_invoker

        # ---- 1d. (Optional) Trifecta milestone wrapper (V7 §12.4) ----
        # When trifecta_coordinator + trifecta_every_n_steps are wired, wrap
        # the invoker (possibly already critique-wrapped) so every N steps
        # we fire `TrifectaCoordinator.run()` with the running step summary
        # as recent_steps + current_step. Hooks already baked into the
        # injected coordinator. Restored in finally below.
        trifecta_hook_active = (
            self._trifecta_every_n_steps is not None
            and self._trifecta_coordinator is not None
        )
        if trifecta_hook_active:
            current_invoker = self._executor._llm
            self._executor._llm = self._build_trifecta_wrapped_invoker(
                original_invoker=current_invoker,
                emit=_emit,
                task_id=task_id,
                anchor_dict=anchor_dict,
            )

        try:
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

            # ---- 5b. (Optional) Engineering discipline enforcer (X.I-0) ----
            # V7 §4.3 + Phase F: every long-task run now passes its final
            # answer + skill calls + cost / token / file change context
            # through the discipline enforcer. Was a TOTAL ORPHAN before
            # X.I-0 — defined but never called from production.
            if self._discipline_enforcer is not None:
                try:
                    discipline_ctx = {
                        "answer_text": loop_result.final_text or "",
                        "skill_calls_in_response": [],  # extracted from messages
                        "has_code_changes": False,
                        "commit_message": "",
                        "cost_usd_total": float(loop_result.total_cost_usd),
                        "tokens_total": int(loop_result.total_tokens),
                        "step_count": int(loop_result.steps_taken),
                    }
                    report = self._discipline_enforcer.check(discipline_ctx)
                    failed_disciplines = [
                        c.discipline.value
                        for c in report.checks
                        if not c.passed
                    ]
                    await _emit(
                        "long_task.discipline_report",
                        {
                            "task_id": task_id,
                            "overall_score": round(report.overall_score, 3),
                            "n_total": len(report.checks),
                            "n_passed": sum(
                                1 for c in report.checks if c.passed
                            ),
                            "failed_disciplines": failed_disciplines,
                        },
                    )
                    self._runtime_features_trace["discipline_report"] = {
                        "overall_score": round(report.overall_score, 3),
                        "failed_disciplines": failed_disciplines,
                    }
                except Exception as e:  # pragma: no cover
                    log.warning(
                        "long_task_orchestrator.discipline_enforcer_failed",
                        task_id=task_id,
                        error=f"{type(e).__name__}: {e}",
                    )

            # ---- 6. Return outcome ----
            return LongTaskRunOutcome(
                loop_result=loop_result,
                plan_tree=plan_tree,
                final_checkpoint_id=loop_result.last_checkpoint_id,
                events_emitted=events_emitted,
            )
        finally:
            # Restore shared heartbeat / service if we rebuilt per-task ones.
            if per_task_heartbeat is not None:
                self._heartbeat = original_heartbeat
                self._plan_review = original_plan_review
                self._executor._plan_review = original_plan_review
            # Restore original LLM invoker if we wrapped it for critique
            # or trifecta. (We track via the original handle so stacked
            # wrappers all unwind cleanly.)
            if critique_hook_active or trifecta_hook_active:
                self._executor._llm = original_llm_invoker

    # ----------------------------- internals --------------------------------

    def _build_critique_wrapped_invoker(
        self,
        *,
        original_invoker: LLMInvoker,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
        task_id: str,
        anchor_id: str | None,
        anchor_dict: dict[str, Any] | None,
    ) -> LLMInvoker:
        """Build an LLM invoker wrapper that fires a critique every K steps.

        Each call counts as one main-line step. When the counter is a multiple
        of ``self._critique_every_n_steps``, we call
        ``ExternalSupervisorService.analyze_observation`` with a critique
        prompt summarising the last K steps, then emit:
          - ``long_task.critique`` (always — verdict + rationale)
          - ``long_task.drift_alarm`` (when verdict == 'alarming')

        The wrapper never raises into the ExecutorLoop: if the supervisor
        call fails we log + skip, so a flaky local LLM doesn't kill the run.
        """
        every = self._critique_every_n_steps
        svc = self._external_supervisor_service
        if every is None or svc is None:
            # Defensive — caller guards on this, but keep typing honest.
            raise RuntimeError(
                "_build_critique_wrapped_invoker called without "
                "critique_every_n_steps + external_supervisor_service"
            )
        # Build the critique anchor exactly once per run. We accept `None` /
        # `{}` here and degrade: render_critique_prompt rejects an empty
        # anchor, so we set a sentinel that disables the critique call (still
        # increments the counter so tests can observe the call cadence).
        critique_anchor: dict[str, Any] = anchor_dict or {}
        step_summaries: list[dict[str, Any]] = []
        call_count = 0

        async def _wrapped(messages: list[dict[str, Any]]) -> LLMStepResponse:
            nonlocal call_count
            response = await original_invoker(messages)
            call_count += 1
            # Capture this step's summary. Prefer assistant text content; fall
            # back to a tool-call digest if the model only emitted tools.
            summary = (response.content or "").strip()
            if not summary and response.tool_calls:
                summary = "tool_calls=" + ",".join(
                    tc.name for tc in response.tool_calls
                )
            step_summaries.append({"step_idx": call_count, "summary": summary})

            if call_count % every != 0:
                return response

            # Critique threshold hit — call the supervisor.
            if not critique_anchor:
                log.info(
                    "long_task_orchestrator.critique_skipped_no_anchor",
                    task_id=task_id,
                    call_count=call_count,
                )
                return response

            try:
                critique_prompt = render_critique_prompt(
                    anchor=critique_anchor,
                    last_steps=step_summaries,
                )
                observation = await svc.analyze_observation(
                    obs_kind="critique",
                    observation_payload={
                        "critique_prompt": critique_prompt,
                        "last_steps": list(step_summaries[-every:]),
                        "call_count": call_count,
                    },
                    anchor=critique_anchor,
                    target_task_id=task_id,
                    target_anchor_id=anchor_id,
                )
            except Exception as e:
                log.warning(
                    "long_task_orchestrator.critique_failed",
                    task_id=task_id,
                    call_count=call_count,
                    error=str(e),
                )
                return response

            verdict = getattr(observation, "verdict", None) or "ok"
            rationale = getattr(observation, "rationale", "") or ""
            recommended = getattr(observation, "recommended_action", None)
            await emit(
                "long_task.critique",
                {
                    "task_id": task_id,
                    "anchor_id": anchor_id,
                    "call_count": call_count,
                    "verdict": verdict,
                    "rationale": rationale,
                    "recommended_action": recommended,
                },
            )
            if verdict == "alarming":
                await emit(
                    "long_task.drift_alarm",
                    {
                        "task_id": task_id,
                        "anchor_id": anchor_id,
                        "call_count": call_count,
                        "verdict": verdict,
                        "rationale": rationale,
                        "recommended_action": recommended,
                    },
                )
            return response

        return _wrapped

    def _build_trifecta_wrapped_invoker(
        self,
        *,
        original_invoker: LLMInvoker,
        emit: Callable[[str, dict[str, Any]], Awaitable[None]],
        task_id: str,
        anchor_dict: dict[str, Any] | None,
    ) -> LLMInvoker:
        """Build an LLM invoker wrapper that fires `TrifectaCoordinator.run()`
        every N steps (V7 §12.4 RSI 三线 milestone).

        Each call counts as one main-line step. When the counter is a multiple
        of ``self._trifecta_every_n_steps``, we:
          1. Build a synthetic ``recent_steps`` from the running step summary
          2. Compose a ``current_step`` from the most recent assistant payload
          3. Compose a ``future_plan`` from anchor_dict (goal_statement /
             success_criteria) so the future-line hook has goal context
          4. Call ``TrifectaCoordinator.run(...)`` — caller's pre-baked hooks
             handle real-LLM dispatch
          5. Emit ``long_task.trifecta_tick`` event with per-line state +
             cost_multiplier_vs_baseline + n_findings

        The wrapper never raises into the ExecutorLoop: if the coordinator
        crashes we log + skip, so a flaky LLM provider in a trifecta line
        doesn't kill the long task.
        """
        every = self._trifecta_every_n_steps
        coord = self._trifecta_coordinator
        n_candidates = self._trifecta_n_future_candidates
        if every is None or coord is None:
            raise RuntimeError(
                "_build_trifecta_wrapped_invoker called without "
                "trifecta_every_n_steps + trifecta_coordinator"
            )
        step_summaries: list[dict[str, Any]] = []
        call_count = 0

        async def _wrapped(messages: list[dict[str, Any]]) -> LLMStepResponse:
            nonlocal call_count
            response = await original_invoker(messages)
            call_count += 1
            # Capture this step's summary (prefer text content; fall back to
            # tool-call digest if the model only emitted tools).
            summary = (response.content or "").strip()
            if not summary and response.tool_calls:
                summary = "tool_calls=" + ",".join(
                    tc.name for tc in response.tool_calls
                )
            step_summaries.append({"step_idx": call_count, "summary": summary})

            if call_count % every != 0:
                return response

            # Trifecta milestone hit.
            recent_window = list(step_summaries[-every:])
            current_step = {
                "step_idx": call_count,
                "summary": summary[:500],
            }
            future_plan: dict[str, Any] = {}
            if anchor_dict:
                # Pass the goal + success_criteria so the future line knows
                # what target to propose candidates against.
                future_plan = {
                    "goal": anchor_dict.get("goal_statement", ""),
                    "success_criteria": list(
                        anchor_dict.get("success_criteria", []) or []
                    ),
                }

            try:
                report = await coord.run(
                    task_id=task_id,
                    recent_steps=recent_window,
                    current_step=current_step,
                    future_plan=future_plan,
                    n_future_candidates=n_candidates,
                )
            except Exception as e:
                log.warning(
                    "long_task_orchestrator.trifecta_failed",
                    task_id=task_id,
                    call_count=call_count,
                    error=str(e),
                )
                return response

            # X.H.TRACE — accumulate per-tick state into orchestrator trace
            ticks = self._runtime_features_trace.setdefault("trifecta_ticks", [])
            ticks.append(
                {
                    "call_count": call_count,
                    "past_state": report.past.state.value,
                    "present_state": report.present.state.value,
                    "future_state": report.future.state.value,
                    "n_findings": report.n_findings,
                    "total_cost_usd": round(report.total_cost_usd, 6),
                    "any_line_failed": report.any_line_failed,
                }
            )
            await emit(
                "long_task.trifecta_tick",
                {
                    "task_id": task_id,
                    "call_count": call_count,
                    "past_state": report.past.state.value,
                    "present_state": report.present.state.value,
                    "future_state": report.future.state.value,
                    "n_findings": report.n_findings,
                    "total_cost_usd": round(report.total_cost_usd, 6),
                    "cost_multiplier_vs_baseline": round(
                        report.cost_multiplier_vs_baseline, 3
                    ),
                    "any_line_failed": report.any_line_failed,
                },
            )
            if report.any_line_failed:
                await emit(
                    "long_task.trifecta_line_failed",
                    {
                        "task_id": task_id,
                        "call_count": call_count,
                        "past_error": report.past.error_detail,
                        "present_error": report.present.error_detail,
                        "future_error": report.future.error_detail,
                    },
                )
            return response

        return _wrapped

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
    "PlanReviewWriterFactory",
]
