"""Cron scheduler unit tests (V2.1 M4 part2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.engineering.cron_scheduler import (
    PRESETS,
    CronScheduler,
    cron_matches,
)

# ---- cron_matches ----


def test_star_matches_all() -> None:
    now = datetime(2026, 4, 26, 14, 30, tzinfo=UTC)
    assert cron_matches("* * * * *", now)


def test_specific_minute_matches() -> None:
    now = datetime(2026, 4, 26, 14, 30, tzinfo=UTC)
    assert cron_matches("30 * * * *", now)
    assert not cron_matches("31 * * * *", now)


def test_specific_hour_matches() -> None:
    now = datetime(2026, 4, 26, 9, 0, tzinfo=UTC)
    assert cron_matches("0 9 * * *", now)
    assert not cron_matches("0 10 * * *", now)


def test_step_field() -> None:
    now15 = datetime(2026, 4, 26, 14, 15, tzinfo=UTC)
    now16 = datetime(2026, 4, 26, 14, 16, tzinfo=UTC)
    assert cron_matches("*/15 * * * *", now15)
    assert not cron_matches("*/15 * * * *", now16)


def test_list_field() -> None:
    now = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)
    assert cron_matches("0 9,12,14 * * *", now)
    now2 = datetime(2026, 4, 26, 13, 0, tzinfo=UTC)
    assert not cron_matches("0 9,12,14 * * *", now2)


def test_dow_sunday_zero() -> None:
    """Sunday 应 = 0 (cron 标准)."""
    sunday = datetime(2026, 4, 26, 9, 0, tzinfo=UTC)  # 2026-04-26 是 Sunday
    assert sunday.weekday() == 6  # python: Sun = 6
    assert cron_matches("0 9 * * 0", sunday)
    assert not cron_matches("0 9 * * 1", sunday)


def test_dow_monday_one() -> None:
    monday = datetime(2026, 4, 27, 9, 0, tzinfo=UTC)  # 周一
    assert monday.weekday() == 0
    assert cron_matches("0 9 * * 1", monday)


def test_preset_hourly() -> None:
    now = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)
    assert cron_matches("@hourly", now)
    now1 = datetime(2026, 4, 26, 14, 1, tzinfo=UTC)
    assert not cron_matches("@hourly", now1)


def test_preset_daily() -> None:
    now = datetime(2026, 4, 26, 0, 0, tzinfo=UTC)
    assert cron_matches("@daily", now)
    now1 = datetime(2026, 4, 26, 1, 0, tzinfo=UTC)
    assert not cron_matches("@daily", now1)


def test_preset_weekly_is_sunday_midnight() -> None:
    sunday_midnight = datetime(2026, 4, 26, 0, 0, tzinfo=UTC)
    assert cron_matches("@weekly", sunday_midnight)


def test_preset_monthly_is_first_midnight() -> None:
    first = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    assert cron_matches("@monthly", first)
    second = datetime(2026, 5, 2, 0, 0, tzinfo=UTC)
    assert not cron_matches("@monthly", second)


def test_invalid_expr_returns_false() -> None:
    now = datetime(2026, 4, 26, tzinfo=UTC)
    assert not cron_matches("nonsense", now)
    assert not cron_matches("0 9 * *", now)  # 4 fields


# ---- CronScheduler ----


@pytest.mark.asyncio
async def test_register_and_list() -> None:
    sched = CronScheduler()

    async def cb() -> None:
        pass

    sched.register("j1", "@hourly", cb)
    sched.register("j2", "@daily", cb)
    assert "j1" in sched.list_jobs()
    assert "j2" in sched.list_jobs()


def test_register_invalid_raises() -> None:
    sched = CronScheduler()

    async def cb() -> None:
        pass

    with pytest.raises(ValueError):
        sched.register("bad", "0 9 * *", cb)  # 4 fields


@pytest.mark.asyncio
async def test_tick_fires_matching_job() -> None:
    sched = CronScheduler()
    fired: list[str] = []

    async def cb() -> None:
        fired.append("j1")

    sched.register("j1", "* * * * *", cb)  # 每分钟跑
    fired_names = await sched.tick(now=datetime(2026, 4, 26, 14, 30, tzinfo=UTC))
    assert "j1" in fired_names

    # 等 task 跑完
    import asyncio

    await asyncio.sleep(0.05)
    assert fired == ["j1"]


@pytest.mark.asyncio
async def test_tick_does_not_repeat_same_minute() -> None:
    sched = CronScheduler()

    async def cb() -> None:
        pass

    sched.register("j1", "* * * * *", cb)
    now = datetime(2026, 4, 26, 14, 30, tzinfo=UTC)
    fired1 = await sched.tick(now=now)
    fired2 = await sched.tick(now=now)
    assert fired1 == ["j1"]
    assert fired2 == []


@pytest.mark.asyncio
async def test_disabled_job_does_not_fire() -> None:
    sched = CronScheduler()

    async def cb() -> None:
        pass

    sched.register("j1", "* * * * *", cb)
    job = sched.get_job("j1")
    assert job is not None
    job.disable()
    fired = await sched.tick(now=datetime(2026, 4, 26, 14, 30, tzinfo=UTC))
    assert fired == []


@pytest.mark.asyncio
async def test_callback_error_increments_error_count() -> None:
    sched = CronScheduler()

    async def cb_fail() -> None:
        raise ValueError("boom")

    sched.register("j1", "* * * * *", cb_fail)
    await sched.tick(now=datetime(2026, 4, 26, 14, 30, tzinfo=UTC))

    import asyncio

    await asyncio.sleep(0.05)
    job = sched.get_job("j1")
    assert job is not None
    assert job.error_count == 1
    assert "boom" in job.last_error


@pytest.mark.asyncio
async def test_callback_success_increments_run_count() -> None:
    sched = CronScheduler()

    async def cb_ok() -> None:
        pass

    sched.register("j1", "* * * * *", cb_ok)
    await sched.tick(now=datetime(2026, 4, 26, 14, 30, tzinfo=UTC))

    import asyncio

    await asyncio.sleep(0.05)
    job = sched.get_job("j1")
    assert job is not None
    assert job.run_count == 1


def test_unregister() -> None:
    sched = CronScheduler()

    async def cb() -> None:
        pass

    sched.register("j1", "@daily", cb)
    assert sched.unregister("j1") is True
    assert "j1" not in sched.list_jobs()
    assert sched.unregister("nonexistent") is False


def test_presets_complete() -> None:
    expected = {"@hourly", "@daily", "@weekly", "@monthly"}
    assert set(PRESETS.keys()) == expected
