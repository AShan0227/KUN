"""V3 three-layer memory writeback.

This module turns real execution artifacts into context assets:
- result memory: final delivery and quality;
- process memory: step path and tool/model usage;
- meta-decision memory: why KUN chose this strategy/protocol/path.

The write path uses the existing AssetStore so the existing ContextPacker can
retrieve these memories on later similar tasks.  No separate "memory silo".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel

from kun.context.assets import AssetLayer, LayeredAsset
from kun.context.storage import AssetStore, get_store
from kun.datamodel.decision_ticket import DecisionTicket
from kun.datamodel.runtime import RuntimeState, StepRecord
from kun.datamodel.task import TaskRef

MemoryLayer = Literal["task_result", "execution_process", "meta_decision"]


class MemoryWritebackResult(BaseModel):
    """One memory writeback result."""

    asset_id: str
    memory_layer: MemoryLayer
    asset_kind: str
    summary: str


@dataclass
class MemoryWriteback:
    """Write task memories into the shared context store."""

    store: AssetStore | None = None

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = get_store()

    async def record_meta_decision(
        self,
        *,
        tenant_id: str,
        task_ref: TaskRef,
        decision: Any,
        decision_ticket: DecisionTicket | None = None,
    ) -> MemoryWritebackResult:
        decision_point = decision_ticket.decision_point if decision_ticket else "strategy_selected"
        selected_action = decision_ticket.selected_action if decision_ticket else ""
        if decision_ticket and decision_ticket.decision_point == "protocol_applied":
            strategy_pack_id = str(decision_ticket.metadata.get("protocol_id") or "protocol")
            execution_mode = str(
                decision_ticket.metadata.get("execution_mode") or task_ref.meta.execution_mode
            )
            metric_dimensions = list((decision_ticket.evidence.get("reward_weights") or {}).keys())
            skill_hints = [
                str(item.get("skill", ""))
                for item in decision_ticket.evidence.get("skill_chain", [])
                if isinstance(item, dict) and str(item.get("skill", ""))
            ]
            reason = decision_ticket.reason
        else:
            strategy_pack_id = str(getattr(decision, "strategy_pack_id", "default"))
            execution_mode = str(getattr(decision, "execution_mode", task_ref.meta.execution_mode))
            metric_dimensions = list(getattr(decision, "metric_dimensions", []) or [])
            skill_hints = list(getattr(decision, "skill_hints", []) or [])
            reason = str(getattr(decision, "reason", ""))
        ticket_ref = decision_ticket.ref().model_dump(mode="json") if decision_ticket else None
        summary = (
            f"元决策: task_type={task_ref.meta.task_type}; "
            f"point={decision_point}; "
            f"strategy={strategy_pack_id}; mode={execution_mode}; "
            f"skills={', '.join(skill_hints[:5])}; reason={reason}"
        )
        asset = LayeredAsset.build(
            "methodology",
            tenant_id,
            metadata={
                "memory_layer": "meta_decision",
                "task_id": task_ref.meta.task_id,
                "task_type": task_ref.meta.task_type,
                "decision_point": decision_point,
                "selected_action": selected_action,
                "strategy_pack_id": strategy_pack_id,
                "execution_mode": execution_mode,
                "metric_dimensions": metric_dimensions,
                "skill_hints": skill_hints,
                "reason": reason,
                "decision_ticket": ticket_ref,
            },
            summary=summary,
            layer=AssetLayer.L2_PROJECT,
            tags=[
                "v3",
                "meta_decision",
                task_ref.meta.task_type,
                decision_point,
                strategy_pack_id,
                execution_mode,
            ],
        )
        await self._put(asset)
        return MemoryWritebackResult(
            asset_id=asset.asset_id,
            memory_layer="meta_decision",
            asset_kind=asset.asset_kind,
            summary=summary,
        )

    async def record_process_step(
        self,
        *,
        tenant_id: str,
        task_ref: TaskRef,
        step: StepRecord,
        answer: str,
        provider: str,
        model: str,
        tier: str,
    ) -> MemoryWritebackResult:
        preview = _preview(answer, 500)
        summary = (
            f"执行过程: step={step.step_id}; skill={step.skill_used}; "
            f"model={model or 'unknown'}; cost=${step.cost_usd_equivalent:.4f}; "
            f"output={preview}"
        )
        asset = LayeredAsset.build(
            "memory",
            tenant_id,
            metadata={
                "memory_layer": "execution_process",
                "task_id": task_ref.meta.task_id,
                "task_type": task_ref.meta.task_type,
                "step_id": step.step_id,
                "skill_used": step.skill_used,
                "provider": provider,
                "model": model,
                "tier": tier,
                "cost_usd": step.cost_usd_equivalent,
                "tokens_in": step.tokens_in,
                "tokens_out": step.tokens_out,
            },
            summary=summary,
            layer=AssetLayer.L1_TASK,
            tags=["v3", "execution_process", task_ref.meta.task_type, step.skill_used],
        )
        await self._put(asset)
        return MemoryWritebackResult(
            asset_id=asset.asset_id,
            memory_layer="execution_process",
            asset_kind=asset.asset_kind,
            summary=summary,
        )

    async def record_task_result(
        self,
        *,
        tenant_id: str,
        task_ref: TaskRef,
        status: str,
        answer: str,
        runtime: RuntimeState,
        validation_outcome: str,
        validation_score: float | None,
        surprise_score: float,
        score_overall: float | None = None,
        decision_tickets: list[DecisionTicket] | None = None,
    ) -> MemoryWritebackResult:
        ticket_refs = [ticket.ref().model_dump(mode="json") for ticket in decision_tickets or []]
        summary = (
            f"任务结果: status={status}; outcome={validation_outcome}; "
            f"score={score_overall if score_overall is not None else validation_score}; "
            f"cost=${runtime.accumulated_cost_usd_equivalent:.4f}; "
            f"answer={_preview(answer, 600)}"
        )
        asset = LayeredAsset.build(
            "memory",
            tenant_id,
            metadata={
                "memory_layer": "task_result",
                "task_id": task_ref.meta.task_id,
                "task_type": task_ref.meta.task_type,
                "status": status,
                "validation_outcome": validation_outcome,
                "validation_score": validation_score,
                "score_overall": score_overall,
                "surprise_score": surprise_score,
                "cost_usd": runtime.accumulated_cost_usd_equivalent,
                "tokens": runtime.accumulated_tokens,
                "step_count": len(runtime.completed_steps),
                "decision_tickets": ticket_refs,
            },
            summary=summary,
            layer=AssetLayer.L2_PROJECT if status == "done" else AssetLayer.L1_TASK,
            tags=["v3", "task_result", task_ref.meta.task_type, status, validation_outcome],
        )
        await self._put(asset)
        return MemoryWritebackResult(
            asset_id=asset.asset_id,
            memory_layer="task_result",
            asset_kind=asset.asset_kind,
            summary=summary,
        )

    async def _put(self, asset: LayeredAsset) -> None:
        assert self.store is not None
        await self.store.put(asset)


def _preview(text: str, max_chars: int) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


__all__ = ["MemoryWriteback", "MemoryWritebackResult"]
