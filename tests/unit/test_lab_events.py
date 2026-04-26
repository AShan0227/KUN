"""LabEventEmitter + EnsembleExecutor/RecipePromoter event-emit 集成测试 (Wire 21)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from kun.lab import (
    EnsembleConfig,
    EnsembleExecutor,
    EnsemblePathResult,
    EnsembleResult,
    ExperimentLog,
    LabEventEmitter,
    RecipePromoter,
    summarize_ensemble,
    summarize_promotion,
)
from kun.lab.recipe_promoter import RecipePromotion


def _fake_ensemble_result(*, n_paths: int = 3, winner_idx: int = 0) -> EnsembleResult:
    return EnsembleResult(
        experiment_id="exp-fake-1",
        config=EnsembleConfig(n_paths=n_paths, selection_method="best_score"),
        path_results=[
            EnsemblePathResult(
                path_idx=i,
                config={"strategy": f"strat_{i}", "tier": "top"},
                output=f"out_{i}",
                score=0.9 if i == winner_idx else 0.3,
                cost_usd=0.05,
                latency_sec=1.2,
            )
            for i in range(n_paths)
        ],
        winning_path_idx=winner_idx,
        winning_output=f"out_{winner_idx}",
        total_cost_usd=0.15,
        total_latency_sec=1.2,
        selection_reason="best_score:0.90",
    )


# ---- summarize helpers ----


def test_summarize_ensemble_extracts_key_fields() -> None:
    res = _fake_ensemble_result(n_paths=3, winner_idx=1)
    payload = summarize_ensemble(res, task_type="biz_plan")

    assert payload["experiment_id"] == "exp-fake-1"
    assert payload["task_type"] == "biz_plan"
    assert payload["n_paths"] == 3
    assert payload["path_count_success"] == 3
    assert payload["winning_path_idx"] == 1
    assert payload["winning_strategy"] == "strat_1"
    assert payload["winning_score"] == pytest.approx(0.9)
    assert payload["selection_method"] == "best_score"


def test_summarize_ensemble_no_winner_safe() -> None:
    res = _fake_ensemble_result(n_paths=2, winner_idx=-1)
    payload = summarize_ensemble(res, task_type="x")
    assert payload["winning_path_idx"] == -1
    assert payload["winning_strategy"] == ""
    assert payload["winning_score"] == 0.0


def test_summarize_ensemble_filters_failed_paths() -> None:
    res = _fake_ensemble_result(n_paths=3, winner_idx=0)
    res.path_results[2].error = "timeout"
    payload = summarize_ensemble(res, task_type="x")
    assert payload["path_count_success"] == 2
    assert payload["n_paths"] == 3


def test_summarize_promotion_dumps_recipe_fields() -> None:
    promo = RecipePromotion(
        promotion_id="prom-1",
        task_type="ad",
        strategy="tier_top_low_temp",
        win_rate=0.85,
        total_count=12,
        avg_score=0.78,
        avg_cost_usd=0.04,
        target_module="execution_mode_classifier",
    )
    payload = summarize_promotion(promo)
    assert payload["promotion_id"] == "prom-1"
    assert payload["strategy"] == "tier_top_low_temp"
    assert payload["win_rate"] == pytest.approx(0.85)
    assert payload["target_module"] == "execution_mode_classifier"
    assert "promoted_at" in payload  # ISO 字符串


# ---- LabEventEmitter best-effort ----


@pytest.mark.asyncio
async def test_lab_event_emitter_no_tenant_returns_false() -> None:
    """没 tenant context → 静默返 False, 不抛."""
    emitter = LabEventEmitter()
    res = _fake_ensemble_result()

    # Force MissingTenantContextError
    from kun.core.tenancy import MissingTenantContextError

    with patch(
        "kun.core.tenancy.current_tenant",
        side_effect=MissingTenantContextError("test-no-tenant"),
    ):
        ok = await emitter.on_experiment_completed(res, task_type="x")
    assert ok is False


@pytest.mark.asyncio
async def test_lab_event_emitter_db_failure_returns_false() -> None:
    """DB session 失败 → False, 不抛."""
    emitter = LabEventEmitter()
    res = _fake_ensemble_result()

    from kun.core.tenancy import TenantContext

    fake_tenant = TenantContext(tenant_id="test-tenant")
    with (
        patch("kun.core.tenancy.current_tenant", return_value=fake_tenant),
        patch("kun.core.db.session_scope", side_effect=RuntimeError("no db")),
    ):
        ok = await emitter.on_experiment_completed(res, task_type="x")
    assert ok is False


# ---- EnsembleExecutor 集成 event_emitter ----


@pytest.mark.asyncio
async def test_executor_calls_event_emitter_on_completion() -> None:
    """跑完 → event_emitter 被 call 1 次, 拿到 EnsembleResult."""
    captured: list[tuple] = []

    async def fake_emitter(result, *, task_type=None):
        captured.append((result, task_type))
        return True

    async def fake_invoker(prompt, path):
        return ("ok", 0.01, 0.5)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        executor = EnsembleExecutor(fake_invoker, event_emitter=fake_emitter)
        result = await executor.run(
            "test", config=EnsembleConfig(n_paths=2), task_type="biz_plan"
        )

    assert len(captured) == 1
    captured_result, captured_tt = captured[0]
    assert captured_result is result
    assert captured_tt == "biz_plan"


@pytest.mark.asyncio
async def test_executor_emitter_failure_doesnt_break_result() -> None:
    """event_emitter 抛异常 → executor 仍正常返 result."""

    async def failing_emitter(result, *, task_type=None):
        raise RuntimeError("emitter blew up")

    async def fake_invoker(prompt, path):
        return ("ok", 0.01, 0.5)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        executor = EnsembleExecutor(fake_invoker, event_emitter=failing_emitter)
        result = await executor.run("t", config=EnsembleConfig(n_paths=2))

    assert result.experiment_id  # result 仍正常返回
    assert len(result.path_results) == 2


@pytest.mark.asyncio
async def test_executor_no_emitter_works_silently() -> None:
    """没 emitter → 不调用任何东西."""

    async def fake_invoker(prompt, path):
        return ("ok", 0.01, 0.5)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        executor = EnsembleExecutor(fake_invoker)  # no emitter
        result = await executor.run("t", config=EnsembleConfig(n_paths=2))

    assert result.experiment_id


# ---- RecipePromoter 集成 event_emitter / rollback_emitter ----


def _seed_log_for_promotion(log: ExperimentLog, *, count: int = 15) -> None:
    """加 N 个实验, tier_top 一直赢."""
    for i in range(count):
        result = EnsembleResult(
            experiment_id=f"exp-{i}",
            config=EnsembleConfig(n_paths=2),
            path_results=[
                EnsemblePathResult(
                    path_idx=0, config={"strategy": "tier_top_low_temp"}, score=0.9
                ),
            ],
            winning_path_idx=0,
        )
        log.record(task_type="ad", ensemble_result=result)


@pytest.mark.asyncio
async def test_promoter_calls_event_emitter_per_promotion() -> None:
    """每个 promotion → event_emitter 被 call."""
    log = ExperimentLog()
    _seed_log_for_promotion(log)

    captured: list = []

    async def fake_emitter(promo):
        captured.append(promo)

    promoter = RecipePromoter(log, min_total=10, event_emitter=fake_emitter)
    promotions = await promoter.promote_eligible()

    assert len(promotions) >= 1
    assert len(captured) == len(promotions)
    assert captured[0].strategy == "tier_top_low_temp"


@pytest.mark.asyncio
async def test_promoter_emits_rollback_on_dispatcher_failure() -> None:
    """dispatcher 抛 → rollback_emitter 被 call."""
    log = ExperimentLog()
    _seed_log_for_promotion(log)

    async def failing_dispatcher(promo):
        raise RuntimeError("upstream rejected")

    rollbacks: list[tuple] = []

    async def rollback_emitter(promo, *, reason="", error=""):
        rollbacks.append((promo, reason, error))

    promoter = RecipePromoter(
        log,
        min_total=10,
        precipitation_dispatcher=failing_dispatcher,
        rollback_emitter=rollback_emitter,
    )
    promotions = await promoter.promote_eligible()

    assert len(promotions) >= 1
    assert len(rollbacks) == len(promotions)
    _promo, reason, error = rollbacks[0]
    assert reason == "dispatcher_failed"
    assert "upstream rejected" in error


@pytest.mark.asyncio
async def test_promoter_no_rollback_when_dispatcher_succeeds() -> None:
    """dispatcher 不抛 → rollback_emitter 不调."""
    log = ExperimentLog()
    _seed_log_for_promotion(log)

    async def ok_dispatcher(promo):
        return None

    rollbacks: list = []

    async def rollback_emitter(promo, *, reason="", error=""):
        rollbacks.append(promo)

    promoter = RecipePromoter(
        log,
        min_total=10,
        precipitation_dispatcher=ok_dispatcher,
        rollback_emitter=rollback_emitter,
    )
    await promoter.promote_eligible()
    assert rollbacks == []


@pytest.mark.asyncio
async def test_promoter_emitter_failure_doesnt_break_promotion() -> None:
    """event_emitter 抛 → promotion 仍记录, history 仍更新."""
    log = ExperimentLog()
    _seed_log_for_promotion(log)

    async def failing_emitter(promo):
        raise RuntimeError("emitter dead")

    promoter = RecipePromoter(log, min_total=10, event_emitter=failing_emitter)
    promotions = await promoter.promote_eligible()

    assert len(promotions) >= 1
    assert len(promoter.get_promotions_history()) == len(promotions)
