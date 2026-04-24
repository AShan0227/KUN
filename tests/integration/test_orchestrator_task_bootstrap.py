"""Integration regression: orchestrator's first-transaction task bootstrap.

This test pins the SQLAlchemy unit-of-work ordering fix from 56c846b:

    Adding TaskRow + RuntimeStateRow in the same session_scope without a
    preceding flush used to emit the child INSERT before the parent, because
    there's no `relationship()` declared between the two and the pure-FK
    column is not reliably topologically ordered. The result was a
    `fk_runtime_states_task_ref_tasks` violation on every real `kun run`.

Unit tests don't catch it because they use mocked sessions that don't
enforce foreign keys. This one runs against a real Postgres.

Requires the dev docker-compose stack to be up (KUN_PG_ADMIN_DSN reachable).
Uses the admin DSN so we don't couple this test to RLS setup; the FK
ordering behaviour being tested is independent of RLS.
"""

from __future__ import annotations

import os

import pytest
from kun.core.db import get_admin_engine, get_admin_sessionmaker
from kun.core.ids import new_id
from kun.core.orm import IdempotencyRow, RuntimeStateRow, TaskRow
from sqlalchemy import text


pytestmark = pytest.mark.integration


async def _cleanup(tenant_id: str, task_id: str) -> None:
    """Best-effort cleanup — this test runs against a live DB."""
    engine = get_admin_engine()
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM runtime_states WHERE tenant_id = :t AND task_ref = :tid"),
            {"t": tenant_id, "tid": task_id},
        )
        await conn.execute(
            text("DELETE FROM idempotency_keys WHERE tenant_id = :t AND result_ref = :tid"),
            {"t": tenant_id, "tid": task_id},
        )
        await conn.execute(
            text("DELETE FROM tasks WHERE tenant_id = :t AND task_id = :tid"),
            {"t": tenant_id, "tid": task_id},
        )


@pytest.mark.asyncio
async def test_task_bootstrap_flushes_parent_before_child_fk() -> None:
    """Regression: one-transaction TaskRow + RuntimeStateRow must not deadlock on FK.

    Mirrors the exact add-order the orchestrator uses in its first session_scope.
    Without the pre-flush of the parent rows, this reproducibly trips
    `fk_runtime_states_task_ref_tasks` on a real Postgres.
    """
    if "KUN_PG_ADMIN_DSN" not in os.environ and "KUN_PG_DSN" not in os.environ:
        pytest.skip("no Postgres configured (KUN_PG_ADMIN_DSN / KUN_PG_DSN)")

    tenant_id = "u-test-fk-order"
    task_id = new_id("task")
    fingerprint = f"fp-{task_id}"

    maker = get_admin_sessionmaker()
    try:
        async with maker() as s:
            s.add(
                TaskRow(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    fingerprint=fingerprint,
                    task_type="test.fk_order",
                    risk_level="low",
                    complexity_score=0.1,
                    user_id=None,
                    project_id=None,
                    estimated_cost_usd=0.0,
                    estimated_duration_sec=0.0,
                    deadline_iso=None,
                    success_criteria_short="regression check",
                    version=1,
                    spec_json=None,
                    layer3_ref=None,
                )
            )
            s.add(
                IdempotencyRow(
                    key=fingerprint,
                    tenant_id=tenant_id,
                    result_ref=task_id,
                )
            )
            # The critical step — without this flush, the combined flush
            # below used to INSERT the child row first and fail.
            await s.flush()

            s.add(
                RuntimeStateRow(
                    state_id=new_id("runtime"),
                    task_ref=task_id,
                    tenant_id=tenant_id,
                    current_step=0,
                    total_planned_steps=1,
                    status="queued",
                    accumulated_cost_usd_actual=0.0,
                    accumulated_cost_usd_equivalent=0.0,
                    accumulated_tokens=0,
                    failures_this_run=0,
                    blob={},
                )
            )
            await s.flush()
            await s.commit()
    finally:
        await _cleanup(tenant_id, task_id)
