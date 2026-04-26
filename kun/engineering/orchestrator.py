"""Orchestrator — walking skeleton 端到端任务执行器.

Pipeline (§5.1-5.3):
  1. 事前: 意图 → TaskRef; TASK.md 持久化; 幂等检查; 指纹登记; 事件 task.created
  2. RuntimeState 初始化
  3. 计划生成 (Planner)
  4. 路由 (Router → role_template + model tier)
  5. 事中: 逐步执行 + 成本追踪 + 事件 task.step.completed
  6. 事后: 状态更新 done/failed; 事件 task.done
  7. Notification: cost_tick, insight/surprise 推送

所有事件走 Outbox (ADR-005).
所有成本按 ADR-008 双口径记录.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, cast

from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from kun.brain.intent import IntentInterpreter
from kun.brain.planner import TaskPlanner
from kun.brain.router import TaskRouter
from kun.context.packer import ContextPacker
from kun.core.config import settings
from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.logging import get_logger
from kun.core.metrics import (
    task_duration_seconds,
    task_started_total,
    task_surprise_score,
)
from kun.core.orm import IdempotencyRow, RuntimeStateRow, TaskResultRow, TaskRow
from kun.core.tenancy import current_tenant
from kun.datamodel.events import Event
from kun.datamodel.notification import Notification
from kun.datamodel.runtime import RuntimeState, StepRecord, TaskStatus
from kun.datamodel.task import Owner, TaskMeta, TaskRef
from kun.engineering.capability_writeback import Outcome, TaskOutcome, record_outcome
from kun.engineering.concurrency import (
    enqueue_pending_actions,
    pending_actions_for,
    scan_pre_conflicts,
)
from kun.engineering.validation import ValidationPipeline, pick_tier
from kun.interface.adapters import translate_for
from kun.interface.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRouter,
    TaskProfile,
    get_router,
)
from kun.interface.llm.router import TaskPurpose
from kun.skills.selector import get_selector as get_skill_selector
from kun.watchtower.engine import RuleEngine

log = get_logger("kun.engineering.orchestrator")

_STALE_QUEUED_TASK_AFTER = timedelta(seconds=30)


class OutputTranslator(Protocol):
    async def __call__(
        self,
        *,
        payload: dict[str, Any],
        recipient_kind: str,
        context: dict[str, Any] | None = None,
    ) -> str: ...


# R-N3: per-audience system-prompt directives. Concatenated after the base
# "执行角色" preamble so the assistant adjusts voice / depth without losing
# its primary instructions.
_AUDIENCE_DIRECTIVES: dict[str, str] = {
    "novice": (
        "回答风格: 大白话, 不堆英文术语和缩写, 必要时打比方, 短句, 不堆代码. "
        "用户是非技术背景, 给结论 + 一两句解释 + 下一步建议."
    ),
    "developer": (
        "回答风格: 简洁直接, 给代码块或具体命令, 文件路径精确. 默认对方懂基础概念, 不解释入门术语."
    ),
    "expert": (
        "回答风格: 深度分析, 列替代方案 + 每个方案的 trade-off, 引用具体代码位置 / ADR. "
        "可以长但要结构化."
    ),
}


# OTel tracer — best-effort, lazy-initialized; no-op if SDK isn't wired.
def _tracer() -> Any:
    try:
        from opentelemetry import trace

        return trace.get_tracer("kun.orchestrator")
    except Exception:
        return None


class TaskTimedOutError(RuntimeError):
    """Raised when a single task exceeds its hard duration cap."""

    def __init__(self, task_id: str, elapsed_sec: float, cap_sec: float) -> None:
        super().__init__(
            f"task {task_id} exceeded duration cap: {elapsed_sec:.1f}s > {cap_sec:.0f}s"
        )
        self.task_id = task_id
        self.elapsed_sec = elapsed_sec
        self.cap_sec = cap_sec


class TaskResult(BaseModel):
    """Final result surfaced to the API / WebSocket."""

    task_id: str
    status: TaskStatus
    answer: str = ""
    cost_usd_actual: float = 0.0
    cost_usd_equivalent: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    duration_sec: float = 0.0
    surprise_score: float = 0.0
    notifications: list[Notification] = Field(default_factory=list)


class OrchestratorEvent(BaseModel):
    """One event yielded during streaming execution — feeds WebSocket side channel."""

    kind: str  # cost_tick | thinking | answer | done | error | insight
    data: dict[str, Any] = Field(default_factory=dict)


class Orchestrator:
    """Walking skeleton: single-step task execution.

    Future iterations plug in:
      - Multi-step planner
      - Skill lookup & dispatch
      - Context preheat (importance scorer)
      - Watchtower event evaluation per step
      - Debate / validation triggers on high-risk tasks
    """

    def __init__(
        self,
        *,
        llm_router: LLMRouter | None = None,
        rule_engine: RuleEngine | None = None,
        validation: ValidationPipeline | None = None,
        context_packer: ContextPacker | None = None,
        output_translator: OutputTranslator | None = None,
        emergent_switch_manager: Any = None,
        value_gate: Any = None,
    ) -> None:
        self.llm_router = llm_router or get_router()
        self.intent = IntentInterpreter(self.llm_router)
        self.planner = TaskPlanner()
        self.task_router = TaskRouter()
        self.rule_engine = rule_engine or RuleEngine()
        self.validation = validation or ValidationPipeline(self.llm_router)
        self.skill_selector = get_skill_selector()
        self.context_packer = context_packer or ContextPacker()
        self.output_translator = output_translator or translate_for
        self.emergent_switch_manager = emergent_switch_manager
        # V2.2 §19.4: 守望主决策 gate (opt-in, 默认 None 不影响现有测试)
        self.value_gate = value_gate
        # 累计 step value history, 给 value_gate marginal_roi 用
        self._value_history: list[float] = []

    # ----------------------------- public entry -----------------------------

    async def run(self, user_message: str, *, output_kind: str = "user") -> TaskResult:
        """Non-streaming entry. Useful for tests / HTTP POST."""
        final: TaskResult | None = None
        async for ev in self.stream(user_message, output_kind=output_kind):
            if ev.kind == "done":
                final = TaskResult.model_validate(ev.data["result"])
        if final is None:
            raise RuntimeError("orchestrator exited without a done event")
        return final

    async def stream(
        self,
        user_message: str,
        *,
        max_duration_sec: float | None = None,
        output_kind: str = "user",
    ) -> AsyncIterator[OrchestratorEvent]:
        """Streaming entry. Yields OrchestratorEvents for WebSocket.

        Events align with ADR-010 dialog protocol message blocks.

        ``max_duration_sec`` overrides the global ``KUN_TASK_MAX_DURATION_SEC``
        cap for this task only. The orchestrator checks the deadline before
        each step; on overshoot it raises :class:`TaskTimedOutError`, persists
        a failed result, and emits ``task.timed_out``.
        """
        tenant = current_tenant()
        owner = Owner(tenant_id=tenant.tenant_id, user_id=tenant.user_id)

        t0 = time.perf_counter()
        cfg = settings()
        duration_cap = float(max_duration_sec or cfg.task_max_duration_sec)
        deadline_monotonic = time.monotonic() + duration_cap

        # OTel: tag the auto-injected HTTP span with task metadata. Tracer
        # auto-instrumentation already creates the span; we just enrich it.
        tracer = _tracer()
        if tracer is not None:
            try:
                from opentelemetry import trace

                current_span = trace.get_current_span()
                current_span.set_attribute("kun.tenant_id", tenant.tenant_id)
                current_span.set_attribute("kun.duration_cap_sec", duration_cap)
            except Exception:
                pass

        # Budget gate (R-A11) — query today's cumulative cost; if past the
        # warn threshold emit an alert, if past the hard cap force every LLM
        # call this task makes onto the cheap MiniMax fallback.
        budget_used, budget_cap = await _today_cost_vs_budget(tenant.tenant_id)
        warn_threshold = cfg.budget_warn_fraction * budget_cap
        force_fallback = budget_cap > 0 and budget_used >= budget_cap
        if budget_cap > 0 and budget_used >= warn_threshold and not force_fallback:
            yield OrchestratorEvent(
                kind="insight",
                data={
                    "stage": "budget",
                    "level": "warn",
                    "used_usd": round(budget_used, 4),
                    "cap_usd": budget_cap,
                    "message": (
                        f"今天累计已花约 ${budget_used:.2f}（占预算 "
                        f"{budget_used / budget_cap * 100:.0f}%），临近上限。"
                    ),
                },
            )
        if force_fallback:
            yield OrchestratorEvent(
                kind="guard_intervention",
                data={
                    "stage": "budget",
                    "level": "exceeded",
                    "used_usd": round(budget_used, 4),
                    "cap_usd": budget_cap,
                    "message": (
                        f"今天累计已花约 ${budget_used:.2f}，已超日预算 ${budget_cap:.2f}。"
                        "本任务会降级到便宜模型（MiniMax fallback）继续，避免烧订阅 quota。"
                    ),
                },
            )

        # 1. 意图理解 -> TaskRef
        yield OrchestratorEvent(kind="thinking", data={"stage": "intent"})
        task_ref = await self.intent.interpret(user_message, owner=owner)

        # 1.5 V2.1 wire (M3.3, opt-in): TaskPanorama 按需展开 + 推黑板
        # KUN_PANORAMA_BUILDER_ENABLED=1 启用; 默认 off, 不破坏现有流程.
        from kun.engineering.panorama_orchestrator_bridge import (
            build_panorama_for_task,
            panorama_to_event_data,
        )
        from kun.engineering.panorama_orchestrator_bridge import (
            is_enabled as _panorama_enabled,
        )

        if _panorama_enabled():
            _panorama = await build_panorama_for_task(task_ref, user_message)
            if _panorama is not None:
                yield OrchestratorEvent(
                    kind="action_plan",
                    data=panorama_to_event_data(_panorama),
                )

        # 2. Idempotency check + persist TaskRow + emit task.created
        duplicate_ref: str | None = None
        try:
            async with session_scope() as s:
                duplicate_ref = await _find_idempotent_result_ref(
                    s,
                    tenant_id=tenant.tenant_id,
                    fingerprint=task_ref.meta.fingerprint,
                )
                if duplicate_ref is None:
                    initial_runtime = RuntimeState(
                        task_ref=task_ref.meta.task_id,
                        total_planned_steps=1,
                        status="queued",
                    )
                    # Persist TaskRow and idempotency key in the same transaction.
                    s.add(
                        TaskRow(
                            task_id=task_ref.meta.task_id,
                            tenant_id=tenant.tenant_id,
                            fingerprint=task_ref.meta.fingerprint,
                            task_type=task_ref.meta.task_type,
                            risk_level=task_ref.meta.risk_level,
                            complexity_score=task_ref.meta.complexity_score,
                            user_id=owner.user_id,
                            project_id=owner.project_id,
                            estimated_cost_usd=task_ref.meta.estimated_cost_usd,
                            estimated_duration_sec=task_ref.meta.estimated_duration_sec,
                            deadline_iso=task_ref.meta.deadline_iso,
                            success_criteria_short=task_ref.meta.success_criteria_short,
                            version=task_ref.meta.version,
                            spec_json=(
                                task_ref.spec.model_dump(mode="json") if task_ref.spec else None
                            ),
                            layer3_ref=task_ref.layer3_ref,
                        )
                    )
                    s.add(
                        IdempotencyRow(
                            key=task_ref.meta.fingerprint,
                            tenant_id=tenant.tenant_id,
                            result_ref=task_ref.meta.task_id,
                        )
                    )
                    # Flush the parent TaskRow first so the child RuntimeStateRow's
                    # foreign-key check sees it. Without a `relationship()` declared
                    # between TaskRow ↔ RuntimeStateRow, SQLAlchemy's unit-of-work
                    # does not topologically order plain FK columns reliably, and
                    # a single combined flush can emit the child INSERT before the
                    # parent — triggering `fk_runtime_states_task_ref_tasks`.
                    # A rollback still undoes both (we're in one session_scope txn),
                    # so crash-window protection from R-1 is preserved.
                    await s.flush()
                    s.add(_runtime_to_row(initial_runtime, tenant.tenant_id))
                    await emit(
                        s,
                        Event.build(
                            tenant_id=tenant.tenant_id,
                            event_type="task.created",
                            payload={
                                "task_id": task_ref.meta.task_id,
                                "task_type": task_ref.meta.task_type,
                            },
                            task_ref=task_ref.meta.task_id,
                        ),
                    )
                    await s.flush()
        except IntegrityError:
            # A concurrent request may have inserted the same tenant/fingerprint first.
            async with session_scope() as s:
                duplicate_ref = await _find_idempotent_result_ref(
                    s,
                    tenant_id=tenant.tenant_id,
                    fingerprint=task_ref.meta.fingerprint,
                )
            if duplicate_ref is None:
                raise

        if duplicate_ref is not None:
            cached_result = await _load_cached_task_result(
                tenant_id=tenant.tenant_id,
                task_id=duplicate_ref,
            )
            if cached_result is not None:
                yield OrchestratorEvent(
                    kind="insight",
                    data={
                        "message": "Duplicate task detected. Returning cached result.",
                        "cached_ref": duplicate_ref,
                        "status": cached_result.status,
                    },
                )
                yield OrchestratorEvent(
                    kind="answer",
                    data={"content": cached_result.answer, "task_id": duplicate_ref},
                )
                yield OrchestratorEvent(
                    kind="done",
                    data={"result": cached_result.model_dump(mode="json")},
                )
                return

            duration = time.perf_counter() - t0
            result = await _resolve_duplicate_without_cached_result(
                tenant_id=tenant.tenant_id,
                task_id=duplicate_ref,
                duration_sec=duration,
            )
            yield OrchestratorEvent(
                kind="insight",
                data={
                    "message": result.answer,
                    "cached_ref": duplicate_ref,
                    "status": result.status,
                },
            )
            yield OrchestratorEvent(
                kind="answer", data={"content": result.answer, "task_id": duplicate_ref}
            )
            yield OrchestratorEvent(
                kind="done",
                data={"result": result.model_dump(mode="json")},
            )
            return

        yield OrchestratorEvent(
            kind="action_plan",
            data={
                "task_id": task_ref.meta.task_id,
                "task_type": task_ref.meta.task_type,
                "risk_level": task_ref.meta.risk_level,
                "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
                "estimated_duration_sec": task_ref.meta.estimated_duration_sec,
            },
        )

        # 3. Planning
        plan = await self.planner.plan(task_ref, router=self.llm_router)

        # V2.1 §5.8 wire: 注册任务到 EmergentSwitchManager (信号驱动, 零额外开销 90% 任务)
        if self.emergent_switch_manager is not None:
            try:
                self.emergent_switch_manager.register_task(
                    task_ref.meta.task_id,
                    estimated_steps=len(plan.steps),
                )
            except Exception:
                log.exception("emergent_switch.register_task failed (non-fatal)")

        # 4. Route (pick role + model purpose)
        choice = self.task_router.choose(task_ref.meta)

        # 5. Pre-start safety: conflict scan + pending side-effect actions.
        pending_actions = pending_actions_for(task_ref)
        async with session_scope() as s:
            pre_conflict_report = await scan_pre_conflicts(
                s,
                tenant_id=tenant.tenant_id,
                task_ref=task_ref,
            )
            if pending_actions:
                await enqueue_pending_actions(
                    s,
                    tenant_id=tenant.tenant_id,
                    task_ref=task_ref,
                    actions=pending_actions,
                )
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.pending_actions.created",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            "actions": [
                                action.model_dump(mode="json") for action in pending_actions
                            ],
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
            if pre_conflict_report.conflicts:
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.pre_conflict_detected",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            "conflicts": [
                                conflict.model_dump(mode="json")
                                for conflict in pre_conflict_report.conflicts
                            ],
                            "resources": [
                                resource.model_dump(mode="json")
                                for resource in pre_conflict_report.resources
                            ],
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )

        if pre_conflict_report.blocking or pending_actions:
            reason_parts: list[str] = []
            if pre_conflict_report.blocking:
                resources = ", ".join(
                    sorted({conflict.resource for conflict in pre_conflict_report.conflicts})
                )
                reason_parts.append(f"检测到资源冲突: {resources}")
            if pending_actions:
                actions = ", ".join(action.action_type for action in pending_actions)
                reason_parts.append(f"检测到需要审批的外部副作用动作: {actions}")

            answer = "任务已暂停，等待确认。" + "；".join(reason_parts)
            answer = await self._translate_answer(
                answer=answer,
                task_ref=task_ref,
                tenant=tenant,
                status="paused",
                output_kind=output_kind,
            )
            paused_result = TaskResult(
                task_id=task_ref.meta.task_id,
                status="paused",
                answer=answer,
                duration_sec=time.perf_counter() - t0,
            )
            paused_runtime = RuntimeState(
                state_id=initial_runtime.state_id,
                task_ref=task_ref.meta.task_id,
                total_planned_steps=len(plan.steps),
                status="paused",
                finished_at=datetime.now(UTC),
            )
            async with session_scope() as s:
                await _persist_runtime_snapshot(s, paused_runtime, tenant.tenant_id)
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.paused.preflight",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            "reason": answer,
                            "conflicts": [
                                conflict.model_dump(mode="json")
                                for conflict in pre_conflict_report.conflicts
                            ],
                            "pending_actions": [
                                action.model_dump(mode="json") for action in pending_actions
                            ],
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
                await _persist_task_result(s, tenant_id=tenant.tenant_id, result=paused_result)

            yield OrchestratorEvent(
                kind="guard_intervention",
                data={
                    "stage": "preflight",
                    "reason": answer,
                    "conflicts": [
                        conflict.model_dump(mode="json")
                        for conflict in pre_conflict_report.conflicts
                    ],
                    "pending_actions": [
                        action.model_dump(mode="json") for action in pending_actions
                    ],
                },
            )
            yield OrchestratorEvent(
                kind="answer",
                data={"content": answer, "task_id": task_ref.meta.task_id},
            )
            yield OrchestratorEvent(
                kind="done",
                data={"result": paused_result.model_dump(mode="json")},
            )
            return

        # 6. Create RuntimeState
        runtime = RuntimeState(
            state_id=initial_runtime.state_id,
            task_ref=task_ref.meta.task_id,
            total_planned_steps=len(plan.steps),
            status="running",
        )
        async with session_scope() as s:
            await _persist_runtime_snapshot(s, runtime, tenant.tenant_id)
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="task.started",
                    payload={
                        "task_id": task_ref.meta.task_id,
                        "role_template": choice.role_template_id,
                        "model_purpose": choice.purpose,
                    },
                    task_ref=task_ref.meta.task_id,
                ),
            )
            task_started_total.labels(
                tenant_id=tenant.tenant_id, task_type=task_ref.meta.task_type
            ).inc()

        # 7. Select candidate skills (L1 summary injected into step prompt)
        context_pack = await self.context_packer.pack(
            task_ref,
            tenant_id=tenant.tenant_id,
            limit=5,
        )
        context_summary = context_pack.summary()
        if context_pack.items:
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "context_preheat",
                    "asset_ids": [item.asset_id for item in context_pack.items],
                },
            )

        skill_candidates = self.skill_selector.select(task_ref, top_k=3)
        if skill_candidates:
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "skill_selection",
                    "candidates": [s.skill_id for s in skill_candidates],
                },
            )

        # Build the <skill>-aware system-prompt addendum so the agent loop
        # can dispatch real tool calls (R-A2). We feed every executable
        # builtin that's both registered AND a candidate into the directive.
        from kun.engineering.agent_loop import build_skill_directive
        from kun.skills.dispatcher import is_registered as _skill_is_registered

        skill_summaries = [
            (
                s.skill_id,
                s.manifest.description,
                dict(s.manifest.input_schema or {}),
            )
            for s in skill_candidates
            if _skill_is_registered(s.skill_id)
        ]
        skill_directive = build_skill_directive(skill_summaries) if skill_summaries else ""

        # Proactive tool dispatch (主动用工具 layer 1) — scan the user message
        # for keyword triggers and pre-dispatch matching skills BEFORE asking
        # the LLM anything. The result is injected into step 1's user-turn so
        # the LLM sees concrete data instead of "you might want to call X".
        from kun.engineering.proactive_tools import proactive_dispatch

        required_tools_hint = list(task_ref.spec.required_tools) if task_ref.spec else None
        proactive_scan = await proactive_dispatch(
            prompt=user_message,
            required_tools_hint=required_tools_hint,
        )
        pre_dispatched_block = proactive_scan.to_prefix_message()
        if proactive_scan.dispatched:
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "proactive_tools",
                    "skills": [d.skill_id for d in proactive_scan.dispatched],
                    "reasons": [d.trigger_reason for d in proactive_scan.dispatched],
                },
            )

        # 主动用工具 layer 4: 触发器命中但没成功 dispatch 的 → emit task.tool_skipped.
        # 守望子系统订阅这个事件 → 累积到 capability_card → 阈值上去自动加触发器
        # 或升级"强制用工具"等级. 现在先把信号送出去.
        if proactive_scan.missed_opportunities:
            try:
                async with session_scope(tenant_id=tenant.tenant_id) as s:
                    await emit(
                        s,
                        Event.build(
                            tenant_id=tenant.tenant_id,
                            event_type="task.tool_skipped",
                            payload={
                                "task_id": task_ref.meta.task_id,
                                "missed": proactive_scan.missed_opportunities,
                                "prompt_excerpt": user_message[:200],
                            },
                            task_ref=task_ref.meta.task_id,
                        ),
                    )
            except Exception as e:
                log.warning("orchestrator.tool_skipped_emit_failed", error=str(e))
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "tool_skipped",
                    "missed": [m["skill_id"] for m in proactive_scan.missed_opportunities],
                    "reasons": [m["reason"] for m in proactive_scan.missed_opportunities],
                },
            )

        # 8. Execute steps
        answer = ""
        status: TaskStatus = "running"
        notifications: list[Notification] = []
        last_response: LLMResponse | None = None
        step_outputs: list[tuple[int, str]] = []

        try:
            for step_plan in plan.steps:
                # Hard task-level deadline check (R-D1).
                if time.monotonic() > deadline_monotonic:
                    raise TaskTimedOutError(
                        task_ref.meta.task_id,
                        time.perf_counter() - t0,
                        duration_cap,
                    )

                step_t0 = time.perf_counter()

                # V2.2 §19.4 wire: 守望主决策 gate (opt-in)
                # 在每一步开始前算 expected_value + marginal_roi, 决定 continue/skip/stop/escalate
                if self.value_gate is not None:
                    try:
                        gate_decision = await self.value_gate.check_step(
                            task_ref=task_ref,
                            step_plan=step_plan,
                            prior_value_history=list(self._value_history),
                            context={"purpose": str(choice.purpose)},
                        )
                        if gate_decision.decision in ("stop", "escalate"):
                            yield OrchestratorEvent(
                                kind="value_gate_intervention",
                                data={
                                    "step_id": step_plan.step_id,
                                    "decision": gate_decision.decision,
                                    "reason": gate_decision.reason,
                                    "expected_value": gate_decision.expected_value,
                                },
                            )
                            # stop / escalate 都中止当前 step loop
                            status = "paused" if gate_decision.decision == "escalate" else "done"
                            break
                        if gate_decision.decision == "skip":
                            yield OrchestratorEvent(
                                kind="value_gate_skip",
                                data={
                                    "step_id": step_plan.step_id,
                                    "reason": gate_decision.reason,
                                    "expected_value": gate_decision.expected_value,
                                },
                            )
                            continue
                    except Exception:
                        log.exception("value_gate.check_step failed (non-fatal)")

                yield OrchestratorEvent(
                    kind="action",
                    data={"step_id": step_plan.step_id, "description": step_plan.description},
                )

                # Build the per-step profile: thread the caller's audience
                # preference (R-N3) and the budget kill switch into a copy so
                # we don't mutate the router-returned default.
                profile_updates: dict[str, Any] = {"audience": tenant.audience}
                if force_fallback:
                    profile_updates["force_fallback"] = True
                exec_profile = choice.task_profile.model_copy(update=profile_updates)

                # Execute via LLM with skill candidates hinted in system prompt.
                # When skill_directive is non-empty the agent loop is active
                # and the LLM may call <skill> tools (dispatched in agent_loop).
                # pre_dispatched_block carries proactive tool results from
                # the keyword trigger scan above; only attach to step 1 to
                # avoid re-injecting the same prefix every iteration.
                step_pre_dispatched = pre_dispatched_block if step_plan.step_id == 1 else ""

                # OTel: per-step span so Grafana 能按 step_id × provider 切片成本和延迟
                from opentelemetry import trace as _trace

                step_tracer = _trace.get_tracer("kun.orchestrator")
                with step_tracer.start_as_current_span("kun.orchestrator.step") as step_span:
                    step_span.set_attribute("kun.task_id", task_ref.meta.task_id)
                    step_span.set_attribute("kun.step_id", step_plan.step_id)
                    step_span.set_attribute("kun.skill_hint", step_plan.skill_hint or "")
                    step_span.set_attribute("kun.audience", tenant.audience)
                    step_span.set_attribute("kun.force_fallback", force_fallback)
                    answer, response = await self._execute_step(
                        task_ref=task_ref,
                        step_description=step_plan.description,
                        purpose=choice.purpose,
                        profile=exec_profile,
                        skills_summary=self.skill_selector.summary(skill_candidates),
                        skill_directive=skill_directive,
                        context_summary=context_summary,
                        prior_outputs=step_outputs,
                        pre_dispatched_block=step_pre_dispatched,
                    )
                    step_span.set_attribute("kun.provider", response.provider)
                    step_span.set_attribute("kun.model", response.model)
                    step_span.set_attribute("kun.tier", str(response.tier))
                    step_span.set_attribute("kun.cost_usd_equivalent", response.cost_usd_equivalent)
                    step_span.set_attribute("kun.tokens_in", response.usage.input_tokens)
                    step_span.set_attribute("kun.tokens_out", response.usage.output_tokens)
                last_response = response
                step_outputs.append((step_plan.step_id, answer))

                duration = time.perf_counter() - step_t0
                step_record = StepRecord(
                    step_id=step_plan.step_id,
                    skill_used=step_plan.skill_hint or "llm.direct",
                    cost_usd_actual=response.cost_usd_actual,
                    cost_usd_equivalent=response.cost_usd_equivalent,
                    duration_sec=duration,
                    tokens_in=response.usage.input_tokens,
                    tokens_out=response.usage.output_tokens,
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                )
                runtime.accumulate_step(step_record)

                # emit cost_tick
                yield OrchestratorEvent(
                    kind="cost_tick",
                    data={
                        "step_id": step_record.step_id,
                        "provider": response.provider,
                        "model": response.model,
                        "tier": response.tier,
                        "cost_usd_actual": response.cost_usd_actual,
                        "cost_usd_equivalent": response.cost_usd_equivalent,
                        "tokens_in": response.usage.input_tokens,
                        "tokens_out": response.usage.output_tokens,
                        "accumulated_usd": runtime.accumulated_cost_usd_equivalent,
                        "latency_ms": response.latency_ms,
                    },
                )

                # Persist event
                async with session_scope() as s:
                    await emit(
                        s,
                        Event.build(
                            tenant_id=tenant.tenant_id,
                            event_type="task.step.completed",
                            payload={
                                "task_id": task_ref.meta.task_id,
                                "step_id": step_record.step_id,
                                "accumulated_cost_usd": runtime.accumulated_cost_usd_equivalent,
                                "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
                                "cost_delta_usd": response.cost_usd_equivalent,
                                "tokens": response.usage.total(),
                            },
                            task_ref=task_ref.meta.task_id,
                        ),
                    )

                # Run watchtower rules
                namespace = {
                    "event": {
                        "event_type": "task.step.completed",
                        "payload": {
                            "accumulated_cost_usd": runtime.accumulated_cost_usd_equivalent,
                            "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
                        },
                        "tenant_id": tenant.tenant_id,
                    },
                    "task": {
                        "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
                        "risk_level": task_ref.meta.risk_level,
                    },
                    "tenant_id": tenant.tenant_id,
                    "task_ref": task_ref.meta.task_id,
                }
                fired = await self.rule_engine.evaluate("task.step.completed", namespace=namespace)
                if fired:
                    yield OrchestratorEvent(
                        kind="guard_intervention",
                        data={"rules_fired": fired},
                    )

                # V2.1 §5.8 wire: step 完, 让 EmergentSwitchManager 检测信号 (M5 真切, 现在只 emit)
                if self.emergent_switch_manager is not None:
                    try:
                        self.emergent_switch_manager.step_completed(
                            task_ref.meta.task_id,
                            surprise_score=0.0,  # M4 接 surprise detector 后接真值
                        )
                        signals = self.emergent_switch_manager.detect_signals(
                            task_ref.meta.task_id,
                            task_type=str(choice.purpose),
                        )
                        if signals:
                            yield OrchestratorEvent(
                                kind="emergent_signal",
                                data={
                                    "task_id": task_ref.meta.task_id,
                                    "signals": signals,
                                    "step_id": step_record.step_id,
                                },
                            )
                    except Exception:
                        log.exception("emergent_switch.detect_signals failed (non-fatal)")

                # V2.2 §19.4 wire: step 完, 让 ValueGate 记 outcome + 更新 value_history
                if self.value_gate is not None:
                    try:
                        # 启发式 step value: 基础 0.6 + 步号 0.05 增量, cap 1.0
                        # M4: 接 capability_card 历史 success_rate / multi_judge consensus
                        step_value = min(
                            1.0,
                            0.6 + 0.05 * step_record.step_id,
                        )
                        self._value_history.append(step_value)
                        await self.value_gate.record_step_outcome(
                            task_id=task_ref.meta.task_id,
                            step_id=step_record.step_id,
                            outcome_value=step_value,
                            cost_usd=response.cost_usd_equivalent,
                            success=True,
                        )
                    except Exception:
                        log.exception("value_gate.record_step_outcome failed (non-fatal)")

            status = "done"
        except TaskTimedOutError as exc:
            status = "failed"
            log.warning(
                "orchestrator.timed_out",
                task_id=task_ref.meta.task_id,
                elapsed_sec=exc.elapsed_sec,
                cap_sec=exc.cap_sec,
            )
            answer = (
                f"任务超时，已强制结束（{exc.elapsed_sec:.0f}s 超过上限 {exc.cap_sec:.0f}s）。"
                "如需放宽，请在 TaskProfile.max_duration_sec 或 KUN_TASK_MAX_DURATION_SEC 调整。"
            )
            yield OrchestratorEvent(
                kind="error",
                data={
                    "message": "task_timed_out",
                    "task_id": task_ref.meta.task_id,
                    "elapsed_sec": exc.elapsed_sec,
                    "cap_sec": exc.cap_sec,
                },
            )
            async with session_scope() as s:
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.timed_out",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            "elapsed_sec": exc.elapsed_sec,
                            "cap_sec": exc.cap_sec,
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
        except Exception as exc:
            status = "failed"
            log.exception("orchestrator.failed", error=str(exc))
            yield OrchestratorEvent(
                kind="error",
                data={"message": str(exc), "task_id": task_ref.meta.task_id},
            )
            answer = f"Sorry — task failed: {exc}"

        # 7. Finalize
        runtime.status = status
        runtime.finished_at = datetime.now(UTC)
        total_duration = time.perf_counter() - t0

        async with session_scope() as s:
            await s.execute(
                update(RuntimeStateRow)
                .where(RuntimeStateRow.state_id == runtime.state_id)
                .values(
                    status=runtime.status,
                    current_step=runtime.current_step,
                    accumulated_cost_usd_actual=runtime.accumulated_cost_usd_actual,
                    accumulated_cost_usd_equivalent=runtime.accumulated_cost_usd_equivalent,
                    accumulated_tokens=runtime.accumulated_tokens,
                    finished_at=runtime.finished_at,
                    blob=runtime.model_dump(mode="json"),
                )
            )
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="task.done" if status == "done" else "task.failed",
                    payload={
                        "task_id": task_ref.meta.task_id,
                        "status": status,
                        "accumulated_cost_usd": runtime.accumulated_cost_usd_equivalent,
                        "tokens": runtime.accumulated_tokens,
                        "duration_sec": total_duration,
                    },
                    task_ref=task_ref.meta.task_id,
                ),
            )

        task_duration_seconds.labels(task_type=task_ref.meta.task_type, status=status).observe(
            total_duration
        )

        # surprise_score (ADR-015)
        surprise = _compute_surprise_score(task_ref.meta, runtime)
        task_surprise_score.labels(task_type=task_ref.meta.task_type).observe(surprise)

        if surprise >= 0.6:
            notifications.append(
                Notification(
                    tenant_id=tenant.tenant_id,
                    kind="surprise"
                    if runtime.accumulated_cost_usd_equivalent < task_ref.meta.estimated_cost_usd
                    else "alert",
                    severity="insight",
                    channel="side",
                    title="任务意外度较高, 值得回看",
                    body=f"surprise_score={surprise:.2f}",
                    payload={"task_id": task_ref.meta.task_id},
                )
            )

        # 7.4 ValidationPipeline (ADR-018 §16.2). Tier selected from risk × complexity.
        validation_outcome: Outcome = "pass" if status == "done" else "fail"
        validation_score: float | None = None
        if status == "done":
            tier = pick_tier(task_ref.meta)
            # tier0 — cheapest path. We still run a structural sanity check
            # (R-A4) so an empty / trivially-broken answer trips a "partial"
            # outcome instead of silently passing.
            if tier == "tier0":
                stripped = answer.strip()
                if not stripped:
                    validation_outcome = "partial"
                    yield OrchestratorEvent(
                        kind="insight",
                        data={
                            "stage": "validation",
                            "tier": tier,
                            "verdict": "empty_answer",
                            "reason": "tier0 sanity: answer was empty",
                        },
                    )
                elif len(stripped) < 4:
                    validation_outcome = "partial"
                    yield OrchestratorEvent(
                        kind="insight",
                        data={
                            "stage": "validation",
                            "tier": tier,
                            "verdict": "too_short",
                            "reason": f"tier0 sanity: answer is only {len(stripped)} chars",
                        },
                    )
            if tier != "tier0" and answer.strip():
                try:
                    results = await self.validation.validate_task(
                        task_ref.meta,
                        answer,
                        goal=task_ref.meta.success_criteria_short,
                    )
                    aggregated = ValidationPipeline.aggregate(results)
                    if aggregated is not None:
                        validation_score = aggregated.score.value
                        if not aggregated.pass_:
                            validation_outcome = "partial"
                            yield OrchestratorEvent(
                                kind="insight",
                                data={
                                    "stage": "validation",
                                    "tier": tier,
                                    "verdict": "did_not_fully_pass",
                                    "score": validation_score,
                                    "reason": aggregated.reason,
                                },
                            )
                        else:
                            yield OrchestratorEvent(
                                kind="insight",
                                data={
                                    "stage": "validation",
                                    "tier": tier,
                                    "verdict": "passed",
                                    "score": validation_score,
                                },
                            )
                except Exception as e:
                    log.warning("validation.failed", error=str(e))

        # 7.5 Capability card writeback (ADR-018 §16.4 KnowledgePrecipitation)
        outcome: Outcome = validation_outcome
        rubric_5 = validation_score * 5.0 if validation_score is not None else None
        try:
            await record_outcome(
                tenant.tenant_id,
                TaskOutcome(
                    entity_type="role_template",
                    entity_id=choice.role_template_id,
                    task_type=task_ref.meta.task_type,
                    outcome=outcome,
                    cost_usd=runtime.accumulated_cost_usd_equivalent,
                    duration_sec=total_duration,
                    rubric_score=rubric_5,
                    surprise_score=surprise,
                ),
            )
            if last_response is not None:
                await record_outcome(
                    tenant.tenant_id,
                    TaskOutcome(
                        entity_type="model",
                        entity_id=last_response.model or "unknown",
                        task_type=task_ref.meta.task_type,
                        outcome=outcome,
                        cost_usd=last_response.cost_usd_equivalent,
                        duration_sec=last_response.latency_ms / 1000.0,
                        rubric_score=rubric_5,
                        surprise_score=surprise,
                    ),
                )
        except Exception as e:
            # Writeback failure must not break the task.
            log.warning("capability.writeback_failed", error=str(e))

        answer = await self._translate_answer(
            answer=answer,
            task_ref=task_ref,
            tenant=tenant,
            status=status,
            output_kind=output_kind,
        )
        result = TaskResult(
            task_id=task_ref.meta.task_id,
            status=status,
            answer=answer,
            cost_usd_actual=runtime.accumulated_cost_usd_actual,
            cost_usd_equivalent=runtime.accumulated_cost_usd_equivalent,
            tokens_in=sum(s.tokens_in for s in runtime.completed_steps),
            tokens_out=sum(s.tokens_out for s in runtime.completed_steps),
            duration_sec=total_duration,
            surprise_score=surprise,
            notifications=notifications,
        )

        async with session_scope() as s:
            await _persist_task_result(s, tenant_id=tenant.tenant_id, result=result)

        yield OrchestratorEvent(
            kind="answer",
            data={"content": answer, "task_id": task_ref.meta.task_id},
        )
        yield OrchestratorEvent(
            kind="done",
            data={"result": result.model_dump(mode="json")},
        )

    # ---------------------------- helpers ----------------------------

    async def _translate_answer(
        self,
        *,
        answer: str,
        task_ref: TaskRef,
        tenant: Any,
        status: str,
        output_kind: str,
    ) -> str:
        payload = {
            "task_id": task_ref.meta.task_id,
            "task_type": task_ref.meta.task_type,
            "status": status,
            "answer": answer,
            "success_criteria": task_ref.meta.success_criteria_short,
        }
        context = {
            "tenant_id": tenant.tenant_id,
            "audience": tenant.audience,
            "language": "zh",
        }
        try:
            return await self.output_translator(
                payload=payload,
                recipient_kind=output_kind,
                context=context,
            )
        except Exception as e:
            log.warning(
                "orchestrator.output_translate_failed", output_kind=output_kind, error=str(e)
            )
            return answer

    async def _execute_step(
        self,
        *,
        task_ref: TaskRef,
        step_description: str,
        purpose: TaskPurpose,
        profile: TaskProfile,
        skills_summary: str = "",
        skill_directive: str = "",
        context_summary: str = "",
        prior_outputs: list[tuple[int, str]] | None = None,
        pre_dispatched_block: str = "",
    ) -> tuple[str, LLMResponse]:
        """Execute a single step. Runs an agent loop so the LLM can call skills."""
        from kun.engineering.agent_loop import run_agent_loop

        # R-N3: pick a voice tier based on the caller's audience preference.
        audience = profile.audience if profile else "developer"
        audience_directive = _AUDIENCE_DIRECTIVES.get(audience, _AUDIENCE_DIRECTIVES["developer"])
        system_parts = [
            "你是 KUN 系统里的执行角色. 按用户要求完成任务, 回答准确、可验证. "
            "若需要外部数据, 说明需要什么. 不要编造.",
            audience_directive,
        ]
        if skills_summary:
            system_parts.append(skills_summary)
        if skill_directive:
            system_parts.append(skill_directive)
        if context_summary:
            system_parts.append(context_summary)
        system_prompt = "\n\n".join(system_parts)
        # User-turn body: standard execution prompt + any proactive tool
        # prefetch results (proactive_tools.py layer 1).
        user_content = _execution_user_prompt(
            task_ref,
            step_description,
            prior_outputs=prior_outputs or [],
        )
        if pre_dispatched_block:
            user_content = user_content + pre_dispatched_block
        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=system_prompt, cache=True),
                LLMMessage(role="user", content=user_content),
            ],
            temperature=0.5,
            max_tokens=1024,
            profile=profile,
        )
        # Run the ReAct loop — LLM may call skills via <skill> envelopes.
        # If it doesn't, this degrades to one normal LLM call.
        loop_result = await run_agent_loop(
            router=self.llm_router,
            purpose=purpose,
            initial_request=request,
            max_iterations=3,
        )
        # Roll iteration cost / tokens back into a single LLMResponse so the
        # rest of orchestrator (StepRecord, capability writeback, NUO panel)
        # sees the full step weight, not just the final iteration.
        aggregated = loop_result.final_response.model_copy(
            update={
                "content": loop_result.final_answer,
                "cost_usd_actual": loop_result.total_cost_actual,
                "cost_usd_equivalent": loop_result.total_cost_equivalent,
                "usage": loop_result.final_response.usage.model_copy(
                    update={
                        "input_tokens": loop_result.total_input_tokens,
                        "output_tokens": loop_result.total_output_tokens,
                    }
                ),
            }
        )
        return loop_result.final_answer, aggregated


# =================== helpers ===================


def _runtime_to_row(runtime: RuntimeState, tenant_id: str) -> RuntimeStateRow:
    return RuntimeStateRow(
        state_id=runtime.state_id,
        task_ref=runtime.task_ref,
        tenant_id=tenant_id,
        current_step=runtime.current_step,
        total_planned_steps=runtime.total_planned_steps,
        status=runtime.status,
        accumulated_cost_usd_actual=runtime.accumulated_cost_usd_actual,
        accumulated_cost_usd_equivalent=runtime.accumulated_cost_usd_equivalent,
        accumulated_tokens=runtime.accumulated_tokens,
        failures_this_run=runtime.failures_this_run,
        blob=runtime.model_dump(mode="json"),
        started_at=runtime.started_at,
        finished_at=runtime.finished_at,
        last_updated=runtime.last_updated,
    )


def _runtime_row_values(runtime: RuntimeState, tenant_id: str) -> dict[str, Any]:
    return {
        "state_id": runtime.state_id,
        "task_ref": runtime.task_ref,
        "tenant_id": tenant_id,
        "current_step": runtime.current_step,
        "total_planned_steps": runtime.total_planned_steps,
        "status": runtime.status,
        "accumulated_cost_usd_actual": runtime.accumulated_cost_usd_actual,
        "accumulated_cost_usd_equivalent": runtime.accumulated_cost_usd_equivalent,
        "accumulated_tokens": runtime.accumulated_tokens,
        "failures_this_run": runtime.failures_this_run,
        "blob": runtime.model_dump(mode="json"),
        "started_at": runtime.started_at,
        "finished_at": runtime.finished_at,
        "last_updated": runtime.last_updated,
    }


async def _persist_runtime_snapshot(session: Any, runtime: RuntimeState, tenant_id: str) -> None:
    """Insert or update a runtime snapshot by state_id."""
    values = _runtime_row_values(runtime, tenant_id)
    stmt = pg_insert(RuntimeStateRow).values(**values)
    update_values = {
        key: getattr(stmt.excluded, key)
        for key in values
        if key not in {"state_id", "task_ref", "tenant_id"}
    }
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[RuntimeStateRow.state_id],
            set_=update_values,
        )
    )


async def _find_idempotent_result_ref(
    session: Any,
    *,
    tenant_id: str,
    fingerprint: str,
) -> str | None:
    """Return the existing task id for a tenant/fingerprint, if any."""
    existing_key = await session.execute(
        select(IdempotencyRow).where(
            IdempotencyRow.key == fingerprint,
            IdempotencyRow.tenant_id == tenant_id,
        )
    )
    existing_row = existing_key.scalar_one_or_none()
    if existing_row is not None:
        return cast(str, existing_row.result_ref)

    existing_task = await session.execute(
        select(TaskRow.task_id).where(
            TaskRow.tenant_id == tenant_id,
            TaskRow.fingerprint == fingerprint,
        )
    )
    task_id = existing_task.scalar_one_or_none()
    return cast(str | None, task_id)


async def _load_cached_task_result(*, tenant_id: str, task_id: str) -> TaskResult | None:
    """Load a persisted final result for an idempotent duplicate request."""
    async with session_scope() as s:
        result = await s.execute(
            select(TaskResultRow.result_json).where(
                TaskResultRow.tenant_id == tenant_id,
                TaskResultRow.task_id == task_id,
            )
        )
        result_json = result.scalar_one_or_none()

    if isinstance(result_json, dict) and result_json:
        return TaskResult.model_validate(result_json)
    return None


async def _resolve_duplicate_without_cached_result(
    *,
    tenant_id: str,
    task_id: str,
    duration_sec: float,
) -> TaskResult:
    """Return a bounded duplicate response when no final result exists yet.

    If an old request crashed before runtime initialization completed, mark it
    failed and persist that result so repeat callers do not see a forever-queued
    task. Fresh queued/running/paused tasks still report their live status.
    """
    progress = await _load_task_progress(tenant_id=tenant_id, task_id=task_id)
    status, last_updated = progress

    if status is None or _is_stale_queued_status(status, last_updated):
        reason = (
            "Duplicate task detected, but the previous attempt appears to have stopped "
            "during initialization. Marked it failed so it will not look stuck forever."
        )
        result = TaskResult(
            task_id=task_id,
            status="failed",
            answer=reason,
            duration_sec=duration_sec,
        )
        now = datetime.now(UTC)
        async with session_scope(tenant_id=tenant_id) as s:
            if status is None:
                failed_runtime = RuntimeState(
                    task_ref=task_id,
                    total_planned_steps=1,
                    status="failed",
                    finished_at=now,
                    last_updated=now,
                )
                await _persist_runtime_snapshot(s, failed_runtime, tenant_id)
            else:
                await s.execute(
                    update(RuntimeStateRow)
                    .where(
                        RuntimeStateRow.tenant_id == tenant_id,
                        RuntimeStateRow.task_ref == task_id,
                        RuntimeStateRow.status == "queued",
                    )
                    .values(status="failed", finished_at=now, last_updated=now)
                )
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="task.failed",
                    payload={
                        "task_id": task_id,
                        "reason": "stale_duplicate_without_cached_result",
                    },
                    task_ref=task_id,
                ),
            )
            await _persist_task_result(s, tenant_id=tenant_id, result=result)
        return result

    message = f"Duplicate task detected. Existing task: {task_id}."
    return TaskResult(task_id=task_id, status=status, answer=message, duration_sec=duration_sec)


def _is_stale_queued_status(status: TaskStatus, last_updated: datetime | None) -> bool:
    if status != "queued" or last_updated is None:
        return False
    return datetime.now(UTC) - last_updated > _STALE_QUEUED_TASK_AFTER


async def _persist_task_result(session: Any, *, tenant_id: str, result: TaskResult) -> None:
    """Upsert the final task result so idempotent retries can return the old answer.

    When the serialized JSON exceeds ``KUN_RESULT_OFFLOAD_THRESHOLD_BYTES``
    (default 50 KiB) the heavy payload is written to MinIO instead and the
    DB row stores a small reference stub. Falls back to inline storage if
    the object store is unavailable.
    """
    from kun.core.object_store import maybe_offload_result_json

    now = datetime.now(UTC)
    full_payload = result.model_dump(mode="json")
    persisted_json, offload_ref = await maybe_offload_result_json(
        full_payload,
        tenant_id=tenant_id,
        task_id=result.task_id,
    )
    values = {
        "task_id": result.task_id,
        "tenant_id": tenant_id,
        "status": result.status,
        "answer": result.answer,
        "cost_usd_actual": result.cost_usd_actual,
        "cost_usd_equivalent": result.cost_usd_equivalent,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "duration_sec": result.duration_sec,
        "surprise_score": result.surprise_score,
        "notifications_json": [
            notification.model_dump(mode="json") for notification in result.notifications
        ],
        "result_json": persisted_json,
        "created_at": now,
        "updated_at": now,
    }
    if offload_ref is not None:
        log.debug(
            "task_result.offloaded",
            task_id=result.task_id,
            uri=offload_ref.uri,
            size_bytes=offload_ref.size_bytes,
        )
    stmt = pg_insert(TaskResultRow).values(**values)
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TaskResultRow.task_id],
            set_={
                "tenant_id": stmt.excluded.tenant_id,
                "status": stmt.excluded.status,
                "answer": stmt.excluded.answer,
                "cost_usd_actual": stmt.excluded.cost_usd_actual,
                "cost_usd_equivalent": stmt.excluded.cost_usd_equivalent,
                "tokens_in": stmt.excluded.tokens_in,
                "tokens_out": stmt.excluded.tokens_out,
                "duration_sec": stmt.excluded.duration_sec,
                "surprise_score": stmt.excluded.surprise_score,
                "notifications_json": stmt.excluded.notifications_json,
                "result_json": stmt.excluded.result_json,
                "updated_at": now,
            },
        )
    )


def _execution_user_prompt(
    task_ref: TaskRef,
    step_description: str,
    *,
    prior_outputs: list[tuple[int, str]] | None = None,
) -> str:
    """Build the execution prompt from TASK.md L1/L2 context."""
    lines = [
        "请执行当前任务步骤。",
        "",
        "任务身份:",
        f"- task_id: {task_ref.meta.task_id}",
        f"- task_type: {task_ref.meta.task_type}",
        f"- risk_level: {task_ref.meta.risk_level}",
        f"- complexity_score: {task_ref.meta.complexity_score:.2f}",
        "",
        "当前步骤:",
        f"- {step_description}",
        "",
        "成功标准:",
        f"- {task_ref.meta.success_criteria_short}",
    ]

    if prior_outputs:
        lines.extend(["", "已完成步骤输出摘要:"])
        for step_id, output in prior_outputs[-3:]:
            preview = output.strip().replace("\n", " ")
            if len(preview) > 400:
                preview = preview[:397].rstrip() + "..."
            lines.append(f"- step {step_id}: {preview}")

    if task_ref.spec is not None:
        spec = task_ref.spec
        lines.extend(["", "原始目标:", f"- {spec.goal_detail}"])
        if spec.success_metrics:
            lines.extend(["", "可验证指标:", *[f"- {metric}" for metric in spec.success_metrics]])
        if spec.constraints:
            lines.extend(
                [
                    "",
                    "约束:",
                    *[
                        f"- {constraint.kind}: {constraint.detail}"
                        for constraint in spec.constraints
                    ],
                ]
            )
        if spec.required_tools:
            lines.extend(["", "可能需要的工具:", *[f"- {tool}" for tool in spec.required_tools]])
        if spec.external_resources:
            lines.extend(
                ["", "外部资源:", *[f"- {resource}" for resource in spec.external_resources]]
            )
        if spec.foreseen_risks:
            lines.extend(
                [
                    "",
                    "已预见风险:",
                    *[
                        f"- {risk.severity}: {risk.description}"
                        + (f"；应对: {risk.mitigation_hint}" if risk.mitigation_hint else "")
                        for risk in spec.foreseen_risks
                    ],
                ]
            )
        if spec.fallback_plan:
            lines.extend(["", "失败回退方案:", f"- {spec.fallback_plan}"])

    lines.extend(
        [
            "",
            "输出要求:",
            "- 直接给结果。",
            "- 如果信息不足，明确说缺什么，不要编造。",
            "- 如果触碰约束或高风险动作，先说明风险和需要确认的点。",
        ]
    )
    return "\n".join(lines)


async def _today_cost_vs_budget(tenant_id: str) -> tuple[float, float]:
    """Return (cost_used_today_usd, daily_cap_usd) for a tenant.

    Sums ``cost_usd_equivalent`` from finished task results since 00:00 UTC.
    A cap of 0 means "no budget configured" — caller treats this as no gate.
    """
    cfg = settings()
    cap = float(cfg.budget_daily_usd)
    if cap <= 0:
        return (0.0, 0.0)
    today_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    async with session_scope(tenant_id=tenant_id) as s:
        result = await s.execute(
            select(func.coalesce(func.sum(TaskResultRow.cost_usd_equivalent), 0.0))
            .where(TaskResultRow.tenant_id == tenant_id)
            .where(TaskResultRow.created_at >= today_start)
        )
        used = float(result.scalar_one() or 0.0)
    return (used, cap)


async def _load_task_progress(
    *,
    tenant_id: str,
    task_id: str,
) -> tuple[TaskStatus | None, datetime | None]:
    """Load the latest runtime status and timestamp for an existing task."""
    async with session_scope(tenant_id=tenant_id) as s:
        result = await s.execute(
            select(RuntimeStateRow.status, RuntimeStateRow.last_updated)
            .where(
                RuntimeStateRow.tenant_id == tenant_id,
                RuntimeStateRow.task_ref == task_id,
            )
            .order_by(RuntimeStateRow.last_updated.desc())
            .limit(1)
        )
        row = result.one_or_none()

    if row is None:
        return None, None
    status, last_updated = row
    if status in {"queued", "running", "paused", "done", "failed", "cancelled"}:
        return cast(TaskStatus, status), cast(datetime, last_updated)
    return None, cast(datetime | None, last_updated)


def _compute_surprise_score(meta: TaskMeta, runtime: RuntimeState) -> float:
    """ADR-015 formula:
    surprise = 0.35*cost_dev + 0.20*step_dev + 0.25*path_novelty + 0.20*quality_dev.
    Walking skeleton: path_novelty and quality_dev = 0 (we don't have baselines yet).
    """
    cost_dev = 0.0
    if meta.estimated_cost_usd > 0:
        cost_dev = max(0.0, runtime.accumulated_cost_usd_equivalent / meta.estimated_cost_usd - 1.0)
    step_dev = 0.0
    if runtime.total_planned_steps > 0:
        step_dev = max(0.0, runtime.current_step / runtime.total_planned_steps - 1.0)
    score = 0.35 * cost_dev + 0.20 * step_dev
    return min(1.0, score)


__all__ = ["Orchestrator", "OrchestratorEvent", "TaskResult"]
