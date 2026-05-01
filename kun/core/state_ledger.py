"""State Ledger — current task truth for humans and LLMs.

V3-2 first cut:
- RuntimeState remains the durable per-task DB snapshot.
- EventRow remains the append-only audit log.
- StateLedger is the hot current-state view used by orchestrator/runtime/blackboard.

V4 first durable cut:
- StateLedgerEntryRow keeps the latest readable current-state snapshot.
- EventRow still owns history; this table only solves restart-loss of current facts.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine, Mapping, Sequence
from concurrent.futures import Future, TimeoutError
from datetime import UTC, datetime
from threading import Event, RLock, Thread
from typing import Any, Protocol, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kun.core.db import session_scope
from kun.core.orm import StateLedgerEntryRow
from kun.datamodel.decision_ticket import DecisionTicket
from kun.datamodel.runtime import RuntimeState, StepRecord, TaskStatus
from kun.datamodel.task import TaskRef

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class StateLedgerTrail(BaseModel):
    """A small recent trail item for debugging and UI context."""

    model_config = ConfigDict(extra="forbid")

    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    kind: str
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)


class StateLedgerEntry(BaseModel):
    """Current readable state for one task."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    tenant_id: str
    user_id: str | None = None
    project_id: str | None = None
    task_type: str = ""
    title: str = ""
    current_goal: str = ""
    status: TaskStatus = "queued"
    current_step: int = 0
    total_steps: int = 0
    current_action: str = ""
    current_risk: str = "low"
    complexity_score: float = 0.0
    execution_mode: str = "FAST"
    strategy_pack_id: str | None = None
    strategy_pack_name: str | None = None
    decision_reason: str = ""
    metric_dimensions: list[str] = Field(default_factory=list)
    reward_weights: dict[str, float] = Field(default_factory=dict)
    context_limit: int | None = None
    context_asset_ids: list[str] = Field(default_factory=list)
    skill_hints: list[str] = Field(default_factory=list)
    credit_assignment_count: int = 0
    credit_assignment_summary: dict[str, Any] | None = None
    resource_credit_summaries: list[dict[str, Any]] = Field(default_factory=list)
    top_credit_resource_kinds: list[str] = Field(default_factory=list)
    critical_path_step_ids: list[int] = Field(default_factory=list)
    risk_watch: list[str] = Field(default_factory=list)
    alert_flags: list[str] = Field(default_factory=list)
    decision_ticket_ids: list[str] = Field(default_factory=list)
    latest_decision_ticket: dict[str, Any] | None = None
    current_model: str = ""
    current_provider: str = ""
    current_tier: str = ""
    current_skill: str = ""
    budget_estimated_usd: float = 0.0
    cost_so_far_usd: float = 0.0
    tokens_so_far: int = 0
    pending_confirmations: list[str] = Field(default_factory=list)
    pending_reason: str = ""
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None
    recent_events: list[StateLedgerTrail] = Field(default_factory=list)

    def add_trail(self, kind: str, summary: str, data: dict[str, Any] | None = None) -> None:
        self.recent_events.append(
            StateLedgerTrail(kind=kind, summary=summary, data=dict(data or {}))
        )
        self.recent_events = self.recent_events[-20:]
        self.updated_at = datetime.now(UTC)


class StateLedgerStore(Protocol):
    """Sync facade used by StateLedger to persist/read current snapshots."""

    def save(self, entry: StateLedgerEntry) -> None: ...

    def get(self, *, task_id: str, tenant_id: str) -> StateLedgerEntry | None: ...

    def list_active(self, *, tenant_id: str, limit: int = 50) -> list[StateLedgerEntry]: ...


class StateLedgerDatabaseStore:
    """Postgres-backed StateLedger store.

    Writes are queued onto a private event loop so the sync ledger API does not
    force orchestrator callers to await DB I/O. Reads block briefly because
    blackboard restart recovery needs a real persisted answer.
    """

    def __init__(self, *, read_timeout_sec: float = 1.0, enable_reads: bool = True) -> None:
        self._runner = _AsyncLoopRunner()
        self._read_timeout_sec = read_timeout_sec
        self._enable_reads = enable_reads
        self._pending: set[Future[None]] = set()
        self._lock = RLock()

    def save(self, entry: StateLedgerEntry) -> None:
        future = cast(
            Future[None],
            self._runner.submit(_upsert_state_ledger_entry(entry), wait=False),
        )
        with self._lock:
            self._pending.add(future)
        future.add_done_callback(self._finish_write)

    def get(self, *, task_id: str, tenant_id: str) -> StateLedgerEntry | None:
        if not self._enable_reads:
            return None
        try:
            return cast(
                StateLedgerEntry | None,
                self._runner.submit(
                    _load_state_ledger_entry(task_id=task_id, tenant_id=tenant_id),
                    wait=True,
                    timeout=self._read_timeout_sec,
                ),
            )
        except Exception:
            logger.exception("state_ledger.read_failed", extra={"task_id": task_id})
            return None

    def list_active(self, *, tenant_id: str, limit: int = 50) -> list[StateLedgerEntry]:
        if not self._enable_reads:
            return []
        try:
            return cast(
                list[StateLedgerEntry],
                self._runner.submit(
                    _list_active_state_ledger_entries(tenant_id=tenant_id, limit=limit),
                    wait=True,
                    timeout=self._read_timeout_sec,
                ),
            )
        except Exception:
            logger.exception("state_ledger.list_active_failed")
            return []

    def flush(self, *, timeout_sec: float = 2.0) -> None:
        with self._lock:
            pending = list(self._pending)
        for future in pending:
            try:
                future.result(timeout=timeout_sec)
            except Exception:
                logger.exception("state_ledger.flush_failed")

    def _finish_write(self, future: Future[None]) -> None:
        with self._lock:
            self._pending.discard(future)
        try:
            future.result()
        except Exception:
            logger.exception("state_ledger.persist_failed")


class _AsyncLoopRunner:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready = Event()
        self._thread = Thread(target=self._run, name="state-ledger-db", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=1.0)

    def submit(
        self,
        coro: Coroutine[Any, Any, _T],
        *,
        wait: bool,
        timeout: float | None = None,
    ) -> _T | Future[_T]:
        if self._loop is None:
            raise RuntimeError("state ledger DB loop did not start")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        if not wait:
            return future
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self._ready.set()
        loop.run_forever()


async def _upsert_state_ledger_entry(entry: StateLedgerEntry) -> None:
    values = _entry_row_values(entry)
    stmt = pg_insert(StateLedgerEntryRow).values(**values)
    update_values = {
        "user_id": stmt.excluded.user_id,
        "project_id": stmt.excluded.project_id,
        "status": stmt.excluded.status,
        "snapshot_json": stmt.excluded.snapshot_json,
        "updated_at": stmt.excluded.updated_at,
    }
    stmt = stmt.on_conflict_do_update(
        index_elements=[StateLedgerEntryRow.tenant_id, StateLedgerEntryRow.task_id],
        set_=update_values,
    )
    async with session_scope(tenant_id=entry.tenant_id) as session:
        await session.execute(stmt)


async def _load_state_ledger_entry(*, task_id: str, tenant_id: str) -> StateLedgerEntry | None:
    async with session_scope(tenant_id=tenant_id) as session:
        row = (
            await session.execute(
                select(StateLedgerEntryRow).where(
                    StateLedgerEntryRow.tenant_id == tenant_id,
                    StateLedgerEntryRow.task_id == task_id,
                )
            )
        ).scalar_one_or_none()
    return _entry_from_row(row) if row is not None else None


async def _list_active_state_ledger_entries(
    *,
    tenant_id: str,
    limit: int = 50,
) -> list[StateLedgerEntry]:
    async with session_scope(tenant_id=tenant_id) as session:
        rows = (
            (
                await session.execute(
                    select(StateLedgerEntryRow)
                    .where(
                        StateLedgerEntryRow.tenant_id == tenant_id,
                        StateLedgerEntryRow.status.in_(("queued", "running", "paused")),
                    )
                    .order_by(desc(StateLedgerEntryRow.updated_at))
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [_entry_from_row(row) for row in rows]


def _entry_row_values(entry: StateLedgerEntry) -> dict[str, Any]:
    return {
        "tenant_id": entry.tenant_id,
        "task_id": entry.task_id,
        "user_id": entry.user_id,
        "project_id": entry.project_id,
        "status": entry.status,
        "snapshot_json": entry.model_dump(mode="json"),
        "created_at": entry.started_at,
        "updated_at": entry.updated_at,
    }


def _entry_from_row(row: StateLedgerEntryRow) -> StateLedgerEntry:
    data = dict(row.snapshot_json or {})
    data.setdefault("tenant_id", row.tenant_id)
    data.setdefault("task_id", row.task_id)
    data.setdefault("user_id", row.user_id)
    data.setdefault("project_id", row.project_id)
    data.setdefault("status", row.status)
    data.setdefault("updated_at", row.updated_at)
    return StateLedgerEntry.model_validate(data)


class StateLedger:
    """Current-state ledger with optional durable snapshots.

    The write API stays sync because the orchestrator hot path already calls it
    that way. A store can persist snapshots behind that sync boundary. Durable
    EventRow is still the historical source; StateLedgerEntryRow is a restart
    survival cache for the latest current view.
    """

    def __init__(self, *, store: StateLedgerStore | None = None) -> None:
        self._lock = RLock()
        self._entries: dict[str, StateLedgerEntry] = {}
        self._store = store

    def record_task_created(
        self,
        task_ref: TaskRef,
        *,
        tenant_id: str,
        status: TaskStatus = "queued",
    ) -> StateLedgerEntry:
        goal = task_ref.spec.goal_detail if task_ref.spec is not None else ""
        if not goal:
            goal = task_ref.meta.success_criteria_short
        entry = StateLedgerEntry(
            task_id=task_ref.meta.task_id,
            tenant_id=tenant_id,
            user_id=task_ref.meta.owner.user_id,
            project_id=task_ref.meta.owner.project_id,
            task_type=task_ref.meta.task_type,
            title=task_ref.meta.success_criteria_short,
            current_goal=goal,
            status=status,
            current_risk=task_ref.meta.risk_level,
            complexity_score=task_ref.meta.complexity_score,
            execution_mode=task_ref.meta.execution_mode,
            budget_estimated_usd=task_ref.meta.estimated_cost_usd,
        )
        entry.add_trail("task.created", "任务已登记", {"status": status})
        with self._lock:
            self._entries[entry.task_id] = entry
            return self._stored_copy(entry)

    def record_decision(self, task_id: str, decision: Any) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            entry.execution_mode = str(getattr(decision, "execution_mode", entry.execution_mode))
            entry.strategy_pack_id = _optional_str(getattr(decision, "strategy_pack_id", None))
            entry.strategy_pack_name = _optional_str(getattr(decision, "strategy_pack_name", None))
            entry.decision_reason = str(getattr(decision, "reason", ""))
            entry.context_limit = _optional_int(getattr(decision, "context_limit", None))
            entry.skill_hints = _string_list(getattr(decision, "skill_hints", []))
            entry.metric_dimensions = _string_list(getattr(decision, "metric_dimensions", []))
            entry.reward_weights = _float_dict(getattr(decision, "reward_weights", {}))
            entry.risk_watch = _string_list(getattr(decision, "risk_watch", []))
            entry.alert_flags = _string_list(getattr(decision, "alert_flags", []))
            entry.add_trail(
                "watchtower.decision",
                entry.decision_reason or "守望已生成策略单",
                {
                    "strategy_pack_id": entry.strategy_pack_id,
                    "execution_mode": entry.execution_mode,
                    "context_limit": entry.context_limit,
                },
            )
            self._store_entry(entry)

    def record_decision_ticket(self, ticket: DecisionTicket) -> None:
        """Record a V4 decision ticket in the hot task view.

        The durable source of truth remains EventRow.  The ledger only keeps a
        compact recent list so humans and LLMs can explain current execution.
        """
        with self._lock:
            entry = self._ensure(ticket.task_id)
            if not entry.tenant_id:
                entry.tenant_id = ticket.tenant_id
            if ticket.ticket_id not in entry.decision_ticket_ids:
                entry.decision_ticket_ids.append(ticket.ticket_id)
                entry.decision_ticket_ids = entry.decision_ticket_ids[-30:]
            entry.latest_decision_ticket = ticket.event_payload()
            if ticket.decision_point == "strategy_selected":
                entry.decision_reason = ticket.reason or entry.decision_reason
                entry.strategy_pack_id = str(ticket.metadata.get("strategy_pack_id") or "")
                entry.execution_mode = str(
                    ticket.metadata.get("execution_mode") or entry.execution_mode
                )
            elif ticket.decision_point == "execution_mode_selected":
                entry.execution_mode = str(
                    ticket.metadata.get("execution_mode") or entry.execution_mode
                )
                entry.decision_reason = ticket.reason or entry.decision_reason
                entry.current_action = f"选择执行模式 {entry.execution_mode}"
            elif ticket.decision_point == "context_selected":
                asset_ids = ticket.metadata.get("asset_ids")
                if isinstance(asset_ids, list):
                    entry.context_asset_ids = [str(asset_id) for asset_id in asset_ids]
                entry.decision_reason = ticket.reason or entry.decision_reason
            elif ticket.decision_point == "memory_policy_selected":
                entry.decision_reason = ticket.reason or entry.decision_reason
                layers = ticket.metadata.get("layers")
                if isinstance(layers, list) and layers:
                    entry.current_action = (
                        f"选择记忆层 {', '.join(str(item) for item in layers[:3])}"
                    )
            elif ticket.decision_point == "skill_selected":
                skill_ids = ticket.metadata.get("skill_ids")
                if isinstance(skill_ids, list):
                    entry.skill_hints = [str(skill_id) for skill_id in skill_ids]
                entry.decision_reason = ticket.reason or entry.decision_reason
            elif ticket.decision_point == "step_action_selected":
                action_type = str(ticket.metadata.get("action_type") or ticket.selected_action)
                entry.current_action = f"Hermes 选择步骤动作 {action_type}"
                entry.decision_reason = ticket.reason or entry.decision_reason
            elif ticket.decision_point == "preflight_guard":
                entry.decision_reason = ticket.reason or entry.decision_reason
                if ticket.status == "blocked":
                    entry.status = "paused"
                    entry.pending_reason = ticket.reason
                    entry.current_action = "预检拦截，等待确认或重排"
                    _append_unique(entry.alert_flags, "preflight_guard_blocked")
            elif ticket.decision_point == "proactive_tool_dispatch":
                if ticket.status in {"applied", "skipped"}:
                    entry.current_action = f"主动工具调度 {ticket.selected_action}"
                entry.decision_reason = ticket.reason or entry.decision_reason
            elif ticket.decision_point == "anti_gaming_detected":
                entry.current_risk = "high"
                entry.decision_reason = ticket.reason or entry.decision_reason
                entry.current_action = f"发现反作弊风险 {ticket.selected_action}"
                _append_unique(entry.alert_flags, f"anti_gaming:{ticket.selected_action}")
            elif ticket.decision_point == "emergent_switch":
                entry.decision_reason = ticket.reason or entry.decision_reason
                entry.current_action = f"动态路径评估 {ticket.selected_action}"
                if ticket.status == "blocked":
                    _append_unique(entry.alert_flags, "emergent_switch_blocked")
            elif ticket.decision_point == "budget_policy":
                used = ticket.metadata.get("used_usd")
                if isinstance(used, int | float):
                    entry.cost_so_far_usd = float(used)
                entry.decision_reason = ticket.reason or entry.decision_reason
            elif ticket.decision_point == "protocol_applied":
                entry.decision_reason = ticket.reason or entry.decision_reason
                entry.execution_mode = str(
                    ticket.metadata.get("execution_mode") or entry.execution_mode
                )
                protocol_id = str(ticket.metadata.get("protocol_id") or "")
                if protocol_id:
                    entry.current_action = f"应用任务协议 {protocol_id}"
            elif ticket.decision_point == "llm_model_selected":
                entry.current_provider = str(
                    ticket.metadata.get("provider") or entry.current_provider
                )
                entry.current_model = str(ticket.metadata.get("model") or entry.current_model)
                selected_parts = ticket.selected_action.split(":")
                if len(selected_parts) >= 3:
                    entry.current_tier = selected_parts[-1] or entry.current_tier
                entry.decision_reason = ticket.reason or entry.decision_reason
            elif ticket.decision_point in {"delivery_review", "world_policy"}:
                entry.decision_reason = ticket.reason or entry.decision_reason
            elif ticket.decision_point == "validation_tier_selected":
                entry.current_tier = str(
                    ticket.metadata.get("validation_tier") or entry.current_tier
                )
                entry.decision_reason = ticket.reason or entry.decision_reason
            elif ticket.decision_point == "ooda_checkpoint":
                checkpoint = str(ticket.metadata.get("checkpoint") or "")
                state = str(ticket.metadata.get("state") or "")
                step_id = ticket.metadata.get("step_id")
                entry.current_action = (
                    f"OODA {checkpoint}: {state}"
                    if checkpoint
                    else f"OODA: {state or ticket.selected_action}"
                )
                if isinstance(step_id, int):
                    entry.current_step = max(entry.current_step, step_id)
                entry.decision_reason = ticket.reason or entry.decision_reason
                if ticket.status in {"needs_review", "blocked", "failed"}:
                    _append_unique(entry.alert_flags, f"ooda:{ticket.status}")
            entry.add_trail(
                "decision.ticket",
                f"{ticket.decision_point}: {ticket.selected_action}",
                {
                    "ticket_id": ticket.ticket_id,
                    "phase": ticket.phase,
                    "decision_point": ticket.decision_point,
                    "status": ticket.status,
                    "reason": ticket.reason,
                },
            )
            self._store_entry(entry)

    def record_plan(self, task_id: str, *, total_steps: int) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            entry.total_steps = max(0, total_steps)
            entry.add_trail("task.plan", f"执行计划共 {entry.total_steps} 步")
            self._store_entry(entry)

    def record_running(self, task_id: str, *, runtime: RuntimeState) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            _apply_runtime(entry, runtime)
            entry.add_trail("task.started", "任务开始执行")
            self._store_entry(entry)

    def record_context(self, task_id: str, *, asset_ids: list[str]) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            entry.context_asset_ids = list(asset_ids)
            entry.add_trail("context.preheated", "上下文已预热", {"asset_ids": asset_ids})
            self._store_entry(entry)

    def record_task_metadata_updated(
        self,
        task_id: str,
        *,
        tenant_id: str | None = None,
        risk_level: str | None = None,
        estimated_cost_usd: float | None = None,
        success_criteria_short: str | None = None,
        constraint_note: str | None = None,
        confirmation_policy: str | None = None,
    ) -> None:
        """Reflect a human/operator task-control edit in the current ledger."""
        with self._lock:
            entry = self._ensure(task_id)
            if tenant_id:
                entry.tenant_id = tenant_id
            changed: dict[str, Any] = {}
            if risk_level:
                entry.current_risk = risk_level
                changed["risk_level"] = risk_level
            if estimated_cost_usd is not None:
                entry.budget_estimated_usd = max(0.0, float(estimated_cost_usd))
                changed["estimated_cost_usd"] = entry.budget_estimated_usd
            if success_criteria_short:
                entry.title = success_criteria_short
                entry.current_goal = success_criteria_short
                changed["success_criteria_short"] = success_criteria_short
            if constraint_note:
                changed["constraint_note"] = constraint_note
            if confirmation_policy:
                changed["confirmation_policy"] = confirmation_policy
            entry.add_trail("task.metadata_updated", "用户/运维更新了任务控制参数", changed)
            self._store_entry(entry)

    def record_current_action(
        self,
        task_id: str,
        *,
        step_id: int,
        description: str,
        skill_hint: str | None = None,
    ) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            entry.current_step = max(entry.current_step, step_id)
            entry.current_action = description
            entry.current_skill = skill_hint or entry.current_skill
            entry.add_trail(
                "task.step.started",
                f"开始第 {step_id} 步",
                {"step_id": step_id, "description": description, "skill_hint": skill_hint or ""},
            )
            self._store_entry(entry)

    def record_step_completed(
        self,
        task_id: str,
        *,
        runtime: RuntimeState,
        step: StepRecord,
        provider: str,
        model: str,
        tier: str,
    ) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            _apply_runtime(entry, runtime)
            entry.current_model = model
            entry.current_provider = provider
            entry.current_tier = tier
            entry.current_skill = step.skill_used
            entry.add_trail(
                "task.step.completed",
                f"完成第 {step.step_id} 步",
                {
                    "step_id": step.step_id,
                    "skill_used": step.skill_used,
                    "cost_usd": step.cost_usd_equivalent,
                    "tokens": step.tokens_in + step.tokens_out,
                },
            )
            self._store_entry(entry)

    def record_paused(
        self,
        task_id: str,
        *,
        reason: str,
        pending_confirmations: list[str] | None = None,
    ) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            entry.status = "paused"
            entry.pending_reason = reason
            entry.pending_confirmations = list(pending_confirmations or [])
            entry.add_trail(
                "task.paused",
                reason,
                {"pending_confirmations": entry.pending_confirmations},
            )
            self._store_entry(entry)

    def record_resumed(self, task_id: str, *, reason: str) -> None:
        """Reflect a durable runtime resume in the hot ledger view."""
        with self._lock:
            entry = self._ensure(task_id)
            entry.status = "queued"
            entry.pending_reason = ""
            entry.pending_confirmations = []
            entry.add_trail("task.resumed", reason)
            self._store_entry(entry)

    def record_world_action_executed(
        self,
        task_id: str,
        *,
        action_id: str,
        action_type: str,
        gateway_mode: str,
        external_dispatched: bool,
        requires_handler: bool,
        handler_id: str | None = None,
        artifact_ref: str | None = None,
        message: str = "",
        decision_ticket: DecisionTicket | None = None,
    ) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            resolved_aliases = {action_id, action_type}
            entry.pending_confirmations = [
                item for item in entry.pending_confirmations if item not in resolved_aliases
            ]
            entry.current_action = f"World action {action_type}: {gateway_mode}"
            if message:
                entry.pending_reason = message if requires_handler else ""
            entry.add_trail(
                "world.action.executed",
                message or f"World action executed via {gateway_mode}",
                {
                    "action_id": action_id,
                    "action_type": action_type,
                    "gateway_mode": gateway_mode,
                    "external_dispatched": external_dispatched,
                    "requires_handler": requires_handler,
                    "handler_id": handler_id or "",
                    "artifact_ref": artifact_ref or "",
                    "decision_ticket_id": decision_ticket.ticket_id if decision_ticket else "",
                },
            )
            if decision_ticket is not None:
                if decision_ticket.ticket_id not in entry.decision_ticket_ids:
                    entry.decision_ticket_ids.append(decision_ticket.ticket_id)
                    entry.decision_ticket_ids = entry.decision_ticket_ids[-30:]
                entry.latest_decision_ticket = decision_ticket.event_payload()
            self._store_entry(entry)

    def record_world_action_blocked(
        self,
        task_id: str,
        *,
        action_id: str,
        action_type: str,
        gateway_mode: str,
        external_dispatched: bool,
        requires_handler: bool,
        capability_status: str = "",
        message: str = "",
        decision_ticket: DecisionTicket | None = None,
    ) -> None:
        """Keep blocked external actions visible in the hot ledger.

        A blocked action is not "done"; it is a state humans and LLMs need to
        see immediately: what was blocked, why, and what confirmation/config is
        still needed.  Durable EventRow remains the audit source of truth.
        """
        with self._lock:
            entry = self._ensure(task_id)
            entry.status = "paused"
            entry.current_action = f"World action {action_type}: blocked by {gateway_mode}"
            entry.pending_reason = message or "外部动作被守望/网关拦截"
            if action_id not in entry.pending_confirmations:
                entry.pending_confirmations.append(action_id)
                entry.pending_confirmations = entry.pending_confirmations[-20:]
            flag = f"world_action_blocked:{action_type}"
            if flag not in entry.alert_flags:
                entry.alert_flags.append(flag)
                entry.alert_flags = entry.alert_flags[-20:]
            entry.add_trail(
                "world.action.blocked",
                entry.pending_reason,
                {
                    "action_id": action_id,
                    "action_type": action_type,
                    "gateway_mode": gateway_mode,
                    "external_dispatched": external_dispatched,
                    "requires_handler": requires_handler,
                    "capability_status": capability_status,
                    "decision_ticket_id": decision_ticket.ticket_id if decision_ticket else "",
                },
            )
            if decision_ticket is not None:
                if decision_ticket.ticket_id not in entry.decision_ticket_ids:
                    entry.decision_ticket_ids.append(decision_ticket.ticket_id)
                    entry.decision_ticket_ids = entry.decision_ticket_ids[-30:]
                entry.latest_decision_ticket = decision_ticket.event_payload()
            self._store_entry(entry)

    def record_world_action_failed(
        self,
        task_id: str,
        *,
        action_id: str,
        action_type: str,
        error: str,
    ) -> None:
        """Keep handler execution failures visible in the hot ledger."""
        with self._lock:
            entry = self._ensure(task_id)
            entry.status = "paused"
            entry.current_action = f"World action {action_type}: execution failed"
            entry.pending_reason = f"外部动作执行失败：{error}"
            if action_id not in entry.pending_confirmations:
                entry.pending_confirmations.append(action_id)
                entry.pending_confirmations = entry.pending_confirmations[-20:]
            flag = f"world_action_failed:{action_type}"
            if flag not in entry.alert_flags:
                entry.alert_flags.append(flag)
                entry.alert_flags = entry.alert_flags[-20:]
            entry.add_trail(
                "world.action.failed",
                entry.pending_reason,
                {
                    "action_id": action_id,
                    "action_type": action_type,
                    "error": error,
                },
            )
            self._store_entry(entry)

    def record_code_change(
        self,
        task_id: str,
        *,
        path: str,
        mode: str,
        phase: str,
        ok: bool,
        applied: bool,
        rolled_back: bool,
        checks_passed: bool,
        tenant_id: str | None = None,
        reason: str = "",
        bytes_changed: int = 0,
    ) -> None:
        """Expose CodeCapability workflow outcomes in the current task view."""
        with self._lock:
            entry = self._ensure(task_id)
            if tenant_id and not entry.tenant_id:
                entry.tenant_id = tenant_id
            status_label = "通过" if ok else "未通过"
            if rolled_back:
                status_label = "已回滚"
            entry.current_action = f"CodeCapability {mode} {path}: {status_label}"
            if reason:
                entry.decision_reason = reason
            if not ok:
                _append_unique(entry.alert_flags, f"code_change:{phase}")
            entry.add_trail(
                "code.change.proposed",
                entry.current_action,
                {
                    "path": path,
                    "mode": mode,
                    "phase": phase,
                    "ok": ok,
                    "applied": applied,
                    "rolled_back": rolled_back,
                    "checks_passed": checks_passed,
                    "bytes_changed": bytes_changed,
                    "reason": reason,
                },
            )
            self._store_entry(entry)

    def record_credit_assignment(
        self,
        task_id: str,
        *,
        task_outcome: str,
        step_count: int,
        critical_path_step_ids: list[int] | None = None,
        total_immediate_reward: float = 0.0,
        resource_count: int = 0,
        resource_kind_summaries: list[dict[str, Any]] | None = None,
    ) -> None:
        """Expose resource credit assignment in the current task view.

        Memory and MoE routing need more than "task succeeded".  They need to
        know which resource classes helped: model, skill, context, protocol,
        decision ticket, and so on.  Durable EventRow remains the historical
        source; this method keeps the hot ledger honest and readable.
        """
        summaries = _resource_credit_summaries(resource_kind_summaries)
        with self._lock:
            entry = self._ensure(task_id)
            entry.credit_assignment_count += 1
            entry.resource_credit_summaries = summaries
            entry.top_credit_resource_kinds = _top_credit_resource_kinds(summaries)
            entry.critical_path_step_ids = _int_list(critical_path_step_ids or [])
            entry.credit_assignment_summary = {
                "task_outcome": task_outcome,
                "step_count": max(0, int(step_count)),
                "critical_path_step_ids": list(entry.critical_path_step_ids),
                "total_immediate_reward": float(total_immediate_reward),
                "resource_count": max(0, int(resource_count)),
                "resource_kind_count": len(summaries),
                "top_resource_kinds": list(entry.top_credit_resource_kinds),
            }
            if entry.top_credit_resource_kinds:
                entry.current_action = (
                    "完成信用归因：" + "、".join(entry.top_credit_resource_kinds[:3]) + " 贡献最高"
                )
            entry.add_trail(
                "credit.assignment.completed",
                "资源信用归因已完成",
                entry.credit_assignment_summary,
            )
            self._store_entry(entry)

    def record_system_health_report(self, report: Any) -> StateLedgerEntry:
        """Expose NUO system findings in the current-state view.

        NUO is the system housekeeper.  A deep health report should not live
        only in append-only events; serious findings need to be visible in the
        same blackboard/status surface that humans and LLMs already consume.
        """

        tenant_id = str(getattr(report, "tenant_id", ""))
        task_id = f"system:nuo:{tenant_id or 'unknown'}"
        findings = list(getattr(report, "findings", []) or [])
        severity = str(getattr(report, "worst_severity", "info"))
        risk_by_severity = {
            "info": "low",
            "warn": "medium",
            "error": "high",
            "critical": "critical",
        }
        status: TaskStatus = "done" if severity == "info" else "paused"
        top_findings = findings[:10]
        with self._lock:
            entry = self._entries.get(task_id)
            if entry is None:
                entry = StateLedgerEntry(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    task_type="system.nuo.health",
                    title="NUO 系统体检",
                    current_goal="诊断 KUN 的任务、事件、外部动作、能力边界和运行风险",
                    status=status,
                    execution_mode="SMART",
                )
                self._entries[task_id] = entry
            entry.status = status
            entry.current_risk = risk_by_severity.get(severity, "medium")
            entry.current_action = f"NUO 发现 {len(findings)} 个系统体检项"
            entry.decision_reason = f"NUO health worst_severity={severity}"
            entry.alert_flags = [
                str(getattr(finding, "finding_id", ""))
                for finding in top_findings
                if str(getattr(finding, "severity", "info")) in {"warn", "error", "critical"}
            ]
            entry.pending_reason = (
                "；".join(str(getattr(finding, "title", "")) for finding in top_findings[:3])
                if findings
                else ""
            )
            entry.add_trail(
                "nuo.health_report.generated",
                entry.decision_reason,
                {
                    "worst_severity": severity,
                    "findings": len(findings),
                    "outbox_lag": int(getattr(report, "outbox_lag", 0) or 0),
                    "pending_approvals": int(getattr(report, "pending_approvals", 0) or 0),
                    "top_findings": [
                        {
                            "finding_id": str(getattr(finding, "finding_id", "")),
                            "severity": str(getattr(finding, "severity", "")),
                            "subsystem": str(getattr(finding, "subsystem", "")),
                            "title": str(getattr(finding, "title", "")),
                            "suggested_action": str(getattr(finding, "suggested_action", "")),
                        }
                        for finding in top_findings
                    ],
                },
            )
            return self._stored_copy(entry)

    def record_finished(self, task_id: str, *, runtime: RuntimeState) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            _apply_runtime(entry, runtime)
            entry.finished_at = runtime.finished_at or datetime.now(UTC)
            entry.add_trail("task.finished", f"任务结束: {entry.status}")
            self._store_entry(entry)

    def snapshot(self, task_id: str, *, tenant_id: str | None = None) -> StateLedgerEntry | None:
        with self._lock:
            entry = self._entries.get(task_id)
            if entry is not None and (tenant_id is None or entry.tenant_id == tenant_id):
                return entry.model_copy(deep=True)
        if self._store is None or tenant_id is None:
            return None
        return self._store.get(task_id=task_id, tenant_id=tenant_id)

    def active_snapshots(self, *, tenant_id: str | None = None) -> list[StateLedgerEntry]:
        persisted: list[StateLedgerEntry] = []
        if self._store is not None and tenant_id is not None:
            persisted = self._store.list_active(tenant_id=tenant_id)
        with self._lock:
            by_key = {(entry.tenant_id, entry.task_id): entry for entry in persisted}
            for entry in self._entries.values():
                if entry.status in {"queued", "running", "paused"} and (
                    tenant_id is None or entry.tenant_id == tenant_id
                ):
                    by_key[(entry.tenant_id, entry.task_id)] = entry
            entries = list(by_key.values())
            entries.sort(key=lambda item: item.updated_at, reverse=True)
            return [entry.model_copy(deep=True) for entry in entries]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _ensure(self, task_id: str) -> StateLedgerEntry:
        entry = self._entries.get(task_id)
        if entry is not None:
            return entry
        entry = StateLedgerEntry(task_id=task_id, tenant_id="")
        entry.add_trail("state.created_placeholder", "状态账本收到未知任务更新")
        self._entries[task_id] = entry
        return entry

    def _stored_copy(self, entry: StateLedgerEntry) -> StateLedgerEntry:
        self._store_entry(entry)
        return entry.model_copy(deep=True)

    def _store_entry(self, entry: StateLedgerEntry) -> None:
        if self._store is None or not entry.tenant_id:
            return
        try:
            self._store.save(entry.model_copy(deep=True))
        except Exception:
            logger.exception("state_ledger.persist_failed", extra={"task_id": entry.task_id})


_default_ledger = StateLedger(store=StateLedgerDatabaseStore(enable_reads=True))


def get_state_ledger() -> StateLedger:
    return _default_ledger


def reset_state_ledger() -> None:
    _default_ledger.clear()


def replay_state_ledger_story(
    task_id: str,
    history: Sequence[Mapping[str, Any]],
    *,
    timeline_limit: int = 20,
    history_limit_reached: bool = False,
) -> dict[str, Any]:
    """Reconstruct a compact durable task story from append-only events.

    This is deliberately conservative. EventRow is the durable fact source, but
    older events do not always carry every field StateLedger wants.  Instead of
    pretending we rebuilt a perfect snapshot, this function returns the facts it
    can prove plus `gaps` and a confidence score.
    """

    timeline = sorted(
        [dict(item) for item in history],
        key=lambda item: str(item.get("occurred_at") or ""),
    )
    status = "unknown"
    current_action = ""
    latest_reason = ""
    decision_ids: list[str] = []
    decision_summary: dict[str, int] = {}
    decision_status_summary: dict[str, int] = {}
    model_routes: list[str] = []
    skill_refs: list[str] = []
    context_asset_ids: list[str] = []
    resource_credit_summaries: list[dict[str, Any]] = []
    top_credit_resource_kinds: list[str] = []
    critical_path_step_ids: list[int] = []
    credit_assignment_count = 0
    latest_credit_assignment_summary: dict[str, Any] | None = None
    pending_confirmations: list[str] = []
    risk_flags: list[str] = []
    open_questions: list[str] = []
    world_action_count = 0
    external_action_count = 0
    total_cost = 0.0
    saw_task_created = False
    saw_terminal = False
    saw_runtime_cost = False

    for item in timeline:
        event_type = str(item.get("event_type") or "")
        payload = _dict_or_empty(item.get("payload"))
        ticket = _story_decision_ticket(payload)
        reason = _first_non_empty(
            item.get("reason"),
            payload.get("reason"),
            payload.get("message"),
            ticket.get("reason"),
            item.get("summary"),
        )
        if reason:
            latest_reason = reason
        current_action = _event_action(event_type, payload, ticket, reason) or current_action

        cost = _story_event_cost(item, payload, ticket)
        if cost:
            saw_runtime_cost = True
        total_cost += cost

        status = _status_after_event(event_type, payload, ticket, status)
        saw_task_created = saw_task_created or event_type == "task.created"
        saw_terminal = saw_terminal or status in {"done", "failed", "cancelled"}

        ticket_id = _first_non_empty(item.get("decision_ticket_id"), ticket.get("ticket_id"))
        if ticket_id:
            _append_unique(decision_ids, ticket_id)
            point = str(ticket.get("decision_point") or "unknown")
            status_value = str(ticket.get("status") or "unknown")
            decision_summary[point] = decision_summary.get(point, 0) + 1
            decision_status_summary[status_value] = decision_status_summary.get(status_value, 0) + 1
            _apply_decision_ticket_replay(
                ticket,
                model_routes=model_routes,
                skill_refs=skill_refs,
                context_asset_ids=context_asset_ids,
                risk_flags=risk_flags,
            )

        if event_type == "task.pending_actions.created":
            actions = payload.get("actions")
            if isinstance(actions, list):
                for action in actions:
                    if isinstance(action, dict):
                        _append_unique(
                            pending_confirmations,
                            _first_non_empty(action.get("action_id"), action.get("action_type")),
                        )
            if pending_confirmations:
                status = "paused"
                _append_unique(open_questions, "等待外部动作审批")
        elif event_type in {
            "task.pending_action.executed",
            "task.pending_action.blocked",
            "task.pending_action.execution_failed",
        }:
            world_action_count += 1
            action_id = _first_non_empty(payload.get("action_id"), payload.get("action_type"))
            if action_id:
                _remove_value(pending_confirmations, action_id)
            action_type = _first_non_empty(payload.get("action_type"), action_id)
            if action_type:
                _remove_value(pending_confirmations, action_type)
            if bool(payload.get("external_dispatched")):
                external_action_count += 1
            if event_type != "task.pending_action.executed":
                status = "paused"
                _append_unique(risk_flags, event_type)
                _append_unique(open_questions, reason or "外部动作未完成")
        elif event_type == "task.resumed":
            pending_confirmations.clear()
            open_questions = [item for item in open_questions if item not in {"等待外部动作审批"}]
        elif event_type == "credit.assignment.completed":
            credit_assignment_count += 1
            resource_credit_summaries = _resource_credit_summaries(
                _list_of_dicts(payload.get("resource_kind_summaries"))
            )
            top_credit_resource_kinds = _top_credit_resource_kinds(resource_credit_summaries)
            critical_path_step_ids = _int_list(payload.get("critical_path_step_ids"))
            latest_credit_assignment_summary = {
                "task_outcome": _first_non_empty(payload.get("task_outcome")),
                "step_count": _safe_int(payload.get("step_count")),
                "critical_path_step_ids": list(critical_path_step_ids),
                "total_immediate_reward": _safe_float(payload.get("total_immediate_reward")),
                "resource_count": _safe_int(payload.get("resource_count")),
                "resource_kind_count": len(resource_credit_summaries),
                "top_resource_kinds": list(top_credit_resource_kinds),
            }
            if top_credit_resource_kinds:
                current_action = (
                    "完成信用归因：" + "、".join(top_credit_resource_kinds[:3]) + " 贡献最高"
                )

        if _event_is_risky(event_type, payload, ticket):
            _append_unique(risk_flags, event_type)

    gaps = _story_gaps(
        timeline=timeline,
        saw_task_created=saw_task_created,
        saw_terminal=saw_terminal,
        saw_runtime_cost=saw_runtime_cost,
        decision_count=len(decision_ids),
        history_limit_reached=history_limit_reached,
    )
    confidence = _story_confidence(
        event_count=len(timeline),
        saw_task_created=saw_task_created,
        saw_terminal=saw_terminal,
        decision_count=len(decision_ids),
        gaps=gaps,
    )
    latest = timeline[-1] if timeline else None
    return {
        "task_id": task_id,
        "event_count": len(timeline),
        "decision_count": len(decision_ids),
        "world_action_count": world_action_count,
        "external_action_count": external_action_count,
        "total_cost_usd": round(total_cost, 4),
        "first_seen_at": timeline[0].get("occurred_at") if timeline else None,
        "last_seen_at": latest.get("occurred_at") if latest else None,
        "latest_event_type": str(latest.get("event_type") or "") if latest else "",
        "latest_reason": latest_reason,
        "status": status,
        "current_action": current_action,
        "decision_summary": decision_summary,
        "decision_status_summary": decision_status_summary,
        "needs_review_decision_count": decision_status_summary.get("needs_review", 0),
        "blocked_decision_count": decision_status_summary.get("blocked", 0),
        "pending_confirmations": pending_confirmations,
        "risk_flags": risk_flags[-20:],
        "open_questions": [item for item in open_questions if item][-20:],
        "decision_ticket_ids": decision_ids[-50:],
        "model_routes": model_routes[-20:],
        "skill_refs": skill_refs[-30:],
        "context_asset_ids": context_asset_ids[-50:],
        "credit_assignment_count": credit_assignment_count,
        "credit_assignment_summary": latest_credit_assignment_summary,
        "resource_credit_summaries": resource_credit_summaries[-30:],
        "top_credit_resource_kinds": top_credit_resource_kinds[-20:],
        "critical_path_step_ids": critical_path_step_ids[-50:],
        "reconstruction_confidence": confidence,
        "gaps": gaps,
        "timeline": timeline[-timeline_limit:],
    }


def _apply_runtime(entry: StateLedgerEntry, runtime: RuntimeState) -> None:
    entry.status = runtime.status
    entry.current_step = runtime.current_step
    entry.total_steps = runtime.total_planned_steps
    entry.cost_so_far_usd = runtime.accumulated_cost_usd_equivalent
    entry.tokens_so_far = runtime.accumulated_tokens
    entry.finished_at = runtime.finished_at
    entry.updated_at = datetime.now(UTC)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, int):
            return value
        if isinstance(value, float | str):
            return int(value)
        return None
    except (TypeError, ValueError):
        return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def _float_dict(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for key, item in value.items():
        try:
            out[str(key)] = float(item)
        except (TypeError, ValueError):
            continue
    return out


def _safe_int(value: object) -> int:
    try:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float | str):
            return int(value)
    except (TypeError, ValueError):
        return 0
    return 0


def _safe_float(value: object) -> float:
    try:
        if isinstance(value, int | float | str):
            return float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0


def _int_list(value: object) -> list[int]:
    if not isinstance(value, list):
        return []
    out: list[int] = []
    for item in value:
        try:
            if isinstance(item, bool):
                continue
            if isinstance(item, int):
                out.append(item)
            elif isinstance(item, float | str):
                out.append(int(item))
        except (TypeError, ValueError):
            continue
    return out


def _list_of_dicts(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _resource_credit_summaries(value: object) -> list[dict[str, Any]]:
    summaries = _list_of_dicts(value)
    out: list[dict[str, Any]] = []
    for item in summaries:
        resource_kind = _first_non_empty(
            item.get("resource_kind"),
            item.get("kind"),
            item.get("resource_type"),
        )
        if not resource_kind:
            continue
        out.append(
            {
                "resource_kind": resource_kind,
                "total_delta": _safe_float(item.get("total_delta")),
                "mean_delta": _safe_float(item.get("mean_delta")),
                "positive_count": _safe_int(item.get("positive_count")),
                "negative_count": _safe_int(item.get("negative_count")),
                "resource_count": _safe_int(item.get("resource_count")),
            }
        )
    out.sort(key=lambda item: float(item.get("total_delta") or 0.0), reverse=True)
    return out[:30]


def _top_credit_resource_kinds(summaries: Sequence[Mapping[str, Any]]) -> list[str]:
    out: list[str] = []
    for item in summaries:
        if _safe_float(item.get("total_delta")) <= 0:
            continue
        _append_unique(out, item.get("resource_kind"))
    return out[:10]


def _dict_or_empty(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _first_non_empty(*values: object) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _append_unique(items: list[str], value: object) -> None:
    text = str(value).strip() if value is not None else ""
    if text and text not in items:
        items.append(text)


def _remove_value(items: list[str], value: object) -> None:
    text = str(value).strip() if value is not None else ""
    if not text:
        return
    items[:] = [item for item in items if item != text]


def _story_event_cost(
    item: Mapping[str, Any],
    payload: Mapping[str, Any],
    ticket: Mapping[str, Any],
) -> float:
    for source in (item, payload, ticket.get("metadata"), ticket.get("evidence")):
        if not isinstance(source, Mapping):
            continue
        for key in ("cost_delta_usd", "cost_usd", "cost_usd_actual"):
            try:
                value = source.get(key)
                if value is not None:
                    return round(float(value), 6)
            except (TypeError, ValueError):
                continue
    return 0.0


def _story_decision_ticket(payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = payload.get("decision_ticket")
    if isinstance(nested, dict):
        return dict(nested)
    if payload.get("ticket_id") and payload.get("decision_point"):
        return dict(payload)
    return {}


def _status_after_event(
    event_type: str,
    payload: Mapping[str, Any],
    ticket: Mapping[str, Any],
    current: str,
) -> str:
    if event_type == "task.created":
        return "queued"
    if event_type in {"task.started", "mission.task.orchestrator_started"}:
        return "running"
    if event_type in {
        "task.paused",
        "task.paused.preflight",
        "task.pending_actions.created",
        "delivery.needs_review",
    }:
        return "paused"
    if event_type in {"task.resumed", "mission.task.resume_requested"}:
        return "queued"
    if event_type in {"task.done", "mission.task.resume_completed"}:
        payload_status = _first_non_empty(payload.get("status"), payload.get("final_status"))
        return payload_status or "done"
    if event_type in {
        "task.failed",
        "task.timed_out",
        "task.budget_exceeded",
        "mission.task.resume_failed",
    }:
        return "failed"
    if event_type == "task.cancelled":
        return "cancelled"
    ticket_status = str(ticket.get("status") or "")
    if ticket_status in {"blocked", "needs_review", "escalated", "failed"}:
        return "paused" if ticket_status != "failed" else "failed"
    return current


def _event_action(
    event_type: str,
    payload: Mapping[str, Any],
    ticket: Mapping[str, Any],
    reason: str,
) -> str:
    if ticket:
        point = _first_non_empty(ticket.get("decision_point"), ticket.get("phase"))
        selected = _first_non_empty(ticket.get("selected_action"))
        if point or selected:
            return f"{point}: {selected}".strip(": ")
    if event_type == "task.step.completed":
        step_id = _first_non_empty(payload.get("step_id"))
        return f"完成第 {step_id} 步" if step_id else "完成一个执行步骤"
    if event_type.startswith("task.pending_action"):
        action_type = _first_non_empty(payload.get("action_type"), payload.get("action_id"))
        return f"外部动作 {action_type}: {event_type}" if action_type else event_type
    return reason or event_type


def _apply_decision_ticket_replay(
    ticket: Mapping[str, Any],
    *,
    model_routes: list[str],
    skill_refs: list[str],
    context_asset_ids: list[str],
    risk_flags: list[str],
) -> None:
    point = str(ticket.get("decision_point") or "")
    selected = _first_non_empty(ticket.get("selected_action"))
    metadata = _dict_or_empty(ticket.get("metadata"))
    evidence = _dict_or_empty(ticket.get("evidence"))
    if point == "llm_model_selected":
        provider = _first_non_empty(metadata.get("provider"), evidence.get("provider"))
        model = _first_non_empty(metadata.get("model"), evidence.get("model"))
        route = selected or ":".join(part for part in (provider, model) if part)
        _append_unique(model_routes, route)
    elif point == "skill_selected":
        skill_ids = metadata.get("skill_ids")
        if isinstance(skill_ids, list):
            for skill_id in skill_ids:
                _append_unique(skill_refs, skill_id)
        elif selected and selected != "none":
            for skill_id in selected.split(","):
                _append_unique(skill_refs, skill_id)
    elif point == "context_selected":
        asset_ids = metadata.get("asset_ids")
        if isinstance(asset_ids, list):
            for asset_id in asset_ids:
                _append_unique(context_asset_ids, asset_id)
        elif selected and selected != "none":
            for asset_id in selected.split(","):
                _append_unique(context_asset_ids, asset_id)
    if str(ticket.get("status") or "") in {"blocked", "failed", "needs_review", "escalated"}:
        _append_unique(risk_flags, f"decision.{point}.{ticket.get('status')}")


def _event_is_risky(
    event_type: str,
    payload: Mapping[str, Any],
    ticket: Mapping[str, Any],
) -> bool:
    risky_terms = (
        "failed",
        "timed_out",
        "budget_exceeded",
        "blocked",
        "pre_conflict",
        "gaming.detected",
        "security.",
        "redteam",
        "needs_review",
    )
    if any(term in event_type for term in risky_terms):
        return True
    if str(payload.get("risk_level") or "") in {"high", "critical"}:
        return True
    return str(ticket.get("risk_level") or "") in {"high", "critical"}


def _story_gaps(
    *,
    timeline: list[dict[str, Any]],
    saw_task_created: bool,
    saw_terminal: bool,
    saw_runtime_cost: bool,
    decision_count: int,
    history_limit_reached: bool,
) -> list[str]:
    gaps: list[str] = []
    if not timeline:
        return ["no_events"]
    if history_limit_reached:
        gaps.append("history_may_be_truncated")
    if not saw_task_created:
        gaps.append("missing_task_created_event")
    if not saw_terminal:
        gaps.append("missing_terminal_status_event")
    if decision_count == 0:
        gaps.append("missing_decision_ticket_events")
    if not saw_runtime_cost:
        gaps.append("missing_cost_fields")
    return gaps


def _story_confidence(
    *,
    event_count: int,
    saw_task_created: bool,
    saw_terminal: bool,
    decision_count: int,
    gaps: list[str],
) -> float:
    if event_count == 0:
        return 0.0
    score = 0.45
    if saw_task_created:
        score += 0.15
    if saw_terminal:
        score += 0.15
    if decision_count:
        score += 0.15
    score += min(event_count, 10) * 0.01
    score -= min(len(gaps), 5) * 0.04
    return round(max(0.1, min(score, 0.95)), 2)
