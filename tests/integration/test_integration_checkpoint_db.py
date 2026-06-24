"""Integration tests for `kun.integration.checkpoint_db`.

Strategy: full Postgres would be heavy for callbacks this small, so we
monkeypatch `session_scope` in the module under test with an in-memory fake
that mirrors the SQLAlchemy 2.0 surface area we actually exercise:
  - `session.add(row)` for insert
  - `session.flush()` no-op (can be wired to raise IntegrityError per-test)
  - `session.execute(stmt)` for the limited `select(...)` and `update(...)`
    statements this module issues — we read the statement's `.whereclause`
    / `.column_descriptions` / `_values` and apply against an in-memory list.

The fake is intentionally minimal — just enough to round-trip writes,
selects, and updates that exercise the three factories. Each test creates a
fresh store + fake session so state is isolated.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from kun.agents.executor.checkpoint import TaskCheckpoint
from kun.core.orm import TaskCheckpointRow
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql import Select, Update

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Fake AsyncSession + session_scope
# ---------------------------------------------------------------------------


@dataclass
class _FakeStore:
    """In-memory storage shared across the fake scope re-entries.

    A list-of-rows model mimics task_checkpoints; per-test we hand a fresh
    instance to `_install_fake_scope`. Tests assert directly against `.rows`.
    """

    rows: list[TaskCheckpointRow]
    # set per-test to make flush() raise N times, then succeed
    flush_raises_remaining: int = 0
    flush_error: Exception | None = None


class _FakeSession:
    def __init__(self, store: _FakeStore) -> None:
        self._store = store
        self._pending_adds: list[TaskCheckpointRow] = []

    def add(self, row: TaskCheckpointRow) -> None:
        self._pending_adds.append(row)

    async def flush(self) -> None:
        if self._store.flush_raises_remaining > 0:
            self._store.flush_raises_remaining -= 1
            # drop any pending adds — emulate a transaction abort
            self._pending_adds.clear()
            assert self._store.flush_error is not None
            raise self._store.flush_error
        # commit pending adds into the store
        self._store.rows.extend(self._pending_adds)
        self._pending_adds.clear()

    async def execute(self, stmt: Any) -> Any:
        if isinstance(stmt, Select):
            return _FakeSelectResult(self._apply_select(stmt))
        if isinstance(stmt, Update):
            return _FakeUpdateResult(self._apply_update(stmt))
        raise NotImplementedError(f"fake session does not handle {type(stmt)!r}")

    # ---- internals ----

    def _apply_select(self, stmt: Select) -> list[TaskCheckpointRow]:
        """Mimic the only select we use: filter + order_by sequence desc + limit.

        We compile the where-clause to literal SQL and parse out the
        constraints we know we set — simpler is to just evaluate the
        SQLAlchemy ClauseElement against each row's attributes. We do the
        latter via a tiny visitor over BinaryExpression nodes.
        """
        from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList

        def matches(row: TaskCheckpointRow, clause: Any) -> bool:
            if clause is None:
                return True
            if isinstance(clause, BooleanClauseList):
                return all(matches(row, c) for c in clause.clauses)
            if isinstance(clause, BinaryExpression):
                col = clause.left.name  # type: ignore[attr-defined]
                val = clause.right.value  # type: ignore[attr-defined]
                return getattr(row, col) == val
            raise NotImplementedError(f"fake select: unhandled clause {clause!r}")

        filtered = [r for r in self._store.rows if matches(r, stmt.whereclause)]
        # honor order_by sequence desc (the only ordering we use)
        order_clauses = stmt._order_by_clauses  # type: ignore[attr-defined]
        if order_clauses:
            # all current callers order by sequence desc
            filtered.sort(key=lambda r: r.sequence, reverse=True)
        # honor limit
        limit = stmt._limit_clause  # type: ignore[attr-defined]
        if limit is not None:
            n = int(limit.value)  # type: ignore[attr-defined]
            filtered = filtered[:n]
        return filtered

    def _apply_update(self, stmt: Update) -> int:
        from sqlalchemy.sql.elements import BinaryExpression, BooleanClauseList

        def matches(row: TaskCheckpointRow, clause: Any) -> bool:
            if clause is None:
                return True
            if isinstance(clause, BooleanClauseList):
                return all(matches(row, c) for c in clause.clauses)
            if isinstance(clause, BinaryExpression):
                col = clause.left.name  # type: ignore[attr-defined]
                val = clause.right.value  # type: ignore[attr-defined]
                return getattr(row, col) == val
            raise NotImplementedError(f"fake update: unhandled clause {clause!r}")

        # extract .values(status=...)
        values = dict(stmt._values)  # type: ignore[attr-defined]
        # column key may be a Column or a string in 2.0; normalize
        norm: dict[str, Any] = {}
        for k, v in values.items():
            key = k.name if hasattr(k, "name") else k
            val = v.value if hasattr(v, "value") else v
            norm[str(key)] = val

        count = 0
        for row in self._store.rows:
            if matches(row, stmt.whereclause):
                for k, v in norm.items():
                    setattr(row, k, v)
                count += 1
        return count


class _FakeSelectResult:
    def __init__(self, rows: list[TaskCheckpointRow]) -> None:
        self._rows = rows

    def scalar_one_or_none(self) -> TaskCheckpointRow | None:
        if not self._rows:
            return None
        return self._rows[0]


class _FakeUpdateResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


def _install_fake_scope(
    monkeypatch: pytest.MonkeyPatch,
    store: _FakeStore,
) -> list[str | None]:
    """Patch `session_scope` in the module under test and return a list
    that receives every tenant_id observed (per-call). Useful for assertions.
    """
    seen: list[str | None] = []

    @asynccontextmanager
    async def fake_scope(
        *, tenant_id: str | None = None, bypass_rls: bool = False
    ) -> AsyncIterator[_FakeSession]:
        seen.append(tenant_id)
        yield _FakeSession(store)

    monkeypatch.setattr(
        "kun.integration.checkpoint_db.session_scope",
        fake_scope,
    )
    return seen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_payload(
    *,
    tenant_id: str = "t-1",
    checkpoint_id: str = "cp-1",
    task_id: str = "task-1",
    step_idx: int = 0,
    sequence: int = 0,
    status: str = "active",
    goal_anchor_id: str | None = "anchor-1",
    last_self_report: dict[str, Any] | None = None,
    cost_usd_so_far: float = 0.0,
) -> dict[str, Any]:
    cp = TaskCheckpoint(
        checkpoint_id=checkpoint_id,
        task_id=task_id,
        step_idx=step_idx,
        sequence=sequence,
        conversation_snapshot=[{"role": "user", "content": "hi"}],
        working_state={"tools_used": ["search"]},
        artifact_refs=["minio://k/v"],
        goal_anchor_id=goal_anchor_id,
        last_self_report=last_self_report,
        cost_usd_so_far=cost_usd_so_far,
        tokens_used_so_far=42,
        status=status,  # type: ignore[arg-type]
        rationale="step done",
        created_at=datetime.now(UTC),
    )
    return cp.to_row_payload(tenant_id)


def _fake_integrity_error(detail: str) -> IntegrityError:
    """Build an IntegrityError whose `.orig` stringifies to `detail`.

    Our retry-classifier inspects `str(e.orig).lower()`; that's all we need.
    """

    class _Orig:
        def __init__(self, msg: str) -> None:
            self._msg = msg
            self.diag = None

        def __str__(self) -> str:
            return self._msg

    return IntegrityError("INSERT ...", params=None, orig=_Orig(detail))


# ---------------------------------------------------------------------------
# Tests — writer
# ---------------------------------------------------------------------------


async def test_writer_happy_path_inserts_row(monkeypatch: pytest.MonkeyPatch) -> None:
    from kun.integration.checkpoint_db import make_checkpoint_writer

    store = _FakeStore(rows=[])
    seen = _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    payload = _make_payload(tenant_id="tenant-A", checkpoint_id="cp-alpha")
    await writer(payload)

    assert len(store.rows) == 1
    assert store.rows[0].tenant_id == "tenant-A"
    assert store.rows[0].checkpoint_id == "cp-alpha"
    assert seen == ["tenant-A"]


async def test_writer_retries_once_on_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First flush trips a unique-key error; second flush succeeds → 1 row."""
    from kun.integration.checkpoint_db import make_checkpoint_writer

    store = _FakeStore(
        rows=[],
        flush_raises_remaining=1,
        flush_error=_fake_integrity_error("duplicate key value violates unique"),
    )
    seen = _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    await writer(_make_payload(tenant_id="t-2"))

    assert len(store.rows) == 1
    # session_scope was entered twice (one failed flush + one retry)
    assert seen == ["t-2", "t-2"]


async def test_writer_reraises_on_persistent_integrity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kun.integration.checkpoint_db import make_checkpoint_writer

    store = _FakeStore(
        rows=[],
        flush_raises_remaining=99,
        flush_error=_fake_integrity_error("duplicate key value violates unique"),
    )
    seen = _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    with pytest.raises(IntegrityError):
        await writer(_make_payload())

    assert store.rows == []
    # Exactly 2 attempts: initial + one retry.
    assert len(seen) == 2


async def test_writer_does_not_retry_on_non_unique_integrity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-uniqueness IntegrityError (e.g. NOT NULL) re-raises immediately."""
    from kun.integration.checkpoint_db import make_checkpoint_writer

    store = _FakeStore(
        rows=[],
        flush_raises_remaining=99,
        flush_error=_fake_integrity_error("null value in column violates not-null"),
    )
    seen = _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    with pytest.raises(IntegrityError):
        await writer(_make_payload())

    assert store.rows == []
    # No retry for a non-duplicate IntegrityError.
    assert len(seen) == 1


# ---------------------------------------------------------------------------
# Tests — reader
# ---------------------------------------------------------------------------


async def test_reader_returns_none_when_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kun.integration.checkpoint_db import make_checkpoint_reader

    store = _FakeStore(rows=[])
    _install_fake_scope(monkeypatch, store)

    reader = make_checkpoint_reader()
    assert await reader("t-empty", "task-x") is None


async def test_reader_returns_latest_by_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kun.integration.checkpoint_db import (
        make_checkpoint_reader,
        make_checkpoint_writer,
    )

    store = _FakeStore(rows=[])
    _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    reader = make_checkpoint_reader()
    for seq in (0, 1, 2):
        await writer(
            _make_payload(
                tenant_id="t-seq",
                checkpoint_id=f"cp-{seq}",
                task_id="task-seq",
                sequence=seq,
                step_idx=seq,
            )
        )

    cp = await reader("t-seq", "task-seq")
    assert cp is not None
    assert cp.sequence == 2
    assert cp.checkpoint_id == "cp-2"
    assert cp.step_idx == 2


async def test_reader_filters_by_task_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row with the same tenant but different task_id must be ignored."""
    from kun.integration.checkpoint_db import (
        make_checkpoint_reader,
        make_checkpoint_writer,
    )

    store = _FakeStore(rows=[])
    _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    reader = make_checkpoint_reader()
    await writer(
        _make_payload(
            tenant_id="t",
            checkpoint_id="cp-other",
            task_id="task-OTHER",
            sequence=99,
        )
    )
    await writer(
        _make_payload(
            tenant_id="t",
            checkpoint_id="cp-mine",
            task_id="task-MINE",
            sequence=1,
        )
    )

    cp = await reader("t", "task-MINE")
    assert cp is not None
    assert cp.checkpoint_id == "cp-mine"
    assert cp.task_id == "task-MINE"


async def test_reader_skips_final_and_failed_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only `status='active'` rows are returned; final / failed_resume ignored."""
    from kun.integration.checkpoint_db import (
        make_checkpoint_reader,
        make_checkpoint_writer,
    )

    store = _FakeStore(rows=[])
    _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    reader = make_checkpoint_reader()
    # sequence 5 — final → should be skipped
    await writer(
        _make_payload(
            tenant_id="t",
            checkpoint_id="cp-final",
            task_id="task-1",
            sequence=5,
            status="final",
        )
    )
    # sequence 4 — failed_resume → should be skipped
    await writer(
        _make_payload(
            tenant_id="t",
            checkpoint_id="cp-failed",
            task_id="task-1",
            sequence=4,
            status="failed_resume",
        )
    )
    # sequence 3 — active → should be returned (highest active sequence)
    await writer(
        _make_payload(
            tenant_id="t",
            checkpoint_id="cp-active-3",
            task_id="task-1",
            sequence=3,
            status="active",
        )
    )
    # sequence 1 — older active
    await writer(
        _make_payload(
            tenant_id="t",
            checkpoint_id="cp-active-1",
            task_id="task-1",
            sequence=1,
            status="active",
        )
    )

    cp = await reader("t", "task-1")
    assert cp is not None
    assert cp.checkpoint_id == "cp-active-3"
    assert cp.status == "active"
    assert cp.sequence == 3


# ---------------------------------------------------------------------------
# Tests — status marker
# ---------------------------------------------------------------------------


async def test_marker_updates_status_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kun.integration.checkpoint_db import (
        make_checkpoint_status_marker,
        make_checkpoint_writer,
    )

    store = _FakeStore(rows=[])
    _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    marker = make_checkpoint_status_marker()
    await writer(
        _make_payload(
            tenant_id="t-mark",
            checkpoint_id="cp-mark",
            task_id="task-mark",
            status="active",
        )
    )

    await marker("t-mark", "cp-mark", "final")

    assert len(store.rows) == 1
    assert store.rows[0].status == "final"


async def test_marker_only_touches_matching_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Update must scope to the (tenant_id, checkpoint_id) pair only."""
    from kun.integration.checkpoint_db import (
        make_checkpoint_status_marker,
        make_checkpoint_writer,
    )

    store = _FakeStore(rows=[])
    _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    marker = make_checkpoint_status_marker()
    await writer(_make_payload(tenant_id="t", checkpoint_id="cp-a"))
    await writer(_make_payload(tenant_id="t", checkpoint_id="cp-b"))
    await writer(_make_payload(tenant_id="t", checkpoint_id="cp-c"))

    await marker("t", "cp-b", "failed_resume")

    by_id = {r.checkpoint_id: r.status for r in store.rows}
    assert by_id == {"cp-a": "active", "cp-b": "failed_resume", "cp-c": "active"}


async def test_marker_rejects_invalid_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kun.integration.checkpoint_db import make_checkpoint_status_marker

    store = _FakeStore(rows=[])
    seen = _install_fake_scope(monkeypatch, store)

    marker = make_checkpoint_status_marker()
    with pytest.raises(ValueError, match="invalid checkpoint status"):
        await marker("t", "cp-x", "garbage")  # type: ignore[arg-type]

    # Must reject before opening a session.
    assert seen == []


# ---------------------------------------------------------------------------
# Round-trip — payload fidelity
# ---------------------------------------------------------------------------


async def test_round_trip_preserves_payload_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write a payload with non-trivial nested fields, read it back via the
    reader, and assert every field round-trips correctly — goal_anchor_id,
    last_self_report, cost_usd_so_far in particular.
    """
    from kun.integration.checkpoint_db import (
        make_checkpoint_reader,
        make_checkpoint_writer,
    )

    store = _FakeStore(rows=[])
    _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    reader = make_checkpoint_reader()

    payload = _make_payload(
        tenant_id="t-rt",
        checkpoint_id="cp-rt",
        task_id="task-rt",
        sequence=7,
        step_idx=7,
        goal_anchor_id="anchor-RT",
        last_self_report={"progress": 0.7, "blockers": ["api-rate-limit"]},
        cost_usd_so_far=1.2345,
    )
    await writer(payload)

    cp = await reader("t-rt", "task-rt")
    assert cp is not None
    assert cp.checkpoint_id == "cp-rt"
    assert cp.task_id == "task-rt"
    assert cp.step_idx == 7
    assert cp.sequence == 7
    assert cp.goal_anchor_id == "anchor-RT"
    assert cp.last_self_report == {"progress": 0.7, "blockers": ["api-rate-limit"]}
    assert cp.cost_usd_so_far == pytest.approx(1.2345)
    assert cp.tokens_used_so_far == 42
    assert cp.conversation_snapshot == [{"role": "user", "content": "hi"}]
    assert cp.working_state == {"tools_used": ["search"]}
    assert cp.artifact_refs == ["minio://k/v"]
    assert cp.status == "active"
    assert cp.rationale == "step done"
    # created_at is preserved from the row (set by the writer payload).
    assert isinstance(cp.created_at, datetime)


async def test_round_trip_handles_null_optional_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """goal_anchor_id=None and last_self_report=None must round-trip cleanly."""
    from kun.integration.checkpoint_db import (
        make_checkpoint_reader,
        make_checkpoint_writer,
    )

    store = _FakeStore(rows=[])
    _install_fake_scope(monkeypatch, store)

    writer = make_checkpoint_writer()
    reader = make_checkpoint_reader()

    await writer(
        _make_payload(
            tenant_id="t-null",
            checkpoint_id="cp-null",
            task_id="task-null",
            goal_anchor_id=None,
            last_self_report=None,
        )
    )

    cp = await reader("t-null", "task-null")
    assert cp is not None
    assert cp.goal_anchor_id is None
    assert cp.last_self_report is None


# ---------------------------------------------------------------------------
# Session scoping — every factory uses session_scope(tenant_id=...)
# ---------------------------------------------------------------------------


async def test_each_factory_binds_session_scope_to_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Writer / reader / marker all open `session_scope(tenant_id=tenant)`."""
    from kun.integration.checkpoint_db import (
        make_checkpoint_reader,
        make_checkpoint_status_marker,
        make_checkpoint_writer,
    )

    store = _FakeStore(rows=[])
    seen = _install_fake_scope(monkeypatch, store)

    await make_checkpoint_writer()(_make_payload(tenant_id="t-W"))
    assert await make_checkpoint_reader()("t-R", "task-anything") is None
    await make_checkpoint_status_marker()("t-M", "cp-anything", "final")

    assert seen == ["t-W", "t-R", "t-M"]


async def test_writer_payload_round_trips_via_orm_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dict produced by `to_row_payload` is a valid kwarg set for
    TaskCheckpointRow — writer should not need to mutate it.
    """
    from kun.integration.checkpoint_db import make_checkpoint_writer

    store = _FakeStore(rows=[])
    _install_fake_scope(monkeypatch, store)

    payload = _make_payload(tenant_id="t-orm", checkpoint_id="cp-orm")
    await make_checkpoint_writer()(payload)

    [row] = store.rows
    assert isinstance(row, TaskCheckpointRow)
    assert row.checkpoint_id == "cp-orm"
    assert row.task_id == payload["task_id"]
    assert row.sequence == payload["sequence"]
    assert row.conversation_snapshot == payload["conversation_snapshot"]
