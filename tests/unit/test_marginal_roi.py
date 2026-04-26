"""Marginal ROI stop criterion 单测 (V2.2 §19.2)."""

from __future__ import annotations

from kun.engineering.marginal_roi import (
    MarginalROIStopCriterion,
    ModulePresets,
    ValueEstimator,
)

# ---- 基础行为 ----


def test_no_history_no_stop() -> None:
    c = MarginalROIStopCriterion()
    d = c.should_stop([])
    assert d.should_stop is False
    assert d.reason == "no_history"


def test_below_min_steps_no_stop() -> None:
    c = MarginalROIStopCriterion(min_steps=3)
    d = c.should_stop([0.5])
    assert d.should_stop is False
    assert d.reason == "below_min_steps"


def test_still_improving_no_stop() -> None:
    c = MarginalROIStopCriterion(delta_threshold=0.05, window_k=2)
    d = c.should_stop([0.3, 0.5, 0.7])  # 每步 +0.2
    assert d.should_stop is False
    assert d.reason == "still_improving"
    assert abs(d.last_marginal - 0.2) < 1e-9


def test_marginal_below_threshold_stops() -> None:
    c = MarginalROIStopCriterion(delta_threshold=0.05, window_k=2)
    d = c.should_stop([0.5, 0.6, 0.62, 0.63])  # 后两步 +0.02 / +0.01 都 < 0.05
    assert d.should_stop is True
    assert d.reason == "marginal_below_threshold"


def test_single_window_below_threshold() -> None:
    c = MarginalROIStopCriterion(delta_threshold=0.05, window_k=1)
    d = c.should_stop([0.5, 0.51])  # 单步 +0.01 < 0.05
    assert d.should_stop is True


def test_window_k_partial_below_no_stop() -> None:
    """window=2, 一个 < threshold, 另一个 ≥ threshold → 不停."""
    c = MarginalROIStopCriterion(delta_threshold=0.05, window_k=2)
    d = c.should_stop([0.3, 0.4, 0.41])  # 倒数 2 个: +0.1, +0.01
    assert d.should_stop is False  # 倒数第 2 步 +0.1 ≥ threshold


def test_below_absolute_floor_stops() -> None:
    c = MarginalROIStopCriterion(absolute_floor=0.3, delta_threshold=0.05)
    d = c.should_stop([0.4, 0.45, 0.2])  # 当前 0.2 < floor 0.3
    assert d.should_stop is True
    assert d.reason == "below_absolute_floor"


def test_above_floor_uses_marginal_check() -> None:
    c = MarginalROIStopCriterion(absolute_floor=0.1, delta_threshold=0.05, window_k=1)
    d = c.should_stop([0.5, 0.51])
    assert d.should_stop is True
    assert d.reason == "marginal_below_threshold"


# ---- 输入校验 ----


def test_invalid_window_k_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        MarginalROIStopCriterion(window_k=0)


def test_invalid_min_steps_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        MarginalROIStopCriterion(min_steps=0)


def test_invalid_delta_threshold_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        MarginalROIStopCriterion(delta_threshold=1.5)


# ---- ValueEstimator ----


def test_value_estimator_dict_quality() -> None:
    ve = ValueEstimator(strategy="cumulative_quality")
    assert ve.estimate({"confidence": 0.7}, []) == 0.7
    assert ve.estimate({"quality": 0.5}, []) == 0.5
    assert ve.estimate({"score": 0.3}, []) == 0.3


def test_value_estimator_default_quality() -> None:
    ve = ValueEstimator(strategy="cumulative_quality")
    assert ve.estimate({}, []) == 0.5  # 默认


def test_value_estimator_attr_quality() -> None:
    class Item:
        confidence = 0.85

    ve = ValueEstimator(strategy="cumulative_quality")
    assert ve.estimate(Item(), []) == 0.85


def test_value_estimator_distinct_information() -> None:
    ve = ValueEstimator(strategy="distinct_information")
    # 跟 prior 完全重复 → value 低
    assert ve.estimate("foo", ["foo", "foo"]) == 0.0
    # 跟 prior 一半重复 → 0.5
    assert ve.estimate("foo", ["foo", "bar"]) == 0.5
    # 全新 → 1.0
    assert ve.estimate("baz", ["foo", "bar"]) == 1.0
    # 无 prior → 1.0
    assert ve.estimate("foo", []) == 1.0


def test_value_estimator_exponential_decay() -> None:
    ve = ValueEstimator(strategy="exponential_decay", decay_scale=5.0)
    # N=1: 1 - exp(-1/5) ≈ 0.18
    v1 = ve.estimate("x", [])
    # N=10: 1 - exp(-10/5) ≈ 0.86
    v10 = ve.estimate("x", ["a"] * 9)
    assert 0.15 < v1 < 0.22
    assert 0.83 < v10 < 0.88
    assert v10 > v1  # 单调递增


def test_value_estimator_custom_fn() -> None:
    ve = ValueEstimator(custom_fn=lambda cur, prior: float(len(str(cur))))
    assert ve.estimate("hi", []) == 2.0
    assert ve.estimate("hello", []) == 5.0


def test_value_estimator_unknown_strategy_raises() -> None:
    import pytest

    ve = ValueEstimator(strategy="bogus")
    with pytest.raises(ValueError):
        ve.estimate("x", [])


# ---- 模块预设 ----


def test_preset_for_multi_judge() -> None:
    c = ModulePresets.for_multi_judge()
    assert c.delta_threshold == 0.03
    assert c.window_k == 2


def test_preset_for_memory_expand() -> None:
    c = ModulePresets.for_memory_expand()
    assert c.delta_threshold == 0.10
    assert c.window_k == 1


def test_preset_for_idle_batch_step() -> None:
    c = ModulePresets.for_idle_batch_step()
    assert c.min_steps == 3


def test_preset_for_external_scan() -> None:
    c = ModulePresets.for_external_scan()
    assert c.delta_threshold == 0.30


def test_preset_for_multi_agent() -> None:
    c = ModulePresets.for_multi_agent()
    assert c.min_steps == 2


def test_preset_for_search_pages() -> None:
    c = ModulePresets.for_search_pages()
    assert c.delta_threshold == 0.20


# ---- 集成场景: 模拟 multi_judge 用 ----


def test_realistic_multi_judge_scenario_stops_at_consensus() -> None:
    """模拟 multi_judge: 5 个 judge 一致率 0.6 → 0.7 → 0.85 → 0.86 → 0.86, 第 4 步该停."""
    c = ModulePresets.for_multi_judge()
    consensus_history = [0.6, 0.7, 0.85, 0.86, 0.86]
    d = c.should_stop(consensus_history)
    assert d.should_stop is True
    assert d.reason == "marginal_below_threshold"


def test_realistic_memory_expand_continues_when_helpful() -> None:
    """模拟拉记忆: 拉了第 2 条 score 提升 0.15 → 不停, 继续."""
    c = ModulePresets.for_memory_expand()
    score_history = [0.5, 0.65]  # +0.15 > 0.10 threshold
    d = c.should_stop(score_history)
    assert d.should_stop is False


def test_realistic_external_scan_stops_on_overlap() -> None:
    """模拟扫多源: 第 2 个源 information distinct=0.6 (跟前 60% overlap), <0.30 实际是反向 — overlap 高 distinct 低."""
    # ValueEstimator(distinct) 返 distinct 分, 高表示新, 低表示重复
    # 这个场景: 第 2 个源 distinct=0.2 (重复多)
    c = ModulePresets.for_external_scan()
    # 单步评估: distinct 从 1.0 → 0.2, 边际 = -0.8 < 0.30 → 停
    distinct_history = [1.0, 0.2]
    d = c.should_stop(distinct_history)
    assert d.should_stop is True


def test_realistic_idle_batch_run_to_min_then_eval() -> None:
    """模拟 idle-batch: 前 min_steps=3 步必跑, 第 4 步才能停."""
    c = ModulePresets.for_idle_batch_step()
    d2 = c.should_stop([0.5, 0.55])
    assert d2.should_stop is False
    assert d2.reason == "below_min_steps"
    # 第 3 步: n=3 == min_steps, 开始评估; 后 2 步 deltas=[0.05, 0.0]
    # 0.05 不 <0.05, 0.0 <0.05 → 不全部低 → 仍 improving
    d3 = c.should_stop([0.5, 0.55, 0.55])
    assert d3.should_stop is False
    assert d3.reason == "still_improving"
    # 第 4 步开始评估, 后两步 +0.0 / +0.0 都 < 0.05 → 停
    d4 = c.should_stop([0.5, 0.55, 0.55, 0.55])
    assert d4.should_stop is True
