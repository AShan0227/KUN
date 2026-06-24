"""Audit F147: SupervisorService.observe must not hold its lock across I/O.

observe() used to wrap its entire body — including the awaited emitter / RCDH
enrich / notification I/O — in `async with self._lock`, serializing the whole
supervise lane on one slow DB/notify call. The lock now covers only the
shared-state mutation (state_for / record / _check_* / _maybe_cluster, which is
where dedup is recorded); the awaited I/O runs after the lock is released.

These tests would fail against the old "await inside the lock" code:
- the emitter observes the lock as *not held* while it runs, and
- two concurrent observes' emitter I/O actually overlaps instead of being
  serialized one-at-a-time by the single lock.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import pytest
from kun.agents.supervisor.service import SupervisorService

pytestmark = pytest.mark.unit

_FALLBACK = "llm.fallback.triggered"
_PAYLOAD = {"tenant_id": "t1", "primary_provider": "openrouter"}


@pytest.mark.asyncio
async def test_emitter_runs_with_lock_released() -> None:
    svc = SupervisorService()
    lock_held_during_emit: list[bool] = []

    async def emitter(req: dict[str, Any]) -> None:
        lock_held_during_emit.append(svc._lock.locked())

    svc._emitter = emitter

    # fallback_threshold is 3 → the 3rd event trips _check_fallback_spike.
    for _ in range(3):
        await svc.observe(_FALLBACK, _PAYLOAD)

    assert lock_held_during_emit, "emitter never ran — the anomaly did not trigger"
    assert not any(lock_held_during_emit), (
        f"observe ran the emitter while still holding self._lock "
        f"({lock_held_during_emit}) — I/O must be outside the critical section (F147)"
    )


@pytest.mark.asyncio
async def test_concurrent_observes_overlap_io() -> None:
    svc = SupervisorService()
    inflight = 0
    max_inflight = 0
    release = asyncio.Event()

    async def emitter(req: dict[str, Any]) -> None:
        nonlocal inflight, max_inflight
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(release.wait(), timeout=0.5)
        inflight -= 1

    svc._emitter = emitter

    # Prime each tenant to one below threshold (no trigger yet).
    for tenant in ("a", "b"):
        for _ in range(2):
            await svc.observe(_FALLBACK, {"tenant_id": tenant, "primary_provider": "p"})

    async def trigger(tenant: str) -> list[dict[str, Any]]:
        return await svc.observe(_FALLBACK, {"tenant_id": tenant, "primary_provider": "p"})

    task = asyncio.gather(trigger("a"), trigger("b"))
    await asyncio.sleep(0.05)  # let both reach the emitter
    release.set()
    await task

    assert max_inflight >= 2, (
        f"emitter I/O was serialized by the lock (max concurrent={max_inflight}); "
        "two tenants' observe() I/O should overlap once the lock is released (F147)"
    )
