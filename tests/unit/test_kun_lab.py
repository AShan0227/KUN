"""KUN-Lab MVP 单测 (V2.2 §26 / Wire 19, HEX 启发)."""

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
    RecipePromoter,
    get_experiment_log,
    reset_experiment_log,
)
from kun.lab.ensemble_executor import is_lab_enabled

# ---- env 隔离 ----


def test_lab_disabled_by_default() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_LAB_MODE", None)
        assert is_lab_enabled() is False


def test_lab_enabled_via_env() -> None:
    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        assert is_lab_enabled() is True


@pytest.mark.asyncio
async def test_executor_run_disabled_raises() -> None:
    """KUN_LAB_MODE=0 时 .run() 抛 RuntimeError."""

    async def fake_invoker(prompt, path):
        return ("output", 0.01, 1.0)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_LAB_MODE", None)
        ex = EnsembleExecutor(fake_invoker)
        with pytest.raises(RuntimeError, match="KUN-Lab disabled"):
            await ex.run("test prompt")


@pytest.mark.asyncio
async def test_executor_can_skip_lab_env_for_production_ensemble() -> None:
    """生产 ENSEMBLE 复用 executor, 但不要求用户打开 KUN_LAB_MODE."""

    async def fake_invoker(prompt, path):
        return (f"{path.strategy}:{prompt}", 0.01, 0.1)

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_LAB_MODE", None)
        ex = EnsembleExecutor(fake_invoker, require_lab_mode=False)
        result = await ex.run("test prompt", config=EnsembleConfig(n_paths=2))

    assert result.winning_output.startswith("tier_top_low_temp:")
    assert result.total_cost_usd == 0.02


# ---- EnsembleExecutor.run ----


@pytest.mark.asyncio
async def test_executor_runs_n_paths_and_picks_best_score() -> None:
    """ensemble 跑 N 路径, scoring_fn 选最高分."""

    call_count = 0

    async def fake_invoker(prompt, path):
        nonlocal call_count
        call_count += 1
        return (f"output_{path.tier}", 0.01, 0.5)

    async def fake_scorer(output, prompt):
        # tier=top 拿最高分
        return 0.95 if "top" in output else 0.5

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(fake_invoker)
        result = await ex.run(
            "test prompt",
            config=EnsembleConfig(n_paths=3, selection_method="best_score"),
            scoring_fn=fake_scorer,
        )

    assert call_count == 3  # 跑了 3 路径
    assert result.winning_path_idx >= 0
    assert "top" in result.winning_output  # tier=top 应该胜
    assert "best_score" in result.selection_reason


@pytest.mark.asyncio
async def test_executor_majority_vote() -> None:
    """3 路径中 2 个一样 → majority vote 选那个."""

    async def fake_invoker(prompt, path):
        # tier=top 和 tier=cheap 输出一样, tier=strong 不一样
        if path.tier in ("top", "cheap"):
            return ("answer_A", 0.01, 0.5)
        return ("answer_B", 0.01, 0.5)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(fake_invoker)
        result = await ex.run(
            "test",
            config=EnsembleConfig(n_paths=3, selection_method="majority_vote"),
        )

    assert result.winning_output == "answer_A"
    assert "majority_vote" in result.selection_reason


@pytest.mark.asyncio
async def test_executor_path_failure_doesnt_break_others() -> None:
    """一条路径失败 → 其他路径仍正常返."""

    async def fake_invoker(prompt, path):
        if path.tier == "strong":
            raise RuntimeError("simulated failure")
        return (f"output_{path.tier}", 0.01, 0.5)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(fake_invoker)
        result = await ex.run("test", config=EnsembleConfig(n_paths=3))

    # 1 个失败, 2 个成功
    error_paths = [r for r in result.path_results if r.error]
    success_paths = [r for r in result.path_results if not r.error]
    assert len(error_paths) == 1
    assert len(success_paths) == 2


@pytest.mark.asyncio
async def test_executor_total_cost_aggregated() -> None:
    async def fake_invoker(prompt, path):
        return ("x", 0.10, 0.5)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(fake_invoker)
        result = await ex.run("t", config=EnsembleConfig(n_paths=3))

    assert abs(result.total_cost_usd - 0.30) < 1e-9


# ---- ExperimentLog ----


def test_experiment_log_record_and_query() -> None:
    log = ExperimentLog()
    fake_result = EnsembleResult(
        experiment_id="exp-1",
        config=EnsembleConfig(n_paths=2),
        path_results=[
            EnsemblePathResult(path_idx=0, config={"strategy": "tier_top_low_temp"}, score=0.9),
            EnsemblePathResult(path_idx=1, config={"strategy": "tier_cheap_high_temp"}, score=0.3),
        ],
        winning_path_idx=0,
        winning_output="best",
    )
    log.record(task_type="marketing.copy", ensemble_result=fake_result)
    assert len(log.list_all()) == 1
    by_type = log.by_task_type("marketing.copy")
    assert len(by_type) == 1


def test_experiment_log_recipe_stats_aggregates() -> None:
    log = ExperimentLog()
    # 跑 3 个实验, tier_top 总赢
    for i in range(3):
        result = EnsembleResult(
            experiment_id=f"exp-{i}",
            config=EnsembleConfig(n_paths=2),
            path_results=[
                EnsemblePathResult(path_idx=0, config={"strategy": "tier_top_low_temp"}, score=0.9),
                EnsemblePathResult(
                    path_idx=1, config={"strategy": "tier_cheap_high_temp"}, score=0.3
                ),
            ],
            winning_path_idx=0,  # tier_top 赢
        )
        log.record(task_type="ad", ensemble_result=result)

    stats = log.recipe_stats(task_type="ad")
    top_stat = next(s for s in stats if s.strategy == "tier_top_low_temp")
    assert top_stat.win_count == 3
    assert top_stat.total_count == 3
    assert top_stat.win_rate == 1.0


def test_experiment_log_best_recipe_for() -> None:
    log = ExperimentLog()
    fake_result = EnsembleResult(
        experiment_id="exp-1",
        config=EnsembleConfig(n_paths=2),
        path_results=[
            EnsemblePathResult(path_idx=0, config={"strategy": "tier_strong_mid_temp"}, score=0.8),
            EnsemblePathResult(path_idx=1, config={"strategy": "tier_cheap_high_temp"}, score=0.3),
        ],
        winning_path_idx=0,
    )
    log.record(task_type="biz_plan", ensemble_result=fake_result)
    best = log.best_recipe_for("biz_plan")
    assert best is not None
    assert best.strategy == "tier_strong_mid_temp"


def test_experiment_log_total_cost() -> None:
    log = ExperimentLog()
    log.record(
        task_type="x",
        ensemble_result=EnsembleResult(
            experiment_id="x",
            config=EnsembleConfig(n_paths=2),
            path_results=[],
            total_cost_usd=0.50,
        ),
    )
    log.record(
        task_type="y",
        ensemble_result=EnsembleResult(
            experiment_id="y",
            config=EnsembleConfig(n_paths=2),
            path_results=[],
            total_cost_usd=0.30,
        ),
    )
    assert abs(log.total_lab_cost_usd() - 0.80) < 1e-9


# ---- RecipePromoter ----


@pytest.mark.asyncio
async def test_promoter_eligible_filter() -> None:
    log = ExperimentLog()
    # 加 12 个实验 tier_top 赢 80% (≥ min_total=10 + ≥ 0.6 winrate)
    for i in range(12):
        result = EnsembleResult(
            experiment_id=f"e{i}",
            config=EnsembleConfig(n_paths=2),
            path_results=[
                EnsemblePathResult(path_idx=0, config={"strategy": "tier_top_low_temp"}, score=0.9),
                EnsemblePathResult(
                    path_idx=1, config={"strategy": "tier_cheap_high_temp"}, score=0.3
                ),
            ],
            winning_path_idx=0 if i < 10 else 1,  # 10/12 = 83% top 赢
        )
        log.record(task_type="biz", ensemble_result=result)

    promoter = RecipePromoter(log, min_total=10, min_winrate=0.6)
    eligible = promoter.find_eligible_recipes()
    # tier_top 10 wins / 12 total → 0.83 win_rate → eligible
    top_eligible = [s for s in eligible if s.strategy == "tier_top_low_temp"]
    assert len(top_eligible) == 1
    assert top_eligible[0].win_rate > 0.8


@pytest.mark.asyncio
async def test_promoter_below_threshold_not_eligible() -> None:
    log = ExperimentLog()
    # 加 5 个实验 (min_total=10 不满足)
    for i in range(5):
        log.record(
            task_type="x",
            ensemble_result=EnsembleResult(
                experiment_id=f"e{i}",
                config=EnsembleConfig(n_paths=2),
                path_results=[
                    EnsemblePathResult(path_idx=0, config={"strategy": "x"}, score=0.9),
                ],
                winning_path_idx=0,
            ),
        )
    promoter = RecipePromoter(log, min_total=10)
    assert promoter.find_eligible_recipes() == []


@pytest.mark.asyncio
async def test_promoter_dispatch_to_precipitation() -> None:
    """promote_eligible 调 dispatcher (mock)."""
    log = ExperimentLog()
    for _ in range(15):
        log.record(
            task_type="ad",
            ensemble_result=EnsembleResult(
                experiment_id="e",
                config=EnsembleConfig(n_paths=2),
                path_results=[
                    EnsemblePathResult(
                        path_idx=0, config={"strategy": "tier_top_low_temp"}, score=0.9
                    ),
                ],
                winning_path_idx=0,
            ),
        )

    captured = []

    async def fake_dispatcher(promotion):
        captured.append(promotion)

    promoter = RecipePromoter(log, min_total=10, precipitation_dispatcher=fake_dispatcher)
    promotions = await promoter.promote_eligible()
    assert len(promotions) >= 1
    assert len(captured) == len(promotions)


@pytest.mark.asyncio
async def test_promoter_dedup_within_window() -> None:
    """同 (task_type, strategy) 1 周内不重复推."""
    log = ExperimentLog()
    for _ in range(15):
        log.record(
            task_type="ad",
            ensemble_result=EnsembleResult(
                experiment_id="e",
                config=EnsembleConfig(n_paths=2),
                path_results=[
                    EnsemblePathResult(
                        path_idx=0, config={"strategy": "tier_top_low_temp"}, score=0.9
                    ),
                ],
                winning_path_idx=0,
            ),
        )
    promoter = RecipePromoter(log, min_total=10)
    p1 = await promoter.promote_eligible()
    p2 = await promoter.promote_eligible()  # 重复跑 → 应该 dedup
    assert len(p1) >= 1
    assert len(p2) == 0


# ---- 集成 + singleton ----


def test_get_experiment_log_singleton() -> None:
    reset_experiment_log()
    a = get_experiment_log()
    b = get_experiment_log()
    assert a is b


def test_reset_experiment_log() -> None:
    reset_experiment_log()
    a = get_experiment_log()
    reset_experiment_log()
    b = get_experiment_log()
    assert a is not b
