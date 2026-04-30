"""V2.3 启 cron jobs — register_qi_cron_jobs."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from kun.engineering.cron_scheduler import CronScheduler
from kun.qi.cron_jobs import register_qi_cron_jobs
from kun.qi.predictive_coding import (
    PredictionRecord,
    get_prediction_log,
    reset_prediction_log,
)


def _app_with_state() -> SimpleNamespace:
    from starlette.datastructures import State

    return SimpleNamespace(state=State())


def test_register_qi_cron_jobs_registers_3() -> None:
    sched = CronScheduler()
    app = _app_with_state()
    register_qi_cron_jobs(sched, app, "u-test")
    jobs = set(sched.list_jobs())
    assert "qi_pc_train_hourly" in jobs
    assert "qi_darwin_explore_hourly" in jobs
    assert "qi_ai_scientist_hourly" in jobs


@pytest.mark.asyncio
async def test_qi_cron_pc_train_skip_when_window_inactive() -> None:
    """启窗口外 → cron skip, 不抛."""
    from kun.qi.cron_jobs import _qi_predictive_coding_train

    app = _app_with_state()
    with patch.dict(os.environ, {"KUN_QI_ENABLED": "0"}, clear=False):
        # 不抛
        await _qi_predictive_coding_train(app, "u-test")


@pytest.mark.asyncio
async def test_qi_cron_pc_train_runs_when_force_active() -> None:
    """KUN_QI_FORCE_ACTIVE=1 → cron 跑 PredictionTrainer (空 log → 返 sample_size=0 model)."""
    from kun.qi.cron_jobs import _qi_predictive_coding_train

    app = _app_with_state()
    with patch.dict(
        os.environ,
        {"KUN_QI_ENABLED": "1", "KUN_QI_FORCE_ACTIVE": "1"},
        clear=False,
    ):
        # 守门通过, train() 跑 (log 空 → 返 placeholder model, 无 save_path → 不写文件)
        await _qi_predictive_coding_train(app, "u-test")
    assert getattr(app.state, "predictive_coding_provider", None) is None


@pytest.mark.asyncio
async def test_qi_cron_pc_train_hot_installs_model_with_samples() -> None:
    """启训练出非空模型后，当前运行中的 orchestrator 立即吃到 prediction_provider."""
    from datetime import UTC, datetime

    from kun.qi.cron_jobs import _qi_predictive_coding_train

    reset_prediction_log()
    log = get_prediction_log()
    await log.append(
        PredictionRecord(
            timestamp=datetime.now(UTC),
            task_type="writing.greeting",
            step_id=1,
            state={"task_type": "writing.greeting"},
            expected={"cost_usd": 0.5},
            actual={"cost_usd": 0.2, "duration_sec": 3.0, "tokens": 80.0},
            error={"cost_usd": -0.3},
        )
    )
    orchestrator = SimpleNamespace(prediction_provider=None)
    app = _app_with_state()
    app.state.orchestrator = orchestrator

    with patch.dict(
        os.environ,
        {
            "KUN_QI_ENABLED": "1",
            "KUN_QI_FORCE_ACTIVE": "1",
        },
        clear=False,
    ):
        await _qi_predictive_coding_train(app, "u-test")

    provider = app.state.predictive_coding_provider
    assert provider is orchestrator.prediction_provider
    assert provider.sample_size == 1
    assert provider.predict({"task_type": "writing.greeting"})["cost_usd"] == pytest.approx(0.2)
    reset_prediction_log()


@pytest.mark.asyncio
async def test_qi_cron_darwin_skip_window_inactive() -> None:
    from kun.qi.cron_jobs import _qi_darwin_godel_explore

    app = _app_with_state()
    with patch.dict(os.environ, {"KUN_QI_ENABLED": "0"}, clear=False):
        await _qi_darwin_godel_explore(app, "u-test")


@pytest.mark.asyncio
async def test_qi_cron_ai_scientist_skip_window_inactive() -> None:
    from kun.qi.cron_jobs import _qi_ai_scientist_explore

    app = _app_with_state()
    with patch.dict(os.environ, {"KUN_QI_ENABLED": "0"}, clear=False):
        await _qi_ai_scientist_explore(app, "u-test")


@pytest.mark.asyncio
async def test_qi_cron_darwin_skips_when_budget_exhausted() -> None:
    """budget 耗尽 → log + skip, 不抛."""
    from kun.qi import get_qi_budget, reset_qi_budget
    from kun.qi.cron_jobs import _qi_darwin_godel_explore

    reset_qi_budget()
    budget = get_qi_budget()
    budget.set_daily_limit(1.0)
    # 直接 push 到上限 (绕过 raise)
    from datetime import UTC, datetime

    today = datetime.now(UTC).date()
    budget._costs[("u-test", today)] = 1.0  # = limit

    app = _app_with_state()
    with patch.dict(os.environ, {"KUN_QI_ENABLED": "1", "KUN_QI_FORCE_ACTIVE": "1"}, clear=False):
        await _qi_darwin_godel_explore(app, "u-test")
    reset_qi_budget()
