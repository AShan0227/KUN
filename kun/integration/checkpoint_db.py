"""DB-backed callbacks for TaskCheckpointService (LT.C wiring).

`kun.agents.executor.checkpoint.TaskCheckpointService` is wired with three
callbacks (writer / reader / status_marker). The service itself stays free of
SQLAlchemy imports — this module supplies the real DB implementations that
sit on top of `kun.core.db.session_scope` + `kun.core.orm.TaskCheckpointRow`.

Each factory returns an async callable matching the type alias declared by
the service:
  - CheckpointWriter         · insert one row payload
  - CheckpointReader         · latest active checkpoint for a (tenant, task)
  - CheckpointStatusMarker   · transition a checkpoint to a new status

Style mirrors `kun.agents.gate.capability_writeback`: explicit session_scope
use, single retry on a unique-key IntegrityError race, otherwise re-raise so
the checkpoint state machine can surface the failure (ADR-024
frozen_dataclass_agent_io_contract — status-machine writes must not be
silently swallowed).
"""

from __future__ import annotations

from typing import Any, get_args

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from kun.agents.executor.checkpoint import (
    CheckpointReader,
    CheckpointStatus,
    CheckpointStatusMarker,
    CheckpointWriter,
    TaskCheckpoint,
)
from kun.core.db import session_scope
from kun.core.logging import get_logger
from kun.core.orm import TaskCheckpointRow

log = get_logger("kun.integration.checkpoint_db")


_ALLOWED_STATUSES: frozenset[str] = frozenset(get_args(CheckpointStatus))


def make_checkpoint_writer() -> CheckpointWriter:
    """Factory: insert a row built from `TaskCheckpoint.to_row_payload(tenant)`.

    Retries IntegrityError exactly once — the only realistic race is a caller
    that re-issues `save()` after a transient transport hiccup, hitting the
    (tenant_id, checkpoint_id) primary key. Any other IntegrityError (NOT NULL
    violation, malformed JSON payload, etc.) is re-raised on the first hit;
    retrying corrupt data is futile.
    """

    async def _write(row_payload: dict[str, Any]) -> None:
        tenant_id = row_payload["tenant_id"]
        checkpoint_id = row_payload["checkpoint_id"]
        for attempt in range(2):
            try:
                async with session_scope(tenant_id=tenant_id) as s:
                    s.add(TaskCheckpointRow(**row_payload))
                    await s.flush()
                return
            except IntegrityError as e:
                constraint = getattr(e.orig, "diag", None)
                constraint_name = (
                    getattr(constraint, "constraint_name", "") if constraint else ""
                )
                is_duplicate = (
                    "unique" in str(e.orig).lower()
                    or "duplicate key" in str(e.orig).lower()
                    or constraint_name.startswith("pk_")
                )
                if not is_duplicate:
                    log.error(
                        "task_checkpoint.writer.integrity_error",
                        tenant_id=tenant_id,
                        checkpoint_id=checkpoint_id,
                        constraint=constraint_name,
                        detail=str(e.orig),
                    )
                    raise
                if attempt >= 1:
                    log.error(
                        "task_checkpoint.writer.retry_exhausted",
                        tenant_id=tenant_id,
                        checkpoint_id=checkpoint_id,
                    )
                    raise
                log.warning(
                    "task_checkpoint.writer.duplicate_retry",
                    tenant_id=tenant_id,
                    checkpoint_id=checkpoint_id,
                )

    return _write


def make_checkpoint_reader() -> CheckpointReader:
    """Factory: return the latest active checkpoint for (tenant_id, task_id).

    SQL semantics: `WHERE tenant_id=? AND task_id=? AND status='active'
    ORDER BY sequence DESC LIMIT 1`. `None` when no matching row exists.
    """

    async def _read(tenant_id: str, task_id: str) -> TaskCheckpoint | None:
        async with session_scope(tenant_id=tenant_id) as s:
            stmt = (
                select(TaskCheckpointRow)
                .where(
                    TaskCheckpointRow.tenant_id == tenant_id,
                    TaskCheckpointRow.task_id == task_id,
                    TaskCheckpointRow.status == "active",
                )
                .order_by(TaskCheckpointRow.sequence.desc())
                .limit(1)
            )
            row = (await s.execute(stmt)).scalar_one_or_none()
            if row is None:
                return None
            return _row_to_checkpoint(row)

    return _read


def make_checkpoint_status_marker() -> CheckpointStatusMarker:
    """Factory: transition a checkpoint row to `new_status`.

    `new_status` must be one of the allowed CheckpointStatus values
    (`'active'`, `'final'`, `'failed_resume'`); anything else raises
    ValueError before touching the DB.
    """

    async def _mark(
        tenant_id: str,
        checkpoint_id: str,
        new_status: CheckpointStatus,
    ) -> None:
        if new_status not in _ALLOWED_STATUSES:
            raise ValueError(
                f"invalid checkpoint status {new_status!r}; "
                f"allowed: {sorted(_ALLOWED_STATUSES)}"
            )
        async with session_scope(tenant_id=tenant_id) as s:
            stmt = (
                update(TaskCheckpointRow)
                .where(
                    TaskCheckpointRow.tenant_id == tenant_id,
                    TaskCheckpointRow.checkpoint_id == checkpoint_id,
                )
                .values(status=new_status)
            )
            await s.execute(stmt)
        log.info(
            "task_checkpoint.status_marked",
            tenant_id=tenant_id,
            checkpoint_id=checkpoint_id,
            new_status=new_status,
        )

    return _mark


def _row_to_checkpoint(row: TaskCheckpointRow) -> TaskCheckpoint:
    """Rebuild a frozen TaskCheckpoint from an ORM row.

    Preserves `created_at` from the DB (override the dataclass default).
    JSONB columns are copied into fresh containers so callers can mutate
    safely without poisoning the session-managed row.
    """
    return TaskCheckpoint(
        checkpoint_id=row.checkpoint_id,
        task_id=row.task_id,
        step_idx=row.step_idx,
        sequence=row.sequence,
        conversation_snapshot=list(row.conversation_snapshot or []),
        working_state=dict(row.working_state or {}),
        artifact_refs=list(row.artifact_refs or []),
        goal_anchor_id=row.goal_anchor_id,
        last_self_report=(
            dict(row.last_self_report)
            if row.last_self_report is not None
            else None
        ),
        cost_usd_so_far=float(row.cost_usd_so_far),
        tokens_used_so_far=int(row.tokens_used_so_far),
        status=row.status,  # type: ignore[arg-type]
        rationale=row.rationale,
        created_at=row.created_at,
    )


__all__ = [
    "make_checkpoint_reader",
    "make_checkpoint_status_marker",
    "make_checkpoint_writer",
]
