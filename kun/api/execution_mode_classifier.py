"""Task execution mode classifier for FAST / SMART / MAX / ENSEMBLE.

# TODO: orchestrator wire by Claude in V2.2
# orchestrator 用 task_meta.execution_mode 决定:
#   - panorama tier (FAST→minimal, SMART→light, MAX→full)
#   - multi_judge_review 启用与否
#   - 守望 ValueDecisionRule 启用与否
#   - ImportanceScorer max_rounds (FAST 0, SMART 1, MAX 3, ENSEMBLE 3)

# V2.2 §26 Wire 25: lab recipe 推荐参与 (lab → 主仓库反哺最后一公里)
# KUN-Lab 跑 ensemble 实验后 RecipePromoter → KP → LabRecipeRegistry 沉淀.
# classifier 在 default_mode fallback 之前查 registry, 让 lab 验证过的
# strategy 影响实际 mode 决策. 优先级低于 force_mode/critical/complexity,
# 不会破坏强约束.
"""

from __future__ import annotations

import logging
from typing import Any, Literal, cast

from kun.datamodel.soul_file import SoulFile

logger = logging.getLogger(__name__)

ExecutionMode = Literal["FAST", "SMART", "MAX", "ENSEMBLE"]
_VALID_MODES: set[str] = {"FAST", "SMART", "MAX", "ENSEMBLE"}

# Lab strategy → mode 推荐. tier_top_low_temp 验证有效 → MAX (慢但稳),
# tier_cheap_high_temp → FAST (省 tokens), 中段 → SMART.
_LAB_STRATEGY_MODE_HINT: dict[str, ExecutionMode] = {
    "tier_top_low_temp": "MAX",
    "tier_strong_mid_temp": "SMART",
    "tier_cheap_high_temp": "FAST",
    "chain_of_thought": "MAX",
    "diverse_perspective": "ENSEMBLE",
}


def classify_execution_mode(task_meta: dict[str, Any], soul_file: SoulFile) -> tuple[str, str]:
    """Return ``(mode, reason)`` for a task.

    Priority:
    1. ``task_meta.force_mode`` user explicit mode.
    2. ``risk_level=critical`` + user can wait → ENSEMBLE.
    3. Near-threshold high-complexity tasks → ENSEMBLE.
    4. Other critical / cost-over-threshold tasks → MAX.
    5. SoulFile kind preferences.
    6. ``complexity_score > 0.7`` → MAX, ``> 0.3`` → SMART.
    7. (V2.2 §26 / Wire 25) Lab recipe registry — validated strategy → mode hint.
    8. SoulFile ``default_mode`` preference, falling back to FAST.
    """

    force_mode = _mode_or_none(task_meta.get("force_mode"))
    if force_mode is not None:
        return force_mode, f"force_mode:{force_mode}"

    risk_level = str(task_meta.get("risk_level", "low")).lower()
    estimated_cost = _float_or_default(
        task_meta.get("estimated_cost", task_meta.get("estimated_cost_usd")),
        0.0,
    )
    complexity = _float_or_default(task_meta.get("complexity_score"), 0.0)

    if risk_level == "critical" and _bool_or_default(task_meta.get("user_can_wait"), False):
        return "ENSEMBLE", "risk_level:critical:user_can_wait"

    if complexity > 0.9 and estimated_cost > soul_file.approval_threshold_money * 0.8:
        return "ENSEMBLE", (
            f"complexity_score:{complexity:g}>0.9+estimated_cost:{estimated_cost:g}>"
            f"approval_threshold_money*0.8:{soul_file.approval_threshold_money * 0.8:g}"
        )

    if risk_level == "critical":
        return "MAX", "risk_level:critical"

    if estimated_cost > soul_file.approval_threshold_money:
        return "MAX", (
            f"estimated_cost:{estimated_cost:g}>approval_threshold_money:"
            f"{soul_file.approval_threshold_money:g}"
        )

    preference = soul_file.execution_mode_preference or {}
    task_kind = _task_kind(task_meta)

    if task_kind in _string_list(preference.get("always_ensemble_kinds")):
        return "ENSEMBLE", f"always_ensemble_kind:{task_kind}"

    if task_kind in _string_list(preference.get("always_fast_kinds")):
        return "FAST", f"always_fast_kind:{task_kind}"

    if task_kind in _string_list(preference.get("always_max_kinds")):
        return "MAX", f"always_max_kind:{task_kind}"

    if complexity > 0.7:
        return "MAX", f"complexity_score:{complexity:g}>0.7"
    if complexity > 0.3:
        return "SMART", f"complexity_score:{complexity:g}>0.3"

    # Layer 5 (Wire 25): lab recipe registry hint
    lab_mode = _lab_recipe_hint(task_meta, task_kind)
    if lab_mode is not None:
        return lab_mode

    default_mode = _mode_or_none(preference.get("default_mode")) or "FAST"
    return default_mode, f"default_mode:{default_mode}"


def _lab_recipe_hint(task_meta: dict[str, Any], task_kind: str) -> tuple[ExecutionMode, str] | None:
    """查 LabRecipeRegistry 看 lab 是否对该 task_type 有 validated strategy.

    返 None → 无 hint 走 default. 返 (mode, reason) → 用 lab 推荐.
    任何异常 (registry 没初始化 / lab 模块没装) → 静默 None, 不破 classifier.
    """
    task_type = str(task_meta.get("task_type") or task_kind or "")
    if not task_type:
        return None
    try:
        from kun.lab.recipe_registry import get_recipe_registry

        registry = get_recipe_registry()
        # 找 ExecutionMode classifier 相关的 entry
        entry = registry.get(task_type, "execution_mode_classifier")
        if entry is None:
            return None
        mode = _LAB_STRATEGY_MODE_HINT.get(entry.strategy)
        if mode is None:
            return None
        return mode, f"lab_recipe:{entry.strategy}(win_rate={entry.win_rate:.2f})"
    except Exception as e:
        logger.debug("classifier.lab_hint_skipped err=%s", e)
        return None


def _mode_or_none(value: Any) -> ExecutionMode | None:
    if not isinstance(value, str):
        return None
    mode = value.upper()
    if mode not in _VALID_MODES:
        return None
    return cast(ExecutionMode, mode)


def _float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_or_default(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return default


def _string_list(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {item for item in value if isinstance(item, str)}


def _task_kind(task_meta: dict[str, Any]) -> str:
    explicit_kind = task_meta.get("task_kind")
    if isinstance(explicit_kind, str):
        return explicit_kind
    task_type = task_meta.get("task_type")
    if isinstance(task_type, str):
        return task_type.split(".", maxsplit=1)[0]
    return ""
