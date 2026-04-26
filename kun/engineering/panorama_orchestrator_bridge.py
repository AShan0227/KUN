"""Panorama → orchestrator 桥 (V2.1 wire M3.3, opt-in).

把 PanoramaBuilder.expand() 接到 orchestrator 事前流程, 在 IntentInterpreter
产出 task_ref 后生成 TaskPanorama 写黑板.

opt-in 模式 (默认 off):
- KUN_PANORAMA_BUILDER_ENABLED=1 启用
- 启用后: orchestrator stream 的事前阶段额外生成 panorama → 推 OrchestratorEvent
- 禁用: 完全走老流程 (零行为变化)
"""

from __future__ import annotations

import logging
import os
from typing import Any

from kun.core.task_panorama import TaskPanorama
from kun.engineering.panorama_builder import PanoramaBuilder

logger = logging.getLogger(__name__)


def is_enabled() -> bool:
    """检查 PanoramaBuilder 是否启用 (默认 off)."""
    return os.getenv("KUN_PANORAMA_BUILDER_ENABLED", "0") == "1"


# 全局 builder (lifespan 装一次, 测试 reset)
_builder: PanoramaBuilder | None = None


def get_builder() -> PanoramaBuilder:
    """获取全局 PanoramaBuilder (单例).

    M3.3: 默认所有 runner=None (跳过模块, panorama 只含必跑步骤).
    M4: 接真实 risk_runner / split_runner / preheat_runner.
    """
    global _builder
    if _builder is None:
        _builder = PanoramaBuilder()
    return _builder


def reset_builder() -> None:
    global _builder
    _builder = None


def set_builder(builder: PanoramaBuilder) -> None:
    """允许外部注入 (M4 接真 runner 时用)."""
    global _builder
    _builder = builder


async def build_panorama_for_task(
    task_ref: Any,
    user_message: str,
) -> TaskPanorama | None:
    """从 task_ref 构造 task_meta + 调 PanoramaBuilder.expand().

    返 None 表示禁用或失败 (orchestrator 该静默继续).
    """
    if not is_enabled():
        return None

    try:
        meta = task_ref.meta
        task_meta = {
            "task_id": meta.task_id,
            "intent_one_sentence": meta.success_criteria_short,
            "user_message": user_message,
            "task_type": meta.task_type,
            "risk_level": meta.risk_level,
            "complexity_score": meta.complexity_score,
            "estimated_steps": getattr(meta, "estimated_steps", 1),
            "estimated_cost_usd": meta.estimated_cost_usd,
            "estimated_failure_rate": 0.0,  # M4 接 capability_card
            "concurrent_running_tasks": 0,  # M4 接 BlackboardState
        }
        builder = get_builder()
        panorama = await builder.expand(task_meta)
        logger.info(
            "panorama.built task_id=%s tier=%s ms=%d modules_run=%d",
            meta.task_id,
            panorama.tier,
            panorama.generated_in_ms,
            len(panorama.modules_run),
        )
        return panorama
    except Exception:
        logger.exception("panorama_builder failed (non-fatal, orchestrator continues)")
        return None


def panorama_to_event_data(panorama: TaskPanorama) -> dict[str, Any]:
    """把 TaskPanorama 转成 OrchestratorEvent.data 格式 (推黑板)."""
    return {
        "stage": "panorama_built",
        "panorama_id": panorama.panorama_id,
        "tier": panorama.tier,
        "generated_in_ms": panorama.generated_in_ms,
        "intent": panorama.intent_one_sentence,
        "modules_run": panorama.modules_run,
        "modules_skipped": panorama.modules_skipped,
        "execution_plan_steps": len(panorama.execution_plan),
    }


async def build_panorama_anchored_for_task(
    task_ref: Any,
    user_message: str,
) -> list[Any]:
    """V2.2 §19.3 + C25 wire: 用 build_anchored 按需展开模块.

    跟 build_panorama_for_task 一样 opt-in (KUN_PANORAMA_BUILDER_ENABLED=1).
    根据 task_ref.meta.execution_mode (FAST/SMART/MAX) 决定跑几轮.

    Returns:
        list[ModuleResult] — 已 yield 的模块清单. 空 list 表示禁用 / 失败.
    """
    if not is_enabled():
        return []

    try:
        meta = task_ref.meta
        intent = getattr(meta, "success_criteria_short", "")
        risk = getattr(meta, "risk_level", "low")
        # 创建一个 minimal TaskPanorama 实例供 build_anchored 用
        panorama = TaskPanorama(
            task_ref=meta.task_id,
            intent_one_sentence=intent,
            tier="medium",  # 占位, 实际 mode 决定 round 数
        )
        modules: list[Any] = []
        async for module in panorama.build_anchored(task_ref):
            modules.append(module)
        logger.info(
            "panorama.anchored.built task_id=%s mode=%s modules=%d risk=%s",
            meta.task_id,
            getattr(meta, "execution_mode", "FAST"),
            len(modules),
            risk,
        )
        return modules
    except Exception:
        logger.exception(
            "build_panorama_anchored_for_task failed (non-fatal, orchestrator continues)"
        )
        return []


def anchored_modules_to_event_data(task_ref: Any, modules: list[Any]) -> dict[str, Any]:
    """把 anchored module 列表转成 OrchestratorEvent.data 格式 (推黑板)."""
    return {
        "stage": "panorama_anchored_built",
        "task_id": task_ref.meta.task_id,
        "mode": getattr(task_ref.meta, "execution_mode", "FAST"),
        "module_count": len(modules),
        "modules": [
            {
                "name": m.module_name,
                "round": m.round_index,
                "depth": m.depth,
                "required": m.required,
            }
            for m in modules
        ],
    }


__all__ = [
    "anchored_modules_to_event_data",
    "build_panorama_anchored_for_task",
    "build_panorama_for_task",
    "get_builder",
    "is_enabled",
    "panorama_to_event_data",
    "reset_builder",
    "set_builder",
]
