"""State Ledger — current task truth for humans and LLMs.

V3-2 first cut:
- RuntimeState remains the durable per-task DB snapshot.
- EventRow remains the append-only audit log.
- StateLedger is the hot current-state view used by orchestrator/runtime/blackboard.

This module is intentionally lightweight and in-memory. Durable history is
served by blackboard_data_sources from EventRow; this hot ledger only answers
"what KUN is doing right now, why, and where it is".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kun.datamodel.decision_ticket import DecisionTicket
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
            elif ticket.decision_point == "context_selected":
                asset_ids = ticket.metadata.get("asset_ids")
                if isinstance(asset_ids, list):
                    entry.context_asset_ids = [str(asset_id) for asset_id in asset_ids]
                entry.decision_reason = ticket.reason or entry.decision_reason
            elif ticket.decision_point == "skill_selected":
                skill_ids = ticket.metadata.get("skill_ids")
                if isinstance(skill_ids, list):
                    entry.skill_hints = [str(skill_id) for skill_id in skill_ids]
                entry.decision_reason = ticket.reason or entry.decision_reason
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

    def record_resumed(self, task_id: str, *, reason: str) -> None:
        """Reflect a durable runtime resume in the hot ledger view."""
        with self._lock:
            entry = self._ensure(task_id)
            entry.status = "queued"
            entry.pending_reason = ""
            entry.pending_confirmations = []
            entry.add_trail("task.resumed", reason)

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
            return entry.model_copy(deep=True)

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
    model_routes: list[str] = []
    skill_refs: list[str] = []
    context_asset_ids: list[str] = []
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
        "pending_confirmations": pending_confirmations,
        "risk_flags": risk_flags[-20:],
        "open_questions": [item for item in open_questions if item][-20:],
        "decision_ticket_ids": decision_ids[-50:],
        "model_routes": model_routes[-20:],
        "skill_refs": skill_refs[-30:],
        "context_asset_ids": context_asset_ids[-50:],
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
