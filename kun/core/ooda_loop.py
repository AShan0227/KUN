"""OODA 外层循环状态机 (BATCH5 C11 / T21).

这个模块建模外层 Observe → Orient → Decide → Act → Reflect → Adjust
循环。Orchestrator 会把主执行路径里的关键阶段写成 OODA checkpoint,
让长周期任务可以被观察、复盘和后续重规划。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from ulid import ULID


class OODAState(StrEnum):
    OBSERVE = "observe"
    ORIENT = "orient"
    DECIDE = "decide"
    ACT = "act"
    REFLECT = "reflect"
    ADJUST = "adjust"
    DONE = "done"


class OODACycle(BaseModel):
    """一次任务的 OODA 外层循环快照."""

    model_config = ConfigDict(extra="forbid")

    cycle_id: str = Field(default_factory=lambda: f"ooda-{ULID()}")
    task_ref: str
    current_state: OODAState = OODAState.OBSERVE
    state_history: list[tuple[OODAState, datetime]] = Field(
        default_factory=lambda: [(OODAState.OBSERVE, datetime.now(UTC))]
    )
    observations: list[dict[str, Any]] = Field(default_factory=list)
    orientation: dict[str, Any] | None = None
    decision: dict[str, Any] | None = None
    actions_taken: list[dict[str, Any]] = Field(default_factory=list)
    reflections: list[dict[str, Any]] = Field(default_factory=list)
    adjustments: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


_ALLOWED_TRANSITIONS: dict[OODAState, set[OODAState]] = {
    OODAState.OBSERVE: {OODAState.ORIENT},
    OODAState.ORIENT: {OODAState.DECIDE},
    OODAState.DECIDE: {OODAState.ACT},
    OODAState.ACT: {OODAState.REFLECT},
    OODAState.REFLECT: {OODAState.DECIDE, OODAState.ADJUST, OODAState.DONE},
    OODAState.ADJUST: {OODAState.ORIENT, OODAState.DECIDE},
    OODAState.DONE: set(),
}


class OODAEngine:
    """确定性的 OODA 状态机.

    它负责外层节奏, 不负责生成复杂策略；复杂策略可以由上层 LLM 或
    orchestrator 产出后作为 payload 塞进来。
    """

    async def transition(
        self,
        cycle: OODACycle,
        next_state: OODAState,
        payload: dict[str, Any] | None = None,
    ) -> OODACycle:
        """状态迁移, 并把 payload 落到对应阶段字段."""

        payload = dict(payload or {})
        if next_state not in _ALLOWED_TRANSITIONS[cycle.current_state]:
            raise ValueError(
                f"illegal OODA transition: {cycle.current_state.value} -> {next_state.value}"
            )

        updated = cycle.model_copy(deep=True)
        updated.current_state = next_state
        updated.state_history.append((next_state, datetime.now(UTC)))
        _apply_payload(updated, next_state, payload)
        return updated

    async def reflect(self, cycle: OODACycle) -> dict[str, Any]:
        """评估最新 action 和 decision 期望是否一致."""

        expected = _expected_outcome(cycle.decision)
        latest_action = cycle.actions_taken[-1] if cycle.actions_taken else None
        if latest_action is None:
            return {
                "success": False,
                "needs_adjust": True,
                "reason": "no action has been recorded",
                "expected": expected,
                "actual": None,
            }

        action_failed = latest_action.get("passed") is False or latest_action.get("status") in {
            "failed",
            "error",
            "cancelled",
        }
        actual = latest_action.get("outcome") or latest_action.get("status")
        mismatch = bool(expected and actual and str(actual) != str(expected))
        needs_adjust = action_failed or mismatch
        reason = "action matched decision"
        if action_failed:
            reason = "latest action failed"
        elif mismatch:
            reason = "latest action outcome differed from decision"

        return {
            "success": not needs_adjust,
            "needs_adjust": needs_adjust,
            "reason": reason,
            "expected": expected,
            "actual": actual,
            "action": latest_action,
        }

    async def should_adjust(self, cycle: OODACycle) -> bool:
        """根据最新 reflection 判断是否需要进入 Adjust."""

        if cycle.reflections:
            return bool(cycle.reflections[-1].get("needs_adjust", False))
        reflection = await self.reflect(cycle)
        return bool(reflection.get("needs_adjust", False))

    async def adjust(self, cycle: OODACycle) -> OODACycle:
        """记录调整, 并把 cycle 带回 Decide 阶段."""

        if cycle.current_state != OODAState.REFLECT:
            raise ValueError("adjust can only run from reflect state")
        if not await self.should_adjust(cycle):
            raise ValueError("cycle does not need adjustment")

        latest_reflection = (
            cycle.reflections[-1] if cycle.reflections else await self.reflect(cycle)
        )
        revised_decision = dict(cycle.decision or {})
        revised_decision["adjusted"] = True
        revised_decision["adjust_reason"] = latest_reflection.get("reason", "reflection requested")

        adjusted = await self.transition(
            cycle,
            OODAState.ADJUST,
            {
                "reason": latest_reflection.get("reason"),
                "reflection": latest_reflection,
                "previous_decision": cycle.decision,
                "revised_decision": revised_decision,
            },
        )
        return await self.transition(adjusted, OODAState.DECIDE, revised_decision)


def _apply_payload(cycle: OODACycle, state: OODAState, payload: dict[str, Any]) -> None:
    if state == OODAState.OBSERVE:
        cycle.observations.append(payload)
    elif state == OODAState.ORIENT:
        cycle.orientation = payload
    elif state == OODAState.DECIDE:
        cycle.decision = payload
    elif state == OODAState.ACT:
        cycle.actions_taken.append(payload)
    elif state == OODAState.REFLECT:
        cycle.reflections.append(payload)
    elif state == OODAState.ADJUST:
        cycle.adjustments.append(payload)
    elif state == OODAState.DONE:
        cycle.metadata["done"] = payload


def _expected_outcome(decision: dict[str, Any] | None) -> Any:
    if not decision:
        return None
    return decision.get("expected_outcome") or decision.get("success_criteria")


__all__ = ["OODACycle", "OODAEngine", "OODAState"]
