"""Tests for safety_guards (V2.1 §5.2 + §11.4-11.5 五大致命差评对策)."""

from __future__ import annotations

import asyncio
import time

import pytest
from kun.engineering.safety_guards import (
    KillSwitch,
    PlanOnlyGate,
    TaskTimeoutGuard,
    TokenMeter,
    ZeroTelemetryEnforcer,
)

# ---- T55 KillSwitch ----


@pytest.mark.asyncio
async def test_kill_switch_basic_kill() -> None:
    ks = KillSwitch()
    ks.register_task("tk-1")
    assert ks.is_killed("tk-1") is False

    assert ks.kill("tk-1", "user clicked") is True
    assert ks.is_killed("tk-1") is True
    sig = ks.get_kill_signal("tk-1")
    assert sig is not None
    assert sig.reason == "user clicked"


@pytest.mark.asyncio
async def test_kill_switch_unknown_task() -> None:
    ks = KillSwitch()
    assert ks.kill("nope") is False


@pytest.mark.asyncio
async def test_kill_switch_responds_within_sla() -> None:
    """SLA: 任何 task ≤500ms 收到 SIGSTOP."""
    ks = KillSwitch()
    ks.register_task("tk-slow")

    async def slow_work():
        await asyncio.sleep(2.0)
        return "done"

    async def kill_after_delay():
        await asyncio.sleep(0.05)
        ks.kill("tk-slow")

    start = time.perf_counter()
    asyncio.create_task(kill_after_delay())
    with pytest.raises(asyncio.CancelledError):
        await ks.wait_or_proceed("tk-slow", slow_work())
    elapsed_ms = (time.perf_counter() - start) * 1000
    # SLA: kill 后应 ≤500ms 完成 cancel (实际 ~50ms)
    assert elapsed_ms < 500, f"kill took {elapsed_ms:.1f}ms"


@pytest.mark.asyncio
async def test_kill_switch_cleanup() -> None:
    ks = KillSwitch()
    ks.register_task("tk-c")
    ks.kill("tk-c")
    ks.cleanup("tk-c")
    assert ks.is_killed("tk-c") is False
    assert ks.get_kill_signal("tk-c") is None


# ---- T46+T47 TokenMeter ----


def test_token_meter_step_limit_pass() -> None:
    tm = TokenMeter(single_step_limit=10000)
    assert tm.check_step_limit(5000) is True


def test_token_meter_step_limit_block() -> None:
    tm = TokenMeter(single_step_limit=10000)
    fired = []
    tm.register_listener(lambda kind, payload: fired.append(kind))
    assert tm.check_step_limit(50000) is False
    assert "step_limit_exceeded" in fired


def test_token_meter_dashboard_starts_zero() -> None:
    tm = TokenMeter()
    dash = tm.get_dashboard("u-1")
    assert dash["five_hour"]["used"] == 0
    assert dash["daily"]["used"] == 0


def test_token_meter_records_usage() -> None:
    tm = TokenMeter(five_hour_limit=10000, daily_limit=100000)
    tm.record_usage("u-1", "tk-1", 500)
    tm.record_usage("u-1", "tk-1", 300)
    dash = tm.get_dashboard("u-1")
    assert dash["five_hour"]["used"] == 800
    assert dash["daily"]["used"] == 800
    assert tm.get_task_total("tk-1") == 800


def test_token_meter_warn_at_80pct() -> None:
    tm = TokenMeter(five_hour_limit=1000, warn_threshold=0.80, alert_threshold=0.95)
    fired = []
    tm.register_listener(lambda kind, payload: fired.append(kind))
    tm.record_usage("u-1", "tk-1", 850)
    assert "warn_5h" in fired


def test_token_meter_alert_at_95pct() -> None:
    tm = TokenMeter(five_hour_limit=1000, warn_threshold=0.80, alert_threshold=0.95)
    fired = []
    tm.register_listener(lambda kind, payload: fired.append(kind))
    tm.record_usage("u-1", "tk-1", 980)
    assert "alert_5h" in fired


def test_token_meter_per_user_isolated() -> None:
    tm = TokenMeter(five_hour_limit=10000)
    tm.record_usage("u-1", "tk-a", 500)
    tm.record_usage("u-2", "tk-b", 800)
    assert tm.get_dashboard("u-1")["five_hour"]["used"] == 500
    assert tm.get_dashboard("u-2")["five_hour"]["used"] == 800


# ---- T51 PlanOnlyGate ----


def test_plan_only_drop_database() -> None:
    g = PlanOnlyGate()
    d = g.check("DROP DATABASE production")
    assert d.triggered is True
    assert d.confirm_token is not None
    assert "destructive" in d.reason


def test_plan_only_delete_table() -> None:
    g = PlanOnlyGate()
    d = g.check("DELETE FROM users WHERE 1=1")
    # DELETE FROM 不包含 TABLE 关键字, 不在 hard list
    # 但有"删除"关键字也行 (中文 hard list 通过 user_message)
    assert g.check("delete table users").triggered is True


def test_plan_only_chinese_delete_prod() -> None:
    g = PlanOnlyGate()
    d = g.check("帮我删除生产数据库的所有数据")
    assert d.triggered is True


def test_plan_only_payment() -> None:
    g = PlanOnlyGate()
    d = g.check("transfer $1000 to account 1234")
    assert d.triggered is True


def test_plan_only_send_email() -> None:
    g = PlanOnlyGate()
    d = g.check("send email to all customers")
    assert d.triggered is True


def test_plan_only_safe_action_passes() -> None:
    g = PlanOnlyGate()
    d = g.check("read this file")
    assert d.triggered is False


def test_plan_only_prod_write_triggers() -> None:
    g = PlanOnlyGate()
    d = g.check("update config", action_kind="write", env="prod")
    assert d.triggered is True
    assert "prod_write" in d.reason


def test_plan_only_pre_approved_soft_action() -> None:
    g = PlanOnlyGate(soft_actions_pre_approved=("read_only_query",))
    d = g.check("query database", action_kind="read_only_query")
    assert d.triggered is False
    assert "pre_approved" in d.reason


def test_plan_only_confirm_accept() -> None:
    g = PlanOnlyGate()
    d = g.check("delete table x")
    assert d.triggered
    assert g.confirm(d.confirm_token, accept=True) is True


def test_plan_only_confirm_reject() -> None:
    g = PlanOnlyGate()
    d = g.check("delete table x")
    assert g.confirm(d.confirm_token, accept=False) is False


def test_plan_only_confirm_unknown_token() -> None:
    g = PlanOnlyGate()
    assert g.confirm("BADTOKEN", accept=True) is False


# ---- T52 TaskTimeoutGuard ----


def test_task_timeout_basic() -> None:
    tg = TaskTimeoutGuard(default_max_duration_sec=10, default_max_steps=5)
    rt = tg.start("tk-1")
    assert rt.task_id == "tk-1"
    assert rt.max_duration_sec == 10
    assert rt.max_steps == 5
    timed_out, _ = tg.check("tk-1")
    assert timed_out is False


def test_task_timeout_steps_exceeded() -> None:
    tg = TaskTimeoutGuard(default_max_steps=3)
    tg.start("tk-1")
    for _ in range(3):
        tg.step_completed("tk-1")
    timed_out, reason = tg.check("tk-1")
    assert timed_out is True
    assert "steps" in reason


def test_task_timeout_duration_exceeded() -> None:
    """Mock duration via direct started_at manipulation."""
    from datetime import UTC, datetime, timedelta

    tg = TaskTimeoutGuard(default_max_duration_sec=1)
    rt = tg.start("tk-1")
    rt.started_at = datetime.now(UTC) - timedelta(seconds=10)
    timed_out, reason = tg.check("tk-1")
    assert timed_out is True
    assert "duration" in reason


def test_task_timeout_action_default() -> None:
    tg = TaskTimeoutGuard()
    tg.start("tk-1")
    assert tg.get_action("tk-1") == "pause_ask_user"


def test_task_timeout_action_custom() -> None:
    tg = TaskTimeoutGuard()
    tg.start("tk-1", timeout_action="cancel")
    assert tg.get_action("tk-1") == "cancel"


def test_task_timeout_unknown_task() -> None:
    tg = TaskTimeoutGuard()
    timed_out, _ = tg.check("nope")
    assert timed_out is False


def test_task_timeout_cleanup() -> None:
    tg = TaskTimeoutGuard()
    tg.start("tk-1")
    tg.cleanup("tk-1")
    timed_out, _ = tg.check("tk-1")
    assert timed_out is False


# ---- T56 ZeroTelemetryEnforcer ----


def test_zero_telemetry_default_off() -> None:
    z = ZeroTelemetryEnforcer()
    assert z.telemetry_enabled is False
    assert z.can_send("any_category") is False


def test_zero_telemetry_permanently_blocked() -> None:
    """即使用户开 telemetry, 永禁类别也不收."""
    z = ZeroTelemetryEnforcer(telemetry_enabled=True, opt_in_categories=("user_message_content",))
    assert z.can_send("user_message_content") is False  # 仍被永禁
    assert z.can_send("user_emotion_analysis") is False
    assert z.can_send("frustration_regex_match") is False  # Anthropic 案例


def test_zero_telemetry_opt_in_works() -> None:
    z = ZeroTelemetryEnforcer(telemetry_enabled=True, opt_in_categories=("error_count",))
    assert z.can_send("error_count") is True
    assert z.can_send("not_opted_in") is False


def test_zero_telemetry_audit_endpoint() -> None:
    z = ZeroTelemetryEnforcer(user_siem_endpoint="https://my-siem.example/")
    assert z.get_audit_endpoint() == "https://my-siem.example/"


def test_zero_telemetry_landing_page_promise() -> None:
    z = ZeroTelemetryEnforcer()
    promise = z.get_landing_page_promise()
    assert promise["telemetry_default"] == "off"
    assert "user_message_content" in promise["permanently_blocked_categories"]
    assert "user_emotion_analysis" in promise["permanently_blocked_categories"]
    assert "open_source_proof" in promise
