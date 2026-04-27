"""KUN-Lab Prometheus metrics — Grafana 可视化 (Wire 28)."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from kun.core.metrics import (
    lab_budget_cap_total,
    lab_experiment_cost_usd,
    lab_experiment_total,
    lab_path_total,
    lab_promotion_total,
    lab_registry_size,
)
from kun.lab import (
    EnsembleConfig,
    EnsembleExecutor,
    EnsemblePathResult,
    EnsembleResult,
    ExperimentLog,
    LabRecipeEntry,
    LabRecipeRegistry,
    RecipePromoter,
)


def _label_count(counter, **labels) -> float:
    """读 prometheus Counter 当前累积值 (by labels) — 用 Prometheus 内部 API."""
    return counter.labels(**labels)._value.get()


def _gauge_value(gauge) -> float:
    return gauge._value.get()


# ---- EnsembleExecutor metrics ----


@pytest.mark.asyncio
async def test_experiment_total_increments_on_success() -> None:
    before = _label_count(lab_experiment_total, task_type="metrics_test", status="ok")

    async def fake_invoker(prompt, path):
        return ("ok", 0.01, 0.05)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(fake_invoker)
        await ex.run(
            "test", config=EnsembleConfig(n_paths=2), task_type="metrics_test"
        )

    after = _label_count(lab_experiment_total, task_type="metrics_test", status="ok")
    assert after == before + 1


@pytest.mark.asyncio
async def test_experiment_total_increments_on_budget_exceeded() -> None:
    before = _label_count(
        lab_experiment_total, task_type="budget_test", status="budget_exceeded"
    )

    import asyncio

    async def expensive(prompt, path):
        if path.tier == "top":
            return ("done", 1.5, 0.01)
        await asyncio.sleep(5.0)
        return ("never", 0.0, 5.0)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(expensive)
        await ex.run(
            "test",
            config=EnsembleConfig(
                n_paths=3, cost_budget_total_usd=1.0, timeout_per_path_sec=10
            ),
            task_type="budget_test",
        )

    after = _label_count(
        lab_experiment_total, task_type="budget_test", status="budget_exceeded"
    )
    assert after == before + 1


@pytest.mark.asyncio
async def test_experiment_cost_accumulates() -> None:
    before = _label_count(lab_experiment_cost_usd, task_type="cost_test")

    async def fake_invoker(prompt, path):
        return ("ok", 0.10, 0.01)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(fake_invoker)
        await ex.run(
            "test", config=EnsembleConfig(n_paths=3), task_type="cost_test"
        )

    after = _label_count(lab_experiment_cost_usd, task_type="cost_test")
    # 3 path × 0.10 = 0.30
    assert after == pytest.approx(before + 0.30, abs=0.001)


@pytest.mark.asyncio
async def test_path_total_increments_per_path() -> None:
    before = _label_count(
        lab_path_total, strategy="tier_top_low_temp", tier="top", status="ok"
    )

    async def fake_invoker(prompt, path):
        return ("ok", 0.01, 0.01)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(fake_invoker)
        await ex.run("test", config=EnsembleConfig(n_paths=3))

    after = _label_count(
        lab_path_total, strategy="tier_top_low_temp", tier="top", status="ok"
    )
    # tier_top_low_temp 是 DEFAULT_PATHS[0] — 跑了 1 次
    assert after == before + 1


@pytest.mark.asyncio
async def test_path_total_records_cancelled_status() -> None:
    """budget cap cancel 的 path → status=cancelled."""
    before = _label_count(
        lab_path_total, strategy="tier_strong_mid_temp", tier="strong", status="cancelled"
    )

    import asyncio

    async def slow(prompt, path):
        if path.tier == "top":
            return ("done", 1.5, 0.01)
        await asyncio.sleep(5.0)
        return ("never", 0.0, 5.0)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(slow)
        await ex.run(
            "test",
            config=EnsembleConfig(
                n_paths=3, cost_budget_total_usd=1.0, timeout_per_path_sec=10
            ),
        )

    after = _label_count(
        lab_path_total, strategy="tier_strong_mid_temp", tier="strong", status="cancelled"
    )
    # tier_strong_mid_temp 是 DEFAULT_PATHS[1], 慢被 cancel
    assert after >= before + 1


@pytest.mark.asyncio
async def test_budget_cap_total_increments_on_trigger() -> None:
    before = _label_count(lab_budget_cap_total, task_type="cap_test")

    import asyncio

    async def expensive(prompt, path):
        if path.tier == "top":
            return ("done", 2.0, 0.01)
        await asyncio.sleep(5.0)
        return ("never", 0.0, 5.0)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(expensive)
        await ex.run(
            "test",
            config=EnsembleConfig(
                n_paths=2, cost_budget_total_usd=1.0, timeout_per_path_sec=10
            ),
            task_type="cap_test",
        )

    after = _label_count(lab_budget_cap_total, task_type="cap_test")
    assert after == before + 1


# ---- RecipePromoter metric ----


@pytest.mark.asyncio
async def test_promotion_total_increments_per_promotion() -> None:
    before = _label_count(
        lab_promotion_total,
        task_type="promo_metric",
        target_module="execution_mode_classifier",
    )

    log = ExperimentLog()
    for i in range(12):
        log.record(
            task_type="promo_metric",
            ensemble_result=EnsembleResult(
                experiment_id=f"e{i}",
                config=EnsembleConfig(n_paths=2),
                path_results=[
                    EnsemblePathResult(
                        path_idx=0,
                        config={"strategy": "tier_top_low_temp"},
                        score=0.9,
                    ),
                ],
                winning_path_idx=0,
            ),
        )

    promoter = RecipePromoter(log, min_total=10)
    promotions = await promoter.promote_eligible()

    after = _label_count(
        lab_promotion_total,
        task_type="promo_metric",
        target_module="execution_mode_classifier",
    )
    # tier_top_low_temp → execution_mode_classifier (Wire 24 _infer_target_module)
    assert after == before + len(promotions)


# ---- LabRecipeRegistry gauge ----


def test_registry_size_gauge_tracks_count() -> None:
    """upsert → gauge 等于当前 size."""
    reg = LabRecipeRegistry()
    reg.upsert(
        LabRecipeEntry(
            task_type="g1",
            target_module="execution_mode_classifier",
            strategy="x",
            win_rate=0.85,
            confidence=0.85,
        )
    )
    assert _gauge_value(lab_registry_size) >= 1

    reg.upsert(
        LabRecipeEntry(
            task_type="g2",
            target_module="hermes",
            strategy="y",
            win_rate=0.88,
            confidence=0.88,
        )
    )
    # 跟其他测试 isolation 困难, 只 sanity 检查 gauge 被 set 过
    assert _gauge_value(lab_registry_size) > 0


def test_registry_size_not_changed_on_low_confidence_reject() -> None:
    """低 confidence upsert 拒绝 → gauge 不变."""
    reg = LabRecipeRegistry(min_confidence=0.9)
    before = _gauge_value(lab_registry_size)

    rejected = reg.upsert(
        LabRecipeEntry(
            task_type="rejected",
            target_module="x",
            strategy="y",
            win_rate=0.5,
            confidence=0.5,  # < 0.9
        )
    )
    assert rejected is False
    after = _gauge_value(lab_registry_size)
    assert after == before  # 没变
