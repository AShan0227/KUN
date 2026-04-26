"""LabRecipePrecipitationStep — 把 lab recipe 接进主仓库 KnowledgePrecipitation (Wire 24).

Wire 23 完成数据流通 (idle_batch.LabRecipeAdoptionStep 拉 experiment.promoted
事件), 但默认 adopter 只 noop log — recipe 没真改主仓库参数. Wire 24 把
adopter 真接 V2.1 §16.12 KnowledgePrecipitation 管道, 让 lab recipe 走
统一的 §16.6 GuardPolicy 审计回滚链路.

完整闭环:
    KUN-Lab.RecipePromoter.promote_eligible
        → events.experiment.promoted
        → idle_batch.LabRecipeAdoptionStep
        → make_kp_adopter(kp)(payload)
        → KnowledgePrecipitation.dispatch(PrecipitationEvent)
        → LabRecipePrecipitationStep.precipitate
        → AssetUpdate(asset_kind="playbook" | "rule")
        → kp._asset_apply_hook (主仓库注入, 真改 ExecutionMode classifier
                                / hermes prompt template / etc.)

设计要点:
- step_kind="weight_tune" — lab recipe 本质是调度策略权重的微调, 复用现有
  ADR-025 PrecipitationKind, 不破坏 Literal 接口
- schedule="realtime" — lab promote 已经经过 win_rate/min_total 阈值过滤,
  不再排队等周期, 立即走 KP 管道
- requires_approval — win_rate < 0.8 的需要主仓库审批后才生效;
  win_rate ≥ 0.8 视为高置信, 直接进 stable (走 §16.6 canary→stable)
- target_module → asset_kind 推断:
    "execution_mode_classifier" / "*_classifier" → "playbook"
    "hermes_prompt_template" / "*_prompt"        → "playbook"
    其他                                          → "rule"
- 不直接调主仓库代码: 通过 AssetUpdate + asset_apply_hook 注入式, 保持
  跟 V2.1 §16.12 已有 4 个内置 step 同模式
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kun.core.ids import new_id
from kun.engineering.precipitation import (
    AssetUpdate,
    PrecipitationEvent,
    PrecipitationKind,
    PrecipitationSchedule,
)

if TYPE_CHECKING:
    from kun.engineering.precipitation import KnowledgePrecipitation
    from kun.lab.adoption import LabAdopter

logger = logging.getLogger(__name__)


# 高置信门槛: win_rate ≥ 此值 → 不要求主仓库审批 (走 §16.6 canary 自动晋升)
HIGH_CONFIDENCE_WIN_RATE = 0.8


class LabRecipePrecipitationStep:
    """实现 PrecipitationStep Protocol. 消费 lab.experiment.promoted 事件."""

    source_event_type = "experiment.promoted"
    step_kind: PrecipitationKind = "weight_tune"
    schedule: PrecipitationSchedule = "realtime"

    async def precipitate(
        self,
        event: PrecipitationEvent,
        context: dict[str, Any] | None = None,
    ) -> list[AssetUpdate]:
        payload = event.payload
        target = str(payload.get("target_module") or "general")
        win_rate = float(payload.get("win_rate") or 0.0)
        asset_kind = self._infer_asset_kind(target)

        update = AssetUpdate(
            update_id=new_id("score"),
            asset_kind=asset_kind,
            asset_ref=target,
            update_kind="update",
            payload={
                "source": "kun_lab",
                "promotion_id": payload.get("promotion_id"),
                "task_type": payload.get("task_type"),
                "strategy": payload.get("strategy"),
                "win_rate": win_rate,
                "total_count": payload.get("total_count"),
                "avg_score": payload.get("avg_score"),
                "avg_cost_usd": payload.get("avg_cost_usd"),
                "recommended_change": self._infer_change(target, payload),
            },
            confidence=min(1.0, max(0.0, win_rate)),
            requires_approval=win_rate < HIGH_CONFIDENCE_WIN_RATE,
        )
        logger.info(
            "lab.precipitation.bridge promotion=%s strategy=%s target=%s "
            "asset_kind=%s win_rate=%.2f requires_approval=%s",
            payload.get("promotion_id"),
            payload.get("strategy"),
            target,
            asset_kind,
            win_rate,
            update.requires_approval,
        )
        return [update]

    @staticmethod
    def _infer_asset_kind(target_module: str) -> str:
        """根据 target_module 推主仓库该改哪个 asset 类别."""
        t = target_module.lower()
        if "classifier" in t or "prompt" in t or "template" in t:
            return "playbook"
        return "rule"

    @staticmethod
    def _infer_change(target_module: str, payload: dict[str, Any]) -> dict[str, Any]:
        """生成结构化 recommended_change. 主仓库 hook 按此真改参数."""
        return {
            "kind": target_module,
            "strategy": payload.get("strategy"),
            "stats": {
                "win_rate": payload.get("win_rate"),
                "total_count": payload.get("total_count"),
                "avg_score": payload.get("avg_score"),
            },
            "task_type": payload.get("task_type"),
        }


def make_kp_adopter(kp: KnowledgePrecipitation) -> LabAdopter:
    """把 KnowledgePrecipitation 包装成 LabAdopter (Wire 23 idle_batch step 用).

    用法:
        from kun.engineering.precipitation import KnowledgePrecipitation
        from kun.lab.adoption import install_lab_adoption_step
        from kun.lab.precipitation_bridge import (
            LabRecipePrecipitationStep, make_kp_adopter
        )

        kp = app.state.knowledge_precipitation
        kp.register_step(LabRecipePrecipitationStep())
        install_lab_adoption_step(adopter=make_kp_adopter(kp))
    """

    async def adopter(payload: dict[str, Any]) -> None:
        promotion_id = str(payload.get("promotion_id") or "")
        event = PrecipitationEvent(
            event_id=promotion_id or new_id("event"),
            event_type="experiment.promoted",
            payload=payload,
        )
        updates = await kp.dispatch(event)
        logger.debug(
            "lab.kp_adopter.dispatched promotion=%s updates=%d",
            promotion_id,
            len(updates),
        )

    return adopter


def install_lab_kp_bridge(
    kp: KnowledgePrecipitation,
) -> LabRecipePrecipitationStep:
    """One-shot install: 注册 LabRecipePrecipitationStep + 把 KP 注入 idle_batch step.

    runtime 启动调一次 (或在 install_runtime 里), 整条 lab → 主仓库参数链路就通了.
    """
    from kun.lab.adoption import install_lab_adoption_step

    step = LabRecipePrecipitationStep()
    kp.register_step(step)
    install_lab_adoption_step(adopter=make_kp_adopter(kp))
    return step


__all__ = [
    "HIGH_CONFIDENCE_WIN_RATE",
    "LabRecipePrecipitationStep",
    "install_lab_kp_bridge",
    "make_kp_adopter",
]
