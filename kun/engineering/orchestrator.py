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

import asyncio
import json
import os as _os
import time
from collections.abc import AsyncIterator, Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from kun.brain.intent import IntentInterpreter
from kun.brain.planner import TaskPlanner
from kun.brain.router import TaskRouter
from kun.context.packer import ContextPack, ContextPacker
from kun.core.config import settings
from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.logging import get_logger
from kun.core.metrics import (
    task_duration_seconds,
    task_started_total,
    task_surprise_score,
)
from kun.core.ooda_loop import OODACycle, OODAEngine, OODAState
from kun.core.orm import IdempotencyRow, MissionRow, RuntimeStateRow, TaskResultRow, TaskRow
from kun.core.tenancy import current_tenant
from kun.datamodel.decision_ticket import (
    DecisionTicket,
    ticket_from_anti_gaming_finding,
    ticket_from_budget_policy,
    ticket_from_context_selection,
    ticket_from_delivery_review,
    ticket_from_emergent_switch,
    ticket_from_execution_mode_selection,
    ticket_from_llm_route,
    ticket_from_memory_policy_selection,
    ticket_from_ooda_checkpoint,
    ticket_from_preflight_guard,
    ticket_from_proactive_tool_dispatch,
    ticket_from_protocol_applied,
    ticket_from_route_choice,
    ticket_from_skill_selection,
    ticket_from_step_action_selection,
    ticket_from_validation_tier,
    ticket_from_value_gate_decision,
    ticket_from_watchtower_decision,
)
from kun.datamodel.events import Event
from kun.datamodel.mission import ResumeRequest
from kun.datamodel.notification import Notification
from kun.datamodel.runtime import RuntimeState, StepRecord, TaskStatus
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.budget_tracker import HARD_BREAK_RATIO_TASK, BudgetTracker
from kun.engineering.capability_writeback import Outcome, TaskOutcome, record_outcome
from kun.engineering.concurrency import (
    PendingActionSpec,
    PreConflictReport,
    enqueue_pending_actions,
    pending_actions_for,
    scan_pre_conflicts,
)
from kun.engineering.credit_assignment import (
    CreditAssignment,
    get_contribution_tracker,
    heuristic_reflector,
    hydrate_contribution_tracker_from_db,
    load_resource_credit_scores,
    persist_resource_credit_report,
)
from kun.engineering.validation import ValidationPipeline, pick_tier
from kun.interface.adapters import translate_for
from kun.interface.hermes import DefaultHermesAdapter, HermesAdapter
from kun.interface.llm import (
    LLMMessage,
    LLMRequest,
    LLMResponse,
    LLMRouter,
    TaskProfile,
    UsageInfo,
    get_router,
)
from kun.interface.llm.router import TaskPurpose
from kun.memory.policy import MemoryPolicyTicket, decide_memory_policy
from kun.memory.similar_task_recall import recall_similar_task_experiences
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


class TaskBudgetExceededError(RuntimeError):
    """Raised when a task exceeds its hard budget cap."""

    def __init__(self, task_id: str, used_usd: float, limit_usd: float) -> None:
        super().__init__(f"task {task_id} exceeded budget cap: ${used_usd:.4f} > ${limit_usd:.4f}")
        self.task_id = task_id
        self.used_usd = used_usd
        self.limit_usd = limit_usd


class TaskCancelledByUserError(RuntimeError):
    """Raised when the shared KillSwitch asks this task to stop."""

    def __init__(self, task_id: str, reason: str) -> None:
        super().__init__(f"task {task_id} cancelled by user: {reason}")
        self.task_id = task_id
        self.reason = reason


class TaskPausedByWatchtowerError(RuntimeError):
    """Raised when a Watchtower hard action pauses a task."""

    def __init__(self, task_id: str, rules_fired: list[str]) -> None:
        super().__init__(f"task {task_id} paused by watchtower rules: {', '.join(rules_fired)}")
        self.task_id = task_id
        self.rules_fired = list(rules_fired)


class TaskPausedByWorldActionError(RuntimeError):
    """Raised when an execution-time skill requests a WorldGateway approval gate."""

    def __init__(self, task_id: str, actions: list[PendingActionSpec]) -> None:
        action_types = ", ".join(action.action_type for action in actions)
        super().__init__(f"task {task_id} paused for world action approval: {action_types}")
        self.task_id = task_id
        self.actions = actions


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
        structured_step_generator: Any = None,
        verification_runner: Any = None,
        prediction_provider: Any = None,
        model_updater: Any = None,
        protocol_registry: Any = None,
        anti_gaming_detector: Any = None,
        decision_plane: Any = None,
        state_ledger: Any = None,
        hermes_adapter: HermesAdapter | None = None,
        memory_writeback: Any = None,
        scoring_system: Any = None,
        credit_assignment: CreditAssignment | None = None,
        budget_tracker: BudgetTracker | None = None,
        kill_switch: Any = None,
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
        # V2.2 §22 wire: hermes 结构化执行协议 generator (SMART/MAX 模式启用)
        self.structured_step_generator = structured_step_generator
        self.verification_runner = verification_runner
        # V2.3 Wire 41 Predictive Coding hook (插件式, None 时鲲行为完全不变)
        self.prediction_provider = prediction_provider
        self.model_updater = model_updater
        # V2.3 Wire 53 (C71): ProtocolRegistry — task 启动前 match → 改 ExecutionMode
        # / hermes addon / verification specs. None 时鲲行为完全不变.
        self.protocol_registry = protocol_registry
        # V2.3 Wire 53 (C72): AntiGamingDetector — step 完后跑 quick check.
        # None 时鲲行为完全不变.
        self.anti_gaming_detector = anti_gaming_detector
        # V3: Watchtower Decision Plane — 守望统一产出策略单.
        # 它不执行任务, 但策略单会被本 orchestrator 消费.
        self.decision_plane = decision_plane
        # V3-2: State Ledger — 当前状态账本. 它不替代 DB/EventRow,
        # 只提供 UI/LLM/黑板读的热快照。
        self.state_ledger = state_ledger
        # V3-3: Hermes full-chain adapter. 它把 LLM prompt / skill 输入输出 /
        # 外部对象格式统一成一层翻译契约, 避免各模块各说各话。
        self.hermes_adapter = hermes_adapter or DefaultHermesAdapter()
        # V3-4: three-layer memory writeback.  Optional in tests, but the
        # production runtime installs it so later ContextPacker calls can reuse
        # result/process/meta-decision memories.
        self.memory_writeback = memory_writeback
        # V3-6: unified scorecard.  Its output feeds capability writeback and
        # memory, so metrics are not just a side dashboard.
        self.scoring_system = scoring_system
        self.credit_assignment = (
            credit_assignment
            if credit_assignment is not None
            else (
                CreditAssignment()
                if _os.getenv("KUN_CREDIT_ASSIGNMENT_ENABLED", "1") == "1"
                else None
            )
        )
        self.budget_tracker = (
            budget_tracker
            if budget_tracker is not None
            else BudgetTracker()
            if _os.getenv("KUN_BUDGET_TRACKER_ENABLED", "1") == "1"
            else None
        )
        # Shared with /api/tasks/{id}/kill.  This is process-local by design:
        # REST/WS can interrupt work running in this API process.  Durable
        # queued/resume control is a separate worker concern.
        self.kill_switch = kill_switch
        # 累计 step value history, 给 value_gate marginal_roi 用
        self._value_history: list[float] = []

    # ----------------------------- public entry -----------------------------

    async def run(
        self,
        user_message: str,
        *,
        output_kind: str = "user",
        mission_id: str | None = None,
        mission_strategy: dict[str, Any] | None = None,
    ) -> TaskResult:
        """Non-streaming entry. Useful for tests / HTTP POST."""
        final: TaskResult | None = None
        async for ev in self.stream(
            user_message,
            output_kind=output_kind,
            mission_id=mission_id,
            mission_strategy=mission_strategy,
        ):
            if ev.kind == "done":
                final = TaskResult.model_validate(ev.data["result"])
        if final is None:
            raise RuntimeError("orchestrator exited without a done event")
        return final

    async def run_mission_continuation(
        self,
        request: ResumeRequest,
        resume_prompt: str,
        *,
        output_kind: str = "mission_worker",
    ) -> TaskResult:
        """Start a continuation execution for a durable Mission task.

        This does not execute the original TaskRow in-place. The Mission worker
        links the returned continuation task back to the original mission task.
        """
        tenant = current_tenant()
        async with session_scope(tenant_id=tenant.tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="task.resumed",
                    payload={
                        **request.model_dump(mode="json"),
                        "mode": "mission_continuation_task",
                        "source_task_id": request.task_id,
                        "output_kind": output_kind,
                    },
                    task_ref=request.task_id,
                ),
            )
        mission_strategy = await _load_mission_strategy(
            tenant_id=tenant.tenant_id,
            mission_id=request.mission_id,
        )
        return await self.run(
            resume_prompt,
            output_kind=output_kind,
            mission_id=request.mission_id,
            mission_strategy=mission_strategy,
        )

    async def stream(
        self,
        user_message: str,
        *,
        max_duration_sec: float | None = None,
        output_kind: str = "user",
        mission_id: str | None = None,
        mission_strategy: dict[str, Any] | None = None,
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
        _attach_task_parent(task_ref, mission_id)

        # 1.5 V2.1 wire (M3.3, opt-in) + V2.2 §19.3/C25 wire (always on if panorama enabled):
        # FAST/SMART/MAX 模式按需展开 panorama 模块, 不一次性构造 12 个.
        # KUN_PANORAMA_BUILDER_ENABLED=1 启用; 默认 off, 不破坏现有流程.
        from kun.engineering.panorama_orchestrator_bridge import (
            anchored_modules_to_event_data,
            build_panorama_anchored_for_task,
            build_panorama_for_task,
            panorama_to_event_data,
        )
        from kun.engineering.panorama_orchestrator_bridge import (
            is_enabled as _panorama_enabled,
        )

        if _panorama_enabled():
            # V2.2 wire: 优先用 build_anchored (按 mode 展开), fallback 老 expand
            _anchored_modules = await build_panorama_anchored_for_task(task_ref, user_message)
            if _anchored_modules:
                yield OrchestratorEvent(
                    kind="action_plan",
                    data=anchored_modules_to_event_data(task_ref, _anchored_modules),
                )
            else:
                # fallback: 老 expand 路径 (已存在测试覆盖)
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

        self._record_state_ledger(
            "record_task_created",
            task_ref,
            tenant_id=tenant.tenant_id,
            status="queued",
        )
        self._register_task_control(task_ref.meta.task_id)
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
        if self.budget_tracker is not None:
            self.budget_tracker.register_budget(
                "task",
                task_ref.meta.task_id,
                max(float(task_ref.meta.estimated_cost_usd), 0.000001),
            )

        # 3. Protocol + Watchtower pre-planning decision.
        # 这一步必须在 Planning 前完成, 否则策略包补充的 skill_hints
        # 只能影响 skill_selector, 不能影响 planner 生成的执行步骤。
        #
        # V2.3 Wire 53 (C71): ProtocolRegistry consume — task 启动前 match 协议
        # 找到 stable 协议 → 改 task_ref.meta.execution_mode (按 protocol.execution.mode)
        # 协议是 KUN 沉淀的 IP, 鲲消费协议 = "怎么做这个 task" 的标准说明书
        active_protocol: Any = None
        decision_tickets: list[DecisionTicket] = []
        ooda_engine = OODAEngine()
        ooda_cycle = OODACycle(
            task_ref=task_ref.meta.task_id,
            metadata={
                "mission_id": mission_id or "",
                "output_kind": output_kind,
                "duration_cap_sec": duration_cap,
            },
        )
        ooda_cycle = await ooda_engine.transition(
            ooda_cycle,
            OODAState.ORIENT,
            {
                "task_type": task_ref.meta.task_type,
                "risk_level": task_ref.meta.risk_level,
                "complexity_score": task_ref.meta.complexity_score,
                "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
                "success_criteria_short": task_ref.meta.success_criteria_short,
            },
        )
        ooda_ticket = await self._record_ooda_checkpoint(
            tenant_id=tenant.tenant_id,
            task_ref=task_ref,
            cycle=ooda_cycle,
            checkpoint="orient",
            decision_tickets=decision_tickets,
            reason="任务已进入外层 OODA 定向阶段",
            evidence={"task_l1": task_ref.meta.model_dump(mode="json")},
        )
        yield OrchestratorEvent(
            kind="action_plan",
            data={
                "stage": "ooda_orient",
                "decision_ticket": ooda_ticket.event_payload(),
            },
        )

        boundary_result = await _evaluate_task_boundary(
            tenant_id=tenant.tenant_id,
            task_ref=task_ref,
            output_kind=output_kind,
            mission_id=mission_id,
        )
        if boundary_result is not None:
            boundary_ticket, boundary_decision, boundary_scope = boundary_result
            decision_tickets.append(boundary_ticket)
            self._record_state_ledger("record_decision_ticket", boundary_ticket)
            async with session_scope(tenant_id=tenant.tenant_id) as s:
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.preflight_guard.evaluated",
                        payload=boundary_ticket.event_payload(),
                        task_ref=task_ref.meta.task_id,
                    ),
                )
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "task_boundary_guard",
                    "decision_ticket": boundary_ticket.event_payload(),
                    "in_scope": boundary_decision.in_scope,
                },
            )
            if not boundary_decision.in_scope and boundary_scope.boundary_strict_mode:
                answer = _boundary_pause_answer(boundary_decision, boundary_scope)
                paused_result = TaskResult(
                    task_id=task_ref.meta.task_id,
                    status="paused",
                    answer=answer,
                    duration_sec=time.perf_counter() - t0,
                )
                paused_runtime = RuntimeState(
                    task_ref=task_ref.meta.task_id,
                    total_planned_steps=1,
                    status="paused",
                    finished_at=datetime.now(UTC),
                )
                self._record_state_ledger(
                    "record_paused",
                    task_ref.meta.task_id,
                    reason="task_boundary_guard",
                    pending_confirmations=["task_boundary_redirect"],
                )
                async with session_scope(tenant_id=tenant.tenant_id) as s:
                    await _persist_runtime_snapshot(s, paused_runtime, tenant.tenant_id)
                    await _persist_task_result(s, tenant_id=tenant.tenant_id, result=paused_result)
                    await emit(
                        s,
                        Event.build(
                            tenant_id=tenant.tenant_id,
                            event_type="task.paused",
                            payload={
                                "task_id": task_ref.meta.task_id,
                                "task_type": task_ref.meta.task_type,
                                "scope": boundary_scope.model_dump(mode="json"),
                                "boundary_decision": boundary_decision.model_dump(mode="json"),
                            },
                            task_ref=task_ref.meta.task_id,
                        ),
                    )
                yield OrchestratorEvent(
                    kind="guard_intervention",
                    data={
                        "stage": "task_boundary_guard",
                        "level": "blocked",
                        "message": answer,
                        "decision_ticket": boundary_ticket.event_payload(),
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

        if (
            self.protocol_registry is not None
            and _os.getenv("KUN_PROTOCOL_CONSUME_ENABLED", "1") == "1"
        ):
            try:
                task_meta_dict = {
                    "task_type": task_ref.meta.task_type,
                    "complexity_score": task_ref.meta.complexity_score,
                    "risk_level": task_ref.meta.risk_level,
                }
                active_protocol = await self.protocol_registry.find_protocol_for(
                    task_meta_dict, tenant.tenant_id
                )
                if active_protocol is not None:
                    # 改 execution_mode (协议优先于 router decision)
                    if active_protocol.execution.mode in ("FAST", "SMART", "MAX", "ENSEMBLE"):
                        task_ref.meta.execution_mode = active_protocol.execution.mode
                    # 协议 hermes addon → 注入 step prompt (Wire 31 hermes 已 wire)
                    # 协议 verification → 加到 task_ref.spec.verification_specs.
                    # 不能因为 intent 没产 L2 TaskSpec 就静默丢掉协议验收；
                    # 没 spec 时补一个最小 TaskSpec, 保证 PreDeliverGate 真能跑。
                    if active_protocol.verification and task_ref.spec is None:
                        task_ref.spec = TaskSpec(
                            goal_detail=task_ref.meta.success_criteria_short,
                            success_metrics=[task_ref.meta.success_criteria_short],
                        )
                    if active_protocol.verification and task_ref.spec is not None:
                        from kun.datamodel.verification_spec import VerificationSpec

                        existing_specs = list(task_ref.spec.verification_specs or [])
                        for pv in active_protocol.verification:
                            existing_specs.append(
                                VerificationSpec(kind=pv.kind, spec=pv.spec, required=pv.required)
                            )
                        task_ref.spec.verification_specs = existing_specs

                    protocol_ticket = ticket_from_protocol_applied(
                        tenant_id=tenant.tenant_id,
                        task_id=task_ref.meta.task_id,
                        risk_level=task_ref.meta.risk_level,
                        estimated_cost_usd=task_ref.meta.estimated_cost_usd,
                        protocol=active_protocol,
                        mission_id=_mission_id_from_task(task_ref),
                    )
                    decision_tickets.append(protocol_ticket)
                    self._record_state_ledger("record_decision_ticket", protocol_ticket)
                    async with session_scope(tenant_id=tenant.tenant_id) as s:
                        await emit(
                            s,
                            Event.build(
                                tenant_id=tenant.tenant_id,
                                event_type="protocol.applied",
                                payload={
                                    "task_id": task_ref.meta.task_id,
                                    "protocol_id": active_protocol.protocol_id,
                                    "version": active_protocol.version,
                                    "applied_mode": active_protocol.execution.mode,
                                    "addon_skills": [s.skill for s in active_protocol.skill_chain],
                                    "addon_verifications": [
                                        v.kind for v in active_protocol.verification
                                    ],
                                    "decision_ticket": protocol_ticket.event_payload(),
                                },
                                task_ref=task_ref.meta.task_id,
                            ),
                        )
                    yield OrchestratorEvent(
                        kind="action_plan",
                        data={
                            "stage": "protocol_applied",
                            "task_id": task_ref.meta.task_id,
                            "decision_ticket": protocol_ticket.event_payload(),
                        },
                    )
                    await self._record_meta_decision_memory(
                        tenant_id=tenant.tenant_id,
                        task_ref=task_ref,
                        decision=active_protocol,
                        decision_ticket=protocol_ticket,
                    )
                    try:
                        from kun.core.metrics import protocol_match_total

                        protocol_match_total.labels(
                            protocol_id=active_protocol.protocol_id, hit="true"
                        ).inc()
                    except Exception:
                        pass
            except Exception:
                log.exception("protocol_consume.failed (non-fatal)")

        # V3: Watchtower Decision Plane consume — task 启动前选 StrategyPack,
        # 并真实影响 execution_mode / context_limit / required_skills.
        watchtower_decision: Any = None
        if (
            self.decision_plane is not None
            and _os.getenv("KUN_WATCHTOWER_DECISION_PLANE_ENABLED", "1") == "1"
        ):
            try:
                async with session_scope(tenant_id=tenant.tenant_id) as s:
                    await hydrate_contribution_tracker_from_db(
                        s,
                        tenant_id=tenant.tenant_id,
                        resource_kinds=("strategy_pack",),
                        limit=200,
                    )
                similar_experiences = await recall_similar_task_experiences(
                    tenant_id=tenant.tenant_id,
                    task_ref=task_ref,
                    store=_memory_store_from_writeback(self.memory_writeback),
                    limit=5,
                )
                watchtower_decision = self.decision_plane.decide(
                    task_ref,
                    active_protocol=active_protocol,
                    mission_strategy=mission_strategy,
                    similar_experiences=similar_experiences,
                )
                watchtower_ticket = ticket_from_watchtower_decision(
                    tenant_id=tenant.tenant_id,
                    task_id=task_ref.meta.task_id,
                    risk_level=task_ref.meta.risk_level,
                    estimated_cost_usd=task_ref.meta.estimated_cost_usd,
                    decision=watchtower_decision,
                    mission_id=_mission_id_from_task(task_ref),
                )
                decision_tickets.append(watchtower_ticket)
                self.decision_plane.apply(task_ref, watchtower_decision)
                self._record_state_ledger(
                    "record_decision",
                    task_ref.meta.task_id,
                    watchtower_decision,
                )
                self._record_state_ledger("record_decision_ticket", watchtower_ticket)
                async with session_scope(tenant_id=tenant.tenant_id) as s:
                    await emit(
                        s,
                        Event.build(
                            tenant_id=tenant.tenant_id,
                            event_type="watchtower.decision_plan.created",
                            payload={
                                "task_id": task_ref.meta.task_id,
                                **watchtower_decision.event_payload(),
                                "decision_ticket": watchtower_ticket.event_payload(),
                            },
                            task_ref=task_ref.meta.task_id,
                        ),
                    )
                yield OrchestratorEvent(
                    kind="action_plan",
                    data={
                        "stage": "watchtower_decision",
                        "task_id": task_ref.meta.task_id,
                        **watchtower_decision.event_payload(),
                        "decision_ticket": watchtower_ticket.event_payload(),
                    },
                )
                await self._record_meta_decision_memory(
                    tenant_id=tenant.tenant_id,
                    task_ref=task_ref,
                    decision=watchtower_decision,
                    decision_ticket=watchtower_ticket,
                )
            except Exception:
                log.exception("watchtower.decision_plane.failed (non-fatal)")

        execution_mode_ticket = ticket_from_execution_mode_selection(
            tenant_id=tenant.tenant_id,
            task_id=task_ref.meta.task_id,
            risk_level=task_ref.meta.risk_level,
            execution_mode=task_ref.meta.execution_mode,
            task_type=task_ref.meta.task_type,
            complexity_score=task_ref.meta.complexity_score,
            estimated_cost_usd=task_ref.meta.estimated_cost_usd,
            mode_override_reason=task_ref.meta.mode_override_reason,
            active_protocol=active_protocol,
            watchtower_decision=watchtower_decision,
            mission_id=_mission_id_from_task(task_ref),
        )
        decision_tickets.append(execution_mode_ticket)
        self._record_state_ledger("record_decision_ticket", execution_mode_ticket)
        async with session_scope(tenant_id=tenant.tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="task.execution_mode.selected",
                    payload=execution_mode_ticket.event_payload(),
                    task_ref=task_ref.meta.task_id,
                ),
            )
        yield OrchestratorEvent(
            kind="action_plan",
            data={
                "stage": "execution_mode",
                "task_id": task_ref.meta.task_id,
                "execution_mode": task_ref.meta.execution_mode,
                "decision_ticket": execution_mode_ticket.event_payload(),
            },
        )

        # 4. Planning
        plan = await self.planner.plan(task_ref, router=self.llm_router)
        self._record_state_ledger(
            "record_plan",
            task_ref.meta.task_id,
            total_steps=len(plan.steps),
        )

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
        route_ticket = ticket_from_route_choice(
            tenant_id=tenant.tenant_id,
            task_id=task_ref.meta.task_id,
            risk_level=task_ref.meta.risk_level,
            estimated_cost_usd=task_ref.meta.estimated_cost_usd,
            choice=choice,
            mission_id=_mission_id_from_task(task_ref),
        )
        decision_tickets.append(route_ticket)
        self._record_state_ledger("record_decision_ticket", route_ticket)
        async with session_scope(tenant_id=tenant.tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="task.route.selected",
                    payload=route_ticket.event_payload(),
                    task_ref=task_ref.meta.task_id,
                ),
            )
        yield OrchestratorEvent(
            kind="action_plan",
            data={
                "stage": "task_route",
                "task_id": task_ref.meta.task_id,
                "decision_ticket": route_ticket.event_payload(),
            },
        )
        ooda_cycle = await ooda_engine.transition(
            ooda_cycle,
            OODAState.DECIDE,
            {
                "execution_mode": task_ref.meta.execution_mode,
                "planned_steps": len(plan.steps),
                "role_template_id": choice.role_template_id,
                "purpose": str(choice.purpose),
                "expected_outcome": "done",
            },
        )
        ooda_ticket = await self._record_ooda_checkpoint(
            tenant_id=tenant.tenant_id,
            task_ref=task_ref,
            cycle=ooda_cycle,
            checkpoint="decide",
            decision_tickets=decision_tickets,
            reason="任务已完成路线选择，进入可执行决策阶段",
            evidence={
                "execution_mode": task_ref.meta.execution_mode,
                "planned_steps": len(plan.steps),
                "role_template_id": choice.role_template_id,
                "purpose": str(choice.purpose),
            },
        )
        yield OrchestratorEvent(
            kind="action_plan",
            data={
                "stage": "ooda_decide",
                "decision_ticket": ooda_ticket.event_payload(),
            },
        )

        # 5. Pre-start safety: conflict scan + pending side-effect actions.
        pending_actions = pending_actions_for(task_ref)
        preflight_ticket: DecisionTicket | None = None
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
            if pre_conflict_report.resources or pre_conflict_report.conflicts or pending_actions:
                preflight_ticket = ticket_from_preflight_guard(
                    tenant_id=tenant.tenant_id,
                    task_id=task_ref.meta.task_id,
                    risk_level=task_ref.meta.risk_level,
                    report=pre_conflict_report,
                    pending_actions=pending_actions,
                    mission_id=_mission_id_from_task(task_ref),
                )
                decision_tickets.append(preflight_ticket)
                self._record_state_ledger("record_decision_ticket", preflight_ticket)
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.preflight_guard.evaluated",
                        payload=preflight_ticket.event_payload(),
                        task_ref=task_ref.meta.task_id,
                    ),
                )

        if preflight_ticket is not None:
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "preflight_guard",
                    "decision_ticket": preflight_ticket.event_payload(),
                },
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
            self._record_state_ledger(
                "record_paused",
                task_ref.meta.task_id,
                reason=answer,
                pending_confirmations=[
                    *[conflict.resource for conflict in pre_conflict_report.conflicts],
                    *[action.action_type for action in pending_actions],
                ],
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
            self._cleanup_task_control(task_ref.meta.task_id)
            return

        # 6. Create RuntimeState
        runtime = RuntimeState(
            state_id=initial_runtime.state_id,
            task_ref=task_ref.meta.task_id,
            total_planned_steps=len(plan.steps),
            status="running",
        )
        self._record_state_ledger("record_running", task_ref.meta.task_id, runtime=runtime)
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
        # V2.2 §21 wire: mode-driven context limit
        # FAST 不查记忆 (limit=0), SMART 1 条, MAX/ENSEMBLE 3 条
        _task_mode = getattr(task_ref.meta, "execution_mode", "FAST")
        _context_limit = (
            int(watchtower_decision.context_limit)
            if watchtower_decision is not None
            else {"FAST": 0, "SMART": 1, "MAX": 3, "ENSEMBLE": 3}.get(_task_mode, 1)
        )
        memory_policy = _memory_policy_from_watchtower(watchtower_decision)
        memory_policy_source = "watchtower.decision_plane"
        if memory_policy is None:
            memory_policy = decide_memory_policy(
                task_ref,
                watchtower_decision=watchtower_decision,
            )
            memory_policy_source = "memory.policy.fallback"
        memory_policy_ticket = ticket_from_memory_policy_selection(
            tenant_id=tenant.tenant_id,
            task_id=task_ref.meta.task_id,
            risk_level=task_ref.meta.risk_level,
            policy=memory_policy,
            mission_id=_mission_id_from_task(task_ref),
            source_module=memory_policy_source,
        )
        decision_tickets.append(memory_policy_ticket)
        self._record_state_ledger("record_decision_ticket", memory_policy_ticket)
        async with session_scope(tenant_id=tenant.tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="memory.policy.selected",
                    payload=memory_policy_ticket.event_payload(),
                    task_ref=task_ref.meta.task_id,
                ),
            )
        yield OrchestratorEvent(
            kind="action_plan",
            data={
                "stage": "memory_policy_selected",
                "decision_ticket": memory_policy_ticket.event_payload(),
            },
        )
        if memory_policy is not None:
            if not memory_policy.use_memory:
                _context_limit = 0
            elif memory_policy.max_items > 0:
                _context_limit = min(_context_limit, memory_policy.max_items)
        memory_context_kwargs = (
            memory_policy.as_context_packer_kwargs()
            if memory_policy is not None and memory_policy.use_memory
            else {}
        )
        if _context_limit > 0:
            if _task_mode in {"MAX", "ENSEMBLE"}:
                context_items = []
                async for item in self.context_packer.pack_anchor_then_expand(
                    task_ref,
                    tenant_id=tenant.tenant_id,
                    kinds=memory_context_kwargs.get("kinds"),
                    max_rounds=_context_limit,
                    memory_layers=memory_context_kwargs.get("memory_layers"),
                    avoid_memory_layers=memory_context_kwargs.get("avoid_memory_layers"),
                    preferred_tags=memory_context_kwargs.get("preferred_tags"),
                    high_risk_task=bool(memory_context_kwargs.get("high_risk_task")),
                ):
                    context_items.append(item)
                    if len(context_items) >= _context_limit:
                        break
                process_experiences = (
                    await self.context_packer.recall_process_experiences(
                        task_ref,
                        tenant_id=tenant.tenant_id,
                    )
                    if _memory_policy_allows_process_recall(memory_policy)
                    else []
                )
                context_pack = ContextPack(
                    items=context_items,
                    process_experiences=process_experiences,
                )
            else:
                context_pack = await self.context_packer.pack(
                    task_ref,
                    tenant_id=tenant.tenant_id,
                    kinds=memory_context_kwargs.get("kinds"),
                    limit=_context_limit,
                    memory_layers=memory_context_kwargs.get("memory_layers"),
                    avoid_memory_layers=memory_context_kwargs.get("avoid_memory_layers"),
                    preferred_tags=memory_context_kwargs.get("preferred_tags"),
                    high_risk_task=bool(memory_context_kwargs.get("high_risk_task")),
                )
        else:
            context_pack = ContextPack()  # FAST 模式跳过, 空 pack
        context_summary = context_pack.summary()
        context_asset_ids = [item.asset_id for item in context_pack.items]
        context_resource_ids = _context_resource_ids(context_pack)
        self._record_state_ledger(
            "record_context",
            task_ref.meta.task_id,
            asset_ids=context_asset_ids,
        )
        context_ticket = ticket_from_context_selection(
            tenant_id=tenant.tenant_id,
            task_id=task_ref.meta.task_id,
            risk_level=task_ref.meta.risk_level,
            execution_mode=_task_mode,
            context_limit=_context_limit,
            context_pack=context_pack,
            mission_id=_mission_id_from_task(task_ref),
            memory_policy=memory_policy.model_dump(mode="json") if memory_policy else None,
        )
        decision_tickets.append(context_ticket)
        self._record_state_ledger("record_decision_ticket", context_ticket)
        async with session_scope(tenant_id=tenant.tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="context.selected",
                    payload=context_ticket.event_payload(),
                    task_ref=task_ref.meta.task_id,
                ),
            )
        if context_pack.items:
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "context_preheat",
                    "asset_ids": context_asset_ids,
                    "mode": _task_mode,
                    "decision_ticket": context_ticket.event_payload(),
                },
            )

        _skill_top_k = (
            max(3, len(watchtower_decision.skill_hints)) if watchtower_decision is not None else 3
        )
        skill_candidates = await self._select_skills_with_moe(
            tenant_id=tenant.tenant_id,
            task_ref=task_ref,
            top_k=_skill_top_k,
        )
        skill_ticket = ticket_from_skill_selection(
            tenant_id=tenant.tenant_id,
            task_id=task_ref.meta.task_id,
            risk_level=task_ref.meta.risk_level,
            top_k=_skill_top_k,
            skills=skill_candidates,
            mission_id=_mission_id_from_task(task_ref),
        )
        decision_tickets.append(skill_ticket)
        self._record_state_ledger("record_decision_ticket", skill_ticket)
        async with session_scope(tenant_id=tenant.tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="skill.selected",
                    payload=skill_ticket.event_payload(),
                    task_ref=task_ref.meta.task_id,
                ),
            )
        if skill_candidates:
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "skill_selection",
                    "candidates": [s.skill_id for s in skill_candidates],
                    "decision_ticket": skill_ticket.event_payload(),
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
        if _skill_is_registered("world-request") and not any(
            skill_id == "world-request" for skill_id, _, _ in skill_summaries
        ):
            skill_summaries.append(
                (
                    "world-request",
                    (
                        "执行中发现需要外部动作时使用。它只生成待审批 WorldGateway "
                        "动作并暂停任务，不会真实外发。"
                    ),
                    {
                        "type": "object",
                        "properties": {
                            "action_type": {"type": "string"},
                            "target_ref": {"type": "string"},
                            "risk_level": {"type": "string"},
                            "payload": {"type": "object"},
                        },
                        "required": ["action_type", "payload"],
                    },
                )
            )
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
        proactive_ticket: DecisionTicket | None = None
        if proactive_scan.dispatched or proactive_scan.missed_opportunities:
            proactive_ticket = ticket_from_proactive_tool_dispatch(
                tenant_id=tenant.tenant_id,
                task_id=task_ref.meta.task_id,
                risk_level=task_ref.meta.risk_level,
                scan_result=proactive_scan,
                prompt_excerpt=user_message[:200],
                mission_id=_mission_id_from_task(task_ref),
            )
            decision_tickets.append(proactive_ticket)
            self._record_state_ledger("record_decision_ticket", proactive_ticket)
            async with session_scope(tenant_id=tenant.tenant_id) as s:
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="proactive.tool_dispatch.evaluated",
                        payload=proactive_ticket.event_payload(),
                        task_ref=task_ref.meta.task_id,
                    ),
                )
        if proactive_scan.dispatched:
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "proactive_tools",
                    "skills": [d.skill_id for d in proactive_scan.dispatched],
                    "reasons": [d.trigger_reason for d in proactive_scan.dispatched],
                    "decision_ticket": (
                        proactive_ticket.event_payload() if proactive_ticket else None
                    ),
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
                    "decision_ticket": (
                        proactive_ticket.event_payload() if proactive_ticket else None
                    ),
                },
            )

        # 8. Execute steps
        answer = ""
        status: TaskStatus = "running"
        notifications: list[Notification] = []
        last_response: LLMResponse | None = None
        step_outputs: list[tuple[int, str]] = []

        try:
            step_index = 0
            while step_index < len(plan.steps):
                step_plan = plan.steps[step_index]
                self._raise_if_task_cancelled(task_ref.meta.task_id)
                # Hard task-level deadline check (R-D1).
                if time.monotonic() > deadline_monotonic:
                    raise TaskTimedOutError(
                        task_ref.meta.task_id,
                        time.perf_counter() - t0,
                        duration_cap,
                    )

                step_t0 = time.perf_counter()

                # V2.3 Wire 41: Predictive Coding pre-step hook (插件式)
                # prediction_provider == None → 鲲行为完全不变
                _pc_expected: dict[str, Any] | None = None
                if self.prediction_provider is not None:
                    try:
                        _pc_expected = await self.prediction_provider.predict(
                            {
                                "task_type": task_ref.meta.task_type,
                                "step_id": step_plan.step_id,
                                "skill_hint": step_plan.skill_hint or "",
                                "complexity_score": task_ref.meta.complexity_score,
                            }
                        )
                        yield OrchestratorEvent(
                            kind="pc_expected",
                            data={
                                "step_id": step_plan.step_id,
                                "expected": _pc_expected,
                            },
                        )
                    except Exception:
                        log.exception("predictive_coding.predict failed (non-fatal)")
                        _pc_expected = None

                # V2.2 §19.4 + §21 wire: 守望主决策 gate
                # ExecutionMode 决定 ValueGate 启用 (FAST 跳过, SMART/MAX 启用)
                _exec_mode = getattr(task_ref.meta, "execution_mode", "FAST")

                # V2.2 §22 wire: SMART/MAX 模式生成 hermes ExecutionStep, 让守望介入
                # FAST 跳过; ENSEMBLE 自己跑 5-path, 不再额外消耗 hermes call.
                _hermes_step: Any = None
                if self.structured_step_generator is not None and _exec_mode in ("SMART", "MAX"):
                    try:
                        _hermes_step = await self.structured_step_generator.generate(
                            prompt=step_plan.description,
                            context={
                                "purpose": str(choice.purpose),
                                "risk_level": task_ref.meta.risk_level,
                                "step_id": step_plan.step_id,
                                "max_cost_usd": task_ref.meta.estimated_cost_usd,
                            },
                            mode=_exec_mode,
                        )
                        yield OrchestratorEvent(
                            kind="hermes_step",
                            data={
                                "step_id": step_plan.step_id,
                                "thought": _hermes_step.thought,
                                "action_type": _hermes_step.action_type,
                                "expected_outcome": _hermes_step.expected_outcome,
                                "confidence": _hermes_step.confidence,
                                "cost_estimate_usd": _hermes_step.cost_estimate_usd,
                            },
                        )
                        hermes_ticket = ticket_from_step_action_selection(
                            tenant_id=tenant.tenant_id,
                            task_id=task_ref.meta.task_id,
                            risk_level=task_ref.meta.risk_level,
                            step_id=step_plan.step_id,
                            hermes_step=_hermes_step,
                            mission_id=_mission_id_from_task(task_ref),
                        )
                        decision_tickets.append(hermes_ticket)
                        self._record_state_ledger("record_decision_ticket", hermes_ticket)
                        async with session_scope(tenant_id=tenant.tenant_id) as s:
                            await emit(
                                s,
                                Event.build(
                                    tenant_id=tenant.tenant_id,
                                    event_type="hermes.step_action.selected",
                                    payload=hermes_ticket.event_payload(),
                                    task_ref=task_ref.meta.task_id,
                                ),
                            )
                        yield OrchestratorEvent(
                            kind="action_plan",
                            data={
                                "stage": "hermes_step_action_selected",
                                "decision_ticket": hermes_ticket.event_payload(),
                            },
                        )
                    except Exception:
                        log.exception("hermes_step.generate failed (non-fatal)")

                # V2.2 §22 Wire 31/32/33: hermes ExecutionStep.action_type 真驱动 step
                # use_skill / web_search → 覆盖 step_plan.skill_hint (Wire 31)
                # ask_user → 暂停 task 等用户回复 (Wire 32)
                # use_memory → 拉 query 相关 memory 加塞 step context (Wire 33)
                # direct_llm → 走现有路径
                step_context_summary = context_summary  # per-step 副本, hermes use_memory 可加塞
                step_context_resources = list(context_resource_ids)
                if _hermes_step is not None and _exec_mode in ("SMART", "MAX"):
                    _hermes_override = _hermes_skill_from_action(_hermes_step)
                    if _hermes_override and _hermes_override != step_plan.skill_hint:
                        old_hint = step_plan.skill_hint or ""
                        step_plan.skill_hint = _hermes_override
                        yield OrchestratorEvent(
                            kind="hermes_skill_override",
                            data={
                                "step_id": step_plan.step_id,
                                "from": old_hint,
                                "to": _hermes_override,
                                "action_type": _hermes_step.action_type,
                                "reason": "hermes_executionstep",
                            },
                        )
                    elif _hermes_step.action_type == "ask_user":
                        # Wire 32: hermes 决定要问 user → 暂停 task, 把问题 emit 出去
                        question = _hermes_question_from_step(_hermes_step)
                        yield OrchestratorEvent(
                            kind="hermes_ask_user",
                            data={
                                "step_id": step_plan.step_id,
                                "question": question,
                                "thought": _hermes_step.thought,
                                "expected_outcome": _hermes_step.expected_outcome,
                            },
                        )
                        log.info(
                            "hermes.ask_user_pause task=%s step=%d question=%s",
                            task_ref.meta.task_id,
                            step_plan.step_id,
                            question[:100],
                        )
                        status = "paused"
                        self._record_state_ledger(
                            "record_paused",
                            task_ref.meta.task_id,
                            reason=question,
                            pending_confirmations=["user_input"],
                        )
                        break
                    elif _hermes_step.action_type == "use_memory":
                        # Wire 33: hermes 主动拉相关 memory → 加塞进 step context_summary
                        memory_query = _hermes_memory_query_from_step(_hermes_step, step_plan)
                        if memory_query and _memory_policy_allows_mid_run_recall(memory_policy):
                            try:
                                extra_pack = await self.context_packer.pack_query(
                                    memory_query,
                                    tenant_id=tenant.tenant_id,
                                    limit=_memory_policy_mid_run_limit(
                                        memory_policy,
                                        execution_mode=str(_exec_mode),
                                    ),
                                    memory_layers=memory_context_kwargs.get("memory_layers"),
                                    avoid_memory_layers=memory_context_kwargs.get(
                                        "avoid_memory_layers"
                                    ),
                                    preferred_tags=memory_context_kwargs.get("preferred_tags"),
                                    high_risk_task=bool(
                                        memory_context_kwargs.get("high_risk_task")
                                    ),
                                )
                            except Exception:
                                log.exception("hermes.use_memory pack_query failed")
                                extra_pack = None
                            if extra_pack and extra_pack.items:
                                extra_summary = extra_pack.summary(max_chars=900)
                                step_context_summary = (
                                    f"{step_context_summary}\n\n[Hermes use_memory] "
                                    f"额外拉的相关 memory:\n{extra_summary}"
                                ).strip()
                                step_context_resources.extend(_context_resource_ids(extra_pack))
                                yield OrchestratorEvent(
                                    kind="hermes_memory_injected",
                                    data={
                                        "step_id": step_plan.step_id,
                                        "query": memory_query,
                                        "asset_ids": [it.asset_id for it in extra_pack.items],
                                        "count": len(extra_pack.items),
                                    },
                                )
                        elif memory_query:
                            yield OrchestratorEvent(
                                kind="hermes_memory_skipped",
                                data={
                                    "step_id": step_plan.step_id,
                                    "query": memory_query,
                                    "reason": "memory_policy_disallows_mid_run_retrieval",
                                    "memory_policy": (
                                        memory_policy.model_dump(mode="json")
                                        if memory_policy is not None
                                        else None
                                    ),
                                },
                            )

                if self.value_gate is not None and _exec_mode != "FAST":
                    try:
                        # 把 hermes ExecutionStep 的 cost_estimate / confidence 喂给 ValueGate
                        # 让 estimator 看到 LLM 自评的 cost (产 production estimator 用)
                        _gate_ctx: dict[str, Any] = {
                            "purpose": str(choice.purpose),
                            "mode": _exec_mode,
                            "tenant_id": tenant.tenant_id,
                            "task_type": task_ref.meta.task_type,
                            "accumulated_cost_usd": runtime.accumulated_cost_usd_equivalent,
                            "budget_usd": task_ref.meta.estimated_cost_usd,
                        }
                        if _hermes_step is not None:
                            _gate_ctx["hermes_confidence"] = _hermes_step.confidence
                            _gate_ctx["hermes_cost_estimate"] = _hermes_step.cost_estimate_usd
                            _gate_ctx["hermes_action_type"] = _hermes_step.action_type
                        if watchtower_decision is not None:
                            _gate_ctx["strategy_pack_id"] = watchtower_decision.strategy_pack_id
                            _gate_ctx["metric_dimensions"] = watchtower_decision.metric_dimensions
                            _gate_ctx["reward_weights"] = watchtower_decision.reward_weights
                        _gate_ctx["value_gate_resource_keys"] = _value_gate_resource_keys(
                            task_ref=task_ref,
                            step_skill=getattr(step_plan, "skill", ""),
                            execution_mode=_exec_mode,
                            watchtower_decision=watchtower_decision,
                        )
                        async with session_scope(tenant_id=tenant.tenant_id) as s:
                            await load_resource_credit_scores(
                                s,
                                tenant_id=tenant.tenant_id,
                                resource_keys=_gate_ctx["value_gate_resource_keys"],
                            )
                        gate_decision = await self.value_gate.check_step(
                            task_ref=task_ref,
                            step_plan=step_plan,
                            prior_value_history=list(self._value_history),
                            context=_gate_ctx,
                        )
                        value_ticket = ticket_from_value_gate_decision(
                            tenant_id=tenant.tenant_id,
                            task_id=task_ref.meta.task_id,
                            step_id=step_plan.step_id,
                            decision=gate_decision,
                            risk_level=task_ref.meta.risk_level,
                        )
                        decision_tickets.append(value_ticket)
                        self._record_state_ledger("record_decision_ticket", value_ticket)
                        async with session_scope(tenant_id=tenant.tenant_id) as s:
                            await emit(
                                s,
                                Event.build(
                                    tenant_id=tenant.tenant_id,
                                    event_type="value_gate.decision.created",
                                    payload={
                                        "task_id": task_ref.meta.task_id,
                                        "step_id": step_plan.step_id,
                                        "decision": gate_decision.decision,
                                        "reason": gate_decision.reason,
                                        "expected_value": gate_decision.expected_value,
                                        "decision_ticket": value_ticket.event_payload(),
                                    },
                                    task_ref=task_ref.meta.task_id,
                                ),
                            )
                        if gate_decision.decision in ("stop", "escalate"):
                            yield OrchestratorEvent(
                                kind="value_gate_intervention",
                                data={
                                    "step_id": step_plan.step_id,
                                    "decision": gate_decision.decision,
                                    "reason": gate_decision.reason,
                                    "expected_value": gate_decision.expected_value,
                                    "decision_ticket": value_ticket.event_payload(),
                                },
                            )
                            # stop / escalate 都中止当前 step loop
                            status = "paused" if gate_decision.decision == "escalate" else "done"
                            if status == "paused":
                                self._record_state_ledger(
                                    "record_paused",
                                    task_ref.meta.task_id,
                                    reason=gate_decision.reason,
                                    pending_confirmations=["watchtower_escalation"],
                                )
                            break
                        if gate_decision.decision == "skip":
                            yield OrchestratorEvent(
                                kind="value_gate_skip",
                                data={
                                    "step_id": step_plan.step_id,
                                    "reason": gate_decision.reason,
                                    "expected_value": gate_decision.expected_value,
                                    "decision_ticket": value_ticket.event_payload(),
                                },
                            )
                            step_index += 1
                            continue
                    except Exception:
                        log.exception("value_gate.check_step failed (non-fatal)")

                yield OrchestratorEvent(
                    kind="action",
                    data={"step_id": step_plan.step_id, "description": step_plan.description},
                )
                self._record_state_ledger(
                    "record_current_action",
                    task_ref.meta.task_id,
                    step_id=step_plan.step_id,
                    description=step_plan.description,
                    skill_hint=step_plan.skill_hint,
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
                    if _exec_mode == "ENSEMBLE":
                        answer, response, ensemble_payload = await self._await_task_control(
                            task_ref.meta.task_id,
                            self._execute_ensemble_step(
                                task_ref=task_ref,
                                step_description=step_plan.description,
                                profile=exec_profile,
                                skills_summary=self.skill_selector.summary(skill_candidates),
                                skill_directive=skill_directive,
                                context_summary=step_context_summary,
                                prior_outputs=step_outputs,
                                pre_dispatched_block=step_pre_dispatched,
                            ),
                        )
                        yield OrchestratorEvent(kind="ensemble_result", data=ensemble_payload)
                    else:
                        answer, response = await self._await_task_control(
                            task_ref.meta.task_id,
                            self._execute_step(
                                task_ref=task_ref,
                                step_description=step_plan.description,
                                purpose=choice.purpose,
                                profile=exec_profile,
                                skills_summary=self.skill_selector.summary(skill_candidates),
                                skill_directive=skill_directive,
                                context_summary=step_context_summary,
                                prior_outputs=step_outputs,
                                pre_dispatched_block=step_pre_dispatched,
                            ),
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
                self._record_state_ledger(
                    "record_step_completed",
                    task_ref.meta.task_id,
                    runtime=runtime,
                    step=step_record,
                    provider=response.provider,
                    model=response.model,
                    tier=str(response.tier),
                )
                llm_route_ticket = ticket_from_llm_route(
                    tenant_id=tenant.tenant_id,
                    task_id=task_ref.meta.task_id,
                    step_id=step_plan.step_id,
                    purpose=choice.purpose,
                    provider=response.provider,
                    model=response.model,
                    tier=str(response.tier),
                    cost_usd=response.cost_usd_equivalent,
                    risk_level=task_ref.meta.risk_level,
                    mission_id=_mission_id_from_task(task_ref),
                    route_debug=response.route_debug,
                )
                decision_tickets.append(llm_route_ticket)
                self._record_state_ledger("record_decision_ticket", llm_route_ticket)
                async with session_scope(tenant_id=tenant.tenant_id) as s:
                    await emit(
                        s,
                        Event.build(
                            tenant_id=tenant.tenant_id,
                            event_type="llm.model_route.selected",
                            payload=llm_route_ticket.event_payload(),
                            task_ref=task_ref.meta.task_id,
                        ),
                    )
                try:
                    if ooda_cycle.current_state == OODAState.REFLECT:
                        ooda_cycle = await ooda_engine.transition(
                            ooda_cycle,
                            OODAState.DECIDE,
                            {
                                "step_id": step_plan.step_id,
                                "expected_outcome": "done",
                                "reason": "continue_next_step",
                            },
                        )
                    if ooda_cycle.current_state == OODAState.DECIDE:
                        ooda_cycle = await ooda_engine.transition(
                            ooda_cycle,
                            OODAState.ACT,
                            {
                                "step_id": step_plan.step_id,
                                "status": "done",
                                "outcome": "done",
                                "skill_used": step_record.skill_used,
                                "provider": response.provider,
                                "model": response.model,
                                "tier": str(response.tier),
                                "cost_usd": response.cost_usd_equivalent,
                                "duration_sec": duration,
                            },
                        )
                    reflection = await ooda_engine.reflect(ooda_cycle)
                    ooda_cycle = await ooda_engine.transition(
                        ooda_cycle,
                        OODAState.REFLECT,
                        reflection,
                    )
                    from kun.engineering.dynamic_replan import DynamicReplanner

                    replan_decision = await DynamicReplanner().detect_replan_decision(ooda_cycle)
                    ooda_status: Literal["applied", "needs_review"] = (
                        "needs_review" if replan_decision.needs_replan else "applied"
                    )
                    ooda_ticket = await self._record_ooda_checkpoint(
                        tenant_id=tenant.tenant_id,
                        task_ref=task_ref,
                        cycle=ooda_cycle,
                        checkpoint="reflect",
                        decision_tickets=decision_tickets,
                        status=ooda_status,
                        reason=replan_decision.reason,
                        step_id=step_plan.step_id,
                        evidence={
                            "reflection": reflection,
                            "replan": {
                                "needs_replan": replan_decision.needs_replan,
                                "reason": replan_decision.reason,
                                "confidence": replan_decision.confidence,
                                "metadata": replan_decision.metadata or {},
                            },
                        },
                    )
                    yield OrchestratorEvent(
                        kind="action_plan",
                        data={
                            "stage": "ooda_reflect",
                            "step_id": step_plan.step_id,
                            "needs_replan": replan_decision.needs_replan,
                            "reason": replan_decision.reason,
                            "decision_ticket": ooda_ticket.event_payload(),
                        },
                    )
                except Exception:
                    log.exception("ooda.checkpoint_failed (non-fatal)")
                self._record_step_credit(
                    task_ref=task_ref,
                    step=step_record,
                    answer=answer,
                    response=response,
                    role_template_id=choice.role_template_id,
                    context_asset_ids=step_context_resources,
                    watchtower_decision=watchtower_decision,
                    active_protocol=active_protocol,
                    decision_ticket_ids=[ticket.ticket_id for ticket in decision_tickets],
                )
                await self._record_process_memory(
                    tenant_id=tenant.tenant_id,
                    task_ref=task_ref,
                    step=step_record,
                    answer=answer,
                    response=response,
                )

                # V2.3 Wire 41: Predictive Coding post-step hook
                # 算 actual + error, 喂 model_updater 让模型实时学
                if self.model_updater is not None and _pc_expected is not None:
                    try:
                        _pc_actual = {
                            "cost_usd": response.cost_usd_equivalent,
                            "duration_sec": duration,
                            "tokens": response.usage.input_tokens + response.usage.output_tokens,
                        }
                        _pc_error = {
                            k: _pc_actual.get(k, 0) - _pc_expected.get(k, 0) for k in _pc_actual
                        }
                        await self.model_updater.record(
                            step_id=step_plan.step_id,
                            task_type=task_ref.meta.task_type,
                            expected=_pc_expected,
                            actual=_pc_actual,
                            error=_pc_error,
                        )
                        yield OrchestratorEvent(
                            kind="pc_error",
                            data={
                                "step_id": step_plan.step_id,
                                "error": _pc_error,
                            },
                        )
                    except Exception:
                        log.exception("predictive_coding.record failed (non-fatal)")

                # V2.3 Wire 47: Pheromone reinforce — 走过 (prior_skill → this_skill) 路径加强
                # 蚁群涌现: 多 task 走过的链路自然强化 → skill_selector 下次自动倾向
                _this_skill = step_record.skill_used or ""
                if _this_skill and _this_skill != "llm.direct" and step_plan.step_id > 1:
                    try:
                        from kun.qi.pheromone import get_pheromone_storage

                        # 上一 step 的 skill (从 runtime.completed_steps 倒数第 2 个拿)
                        prior_skill_used = ""
                        if len(runtime.completed_steps) >= 2:
                            prior_skill_used = runtime.completed_steps[-2].skill_used or ""
                        if prior_skill_used and prior_skill_used != "llm.direct":
                            storage = get_pheromone_storage()
                            await storage.reinforce(
                                tenant.tenant_id,
                                source_kind="skill",
                                source_id=prior_skill_used,
                                target_kind="skill",
                                target_id=_this_skill,
                                relation_type="follows",
                            )
                    except Exception:
                        log.debug("pheromone.reinforce_skipped", exc_info=True)

                # V2.2 Wire 9: skill 级 capability_card writeback
                # 每 step 完后给 skill 写一条 outcome (除了 llm.direct, 那不是真 skill)
                if step_record.skill_used and step_record.skill_used != "llm.direct":
                    try:
                        await record_outcome(
                            tenant.tenant_id,
                            TaskOutcome(
                                entity_type="skill",
                                entity_id=step_record.skill_used,
                                task_type=task_ref.meta.task_type,
                                outcome="pass",  # step 跑完了, 默认 pass; 失败时 except 走
                                cost_usd=step_record.cost_usd_equivalent,
                                duration_sec=duration,
                                rubric_score=None,
                            ),
                        )
                    except Exception:
                        log.exception("skill capability writeback failed (non-fatal)")

                # V2.3 Wire 53 (C72): AntiGamingDetector — step 完后跑 7 套路 quick check
                # 命中 → emit gaming.detected event + 标记 step_record.notes
                # 不阻断流程 (let verification_runner 决定是否真 fail), 让 Watchtower 看
                if (
                    self.anti_gaming_detector is not None
                    and _os.getenv("KUN_ANTI_GAMING_ENABLED", "1") == "1"
                ):
                    try:
                        prior_answers = [out for _, out in step_outputs[:-1]]
                        _step_answer = step_outputs[-1][1] if step_outputs else ""
                        finding = self.anti_gaming_detector.check(
                            prompt=step_plan.description,
                            answer=_step_answer,
                            prior_answers=prior_answers,
                            planned_steps=len(plan.steps),
                            actual_steps=step_plan.step_id,
                            used_skills=[step_record.skill_used] if step_record.skill_used else [],
                            has_assets=False,
                            has_skill_traces=bool(step_record.skill_used),
                        )
                        if finding is not None:
                            anti_gaming_ticket = ticket_from_anti_gaming_finding(
                                tenant_id=tenant.tenant_id,
                                task_id=task_ref.meta.task_id,
                                risk_level=task_ref.meta.risk_level,
                                step_id=step_plan.step_id,
                                finding=finding,
                                mission_id=_mission_id_from_task(task_ref),
                            )
                            decision_tickets.append(anti_gaming_ticket)
                            self._record_state_ledger(
                                "record_decision_ticket",
                                anti_gaming_ticket,
                            )
                            self._attach_decision_ticket_to_step_credit(
                                task_ref.meta.task_id,
                                step_plan.step_id,
                                anti_gaming_ticket,
                            )
                            try:
                                from kun.core.metrics import anti_gaming_detection_total

                                anti_gaming_detection_total.labels(pattern=finding.pattern).inc()
                            except Exception:
                                pass
                            async with session_scope() as s:
                                await emit(
                                    s,
                                    Event.build(
                                        tenant_id=tenant.tenant_id,
                                        event_type="gaming.detected",
                                        payload={
                                            "task_id": task_ref.meta.task_id,
                                            "step_id": step_plan.step_id,
                                            "pattern": finding.pattern,
                                            "severity": finding.severity,
                                            "evidence": finding.evidence,
                                            "decision_ticket": anti_gaming_ticket.event_payload(),
                                        },
                                        task_ref=task_ref.meta.task_id,
                                    ),
                                )
                            log.warning(
                                "anti_gaming.detected",
                                task_id=task_ref.meta.task_id,
                                pattern=finding.pattern,
                                severity=finding.severity,
                            )
                    except Exception:
                        log.debug("anti_gaming.check_skipped", exc_info=True)

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
                    if _watchtower_pause_requested(self.rule_engine, fired):
                        raise TaskPausedByWatchtowerError(task_ref.meta.task_id, fired)

                if self.budget_tracker is not None:
                    budget_level = self.budget_tracker.consume(
                        "task",
                        task_ref.meta.task_id,
                        response.cost_usd_equivalent,
                    )
                    budget_state = self.budget_tracker.get_state("task", task_ref.meta.task_id)
                    if budget_state is not None:
                        budget_usage_ratio = budget_state.used_usd / max(
                            budget_state.limit_usd, 1e-6
                        )
                        budget_hard_break = budget_usage_ratio >= HARD_BREAK_RATIO_TASK
                        if budget_level != "HIGH" or budget_hard_break:
                            budget_ticket = ticket_from_budget_policy(
                                tenant_id=tenant.tenant_id,
                                task_id=task_ref.meta.task_id,
                                risk_level=task_ref.meta.risk_level,
                                level=budget_level,
                                used_usd=budget_state.used_usd,
                                limit_usd=budget_state.limit_usd,
                                behavior=self.budget_tracker.get_behavior(budget_level),
                                hard_break=budget_hard_break,
                                mission_id=_mission_id_from_task(task_ref),
                            )
                            decision_tickets.append(budget_ticket)
                            self._record_state_ledger("record_decision_ticket", budget_ticket)
                            self._attach_decision_ticket_to_step_credit(
                                task_ref.meta.task_id,
                                step_record.step_id,
                                budget_ticket,
                            )
                            budget_event_type: Literal[
                                "task.budget_exceeded", "task.budget_warn"
                            ] = "task.budget_exceeded" if budget_hard_break else "task.budget_warn"
                            async with session_scope(tenant_id=tenant.tenant_id) as s:
                                await emit(
                                    s,
                                    Event.build(
                                        tenant_id=tenant.tenant_id,
                                        event_type=budget_event_type,
                                        payload=budget_ticket.event_payload(),
                                        task_ref=task_ref.meta.task_id,
                                    ),
                                )
                            yield OrchestratorEvent(
                                kind="guard_intervention"
                                if budget_level in {"LOW", "CRITICAL"} or budget_hard_break
                                else "insight",
                                data={
                                    "stage": "budget_policy",
                                    "level": budget_level,
                                    "used_usd": budget_state.used_usd,
                                    "limit_usd": budget_state.limit_usd,
                                    "hard_break": budget_hard_break,
                                    "decision_ticket": budget_ticket.event_payload(),
                                },
                            )
                            if (
                                budget_hard_break
                                and _os.getenv("KUN_TASK_BUDGET_HARD_BREAK_ENABLED", "0") == "1"
                            ):
                                raise TaskBudgetExceededError(
                                    task_ref.meta.task_id,
                                    budget_state.used_usd,
                                    budget_state.limit_usd,
                                )

                # V2.1 §5.8 wire: step 完, 让 EmergentSwitchManager 检测信号.
                # 满足切换条件时会用 DynamicReplanner 改后续 tail plan；
                # 已完成步骤不重跑，避免浪费 sunk cost。
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

                            # V2.2 §5.8 Wire 13: 真切换. 检 evaluate_switch, 满足条件
                            # 直接改后续 tail plan；不只 emit 观察事件。
                            try:
                                eval_result = self.emergent_switch_manager.evaluate_switch(
                                    task_id=task_ref.meta.task_id,
                                    task_type=str(choice.purpose),
                                    current_strategy_outcome=0.7,  # M4 接真 outcome
                                    current_remaining_cost_usd=max(
                                        0.0,
                                        task_ref.meta.estimated_cost_usd
                                        - runtime.accumulated_cost_usd_equivalent,
                                    ),
                                    signals=signals,
                                )
                                switch_ticket = ticket_from_emergent_switch(
                                    tenant_id=tenant.tenant_id,
                                    task_id=task_ref.meta.task_id,
                                    risk_level=task_ref.meta.risk_level,
                                    step_id=step_record.step_id,
                                    signals=list(signals),
                                    evaluation=eval_result,
                                    mission_id=_mission_id_from_task(task_ref),
                                )
                                decision_tickets.append(switch_ticket)
                                self._record_state_ledger(
                                    "record_decision_ticket",
                                    switch_ticket,
                                )
                                self._attach_decision_ticket_to_step_credit(
                                    task_ref.meta.task_id,
                                    step_record.step_id,
                                    switch_ticket,
                                )
                                async with session_scope(tenant_id=tenant.tenant_id) as s:
                                    await emit(
                                        s,
                                        Event.build(
                                            tenant_id=tenant.tenant_id,
                                            event_type="emergent.switch.evaluated",
                                            payload=switch_ticket.event_payload(),
                                            task_ref=task_ref.meta.task_id,
                                        ),
                                    )
                                if eval_result.should_switch and eval_result.chosen_solution:
                                    self.emergent_switch_manager.commit_switch(
                                        task_ref.meta.task_id
                                    )
                                    from kun.engineering.dynamic_replan import DynamicReplanner

                                    replan_observation = _emergent_solution_observation(
                                        eval_result.chosen_solution,
                                        reason=eval_result.reason,
                                        signals=list(signals),
                                    )
                                    replan_result = await DynamicReplanner().replan_with_result(
                                        plan,
                                        step_index,
                                        [replan_observation],
                                        reason=eval_result.reason,
                                    )
                                    plan = replan_result.plan
                                    runtime.total_planned_steps = len(plan.steps)
                                    self._record_state_ledger(
                                        "record_plan",
                                        task_ref.meta.task_id,
                                        total_steps=len(plan.steps),
                                    )
                                    yield OrchestratorEvent(
                                        kind="emergent_switch_committed",
                                        data={
                                            "task_id": task_ref.meta.task_id,
                                            "step_id": step_record.step_id,
                                            "switch_score": eval_result.switch_score,
                                            "solution_id": eval_result.chosen_solution.solution_id,
                                            "solution_status": eval_result.chosen_solution.status,
                                            "reason": eval_result.reason,
                                            "decision_ticket": switch_ticket.event_payload(),
                                            "replan": replan_result.model_dump(mode="json"),
                                        },
                                    )
                                    yield OrchestratorEvent(
                                        kind="action_plan",
                                        data={
                                            "stage": "emergent_replan_applied",
                                            "task_id": task_ref.meta.task_id,
                                            "preserved_step_ids": replan_result.preserved_step_ids,
                                            "replacement_step_ids": (
                                                replan_result.replacement_step_ids
                                            ),
                                            "new_total_steps": len(plan.steps),
                                            "reason": replan_result.reason,
                                            "sunk_cost_usd": replan_result.sunk_cost_usd,
                                        },
                                    )
                                elif eval_result.blocked_by:
                                    yield OrchestratorEvent(
                                        kind="emergent_switch_blocked",
                                        data={
                                            "task_id": task_ref.meta.task_id,
                                            "blocked_by": eval_result.blocked_by,
                                            "switch_score": eval_result.switch_score,
                                            "decision_ticket": switch_ticket.event_payload(),
                                        },
                                    )
                            except Exception:
                                log.exception(
                                    "emergent_switch.evaluate_or_commit failed (non-fatal)"
                                )
                    except Exception:
                        log.exception("emergent_switch.detect_signals failed (non-fatal)")

                # V2.2 §19.4 wire: step 完, 让 ValueGate 记 outcome + 更新 value_history
                # FAST 模式跳过, 不消耗 ValueGate state
                if self.value_gate is not None and _exec_mode != "FAST":
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

                step_index += 1

            # V2.3+ PreDeliverGate (产品级交付前审核, 取代裸 verification 调用)
            # 跑 verification + AntiGaming + 自检 + 协议合规 → 综合 verdict
            # KUN_PRE_DELIVER_GATE_ENABLED=0 → 跳过 (走旧路径, 直接 mark done)
            from kun.engineering.pre_deliver_gate import PreDeliverGate

            if PreDeliverGate.is_enabled():
                gate = PreDeliverGate(
                    verification_runner=self.verification_runner,
                    anti_gaming_detector=self.anti_gaming_detector,
                    active_protocol=active_protocol,
                )
                final_artifact = answer if isinstance(answer, str) else str(answer)
                verdict = await gate.review(
                    answer=final_artifact,
                    task_ref=task_ref,
                    plan=plan,
                    step_records=runtime.completed_steps,
                )
                # Backward compat: emit verification_done with V2.2 shape.
                # failed=True 只在 *required* verification fail 时 (matches V2.2 semantics).
                _verification_checks = [
                    c for c in verdict.checks if c.name.startswith("verification.")
                ]
                _verification_results = [
                    {
                        "kind": c.name.replace("verification.", ""),
                        "passed": c.passed,
                        "error_msg": c.reason if not c.passed else "",
                        # legacy V2.2: exception case used "error" key
                        "error": c.reason
                        if (not c.passed and "exception" in c.reason.lower())
                        else "",
                    }
                    for c in _verification_checks
                ]
                # required = severity high (我们 PreDeliverGate 把 required spec 标 high)
                _verification_failed = any(
                    (not c.passed) and c.severity == "high" for c in _verification_checks
                )
                if _verification_results:
                    yield OrchestratorEvent(
                        kind="verification_done",
                        data={
                            "task_id": task_ref.meta.task_id,
                            "results": _verification_results,
                            "failed": _verification_failed,
                        },
                    )

                delivery_ticket = ticket_from_delivery_review(
                    tenant_id=tenant.tenant_id,
                    task_id=task_ref.meta.task_id,
                    risk_level=task_ref.meta.risk_level,
                    verdict=verdict,
                    mission_id=_mission_id_from_task(task_ref),
                )
                decision_tickets.append(delivery_ticket)
                self._record_state_ledger("record_decision_ticket", delivery_ticket)
                yield OrchestratorEvent(
                    kind="delivery.review_done",
                    data={
                        "task_id": task_ref.meta.task_id,
                        "passed": verdict.passed,
                        "final_status": verdict.final_status,
                        "reason_summary": verdict.reason_summary,
                        "decision_ticket": delivery_ticket.event_payload(),
                        "checks": [
                            {
                                "name": c.name,
                                "passed": c.passed,
                                "severity": c.severity,
                                "reason": c.reason,
                            }
                            for c in verdict.checks
                        ],
                    },
                )
                async with session_scope() as s:
                    await emit(
                        s,
                        Event.build(
                            tenant_id=tenant.tenant_id,
                            event_type="delivery.review_done",
                            payload={
                                "task_id": task_ref.meta.task_id,
                                "passed": verdict.passed,
                                "final_status": verdict.final_status,
                                "reason_summary": verdict.reason_summary,
                                "check_count": len(verdict.checks),
                                "fail_count": sum(1 for c in verdict.checks if not c.passed),
                                "decision_ticket": delivery_ticket.event_payload(),
                            },
                            task_ref=task_ref.meta.task_id,
                        ),
                    )
                if verdict.final_status == "failed":
                    status = "failed"
                    log.warning(
                        "pre_deliver_gate.failed task=%s reason=%s",
                        task_ref.meta.task_id,
                        verdict.reason_summary,
                    )
                elif verdict.final_status == "needs_review":
                    # mapped to "paused" task status (用户 confirm 后可 resume)
                    status = "paused"
                    log.warning(
                        "pre_deliver_gate.needs_review task=%s reason=%s",
                        task_ref.meta.task_id,
                        verdict.reason_summary,
                    )
                else:
                    status = "done"
            else:
                # legacy path (KUN_PRE_DELIVER_GATE_ENABLED=0)
                verification_failed = False
                if (
                    self.verification_runner is not None
                    and task_ref.spec is not None
                    and task_ref.spec.verification_specs
                ):
                    final_artifact = answer if isinstance(answer, str) else str(answer)
                    for vspec in task_ref.spec.verification_specs:
                        try:
                            vresult = await self.verification_runner.verify(vspec, final_artifact)
                            if vspec.required and not vresult.passed:
                                verification_failed = True
                        except Exception:
                            if vspec.required:
                                verification_failed = True
                status = "failed" if verification_failed else "done"
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
        except TaskBudgetExceededError as exc:
            status = "failed"
            log.warning(
                "orchestrator.budget_exceeded",
                task_id=exc.task_id,
                used_usd=exc.used_usd,
                limit_usd=exc.limit_usd,
            )
            answer = (
                f"任务预算已超限，已强制结束（已用 ${exc.used_usd:.4f}，"
                f"预算 ${exc.limit_usd:.4f}）。"
            )
            yield OrchestratorEvent(
                kind="error",
                data={
                    "message": "task_budget_exceeded",
                    "task_id": exc.task_id,
                    "used_usd": exc.used_usd,
                    "limit_usd": exc.limit_usd,
                },
            )
        except TaskCancelledByUserError as exc:
            status = "cancelled"
            answer = f"任务已按用户请求停止：{exc.reason}"
            log.info("orchestrator.cancelled_by_user", task_id=exc.task_id, reason=exc.reason)
            yield OrchestratorEvent(
                kind="guard_intervention",
                data={
                    "stage": "task_control",
                    "message": "task_cancelled_by_user",
                    "task_id": exc.task_id,
                    "reason": exc.reason,
                },
            )
        except TaskPausedByWatchtowerError as exc:
            status = "paused"
            answer = (
                "任务已被守望暂停，原因是规则触发了硬暂停动作："
                f"{', '.join(exc.rules_fired)}。请在任务看板或 NUO 中确认后再继续。"
            )
            log.warning(
                "orchestrator.paused_by_watchtower",
                task_id=exc.task_id,
                rules_fired=exc.rules_fired,
            )
            yield OrchestratorEvent(
                kind="guard_intervention",
                data={
                    "stage": "watchtower_hard_action",
                    "message": "task_paused_by_watchtower",
                    "task_id": exc.task_id,
                    "rules_fired": exc.rules_fired,
                },
            )
        except TaskPausedByWorldActionError as exc:
            status = "paused"
            action_types = [action.action_type for action in exc.actions]
            answer = (
                "任务已暂停，执行过程中发现需要外部动作审批："
                f"{', '.join(action_types)}。请在 NUO 的待审批动作里确认。"
            )
            preflight_ticket = ticket_from_preflight_guard(
                tenant_id=tenant.tenant_id,
                task_id=task_ref.meta.task_id,
                risk_level=task_ref.meta.risk_level,
                report=PreConflictReport(),
                pending_actions=exc.actions,
                mission_id=_mission_id_from_task(task_ref),
            )
            decision_tickets.append(preflight_ticket)
            self._record_state_ledger("record_decision_ticket", preflight_ticket)
            self._record_state_ledger(
                "record_paused",
                task_ref.meta.task_id,
                reason=answer,
                pending_confirmations=action_types,
            )
            async with session_scope() as s:
                await enqueue_pending_actions(
                    s,
                    tenant_id=tenant.tenant_id,
                    task_ref=task_ref,
                    actions=exc.actions,
                )
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.pending_actions.created",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            "source": "world_request_skill",
                            "actions": [action.model_dump(mode="json") for action in exc.actions],
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="task.preflight_guard.evaluated",
                        payload=preflight_ticket.event_payload(),
                        task_ref=task_ref.meta.task_id,
                    ),
                )
            log.info(
                "orchestrator.paused_for_world_action",
                task_id=exc.task_id,
                action_types=action_types,
            )
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "preflight_guard",
                    "decision_ticket": preflight_ticket.event_payload(),
                },
            )
            yield OrchestratorEvent(
                kind="guard_intervention",
                data={
                    "stage": "world_action_approval",
                    "message": "task_paused_for_world_action",
                    "task_id": exc.task_id,
                    "pending_actions": [action.model_dump(mode="json") for action in exc.actions],
                },
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
        try:
            if ooda_cycle.current_state == OODAState.REFLECT:
                ooda_cycle = await ooda_engine.transition(
                    ooda_cycle,
                    OODAState.DONE,
                    {"task_status": status, "result": status},
                )
            final_ooda_status: Literal["applied", "needs_review", "failed", "stopped"]
            if status == "done":
                final_ooda_status = "applied"
            elif status == "failed":
                final_ooda_status = "failed"
            elif status == "cancelled":
                final_ooda_status = "stopped"
            else:
                final_ooda_status = "needs_review"
            final_ooda_ticket = await self._record_ooda_checkpoint(
                tenant_id=tenant.tenant_id,
                task_ref=task_ref,
                cycle=ooda_cycle,
                checkpoint="finalize",
                decision_tickets=decision_tickets,
                status=final_ooda_status,
                reason=f"任务结束状态: {status}",
                evidence={"task_status": status},
            )
            yield OrchestratorEvent(
                kind="action_plan",
                data={
                    "stage": "ooda_finalize",
                    "status": status,
                    "decision_ticket": final_ooda_ticket.event_payload(),
                },
            )
        except Exception:
            log.exception("ooda.finalize_failed (non-fatal)")

        runtime.status = status
        runtime.finished_at = datetime.now(UTC)
        total_duration = time.perf_counter() - t0
        self._record_state_ledger("record_finished", task_ref.meta.task_id, runtime=runtime)

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
                    event_type=_final_task_event_type(status),
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
        self._cleanup_task_control(task_ref.meta.task_id)

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
            validation_ticket = ticket_from_validation_tier(
                tenant_id=tenant.tenant_id,
                task_id=task_ref.meta.task_id,
                risk_level=task_ref.meta.risk_level,
                complexity_score=task_ref.meta.complexity_score,
                tier=tier,
                execution_mode=task_ref.meta.execution_mode,
                mode_override_reason=task_ref.meta.mode_override_reason,
                mission_id=_mission_id_from_task(task_ref),
            )
            decision_tickets.append(validation_ticket)
            self._record_state_ledger("record_decision_ticket", validation_ticket)
            async with session_scope(tenant_id=tenant.tenant_id) as s:
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant.tenant_id,
                        event_type="validation.tier.selected",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            "tier": tier,
                            "risk_level": task_ref.meta.risk_level,
                            "complexity_score": task_ref.meta.complexity_score,
                            "execution_mode": task_ref.meta.execution_mode,
                            "mode_override_reason": task_ref.meta.mode_override_reason,
                            "decision_ticket": validation_ticket.event_payload(),
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
            yield OrchestratorEvent(
                kind="insight",
                data={
                    "stage": "validation_tier",
                    "tier": tier,
                    "decision_ticket": validation_ticket.event_payload(),
                },
            )
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
        scorecard = None
        if self.scoring_system is not None:
            try:
                scorecard = self.scoring_system.score_task(
                    task_ref=task_ref,
                    runtime=runtime,
                    status=status,
                    validation_outcome=validation_outcome,
                    validation_score=validation_score,
                    surprise_score=surprise,
                    decision=watchtower_decision,
                )
                async with session_scope(tenant_id=tenant.tenant_id) as s:
                    await emit(
                        s,
                        Event.build(
                            tenant_id=tenant.tenant_id,
                            event_type="scorecard.created",
                            payload=scorecard.model_dump(mode="json"),
                            task_ref=task_ref.meta.task_id,
                        ),
                    )
                yield OrchestratorEvent(kind="scorecard", data=scorecard.model_dump(mode="json"))
            except Exception as e:
                log.warning("scorecard.create_failed", error=str(e))
        rubric_source = scorecard.overall if scorecard is not None else validation_score
        rubric_5 = rubric_source * 5.0 if rubric_source is not None else None
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

        await self._finalize_credit_assignment(
            tenant_id=tenant.tenant_id,
            task_ref=task_ref,
            outcome=outcome,
        )

        await self._record_result_memory(
            tenant_id=tenant.tenant_id,
            task_ref=task_ref,
            status=status,
            answer=answer,
            runtime=runtime,
            validation_outcome=validation_outcome,
            validation_score=validation_score,
            surprise_score=surprise,
            score_overall=scorecard.overall if scorecard is not None else None,
            decision_tickets=decision_tickets,
        )

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

    def _register_task_control(self, task_id: str) -> None:
        if self.kill_switch is None:
            return
        try:
            self.kill_switch.register_task(task_id)
        except Exception:
            log.exception("task_control.register_failed", task_id=task_id)

    def _cleanup_task_control(self, task_id: str) -> None:
        if self.kill_switch is None:
            return
        try:
            self.kill_switch.cleanup(task_id)
        except Exception:
            log.exception("task_control.cleanup_failed", task_id=task_id)

    def _raise_if_task_cancelled(self, task_id: str) -> None:
        if self.kill_switch is None or not self.kill_switch.is_killed(task_id):
            return
        signal = self.kill_switch.get_kill_signal(task_id)
        raise TaskCancelledByUserError(task_id, signal.reason if signal else "user_interrupt")

    async def _await_task_control[T](self, task_id: str, awaitable: Awaitable[T]) -> T:
        if self.kill_switch is None:
            return await awaitable
        try:
            return cast(T, await self.kill_switch.wait_or_proceed(task_id, awaitable))
        except asyncio.CancelledError as exc:
            if self.kill_switch.is_killed(task_id):
                signal = self.kill_switch.get_kill_signal(task_id)
                raise TaskCancelledByUserError(
                    task_id,
                    signal.reason if signal else "user_interrupt",
                ) from exc
            raise

    async def _select_skills_with_moe(
        self,
        *,
        tenant_id: str,
        task_ref: TaskRef,
        top_k: int,
    ) -> list[Any]:
        """Select skills with durable credit + capability evidence in the hot path."""

        try:
            skill_keys = [f"skill:{skill_id}" for skill_id in self.skill_selector.skill_ids()]
            async with session_scope(tenant_id=tenant_id) as s:
                await load_resource_credit_scores(
                    s,
                    tenant_id=tenant_id,
                    resource_keys=skill_keys,
                )
        except Exception:
            log.debug("skill_selector.credit_hydration_skipped", exc_info=True)
        try:
            return await self.skill_selector.select_with_graph_and_capability(
                task_ref,
                top_k=top_k,
                tenant_id=tenant_id,
            )
        except Exception:
            log.exception("skill_selector.graph_capability_failed_fallback_to_basic")
            return self.skill_selector.select(task_ref, top_k=top_k)

    def _record_state_ledger(self, method_name: str, *args: Any, **kwargs: Any) -> None:
        if self.state_ledger is None:
            return
        try:
            method = getattr(self.state_ledger, method_name)
            method(*args, **kwargs)
        except Exception:
            log.exception("state_ledger.%s_failed (non-fatal)", method_name)

    async def _record_ooda_checkpoint(
        self,
        *,
        tenant_id: str,
        task_ref: TaskRef,
        cycle: OODACycle,
        checkpoint: str,
        decision_tickets: list[DecisionTicket],
        status: Literal[
            "selected",
            "applied",
            "allowed",
            "blocked",
            "skipped",
            "stopped",
            "escalated",
            "needs_review",
            "failed",
        ] = "applied",
        reason: str = "",
        step_id: int | None = None,
        evidence: dict[str, Any] | None = None,
    ) -> DecisionTicket:
        ticket = ticket_from_ooda_checkpoint(
            tenant_id=tenant_id,
            task_id=task_ref.meta.task_id,
            risk_level=task_ref.meta.risk_level,
            checkpoint=checkpoint,
            cycle=cycle,
            status=status,
            reason=reason,
            step_id=step_id,
            mission_id=_mission_id_from_task(task_ref),
            evidence=evidence,
        )
        decision_tickets.append(ticket)
        self._record_state_ledger("record_decision_ticket", ticket)
        async with session_scope(tenant_id=tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type="task.ooda.checkpoint",
                    payload=ticket.event_payload(),
                    task_ref=task_ref.meta.task_id,
                ),
            )
        return ticket

    def _record_step_credit(
        self,
        *,
        task_ref: TaskRef,
        step: StepRecord,
        answer: str,
        response: LLMResponse,
        role_template_id: str,
        context_asset_ids: list[str],
        watchtower_decision: Any,
        active_protocol: Any,
        decision_ticket_ids: list[str],
    ) -> None:
        if self.credit_assignment is None:
            return
        skill_ids = [step.skill_used] if step.skill_used and step.skill_used != "llm.direct" else []
        memory_asset_ids = list(context_asset_ids)
        memory_asset_ids.extend(_watchtower_similar_experience_asset_ids(watchtower_decision))
        resources: dict[str, list[str]] = {
            "memory": _dedupe_strings(memory_asset_ids),
            "skill": skill_ids,
            "model": [response.model or "unknown"],
            "model_tier": [str(response.tier)],
            "llm_route": [
                f"{response.provider or 'unknown'}:{response.model or 'unknown'}:{response.tier}"
            ],
            "execution_mode": [task_ref.meta.execution_mode],
            "role_template": [role_template_id],
            "value_gate": _value_gate_credit_resources(
                task_ref=task_ref,
                execution_mode=task_ref.meta.execution_mode,
                watchtower_decision=watchtower_decision,
            ),
        }
        if skill_ids:
            resources["value_gate_action"] = skill_ids
        if decision_ticket_ids:
            resources["decision_ticket"] = list(decision_ticket_ids)
        if watchtower_decision is not None:
            strategy_pack_id = getattr(watchtower_decision, "strategy_pack_id", "")
            if strategy_pack_id:
                resources["strategy_pack"] = [str(strategy_pack_id)]
        if active_protocol is not None:
            protocol_id = getattr(active_protocol, "protocol_id", "")
            if protocol_id:
                resources["protocol"] = [str(protocol_id)]
        estimated_cost = max(float(task_ref.meta.estimated_cost_usd or 0.0), 0.01)
        cost_penalty = min(0.3, float(step.cost_usd_equivalent or 0.0) / estimated_cost)
        answer_signal = 0.2 if answer.strip() else -0.2
        immediate_reward = max(0.0, min(1.0, 0.55 + answer_signal - cost_penalty))
        try:
            self.credit_assignment.record_step(
                task_ref.meta.task_id,
                step.step_id,
                resources,
                immediate_reward=immediate_reward,
                metadata={
                    "provider": response.provider,
                    "tier": str(response.tier),
                    "cost_usd_equivalent": response.cost_usd_equivalent,
                    "tokens": response.usage.total(),
                    "risk_level": task_ref.meta.risk_level,
                    "execution_mode": task_ref.meta.execution_mode,
                    "decision_ticket_ids": list(decision_ticket_ids),
                },
            )
        except Exception:
            log.exception("credit_assignment.record_step_failed (non-fatal)")

    def _attach_decision_ticket_to_step_credit(
        self,
        task_id: str,
        step_id: int,
        ticket: DecisionTicket,
    ) -> None:
        """Attach late post-step decisions to the step credit record."""

        if self.credit_assignment is None:
            return
        try:
            self.credit_assignment.add_resources_to_step(
                task_id,
                step_id,
                {
                    "decision_ticket": [ticket.ticket_id],
                    str(ticket.decision_point): [ticket.selected_action],
                },
            )
        except Exception:
            log.exception("credit_assignment.attach_late_decision_failed (non-fatal)")

    async def _finalize_credit_assignment(
        self,
        *,
        tenant_id: str,
        task_ref: TaskRef,
        outcome: str,
    ) -> None:
        if self.credit_assignment is None:
            return
        try:
            report = await self.credit_assignment.finalize_task(
                task_ref.meta.task_id,
                outcome,
                reflector=heuristic_reflector,
            )
            async with session_scope(tenant_id=tenant_id) as s:
                deltas = await persist_resource_credit_report(s, tenant_id=tenant_id, report=report)
                get_contribution_tracker().update_from_deltas(deltas, tenant_id=tenant_id)
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant_id,
                        event_type="credit.assignment.completed",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            "task_outcome": report.task_outcome,
                            "step_count": len(report.step_credits),
                            "critical_path_step_ids": report.critical_path_step_ids,
                            "total_immediate_reward": report.total_immediate_reward,
                            "resource_count": len(deltas),
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
        except Exception:
            log.exception("credit_assignment.finalize_failed (non-fatal)")
        finally:
            try:
                self.credit_assignment.reset_task(task_ref.meta.task_id)
            except Exception:
                log.exception("credit_assignment.reset_failed (non-fatal)")

    async def _record_meta_decision_memory(
        self,
        *,
        tenant_id: str,
        task_ref: TaskRef,
        decision: Any,
        decision_ticket: DecisionTicket | None = None,
    ) -> None:
        if self.memory_writeback is None:
            return
        try:
            result = await self.memory_writeback.record_meta_decision(
                tenant_id=tenant_id,
                task_ref=task_ref,
                decision=decision,
                decision_ticket=decision_ticket,
            )
            async with session_scope(tenant_id=tenant_id) as s:
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant_id,
                        event_type="memory.writeback.recorded",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            **result.model_dump(mode="json"),
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
        except Exception as e:
            log.warning("memory.meta_decision_writeback_failed", error=str(e))

    async def _record_process_memory(
        self,
        *,
        tenant_id: str,
        task_ref: TaskRef,
        step: StepRecord,
        answer: str,
        response: LLMResponse,
    ) -> None:
        if self.memory_writeback is None:
            return
        try:
            result = await self.memory_writeback.record_process_step(
                tenant_id=tenant_id,
                task_ref=task_ref,
                step=step,
                answer=answer,
                provider=response.provider,
                model=response.model,
                tier=str(response.tier),
            )
            async with session_scope(tenant_id=tenant_id) as s:
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant_id,
                        event_type="memory.writeback.recorded",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            **result.model_dump(mode="json"),
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
        except Exception as e:
            log.warning("memory.process_writeback_failed", error=str(e))

    async def _record_result_memory(
        self,
        *,
        tenant_id: str,
        task_ref: TaskRef,
        status: str,
        answer: str,
        runtime: RuntimeState,
        validation_outcome: str,
        validation_score: float | None,
        surprise_score: float,
        score_overall: float | None,
        decision_tickets: list[DecisionTicket] | None = None,
    ) -> None:
        if self.memory_writeback is None:
            return
        try:
            result = await self.memory_writeback.record_task_result(
                tenant_id=tenant_id,
                task_ref=task_ref,
                status=status,
                answer=answer,
                runtime=runtime,
                validation_outcome=validation_outcome,
                validation_score=validation_score,
                surprise_score=surprise_score,
                score_overall=score_overall,
                decision_tickets=decision_tickets or [],
            )
            async with session_scope(tenant_id=tenant_id) as s:
                await emit(
                    s,
                    Event.build(
                        tenant_id=tenant_id,
                        event_type="memory.writeback.recorded",
                        payload={
                            "task_id": task_ref.meta.task_id,
                            **result.model_dump(mode="json"),
                        },
                        task_ref=task_ref.meta.task_id,
                    ),
                )
        except Exception as e:
            log.warning("memory.result_writeback_failed", error=str(e))

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
        base_user_prompt = _execution_user_prompt(
            task_ref,
            step_description,
            prior_outputs=prior_outputs or [],
        )
        user_content = self.hermes_adapter.render_llm_step_prompt(
            base_prompt=base_user_prompt,
            task_id=task_ref.meta.task_id,
            task_type=task_ref.meta.task_type,
            risk_level=task_ref.meta.risk_level,
            step_description=step_description,
            pre_dispatched_block=pre_dispatched_block,
        )
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
            hermes_adapter=self.hermes_adapter,
            hermes_context={
                "task_id": task_ref.meta.task_id,
                "task_type": task_ref.meta.task_type,
                "risk_level": task_ref.meta.risk_level,
                "step_description": step_description,
            },
        )
        if loop_result.pause_requests:
            pending_actions = _pending_actions_from_loop_pause(
                task_id=task_ref.meta.task_id,
                pause_requests=loop_result.pause_requests,
            )
            if pending_actions:
                raise TaskPausedByWorldActionError(task_ref.meta.task_id, pending_actions)
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

    async def _execute_ensemble_step(
        self,
        *,
        task_ref: TaskRef,
        step_description: str,
        profile: TaskProfile,
        skills_summary: str = "",
        skill_directive: str = "",
        context_summary: str = "",
        prior_outputs: list[tuple[int, str]] | None = None,
        pre_dispatched_block: str = "",
    ) -> tuple[str, LLMResponse, dict[str, Any]]:
        """Execute one high-stakes step through the production ENSEMBLE path."""
        from kun.engineering.multi_judge import jury_evaluate
        from kun.lab import EnsembleConfig, EnsembleExecutor
        from kun.lab.llm_router_adapter import LLMRouterEnsembleAdapter

        audience = profile.audience if profile else "developer"
        audience_directive = _AUDIENCE_DIRECTIVES.get(audience, _AUDIENCE_DIRECTIVES["developer"])
        system_parts = [
            "你是 KUN 系统里的执行角色. 按用户要求完成任务, 回答准确、可验证. "
            "若需要外部数据, 说明需要什么. 不要编造.",
            audience_directive,
            "[ENSEMBLE] 这是高风险或高复杂任务。请给出完整、可验证、可交付的答案。",
        ]
        if skills_summary:
            system_parts.append(skills_summary)
        if skill_directive:
            system_parts.append(skill_directive)
        if context_summary:
            system_parts.append(context_summary)
        system_prompt = "\n\n".join(system_parts)
        base_user_prompt = _execution_user_prompt(
            task_ref,
            step_description,
            prior_outputs=prior_outputs or [],
        )
        user_content = self.hermes_adapter.render_llm_step_prompt(
            base_prompt=base_user_prompt,
            task_id=task_ref.meta.task_id,
            task_type=task_ref.meta.task_type,
            risk_level=task_ref.meta.risk_level,
            step_description=step_description,
            pre_dispatched_block=pre_dispatched_block,
        )
        ensemble_prompt = f"{system_prompt}\n\nUSER TASK:\n{user_content}"

        async def _score(output_text: str, original_prompt: str) -> float:
            try:
                verdict = await jury_evaluate(
                    artifact=output_text,
                    rubric=(
                        "根据原始任务判断这个候选答案是否准确、完整、可执行、没有编造。"
                        f"\n原始任务:\n{original_prompt[:2000]}"
                    ),
                    judge_models=[
                        "ensemble_judge_1",
                        "ensemble_judge_2",
                        "ensemble_judge_3",
                    ],
                    router=self.llm_router,
                )
                return verdict.avg_score if verdict.pass_ else min(verdict.avg_score, 0.49)
            except Exception:
                log.exception("ensemble.scoring_failed task=%s", task_ref.meta.task_id)
                return 0.5

        executor = EnsembleExecutor(
            LLMRouterEnsembleAdapter(
                self.llm_router,
                task_type=f"production_ensemble.{task_ref.meta.task_type}",
            ),
            require_lab_mode=False,
        )
        budget = max(float(task_ref.meta.estimated_cost_usd or 0.0), 0.1)
        result = await executor.run(
            ensemble_prompt,
            config=EnsembleConfig(
                n_paths=5,
                selection_method="judge_picks",
                cost_budget_total_usd=budget,
                metadata={
                    "task_id": task_ref.meta.task_id,
                    "execution_mode": "ENSEMBLE",
                    "production": True,
                },
            ),
            scoring_fn=_score,
            task_type=task_ref.meta.task_type,
        )
        response = LLMResponse(
            content=result.winning_output,
            usage=UsageInfo(),
            model="ensemble",
            provider="kun-lab",
            tier="top",
            cost_usd_actual=result.total_cost_usd,
            cost_usd_equivalent=result.total_cost_usd,
            latency_ms=result.total_latency_sec * 1000.0,
            raw={"ensemble_result": result.model_dump(mode="json")},
        )
        payload = {
            "task_id": task_ref.meta.task_id,
            "winner": result.winning_path_idx,
            "selection_reason": result.selection_reason,
            "total_cost_usd": result.total_cost_usd,
            "total_latency_sec": result.total_latency_sec,
            "budget_exceeded": result.budget_exceeded,
            "paths": [
                {
                    "path_idx": path.path_idx,
                    "strategy": path.config.get("strategy"),
                    "tier": path.config.get("tier"),
                    "score": path.score,
                    "cost_usd": path.cost_usd,
                    "latency_sec": path.latency_sec,
                    "error": path.error,
                    "output_preview": path.output[:300],
                }
                for path in result.path_results
            ],
        }
        return result.winning_output, response, payload


# =================== helpers ===================


def _hermes_skill_from_action(hermes_step: Any) -> str | None:
    """V2.2 §22 Wire 31 helper: hermes ExecutionStep.action_type → step_plan.skill_hint.

    Returns:
        skill_hint 字符串 (用 use_skill 的 payload.skill_id, 或 web_search 的内置)
        None 表示没有覆盖建议 (use_memory / direct_llm / ask_user — 后两者跑 step
        的默认路径或 Wire 32 单独 wire)
    """
    if hermes_step is None:
        return None
    action_type = getattr(hermes_step, "action_type", "")
    if action_type == "use_skill":
        payload = getattr(hermes_step, "action_payload", None) or {}
        skill_id = str(payload.get("skill_id") or payload.get("skill") or "").strip()
        return skill_id or None
    if action_type == "web_search":
        return "web_search"
    return None


def _hermes_question_from_step(hermes_step: Any) -> str:
    """V2.2 §22 Wire 32 helper: hermes ask_user → 抽问题文本.

    优先级: payload.question > payload.prompt > thought 兜底.
    """
    payload = getattr(hermes_step, "action_payload", None) or {}
    for key in ("question", "prompt", "ask"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(getattr(hermes_step, "thought", "") or "需要您澄清")


def _hermes_memory_query_from_step(hermes_step: Any, step_plan: Any) -> str:
    """V2.2 §22 Wire 33 helper: hermes use_memory → 抽 query 字符串.

    优先级: payload.query > payload.search > payload.topic > thought >
            step description fallback.
    返空字符串 → 不触发 pack_query (避免空 query 拉所有 asset).
    """
    payload = getattr(hermes_step, "action_payload", None) or {}
    for key in ("query", "search", "topic", "keywords"):
        v = payload.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list) and v:
            joined = " ".join(str(x) for x in v if x).strip()
            if joined:
                return joined
    thought = getattr(hermes_step, "thought", "") or ""
    if thought.strip():
        return thought.strip()
    return str(getattr(step_plan, "description", "") or "").strip()


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


async def _evaluate_task_boundary(
    *,
    tenant_id: str,
    task_ref: TaskRef,
    output_kind: str,
    mission_id: str | None,
) -> tuple[DecisionTicket, Any, Any] | None:
    """Run TaskBoundaryGuard when a role/task scope is explicitly configured."""

    if _os.getenv("KUN_TASK_BOUNDARY_ENABLED", "1") != "1":
        return None
    scope = _task_boundary_scope_for(task_ref=task_ref, output_kind=output_kind)
    if scope is None:
        return None

    from kun.security.task_boundary_guard import TaskBoundaryGuard

    task_meta = {
        "task_id": task_ref.meta.task_id,
        "task_type": task_ref.meta.task_type,
        "risk_level": task_ref.meta.risk_level,
        "complexity_score": task_ref.meta.complexity_score,
        "success_criteria_short": task_ref.meta.success_criteria_short,
    }
    decision = await TaskBoundaryGuard().check(task_meta=task_meta, scope=scope)
    strict_block = not decision.in_scope and scope.boundary_strict_mode
    ticket = DecisionTicket(
        tenant_id=tenant_id,
        task_id=task_ref.meta.task_id,
        mission_id=mission_id,
        phase="intake",
        decision_point="preflight_guard",
        source_module="security.task_boundary_guard",
        selected_action=(
            "pause_out_of_scope"
            if strict_block
            else "warn_out_of_scope"
            if not decision.in_scope
            else "allow_in_scope"
        ),
        status="blocked" if strict_block else "allowed",
        reason=decision.reason,
        confidence=decision.boundary_score,
        risk_level=task_ref.meta.risk_level,
        cost_estimate_usd=task_ref.meta.estimated_cost_usd,
        inputs_summary=task_meta,
        constraints=[
            f"role_id={scope.role_id or 'unknown'}",
            f"strict={scope.boundary_strict_mode}",
        ],
        evidence={
            "boundary_decision": decision.model_dump(mode="json"),
            "scope": scope.model_dump(mode="json"),
        },
        metadata={
            "task_type": task_ref.meta.task_type,
            "role_id": scope.role_id,
            "redirect": decision.suggested_redirect or scope.out_of_scope_redirect,
        },
    )
    return ticket, decision, scope


def _task_boundary_scope_for(*, task_ref: TaskRef, output_kind: str) -> Any | None:
    """Load a ScopeConfig from explicit env config.

    Supported shapes:
      - raw ScopeConfig JSON
      - {"default": ScopeConfig, "by_output_kind": {"user": ScopeConfig}}
      - {"by_task_type": {"coding.*": ScopeConfig}}

    No env config means no boundary scope, so simple tasks are not slowed down.
    """

    payload = _task_boundary_scope_payload()
    if not isinstance(payload, dict):
        return None

    raw_scope: Any = payload
    if "allowed_task_types" not in payload and "forbidden_task_types" not in payload:
        raw_scope = None
        by_output_kind = payload.get("by_output_kind")
        if isinstance(by_output_kind, dict):
            candidate = by_output_kind.get(output_kind)
            if isinstance(candidate, dict):
                raw_scope = candidate
        if raw_scope is None:
            by_task_type = payload.get("by_task_type")
            if isinstance(by_task_type, dict):
                for pattern, candidate in by_task_type.items():
                    if not isinstance(candidate, dict):
                        continue
                    if _task_type_matches_pattern(str(pattern), task_ref.meta.task_type):
                        raw_scope = candidate
                        break
        if raw_scope is None and isinstance(payload.get("default"), dict):
            raw_scope = payload["default"]
    if not isinstance(raw_scope, dict):
        return None

    from kun.security.task_boundary_guard import ScopeConfig

    try:
        return ScopeConfig.model_validate(raw_scope)
    except Exception:
        log.exception("task_boundary.scope_invalid")
        return None


def _task_boundary_scope_payload() -> dict[str, Any] | None:
    raw = _os.getenv("KUN_TASK_BOUNDARY_SCOPE_JSON")
    if raw:
        try:
            payload = json.loads(raw)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            log.warning("task_boundary.scope_json_invalid")
            return None

    path = _os.getenv("KUN_TASK_BOUNDARY_SCOPE_FILE")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else None
    except Exception as exc:
        log.warning("task_boundary.scope_file_unreadable", path=path, error=str(exc))
        return None


def _task_type_matches_pattern(pattern: str, task_type: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return task_type == prefix or task_type.startswith(prefix + ".")
    return pattern == task_type


def _boundary_pause_answer(boundary_decision: Any, scope: Any) -> str:
    redirect = boundary_decision.suggested_redirect or getattr(scope, "out_of_scope_redirect", "")
    target = f"建议转给 {redirect}。" if redirect else "你可以换一个更合适的角色或确认是否继续。"
    return (
        "任务已暂停：当前角色边界不覆盖这个任务类型。"
        f"原因：{boundary_decision.reason}；"
        f"命中：{boundary_decision.matched_pattern or '未命中当前 scope'}。"
        f"{target}"
    )


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


def _mission_id_from_task(task_ref: TaskRef) -> str | None:
    """Best-effort mission id extraction without adding a hard mission dependency."""
    if task_ref.spec is None:
        return None
    parent = task_ref.spec.parent_task_id
    if parent and parent.startswith("msn-"):
        return parent
    return None


def _attach_task_parent(task_ref: TaskRef, mission_id: str | None) -> None:
    """Attach durable Mission identity to a continuation TaskRef.

    Mission worker creates a new continuation task rather than mutating the
    original TaskRow in-place.  Without this parent pointer, Watchtower,
    DecisionTicket, StateLedger, and credit assignment cannot attribute the
    continuation back to the long-horizon mission.
    """

    if not mission_id:
        return
    if task_ref.spec is None:
        task_ref.spec = TaskSpec(goal_detail=task_ref.meta.success_criteria_short)
    task_ref.spec.parent_task_id = mission_id


def _memory_store_from_writeback(memory_writeback: Any) -> Any | None:
    """Reuse the same AssetStore used by memory writeback when available."""

    store = getattr(memory_writeback, "store", None)
    return store if store is not None else None


def _watchtower_similar_experience_asset_ids(watchtower_decision: Any) -> list[str]:
    """Return memory assets that influenced Watchtower's sparse path choice."""

    metadata = getattr(watchtower_decision, "metadata", None)
    if not isinstance(metadata, dict):
        return []
    refs = metadata.get("similar_experience_refs")
    if not isinstance(refs, list):
        return []
    out: list[str] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        asset_id = ref.get("asset_id")
        if isinstance(asset_id, str) and asset_id:
            out.append(asset_id)
    return out


def _value_gate_credit_resources(
    *,
    task_ref: TaskRef,
    execution_mode: str,
    watchtower_decision: Any,
) -> list[str]:
    """Return stable resource ids for cross-task ValueGate learning.

    ``persist_resource_credit_report`` stores these under the ``value_gate``
    kind, so the durable keys become ``value_gate:<id>``.  The ids are compact
    and intentionally task-type level rather than task-id level; otherwise the
    gate would only learn one-off facts and never generalize.
    """

    resources: list[str] = []
    task_type = getattr(task_ref.meta, "task_type", "")
    if task_type:
        resources.append(f"task_type:{task_type}")
    if execution_mode:
        resources.append(f"execution_mode:{execution_mode}")
    strategy_pack_id = getattr(watchtower_decision, "strategy_pack_id", "") or ""
    if strategy_pack_id:
        resources.append(f"strategy_pack:{strategy_pack_id}")
    return _dedupe_strings(resources)


def _value_gate_resource_keys(
    *,
    task_ref: TaskRef,
    step_skill: str,
    execution_mode: str,
    watchtower_decision: Any,
) -> list[str]:
    """Return durable resource keys that the ValueGate estimator should preload."""

    keys = [
        f"value_gate:{resource_id}"
        for resource_id in _value_gate_credit_resources(
            task_ref=task_ref,
            execution_mode=execution_mode,
            watchtower_decision=watchtower_decision,
        )
    ]
    if step_skill and step_skill != "llm.direct":
        keys.append(f"value_gate_action:{step_skill}")
    return _dedupe_strings(keys)


def _context_resource_ids(context_pack: ContextPack) -> list[str]:
    """Preserve asset kind for MoE credit keys.

    Older credit code treated every context asset as ``memory:<asset_id>``.
    That made knowledge/methodology/skill assets write to one key and read from
    another.  Returning ``kind:id`` keeps credit assignment aligned with
    ContextPacker's contribution lookup.
    """

    return [
        f"{item.asset_kind}:{item.asset_id}"
        for item in context_pack.items
        if item.asset_id and item.asset_kind
    ]


def _pending_actions_from_loop_pause(
    *,
    task_id: str,
    pause_requests: list[dict[str, Any]],
) -> list[PendingActionSpec]:
    actions: list[PendingActionSpec] = []
    seen: set[str] = set()
    for request in pause_requests:
        metadata = request.get("metadata")
        output = request.get("output")
        candidates: list[Any] = []
        if isinstance(metadata, dict):
            raw_actions = metadata.get("pending_actions")
            if isinstance(raw_actions, list):
                candidates.extend(raw_actions)
        if isinstance(output, dict) and isinstance(output.get("pending_action"), dict):
            candidates.append(output["pending_action"])
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            try:
                action = PendingActionSpec.model_validate(candidate)
            except Exception:
                log.warning(
                    "orchestrator.world_action_pause_bad_spec",
                    task_id=task_id,
                    candidate=candidate,
                )
                continue
            if action.action_id in seen:
                continue
            seen.add(action.action_id)
            actions.append(action)
    return actions


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _final_task_event_type(
    status: TaskStatus,
) -> Literal["task.done", "task.failed", "task.cancelled", "task.paused"]:
    if status == "done":
        return "task.done"
    if status == "paused":
        return "task.paused"
    if status == "cancelled":
        return "task.cancelled"
    return "task.failed"


def _watchtower_pause_requested(rule_engine: RuleEngine, fired_rule_ids: list[str]) -> bool:
    """Return True when a fired rule includes a hard pause action.

    Rule handlers can update external state, but the orchestrator owns the hot
    execution loop.  If the rule says "pause_task", the loop must stop in the
    same process instead of continuing and later overwriting DB state to done.
    """

    fired = set(fired_rule_ids)
    for rule in rule_engine.rules:
        if rule.id not in fired:
            continue
        if any(action.handler == "pause_task" for action in rule.actions):
            return True
    return False


def _memory_policy_from_watchtower(decision: Any | None) -> MemoryPolicyTicket | None:
    """Read the V5 memory policy from Watchtower metadata if available."""

    if decision is None:
        return None
    metadata = getattr(decision, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("memory_policy")
    if raw is None:
        return None
    try:
        return MemoryPolicyTicket.model_validate(raw)
    except Exception:
        log.debug("memory_policy.invalid_from_watchtower", exc_info=True)
        return None


def _memory_policy_allows_process_recall(policy: MemoryPolicyTicket | None) -> bool:
    if policy is None:
        return True
    if not policy.use_memory:
        return False
    layer_values = {layer.value for layer in policy.layers}
    avoided_values = {layer.value for layer in policy.avoid_layers}
    if "execution_process" in avoided_values:
        return False
    return not layer_values or "execution_process" in layer_values


def _memory_policy_allows_mid_run_recall(policy: MemoryPolicyTicket | None) -> bool:
    if policy is None:
        return True
    return policy.use_memory and policy.allow_mid_run_retrieval


def _memory_policy_mid_run_limit(
    policy: MemoryPolicyTicket | None,
    *,
    execution_mode: str,
) -> int:
    fallback = 3 if execution_mode == "MAX" else 2
    if policy is None or policy.max_items <= 0:
        return fallback
    return max(1, min(fallback, policy.max_items))


def _emergent_solution_observation(
    solution: Any,
    *,
    reason: str,
    signals: list[str],
) -> dict[str, Any]:
    """Convert an emergent solution into DynamicReplanner observations."""

    description = str(getattr(solution, "description", "") or "").strip()
    solution_id = str(getattr(solution, "solution_id", "") or "")
    status = str(getattr(solution, "status", "") or "")
    applies_when = [
        str(item).strip()
        for item in list(getattr(solution, "applies_when", []) or [])
        if str(item).strip()
    ]
    summary = description or reason or f"涌现方案 {solution_id or 'unknown'}"
    replacement_steps = [
        {
            "description": f"按涌现方案调整后续执行: {summary}",
            "skill_hint": "task.replan",
        },
        {
            "description": "按调整后的方案重新验证结果并交付",
            "skill_hint": "task.validation",
        },
    ]
    return {
        "needs_replan": True,
        "reason": reason or summary,
        "summary": summary,
        "replacement_steps": replacement_steps,
        "solution_id": solution_id,
        "solution_status": status,
        "signals": signals,
        "applies_when": applies_when,
    }


async def _load_mission_strategy(*, tenant_id: str, mission_id: str) -> dict[str, Any]:
    """Load compact Mission strategy state for Watchtower.

    This deliberately returns only strategy-like fields.  The full mission
    remains owned by mission_control; Orchestrator only consumes review signals
    so the next run can adjust cost/risk/recovery attention.
    """

    try:
        async with session_scope(tenant_id=tenant_id) as s:
            row = (
                await s.execute(
                    select(MissionRow).where(
                        MissionRow.tenant_id == tenant_id,
                        MissionRow.mission_id == mission_id,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                return {}
            strategy = dict(row.strategy_json or {})
            strategy["mission_id"] = row.mission_id
            strategy["mission_risk_level"] = row.risk_level
            strategy["mission_budget_cap_usd"] = row.budget_cap_usd
            strategy["mission_budget_used_usd"] = row.budget_used_usd
            if row.next_step_json:
                strategy["next_step"] = dict(row.next_step_json)
            return strategy
    except Exception:
        log.exception("mission.strategy_load_failed", mission_id=mission_id)
        return {}


__all__ = ["Orchestrator", "OrchestratorEvent", "TaskResult"]
