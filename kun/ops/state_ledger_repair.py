"""State Ledger repair helpers.

StateLedgerEntryRow is the fast "current view" cache.  EventRow is the durable
history.  When NUO detects drift between them, operators need a safe way to
rebuild the current view from the durable event trail instead of only seeing an
alert.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from kun.core.db import session_scope
from kun.core.orm import EventRow, StateLedgerEntryRow, TaskRow
from kun.core.state_ledger import StateLedgerEntry, replay_state_ledger_story


class StateLedgerRepairDiff(BaseModel):
    """Human-readable diff between current snapshot and event replay."""

    model_config = ConfigDict(extra="forbid")

    field: str
    current: Any = None
    replayed: Any = None


class StateLedgerRepairResult(BaseModel):
    """Result of a State Ledger repair dry-run/apply operation."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    task_id: str
    applied: bool = False
    reason: str = ""
    event_count: int = 0
    reconstruction_confidence: float = 0.0
    gaps: list[str] = Field(default_factory=list)
    diffs: list[StateLedgerRepairDiff] = Field(default_factory=list)
    repaired_snapshot: dict[str, Any] = Field(default_factory=dict)


async def repair_state_ledger_snapshot(
    *,
    tenant_id: str,
    task_id: str,
    user_id: str | None = None,
    apply: bool = False,
    limit: int = 300,
) -> StateLedgerRepairResult:
    """Rebuild one current State Ledger snapshot from EventRow.

    `apply=False` is a dry-run.  It returns the reconstructed snapshot and the
    fields that differ from the existing current cache.  `apply=True` writes the
    reconstructed snapshot back into `state_ledger_entries`.
    """

    async with session_scope(tenant_id=tenant_id) as session:
        task = (
            await session.execute(
                select(TaskRow).where(TaskRow.tenant_id == tenant_id, TaskRow.task_id == task_id)
            )
        ).scalar_one_or_none()
        if task is not None and user_id and user_id != "u-anon" and task.user_id != user_id:
            return StateLedgerRepairResult(
                tenant_id=tenant_id,
                task_id=task_id,
                reason="task_user_mismatch",
            )

        event_rows = list(
            (
                await session.execute(
                    select(EventRow)
                    .where(EventRow.tenant_id == tenant_id, EventRow.task_ref == task_id)
                    .order_by(desc(EventRow.occurred_at))
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        if not event_rows:
            return StateLedgerRepairResult(
                tenant_id=tenant_id,
                task_id=task_id,
                reason="missing_durable_history",
            )
        event_rows = list(reversed(event_rows))
        history = [_history_item_from_event(row) for row in event_rows]
        story = replay_state_ledger_story(
            task_id,
            history,
            timeline_limit=20,
            history_limit_reached=len(event_rows) >= limit,
        )
        repaired = build_repaired_state_ledger_entry(
            tenant_id=tenant_id,
            task_id=task_id,
            user_id=user_id,
            task=task,
            story=story,
        )

        existing = (
            await session.execute(
                select(StateLedgerEntryRow).where(
                    StateLedgerEntryRow.tenant_id == tenant_id,
                    StateLedgerEntryRow.task_id == task_id,
                )
            )
        ).scalar_one_or_none()
        current = dict(existing.snapshot_json or {}) if existing is not None else {}
        diffs = diff_state_ledger_snapshots(current=current, repaired=repaired)

        if apply:
            stmt = pg_insert(StateLedgerEntryRow).values(
                tenant_id=repaired.tenant_id,
                task_id=repaired.task_id,
                user_id=repaired.user_id,
                project_id=repaired.project_id,
                status=repaired.status,
                snapshot_json=repaired.model_dump(mode="json"),
                created_at=repaired.started_at,
                updated_at=repaired.updated_at,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=[StateLedgerEntryRow.tenant_id, StateLedgerEntryRow.task_id],
                set_={
                    "user_id": stmt.excluded.user_id,
                    "project_id": stmt.excluded.project_id,
                    "status": stmt.excluded.status,
                    "snapshot_json": stmt.excluded.snapshot_json,
                    "updated_at": stmt.excluded.updated_at,
                },
            )
            await session.execute(stmt)

    return StateLedgerRepairResult(
        tenant_id=tenant_id,
        task_id=task_id,
        applied=apply,
        reason="applied" if apply else "dry_run",
        event_count=int(story.get("event_count") or 0),
        reconstruction_confidence=float(story.get("reconstruction_confidence") or 0.0),
        gaps=[str(item) for item in story.get("gaps", []) if item],
        diffs=diffs,
        repaired_snapshot=repaired.model_dump(mode="json"),
    )


def build_repaired_state_ledger_entry(
    *,
    tenant_id: str,
    task_id: str,
    user_id: str | None,
    task: Any,
    story: Mapping[str, Any],
) -> StateLedgerEntry:
    """Build a StateLedgerEntry from replayed story + optional TaskRow."""

    status = _valid_status(str(story.get("status") or "queued"))
    first_seen = _parse_dt(story.get("first_seen_at")) or datetime.now(UTC)
    last_seen = _parse_dt(story.get("last_seen_at")) or first_seen
    task_user_id = _optional_str(getattr(task, "user_id", None)) if task is not None else user_id
    title = _optional_str(getattr(task, "success_criteria_short", None)) or str(
        story.get("latest_reason") or task_id
    )
    return StateLedgerEntry(
        task_id=task_id,
        tenant_id=tenant_id,
        user_id=task_user_id,
        project_id=_optional_str(getattr(task, "project_id", None)) if task is not None else None,
        task_type=_optional_str(getattr(task, "task_type", None)) or "",
        title=title,
        current_goal=_goal_from_task(task) or title,
        status=status,
        current_action=str(story.get("current_action") or ""),
        current_risk=_optional_str(getattr(task, "risk_level", None)) or "low",
        complexity_score=float(getattr(task, "complexity_score", 0.0) or 0.0),
        budget_estimated_usd=float(getattr(task, "estimated_cost_usd", 0.0) or 0.0),
        cost_so_far_usd=float(story.get("total_cost_usd") or 0.0),
        pending_confirmations=[
            str(item) for item in story.get("pending_confirmations", []) if item
        ],
        pending_reason=str(story.get("latest_reason") or ""),
        alert_flags=[str(item) for item in story.get("risk_flags", []) if item],
        decision_ticket_ids=[str(item) for item in story.get("decision_ticket_ids", []) if item],
        context_asset_ids=[str(item) for item in story.get("context_asset_ids", []) if item],
        skill_hints=[str(item) for item in story.get("skill_refs", []) if item],
        started_at=first_seen,
        updated_at=last_seen,
        finished_at=last_seen if status in {"done", "failed", "cancelled"} else None,
    )


def diff_state_ledger_snapshots(
    *,
    current: Mapping[str, Any],
    repaired: StateLedgerEntry,
) -> list[StateLedgerRepairDiff]:
    """Diff only the fields that affect user/LLM operational truth."""

    repaired_data = repaired.model_dump(mode="json")
    fields = (
        "status",
        "current_action",
        "pending_reason",
        "cost_so_far_usd",
        "pending_confirmations",
        "alert_flags",
        "decision_ticket_ids",
        "context_asset_ids",
        "skill_hints",
    )
    diffs: list[StateLedgerRepairDiff] = []
    for field in fields:
        current_value = current.get(field)
        repaired_value = repaired_data.get(field)
        if _normalize(current_value) != _normalize(repaired_value):
            diffs.append(
                StateLedgerRepairDiff(
                    field=field,
                    current=current_value,
                    replayed=repaired_value,
                )
            )
    return diffs


def _history_item_from_event(row: EventRow) -> dict[str, Any]:
    payload = row.payload if isinstance(row.payload, dict) else {}
    ticket = _decision_ticket_payload(payload)
    return {
        "event_id": row.event_id,
        "event_type": row.event_type,
        "occurred_at": row.occurred_at.isoformat(),
        "task_id": row.task_ref,
        "summary": row.subject[:200],
        "reason": _event_reason(payload, ticket),
        "cost_usd": _event_cost(payload, ticket),
        "decision_ticket_id": _optional_str(ticket.get("ticket_id")),
        "decision_point": str(ticket.get("decision_point") or ""),
        "phase": str(ticket.get("phase") or ""),
        "selected_action": str(ticket.get("selected_action") or ""),
        "decision_status": str(ticket.get("status") or ""),
        "payload": payload,
    }


def _decision_ticket_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    nested = payload.get("decision_ticket")
    if isinstance(nested, dict):
        return dict(nested)
    if payload.get("ticket_id") and payload.get("decision_point"):
        return dict(payload)
    return {}


def _event_reason(payload: Mapping[str, Any], ticket: Mapping[str, Any]) -> str:
    for value in (
        ticket.get("reason"),
        payload.get("reason"),
        payload.get("message"),
        payload.get("reason_summary"),
        payload.get("error"),
    ):
        if value is not None and str(value).strip():
            return str(value)
    return ""


def _event_cost(payload: Mapping[str, Any], ticket: Mapping[str, Any]) -> float:
    for source in (payload, ticket, ticket.get("metadata"), ticket.get("evidence")):
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


def _goal_from_task(task: Any) -> str:
    if task is None:
        return ""
    spec = getattr(task, "spec_json", None)
    if isinstance(spec, dict):
        for key in ("goal_detail", "goal", "objective"):
            value = spec.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return _optional_str(getattr(task, "success_criteria_short", None)) or ""


def _valid_status(value: str) -> str:
    return (
        value
        if value in {"queued", "running", "paused", "done", "failed", "cancelled"}
        else "queued"
    )


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _normalize(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, list):
        return [str(item) for item in value]
    return value


__all__ = [
    "StateLedgerRepairDiff",
    "StateLedgerRepairResult",
    "build_repaired_state_ledger_entry",
    "diff_state_ledger_snapshots",
    "repair_state_ledger_snapshot",
]
