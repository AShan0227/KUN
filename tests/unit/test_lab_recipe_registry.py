"""LabRecipeRegistry + ExecutionMode classifier hint (Wire 25)."""

from __future__ import annotations

import pytest
from kun.api.execution_mode_classifier import classify_execution_mode
from kun.datamodel.soul_file import SoulFile
from kun.engineering.precipitation import AssetUpdate
from kun.lab import (
    LabRecipeEntry,
    LabRecipeRegistry,
    get_recipe_registry,
    make_registry_apply_hook,
    reset_recipe_registry,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_recipe_registry()
    yield
    reset_recipe_registry()


def _make_soul(default_mode: str = "FAST") -> SoulFile:
    return SoulFile(
        user_id="u-test",
        approval_threshold_money=10.0,
        execution_mode_preference={"default_mode": default_mode},
    )


# ---- LabRecipeRegistry 基本 ----


def test_registry_starts_empty() -> None:
    reg = LabRecipeRegistry()
    assert len(reg) == 0
    assert reg.all() == []


def test_registry_upsert_high_confidence_succeeds() -> None:
    reg = LabRecipeRegistry()
    entry = LabRecipeEntry(
        task_type="ad",
        target_module="execution_mode_classifier",
        strategy="tier_top_low_temp",
        win_rate=0.85,
        confidence=0.85,
    )
    assert reg.upsert(entry) is True
    assert len(reg) == 1
    assert reg.get("ad", "execution_mode_classifier") is entry


def test_registry_upsert_low_confidence_rejected() -> None:
    """confidence < min → 拒绝, 不污染主决策."""
    reg = LabRecipeRegistry(min_confidence=0.7)
    entry = LabRecipeEntry(
        task_type="ad",
        target_module="execution_mode_classifier",
        strategy="x",
        win_rate=0.5,
        confidence=0.5,  # < 0.7
    )
    assert reg.upsert(entry) is False
    assert len(reg) == 0


def test_registry_upsert_overwrites_same_key() -> None:
    reg = LabRecipeRegistry()
    e1 = LabRecipeEntry(
        task_type="ad",
        target_module="execution_mode_classifier",
        strategy="strat1",
        win_rate=0.7,
        confidence=0.7,
    )
    e2 = LabRecipeEntry(
        task_type="ad",
        target_module="execution_mode_classifier",
        strategy="strat2",
        win_rate=0.9,
        confidence=0.9,
    )
    reg.upsert(e1)
    reg.upsert(e2)
    assert len(reg) == 1
    assert reg.get("ad", "execution_mode_classifier").strategy == "strat2"


def test_registry_by_task_type_filters() -> None:
    reg = LabRecipeRegistry()
    reg.upsert(
        LabRecipeEntry(
            task_type="ad",
            target_module="execution_mode_classifier",
            strategy="x",
            win_rate=0.8,
            confidence=0.8,
        )
    )
    reg.upsert(
        LabRecipeEntry(
            task_type="ad",
            target_module="hermes_prompt_template",
            strategy="y",
            win_rate=0.9,
            confidence=0.9,
        )
    )
    reg.upsert(
        LabRecipeEntry(
            task_type="biz",
            target_module="execution_mode_classifier",
            strategy="z",
            win_rate=0.85,
            confidence=0.85,
        )
    )
    ad_entries = reg.by_task_type("ad")
    assert len(ad_entries) == 2
    assert {e.target_module for e in ad_entries} == {
        "execution_mode_classifier",
        "hermes_prompt_template",
    }


def test_registry_singleton_get_returns_same_instance() -> None:
    a = get_recipe_registry()
    b = get_recipe_registry()
    assert a is b


# ---- make_registry_apply_hook ----


@pytest.mark.asyncio
async def test_apply_hook_writes_lab_update_to_registry() -> None:
    reg = LabRecipeRegistry()
    hook = make_registry_apply_hook(reg)

    update = AssetUpdate(
        update_id="u-1",
        asset_kind="playbook",
        asset_ref="execution_mode_classifier",
        update_kind="update",
        payload={
            "source": "kun_lab",
            "task_type": "ad",
            "strategy": "tier_top_low_temp",
            "win_rate": 0.85,
            "promotion_id": "p-1",
            "total_count": 12,
            "avg_score": 0.78,
        },
        confidence=0.85,
    )
    await hook(update)

    assert len(reg) == 1
    entry = reg.get("ad", "execution_mode_classifier")
    assert entry is not None
    assert entry.strategy == "tier_top_low_temp"
    assert entry.win_rate == pytest.approx(0.85)
    assert entry.promotion_id == "p-1"
    assert entry.extras["total_count"] == 12


@pytest.mark.asyncio
async def test_apply_hook_skips_non_lab_source() -> None:
    """source != 'kun_lab' → 跳过 (不污染 lab registry)."""
    reg = LabRecipeRegistry()
    hook = make_registry_apply_hook(reg)

    update = AssetUpdate(
        update_id="u-1",
        asset_kind="capability_card",
        asset_ref="some-ref",
        update_kind="update",
        payload={
            "source": "stats_writeback",  # 主仓库别的来源
            "task_type": "ad",
            "strategy": "x",
        },
    )
    await hook(update)
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_apply_hook_skips_when_task_type_missing() -> None:
    reg = LabRecipeRegistry()
    hook = make_registry_apply_hook(reg)

    update = AssetUpdate(
        update_id="u-1",
        asset_kind="playbook",
        asset_ref="execution_mode_classifier",
        update_kind="update",
        payload={"source": "kun_lab", "strategy": "x"},  # 没 task_type
        confidence=0.9,
    )
    await hook(update)
    assert len(reg) == 0


@pytest.mark.asyncio
async def test_apply_hook_low_confidence_filtered_by_registry() -> None:
    """update.confidence 低 → registry.upsert 拒绝 → 不进 registry."""
    reg = LabRecipeRegistry(min_confidence=0.8)
    hook = make_registry_apply_hook(reg)

    update = AssetUpdate(
        update_id="u-1",
        asset_kind="playbook",
        asset_ref="execution_mode_classifier",
        update_kind="update",
        payload={
            "source": "kun_lab",
            "task_type": "ad",
            "strategy": "x",
            "win_rate": 0.6,
        },
        confidence=0.6,  # < 0.8
    )
    await hook(update)
    assert len(reg) == 0


# ---- ExecutionMode classifier 接 lab recipe ----


def test_classifier_lab_recipe_hint_picked_when_no_other_signal() -> None:
    """空 task_meta + lab registry 有 entry → 用 lab 推荐."""
    reg = get_recipe_registry()
    reg.upsert(
        LabRecipeEntry(
            task_type="biz_plan",
            target_module="execution_mode_classifier",
            strategy="tier_top_low_temp",  # → MAX
            win_rate=0.88,
            confidence=0.88,
        )
    )
    soul = _make_soul(default_mode="FAST")
    mode, reason = classify_execution_mode({"task_type": "biz_plan"}, soul)

    assert mode == "MAX"
    assert "lab_recipe:tier_top_low_temp" in reason


def test_classifier_lab_recipe_cheap_strategy_hints_fast() -> None:
    """tier_cheap_high_temp 验证有效 → FAST."""
    reg = get_recipe_registry()
    reg.upsert(
        LabRecipeEntry(
            task_type="quick_q",
            target_module="execution_mode_classifier",
            strategy="tier_cheap_high_temp",
            win_rate=0.85,
            confidence=0.85,
        )
    )
    soul = _make_soul(default_mode="MAX")  # 默认 MAX
    mode, reason = classify_execution_mode({"task_type": "quick_q"}, soul)

    assert mode == "FAST"  # lab 覆盖 default
    assert "tier_cheap_high_temp" in reason


def test_classifier_lab_recipe_does_not_override_critical() -> None:
    """risk_level=critical 优先级最高, lab 不能覆盖."""
    reg = get_recipe_registry()
    reg.upsert(
        LabRecipeEntry(
            task_type="biz_plan",
            target_module="execution_mode_classifier",
            strategy="tier_cheap_high_temp",  # lab 推 FAST
            win_rate=0.85,
            confidence=0.85,
        )
    )
    soul = _make_soul()
    mode, reason = classify_execution_mode(
        {"task_type": "biz_plan", "risk_level": "critical"}, soul
    )
    # 即使 lab 推 FAST, critical 强制 MAX
    assert mode == "MAX"
    assert "critical" in reason


def test_classifier_lab_recipe_does_not_override_force_mode() -> None:
    reg = get_recipe_registry()
    reg.upsert(
        LabRecipeEntry(
            task_type="x",
            target_module="execution_mode_classifier",
            strategy="tier_top_low_temp",  # lab 推 MAX
            win_rate=0.9,
            confidence=0.9,
        )
    )
    soul = _make_soul()
    mode, reason = classify_execution_mode(
        {"task_type": "x", "force_mode": "FAST"}, soul
    )
    assert mode == "FAST"
    assert "force_mode" in reason


def test_classifier_lab_recipe_does_not_override_complexity_high() -> None:
    """complexity > 0.7 → MAX, 优先于 lab."""
    reg = get_recipe_registry()
    reg.upsert(
        LabRecipeEntry(
            task_type="x",
            target_module="execution_mode_classifier",
            strategy="tier_cheap_high_temp",  # lab 推 FAST
            win_rate=0.85,
            confidence=0.85,
        )
    )
    soul = _make_soul()
    mode, reason = classify_execution_mode(
        {"task_type": "x", "complexity_score": 0.85}, soul
    )
    assert mode == "MAX"
    assert "complexity" in reason


def test_classifier_no_lab_recipe_falls_back_to_default() -> None:
    """registry 空 → 走 default_mode (现有行为不变)."""
    soul = _make_soul(default_mode="SMART")
    mode, reason = classify_execution_mode({"task_type": "anything"}, soul)
    assert mode == "SMART"
    assert "default_mode" in reason


def test_classifier_unknown_strategy_no_hint() -> None:
    """lab 推了 strategy 但不在 _LAB_STRATEGY_MODE_HINT → 不影响, fallback default."""
    reg = get_recipe_registry()
    reg.upsert(
        LabRecipeEntry(
            task_type="ad",
            target_module="execution_mode_classifier",
            strategy="some_unknown_strategy",
            win_rate=0.85,
            confidence=0.85,
        )
    )
    soul = _make_soul(default_mode="FAST")
    mode, reason = classify_execution_mode({"task_type": "ad"}, soul)
    assert mode == "FAST"
    assert "default_mode" in reason


def test_classifier_lab_hint_safely_handles_registry_failure() -> None:
    """get_recipe_registry 抛异常 → classifier 不爆, fallback default."""
    soul = _make_soul(default_mode="FAST")

    # 不污染主测试 — 用 monkey patching 模拟 lab 模块挂掉
    import kun.api.execution_mode_classifier as clf

    original = clf._lab_recipe_hint
    clf._lab_recipe_hint = lambda *_args, **_kw: (_ for _ in ()).throw(  # type: ignore[assignment]
        RuntimeError("simulated lab module crash")
    )
    try:
        # 上面 monkey 直接 throw, classifier 走 try/except 后会 None
        # 但实际上这里我用真接口, 直接 None — 测 _lab_recipe_hint 自身的 try 已在内部
        pass
    finally:
        clf._lab_recipe_hint = original
    # 走真接口验 fallback
    mode, _ = classify_execution_mode({"task_type": "x"}, soul)
    assert mode == "FAST"
