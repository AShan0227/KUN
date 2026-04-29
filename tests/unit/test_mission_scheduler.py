"""Mission scheduler registration tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from kun.api.main import register_mission_scheduler_jobs
from kun.engineering.cron_scheduler import CronScheduler


@pytest.mark.unit
@pytest.mark.asyncio
async def test_register_mission_scheduler_jobs_wires_resume_and_reaper(monkeypatch) -> None:
    calls: list[tuple[str, str, int, int]] = []

    class FakeWorker:
        async def run_once(self, *, tenant_id: str, limit: int, max_attempts: int):
            calls.append(("resume", tenant_id, limit, max_attempts))
            return []

    async def fake_reaper(
        *,
        tenant_id: str,
        queued_stale_after_sec: int,
        running_stale_after_sec: int,
        limit: int,
    ):
        calls.append(("reaper", tenant_id, queued_stale_after_sec, running_stale_after_sec))
        calls.append(("reaper_limit", tenant_id, limit, 0))
        return []

    monkeypatch.setenv("KUN_MISSION_RESUME_LIMIT", "7")
    monkeypatch.setenv("KUN_MISSION_RESUME_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("KUN_MISSION_REAPER_CRON", "* * * * *")
    monkeypatch.setenv("KUN_MISSION_QUEUED_STALE_AFTER_SEC", "120")
    monkeypatch.setenv("KUN_MISSION_RUNNING_STALE_AFTER_SEC", "240")
    monkeypatch.setenv("KUN_MISSION_REAPER_LIMIT", "11")
    monkeypatch.setattr("kun.api.runtime.get_mission_resume_worker", lambda _app: FakeWorker())
    monkeypatch.setattr("kun.engineering.mission_control.reap_stale_mission_tasks", fake_reaper)

    sched = CronScheduler()
    register_mission_scheduler_jobs(sched, FastAPI(), "tenant-a")

    assert sched.list_jobs() == ["mission_reaper", "mission_resume"]

    fired = await sched.tick(now=datetime(2026, 4, 29, 10, 5, tzinfo=UTC))
    await asyncio.sleep(0.05)

    assert fired == ["mission_resume", "mission_reaper"]
    assert ("resume", "tenant-a", 7, 4) in calls
    assert ("reaper", "tenant-a", 120, 240) in calls
    assert ("reaper_limit", "tenant-a", 11, 0) in calls


@pytest.mark.unit
def test_register_mission_scheduler_jobs_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("KUN_MISSION_SCHEDULER_ENABLED", "0")
    sched = CronScheduler()

    register_mission_scheduler_jobs(sched, FastAPI(), "tenant-a")

    assert sched.list_jobs() == []


@pytest.mark.unit
def test_register_mission_scheduler_jobs_allows_individual_job_disable(monkeypatch) -> None:
    monkeypatch.setenv("KUN_MISSION_RESUME_WORKER_ENABLED", "0")
    sched = CronScheduler()

    register_mission_scheduler_jobs(sched, FastAPI(), "tenant-a")

    assert sched.list_jobs() == ["mission_reaper"]
