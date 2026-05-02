"""Wire 38: 启 V3 时间窗口 + 日预算 (V2.3 §4)."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest
from kun.qi import (
    QiBudgetExhaustedError,
    QiDailyBudget,
    QiWindowConfig,
    QiWindowError,
    get_qi_budget,
    is_qi_window_active,
    require_qi_active,
)
from kun.qi.budget import reset_qi_budget


@pytest.fixture(autouse=True)
def _isolate():
    reset_qi_budget()
    saved_active = os.environ.pop("KUN_QI_FORCE_ACTIVE", None)
    saved_disable = os.environ.pop("KUN_QI_FORCE_DISABLE", None)
    yield
    reset_qi_budget()
    if saved_active is not None:
        os.environ["KUN_QI_FORCE_ACTIVE"] = saved_active
    if saved_disable is not None:
        os.environ["KUN_QI_FORCE_DISABLE"] = saved_disable


# ---- QiWindowConfig ----


def test_window_disabled_by_default() -> None:
    cfg = QiWindowConfig()
    assert cfg.enabled is False


def test_window_from_dict() -> None:
    cfg = QiWindowConfig.from_dict(
        {"enabled": True, "start_hour": 22, "end_hour": 6, "weekdays": [0, 1, 2]}
    )
    assert cfg.enabled is True
    assert cfg.start_hour == 22
    assert cfg.weekdays == (0, 1, 2)


def test_window_from_none_returns_default() -> None:
    cfg = QiWindowConfig.from_dict(None)
    assert cfg.enabled is False


def test_window_covers_within_hours() -> None:
    cfg = QiWindowConfig(enabled=True, start_hour=2, end_hour=5)
    monday_3am = datetime(2026, 4, 27, 3, 0, tzinfo=UTC)  # Monday 3am
    assert cfg.covers(monday_3am) is True


def test_window_covers_outside_hours() -> None:
    cfg = QiWindowConfig(enabled=True, start_hour=2, end_hour=5)
    monday_10am = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
    assert cfg.covers(monday_10am) is False


def test_window_covers_weekday_filter() -> None:
    cfg = QiWindowConfig(enabled=True, start_hour=2, end_hour=5, weekdays=(0, 1))  # Mon, Tue
    saturday_3am = datetime(2026, 5, 2, 3, 0, tzinfo=UTC)  # Saturday
    assert cfg.covers(saturday_3am) is False


def test_window_covers_overnight_window() -> None:
    """跨午夜窗口 (22-2)."""
    cfg = QiWindowConfig(enabled=True, start_hour=22, end_hour=2)
    monday_23 = datetime(2026, 4, 27, 23, 0, tzinfo=UTC)
    monday_1am = datetime(2026, 4, 28, 1, 0, tzinfo=UTC)
    monday_10am = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
    assert cfg.covers(monday_23) is True
    assert cfg.covers(monday_1am) is True
    assert cfg.covers(monday_10am) is False


def test_window_disabled_never_active() -> None:
    cfg = QiWindowConfig(enabled=False, start_hour=2, end_hour=5)
    monday_3am = datetime(2026, 4, 27, 3, 0, tzinfo=UTC)
    assert cfg.covers(monday_3am) is False


# ---- is_qi_window_active ----


def test_is_active_no_config_returns_false() -> None:
    assert is_qi_window_active(None) is False


def test_is_active_force_active_overrides() -> None:
    """KUN_QI_FORCE_ACTIVE=1 → True 即使 config disabled."""
    with patch.dict(os.environ, {"KUN_QI_FORCE_ACTIVE": "1"}):
        assert is_qi_window_active(None) is True
        assert is_qi_window_active(QiWindowConfig(enabled=False)) is True


def test_is_active_force_disable_overrides_force_active() -> None:
    with patch.dict(os.environ, {"KUN_QI_FORCE_DISABLE": "1", "KUN_QI_FORCE_ACTIVE": "1"}):
        assert is_qi_window_active(QiWindowConfig(enabled=True)) is False


def test_is_active_within_window() -> None:
    cfg = QiWindowConfig(enabled=True, start_hour=2, end_hour=5)
    when = datetime(2026, 4, 27, 3, 0, tzinfo=UTC)
    assert is_qi_window_active(cfg, when=when) is True


# ---- require_qi_active ----


def test_require_active_raises_when_disabled() -> None:
    with pytest.raises(QiWindowError, match="不可用"):
        require_qi_active(QiWindowConfig(enabled=False))


def test_require_active_raises_outside_window() -> None:
    cfg = QiWindowConfig(enabled=True, start_hour=2, end_hour=5)
    when = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
    with pytest.raises(QiWindowError):
        require_qi_active(cfg, when=when)


def test_require_active_no_raise_within_window() -> None:
    cfg = QiWindowConfig(enabled=True, start_hour=2, end_hour=5)
    when = datetime(2026, 4, 27, 3, 0, tzinfo=UTC)
    require_qi_active(cfg, when=when)  # 不抛


def test_require_active_force_override_works() -> None:
    with patch.dict(os.environ, {"KUN_QI_FORCE_ACTIVE": "1"}):
        require_qi_active(None)  # 不抛


# ---- QiDailyBudget ----


def test_budget_starts_empty() -> None:
    b = QiDailyBudget()
    assert b.get_today_spent("u-1") == 0.0
    assert b.remaining_budget("u-1") == 5.0  # default $5


def test_budget_set_daily_limit() -> None:
    b = QiDailyBudget()
    b.set_daily_limit(10.0)
    assert b.remaining_budget("u-1") == 10.0


def test_budget_set_negative_raises() -> None:
    b = QiDailyBudget()
    with pytest.raises(ValueError):
        b.set_daily_limit(-1)


def test_budget_add_cost_under_limit() -> None:
    b = QiDailyBudget()
    b.set_daily_limit(5.0)
    new_total = b.add_cost("u-1", 1.5)
    assert new_total == 1.5
    assert b.remaining_budget("u-1") == 3.5


def test_budget_add_cost_exceeds_raises() -> None:
    b = QiDailyBudget()
    b.set_daily_limit(1.0)
    b.add_cost("u-1", 0.8)  # ok
    with pytest.raises(QiBudgetExhaustedError, match="耗尽"):
        b.add_cost("u-1", 0.5)  # 0.8 + 0.5 = 1.3 > 1.0


def test_budget_per_user_isolation() -> None:
    b = QiDailyBudget()
    b.set_daily_limit(1.0)
    b.add_cost("u-1", 0.8)
    # u-2 不受 u-1 影响
    new_total = b.add_cost("u-2", 0.8)
    assert new_total == 0.8


def test_budget_per_day_isolation() -> None:
    b = QiDailyBudget()
    b.set_daily_limit(1.0)
    yesterday = date(2026, 4, 26)
    today = date(2026, 4, 27)
    b.add_cost("u-1", 0.8, today=yesterday)
    # 今天独立计数
    new_total = b.add_cost("u-1", 0.8, today=today)
    assert new_total == 0.8


def test_budget_singleton() -> None:
    a = get_qi_budget()
    b = get_qi_budget()
    assert a is b


def test_budget_failed_add_doesnt_persist() -> None:
    """超 budget 抛后, 计数不变."""
    b = QiDailyBudget()
    b.set_daily_limit(1.0)
    b.add_cost("u-1", 0.8)
    with pytest.raises(QiBudgetExhaustedError):
        b.add_cost("u-1", 0.5)
    # 还是 0.8, 不是 1.3
    assert b.get_today_spent("u-1") == 0.8


# ---- SoulFile 集成 ----


def test_soulfile_qi_window_default_disabled() -> None:
    from kun.datamodel.soul_file import SoulFile

    soul = SoulFile(user_id="u-test")
    assert soul.qi_window["enabled"] is False
    assert soul.qi_daily_budget_usd == 5.0


def test_soulfile_qi_window_custom() -> None:
    from kun.datamodel.soul_file import SoulFile

    soul = SoulFile(
        user_id="u-test",
        qi_window={
            "enabled": True,
            "start_hour": 22,
            "end_hour": 6,
            "weekdays": [0, 1, 2, 3, 4],
            "timezone": "UTC",
        },
        qi_daily_budget_usd=10.0,
    )
    cfg = QiWindowConfig.from_dict(soul.qi_window)
    assert cfg.enabled is True
    assert cfg.start_hour == 22
    assert soul.qi_daily_budget_usd == 10.0
