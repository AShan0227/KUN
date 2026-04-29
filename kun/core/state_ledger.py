"""State Ledger — current task truth for humans and LLMs.

V3-2 first cut:
- RuntimeState remains the durable per-task DB snapshot.
- EventRow remains the append-only audit log.
- StateLedger is the hot current-state view used by orchestrator/runtime/blackboard.

This module is intentionally lightweight and in-memory. It is not pretending to
be durable storage yet; it gives the running system one shared, queryable view of
"what KUN is doing, why, and where it is".
"""

from __future__ import annotations

from datetime import UTC, datetime
from threading import RLock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kun.datamodel.runtime import RuntimeState, StepRecord, TaskStatus
from kun.datamodel.task import TaskRef


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
    risk_watch: list[str] = Field(default_factory=list)
    alert_flags: list[str] = Field(default_factory=list)
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


class StateLedger:
    """In-memory current-state ledger.

    The ledger is sync by design: callers update it on the hot path without
    awaiting I/O. Durable persistence still belongs to RuntimeStateRow/EventRow.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[str, StateLedgerEntry] = {}

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
            return entry.model_copy(deep=True)

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

    def record_plan(self, task_id: str, *, total_steps: int) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            entry.total_steps = max(0, total_steps)
            entry.add_trail("task.plan", f"执行计划共 {entry.total_steps} 步")

    def record_running(self, task_id: str, *, runtime: RuntimeState) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            _apply_runtime(entry, runtime)
            entry.add_trail("task.started", "任务开始执行")

    def record_context(self, task_id: str, *, asset_ids: list[str]) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            entry.context_asset_ids = list(asset_ids)
            entry.add_trail("context.preheated", "上下文已预热", {"asset_ids": asset_ids})

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
    ) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            entry.pending_confirmations = [
                item for item in entry.pending_confirmations if item != action_id
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
                },
            )

    def record_finished(self, task_id: str, *, runtime: RuntimeState) -> None:
        with self._lock:
            entry = self._ensure(task_id)
            _apply_runtime(entry, runtime)
            entry.finished_at = runtime.finished_at or datetime.now(UTC)
            entry.add_trail("task.finished", f"任务结束: {entry.status}")

    def snapshot(self, task_id: str) -> StateLedgerEntry | None:
        with self._lock:
            entry = self._entries.get(task_id)
            return entry.model_copy(deep=True) if entry is not None else None

    def active_snapshots(self, *, tenant_id: str | None = None) -> list[StateLedgerEntry]:
        with self._lock:
            entries = [
                entry
                for entry in self._entries.values()
                if entry.status in {"queued", "running", "paused"}
                and (tenant_id is None or entry.tenant_id == tenant_id)
            ]
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


_default_ledger = StateLedger()


def get_state_ledger() -> StateLedger:
    return _default_ledger


def reset_state_ledger() -> None:
    _default_ledger.clear()


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
