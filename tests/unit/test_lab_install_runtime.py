"""install_runtime 装上 lab 闭环 (Wire 26)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from kun.api.runtime import install_runtime
from kun.engineering.idle_batch import _steps as _idle_steps
from kun.engineering.idle_batch import list_steps
from kun.lab import (
    LabRecipePrecipitationStep,
    reset_adoption_step,
    reset_recipe_registry,
)
from kun.watchtower.engine import RuleEngine
from kun.watchtower.rules import GuardRule, RuleTrigger
from starlette.datastructures import State


def _fresh_app():
    return SimpleNamespace(state=State())


def _empty_engine() -> RuleEngine:
    rule = GuardRule(
        id="noop",
        kind="guard",
        trigger=RuleTrigger(event_type="*", when="True"),
    )
    return RuleEngine([rule])


def _cleanup_lab():
    """每个测试间清 lab singletons + idle_batch registry."""
    reset_adoption_step()
    reset_recipe_registry()
    _idle_steps.pop("lab_recipe_adoption", None)


def test_lab_bridge_disabled_by_default() -> None:
    """env 没设 → lab 闭环不装, 不影响主仓库行为."""
    _cleanup_lab()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_LAB_BRIDGE_ENABLED", None)
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())

    # lab_recipe_registry 不在 app.state
    assert not hasattr(app.state, "lab_recipe_registry")
    # idle_batch 没多 lab_recipe_adoption step
    assert "lab_recipe_adoption" not in list_steps()
    # KP 只装了 4 个内置 step (没装 LabRecipePrecipitationStep)
    kp = app.state.knowledge_precipitation
    lab_steps = [s for s in kp._steps if isinstance(s, LabRecipePrecipitationStep)]
    assert lab_steps == []
    _cleanup_lab()


def test_lab_bridge_enabled_installs_full_loop() -> None:
    """KUN_LAB_BRIDGE_ENABLED=1 → registry / hook / idle_batch step / KP step 全装."""
    _cleanup_lab()
    with patch.dict(os.environ, {"KUN_LAB_BRIDGE_ENABLED": "1"}):
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())

    # registry 在 app.state
    assert hasattr(app.state, "lab_recipe_registry")
    assert app.state.lab_recipe_registry is not None

    # idle_batch 装了 lab_recipe_adoption step
    assert "lab_recipe_adoption" in list_steps()

    # KP 装了 LabRecipePrecipitationStep
    kp = app.state.knowledge_precipitation
    lab_steps = [s for s in kp._steps if isinstance(s, LabRecipePrecipitationStep)]
    assert len(lab_steps) == 1

    # KP._asset_apply_hook 装了 (lab registry hook)
    assert kp._asset_apply_hook is not None
    _cleanup_lab()


def test_lab_bridge_end_to_end_after_install_runtime() -> None:
    """install_runtime 装完 → emit experiment.promoted → registry 真收到."""
    import asyncio

    from kun.engineering.precipitation import PrecipitationEvent

    _cleanup_lab()
    with patch.dict(os.environ, {"KUN_LAB_BRIDGE_ENABLED": "1"}):
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())

        kp = app.state.knowledge_precipitation
        registry = app.state.lab_recipe_registry

        event = PrecipitationEvent(
            event_id="prom-end2end",
            event_type="experiment.promoted",
            payload={
                "promotion_id": "prom-end2end",
                "task_type": "ad_creative",
                "strategy": "tier_top_low_temp",
                "win_rate": 0.85,
                "total_count": 12,
                "avg_score": 0.78,
                "avg_cost_usd": 0.04,
                "target_module": "execution_mode_classifier",
            },
        )

        async def _go():
            return await kp.dispatch(event)

        updates = asyncio.run(_go())

    # KP 产了 1 个 AssetUpdate
    assert len(updates) == 1
    # registry 真收到了 (走 apply_hook)
    entry = registry.get("ad_creative", "execution_mode_classifier")
    assert entry is not None
    assert entry.strategy == "tier_top_low_temp"
    assert entry.win_rate == 0.85
    _cleanup_lab()


def test_lab_bridge_classifier_uses_registry_after_install() -> None:
    """install_runtime 启用 bridge 后, classifier 真用 lab 推荐."""
    import asyncio

    from kun.api.execution_mode_classifier import classify_execution_mode
    from kun.datamodel.soul_file import SoulFile
    from kun.engineering.precipitation import PrecipitationEvent

    _cleanup_lab()
    with patch.dict(os.environ, {"KUN_LAB_BRIDGE_ENABLED": "1"}):
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())

        # 模拟 lab promote
        async def _seed():
            await app.state.knowledge_precipitation.dispatch(
                PrecipitationEvent(
                    event_id="p-seed",
                    event_type="experiment.promoted",
                    payload={
                        "promotion_id": "p-seed",
                        "task_type": "ad_creative",
                        "strategy": "tier_cheap_high_temp",  # → FAST
                        "win_rate": 0.85,
                        "target_module": "execution_mode_classifier",
                    },
                )
            )

        asyncio.run(_seed())

        # classifier 应该用 lab 推荐 (FAST)
        soul = SoulFile(
            user_id="u-test",
            approval_threshold_money=10.0,
            execution_mode_preference={"default_mode": "MAX"},  # default 是 MAX
        )
        mode, reason = classify_execution_mode({"task_type": "ad_creative"}, soul)
        assert mode == "FAST"
        assert "lab_recipe" in reason
    _cleanup_lab()
