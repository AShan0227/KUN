"""EnsembleExecutor cost-cap hard 执行 (Wire 27)."""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest
from kun.lab import EnsembleConfig, EnsembleExecutor


@pytest.mark.asyncio
async def test_under_budget_no_cancel() -> None:
    """累积 cost < budget → 全跑完, budget_exceeded=False."""

    async def cheap_invoker(prompt, path):
        return ("ok", 0.05, 0.01)  # 5 path × 0.05 = 0.25, 远低于 1.0

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(cheap_invoker)
        result = await ex.run(
            "test",
            config=EnsembleConfig(n_paths=5, cost_budget_total_usd=1.0),
        )

    assert result.budget_exceeded is False
    assert result.budget_cancelled_count == 0
    assert all(not pr.error for pr in result.path_results)
    assert result.total_cost_usd == pytest.approx(0.25, abs=0.001)


@pytest.mark.asyncio
async def test_over_budget_cancels_remaining_paths() -> None:
    """累积 cost 超 budget → 剩余 path 被 cancel, error='cancelled_budget_exceeded'."""

    # 让前几条快 + 贵 (单个就超 budget), 后几条慢 (会被 cancel)
    call_order = 0
    started_paths: set[int] = set()

    async def expensive_then_slow(prompt, path):
        nonlocal call_order
        idx = call_order
        call_order += 1
        started_paths.add(idx)
        if idx < 3:
            await asyncio.sleep(0.01)  # 前 3 条快
            return ("done", 0.5, 0.01)  # 累积 1.5 > budget 1.0
        # 后 2 条慢, 应该被 cancel
        await asyncio.sleep(5.0)
        return ("never", 0.5, 5.0)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(expensive_then_slow)
        result = await ex.run(
            "test",
            config=EnsembleConfig(
                n_paths=5,
                cost_budget_total_usd=1.0,
                timeout_per_path_sec=10,
            ),
        )

    assert result.budget_exceeded is True
    assert result.budget_cancelled_count >= 1
    cancelled = [pr for pr in result.path_results if pr.error == "cancelled_budget_exceeded"]
    assert len(cancelled) == result.budget_cancelled_count


@pytest.mark.asyncio
async def test_budget_cancel_keeps_completed_results() -> None:
    """cost cap 触发 → 已完成 path 仍保留 (output / score), 只 cancel 未完成."""

    async def fast_then_slow(prompt, path):
        if path.tier == "top":
            return ("done_top", 0.6, 0.01)  # 完成 → cost 0.6
        # 其他全慢
        await asyncio.sleep(5.0)
        return ("never", 0.6, 5.0)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(fast_then_slow)
        result = await ex.run(
            "test",
            config=EnsembleConfig(
                n_paths=5,
                cost_budget_total_usd=0.5,  # 0.6 单条已经超
                timeout_per_path_sec=10,
            ),
        )

    completed = [pr for pr in result.path_results if not pr.error]
    cancelled = [pr for pr in result.path_results if pr.error == "cancelled_budget_exceeded"]
    assert len(completed) >= 1
    assert "done_top" in completed[0].output
    assert len(cancelled) >= 1
    assert result.budget_exceeded is True


@pytest.mark.asyncio
async def test_budget_exceeded_field_default_false() -> None:
    """普通运行 result.budget_exceeded 默认 False."""

    async def normal_invoker(prompt, path):
        return ("ok", 0.01, 0.01)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(normal_invoker)
        result = await ex.run("t", config=EnsembleConfig(n_paths=2))

    assert result.budget_exceeded is False
    assert result.budget_cancelled_count == 0


@pytest.mark.asyncio
async def test_budget_cancelled_path_has_full_config() -> None:
    """被 cancel 的 path 仍带 strategy/tier/temperature config (不是空)."""

    async def slow_invoker(prompt, path):
        if path.tier == "top":
            return ("done", 1.5, 0.01)
        await asyncio.sleep(5.0)
        return ("never", 0.0, 5.0)

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(slow_invoker)
        result = await ex.run(
            "t",
            config=EnsembleConfig(n_paths=3, cost_budget_total_usd=1.0),
        )

    cancelled = [pr for pr in result.path_results if pr.error == "cancelled_budget_exceeded"]
    assert len(cancelled) >= 1
    for pr in cancelled:
        assert "strategy" in pr.config
        assert "tier" in pr.config
        assert "temperature" in pr.config


@pytest.mark.asyncio
async def test_winner_selection_skips_cancelled_paths() -> None:
    """winner 从未 cancel 的 path 选, 不会选到 cancelled."""

    async def mixed(prompt, path):
        if path.tier == "top":
            return ("winner_text", 1.5, 0.01)
        await asyncio.sleep(5.0)
        return ("never", 0.0, 5.0)

    async def fake_score(output, prompt):
        return 0.9 if "winner" in output else 0.1

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(mixed)
        result = await ex.run(
            "t",
            config=EnsembleConfig(
                n_paths=3,
                cost_budget_total_usd=1.0,
                selection_method="best_score",
            ),
            scoring_fn=fake_score,
        )

    assert result.budget_exceeded is True
    assert "winner" in result.winning_output
    winner_pr = result.path_results[result.winning_path_idx]
    assert winner_pr.error == ""  # 不会选 cancelled


@pytest.mark.asyncio
async def test_budget_just_at_threshold_no_cancel() -> None:
    """累积 cost == budget (不超) → 不触发 cancel."""

    async def exact_invoker(prompt, path):
        return ("ok", 0.5, 0.01)  # 2 path × 0.5 = 1.0, 等于 budget

    with patch.dict(os.environ, {"KUN_LAB_MODE": "1"}):
        ex = EnsembleExecutor(exact_invoker)
        result = await ex.run(
            "t",
            config=EnsembleConfig(n_paths=2, cost_budget_total_usd=1.0),
        )

    assert result.budget_exceeded is False
    assert result.total_cost_usd == pytest.approx(1.0)
