"""Dynamic local replanning for the outer OODA loop (C20).

This module is intentionally standalone. It does not call the orchestrator.
It only decides whether a running plan should be locally replanned and builds
a replacement tail while preserving already sunk work.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict

from kun.brain.planner import ExecutionPlan, PlanStep
from kun.core.ooda_loop import OODACycle
from kun.datamodel.runtime import validate_dag

Plan = ExecutionPlan


@dataclass(frozen=True)
class ReplanDecision:
    needs_replan: bool
    reason: str
    confidence: float = 0.5
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class SunkCostEstimate:
    completed_steps: int
    total_planned_steps: int
    completed_cost_usd: float
    completed_duration_sec: float
    progress_ratio: float
    can_reuse_outputs: bool


class ReplanResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan: ExecutionPlan
    preserved_step_ids: list[int]
    replacement_step_ids: list[int]
    reason: str
    sunk_cost_usd: float


class DynamicReplanner:
    """Detect and build local plan changes during an OODA cycle."""

    def __init__(self, *, min_replan_confidence: float = 0.6) -> None:
        self.min_replan_confidence = min_replan_confidence

    async def detect_replan_needed(self, cycle: OODACycle) -> tuple[bool, str]:
        """Return whether current cycle needs local replanning plus a human reason."""

        if cycle.metadata.get("replan_requested") is True:
            return (True, str(cycle.metadata.get("replan_reason") or "manual replan requested"))

        for observation in reversed(cycle.observations):
            requested = observation.get("replan") is True or observation.get("needs_replan") is True
            if requested:
                return (True, str(observation.get("reason") or "observation requested replan"))
            if observation.get("status") in {"failed", "blocked", "stale"}:
                return (
                    True,
                    str(observation.get("reason") or "observation status requires replan"),
                )

        if cycle.reflections:
            latest = cycle.reflections[-1]
            if latest.get("needs_adjust") is True:
                return (True, str(latest.get("reason") or "reflection requested adjustment"))

        if cycle.actions_taken:
            latest_action = cycle.actions_taken[-1]
            if latest_action.get("status") in {"failed", "error", "cancelled"}:
                return (True, str(latest_action.get("error") or "latest action failed"))

        budget = cycle.metadata.get("budget")
        spent = cycle.metadata.get("spent")
        if isinstance(budget, (int, float)) and isinstance(spent, (int, float)) and spent > budget:
            return (True, "budget exceeded")

        return (False, "")

    async def detect_replan_decision(self, cycle: OODACycle) -> ReplanDecision:
        """Compatibility wrapper for callers that want a scored decision object."""

        yes, reason = await self.detect_replan_needed(cycle)
        confidence = 0.8 if yes else 0.5
        if reason in {"budget exceeded"} or "failed" in reason or "blocked" in reason:
            confidence = 0.9
        return ReplanDecision(
            needs_replan=yes,
            reason=reason or "no_replan_needed",
            confidence=confidence,
            metadata={"task_ref": cycle.task_ref},
        )

    async def replan_from_step(
        self,
        original_plan: Plan,
        current_step_idx: int,
        new_observations: list[dict[str, Any]],
    ) -> Plan:
        """Replan from the step after current_step_idx while preserving sunk work."""

        if not original_plan.steps:
            raise ValueError("cannot replan an empty plan")
        if current_step_idx < 0 or current_step_idx >= len(original_plan.steps):
            raise IndexError("current_step_idx is out of range")

        preserved = [
            step.model_copy(deep=True) for step in original_plan.steps[: current_step_idx + 1]
        ]
        replacement = _replacement_steps_from_observations(new_observations)
        if not replacement:
            replacement = _fallback_replacement_steps(new_observations)

        next_step_id = max(step.step_id for step in preserved) + 1
        previous_dep = preserved[-1].step_id
        rebuilt_tail: list[PlanStep] = []
        for idx, step in enumerate(replacement):
            new_step = step.model_copy(deep=True)
            new_step.step_id = next_step_id + idx
            new_step.depends_on = [previous_dep]
            rebuilt_tail.append(new_step)
            previous_dep = new_step.step_id

        replanned = ExecutionPlan(
            task_ref=original_plan.task_ref, steps=[*preserved, *rebuilt_tail]
        )
        _spread_remaining_estimates(
            replanned,
            sunk_cost=self.calculate_sunk_cost(original_plan, current_step_idx),
            preserved_count=len(preserved),
        )
        validate_dag({step.step_id: step.depends_on for step in replanned.steps})
        return replanned

    def calculate_sunk_cost(self, original_plan: Plan, current_step_idx: int) -> float:
        """Cost already spent through current_step_idx, inclusive."""

        if current_step_idx < 0:
            return 0.0
        if current_step_idx >= len(original_plan.steps):
            raise IndexError("current_step_idx is out of range")
        return round(
            sum(step.estimated_cost_usd for step in original_plan.steps[: current_step_idx + 1]),
            6,
        )

    def estimate_sunk_cost(self, original_plan: Plan, current_step_idx: int) -> SunkCostEstimate:
        """Detailed sunk-cost estimate for UI and human approval prompts."""

        if current_step_idx < 0:
            completed_steps: list[PlanStep] = []
        elif current_step_idx >= len(original_plan.steps):
            raise IndexError("current_step_idx is out of range")
        else:
            completed_steps = original_plan.steps[: current_step_idx + 1]
        total_steps = len(original_plan.steps)
        completed_cost = round(sum(step.estimated_cost_usd for step in completed_steps), 6)
        completed_duration = round(sum(step.estimated_duration_sec for step in completed_steps), 6)
        return SunkCostEstimate(
            completed_steps=len(completed_steps),
            total_planned_steps=total_steps,
            completed_cost_usd=completed_cost,
            completed_duration_sec=completed_duration,
            progress_ratio=(len(completed_steps) / total_steps if total_steps else 0.0),
            can_reuse_outputs=bool(completed_steps),
        )

    async def replan_with_result(
        self,
        original_plan: Plan,
        current_step_idx: int,
        new_observations: list[dict[str, Any]],
        *,
        reason: str,
    ) -> ReplanResult:
        """Convenience wrapper that returns bookkeeping for UI / audit logs."""

        replanned = await self.replan_from_step(original_plan, current_step_idx, new_observations)
        preserved = [step.step_id for step in replanned.steps[: current_step_idx + 1]]
        replacement = [step.step_id for step in replanned.steps[current_step_idx + 1 :]]
        return ReplanResult(
            plan=replanned,
            preserved_step_ids=preserved,
            replacement_step_ids=replacement,
            reason=reason,
            sunk_cost_usd=self.calculate_sunk_cost(original_plan, current_step_idx),
        )

    def is_replan_worth_it(
        self,
        decision: ReplanDecision,
        sunk_cost: SunkCostEstimate,
        *,
        replan_cost_estimate: float = 0.05,
    ) -> tuple[bool, str]:
        """Small deterministic ROI gate for optional human-facing replan prompts."""

        if not decision.needs_replan:
            return (False, "no_replan_needed")
        if decision.confidence < self.min_replan_confidence:
            return (False, f"confidence_below_threshold:{decision.confidence:.2f}")
        if decision.confidence >= 0.85:
            return (True, "high_confidence_signal")
        if sunk_cost.progress_ratio > 0.8:
            return (False, f"too_much_progress:{sunk_cost.progress_ratio:.2f}")
        score = decision.confidence * (1.0 - sunk_cost.progress_ratio) - replan_cost_estimate
        if score > 0.35:
            return (True, f"roi_positive:{score:.2f}")
        return (False, f"roi_negative:{score:.2f}")


def _replacement_steps_from_observations(observations: list[dict[str, Any]]) -> list[PlanStep]:
    for observation in reversed(observations):
        raw_steps = observation.get("replacement_steps")
        if not isinstance(raw_steps, list):
            continue
        steps: list[PlanStep] = []
        for idx, item in enumerate(raw_steps, start=1):
            if isinstance(item, str):
                description = item.strip()
                skill_hint = None
            elif isinstance(item, dict):
                description = str(item.get("description") or "").strip()
                skill_hint = (
                    str(item["skill_hint"]).strip()
                    if item.get("skill_hint") not in {None, ""}
                    else None
                )
            else:
                continue
            if not description:
                continue
            steps.append(PlanStep(step_id=idx, description=description, skill_hint=skill_hint))
        if steps:
            return steps
    return []


def _fallback_replacement_steps(observations: list[dict[str, Any]]) -> list[PlanStep]:
    reason = _latest_reason(observations)
    return [
        PlanStep(
            step_id=1,
            description=f"根据新观察调整执行策略: {reason}",
            skill_hint="task.replan",
        ),
        PlanStep(
            step_id=2,
            description="按最新观察重新验证结果并交付",
            skill_hint="task.validation",
        ),
    ]


def _latest_reason(observations: list[dict[str, Any]]) -> str:
    for observation in reversed(observations):
        reason = (
            observation.get("reason") or observation.get("summary") or observation.get("status")
        )
        if reason:
            return str(reason)
    return "execution drift detected"


def _spread_remaining_estimates(
    plan: ExecutionPlan, *, sunk_cost: float, preserved_count: int
) -> None:
    total = max(plan.task_ref.meta.estimated_cost_usd - sunk_cost, 0.0)
    # Keep existing estimates on preserved steps; assign remaining budget to rebuilt tail steps.
    replacement_steps = plan.steps[preserved_count:]
    if not replacement_steps:
        return
    each = total / len(replacement_steps) if replacement_steps else 0.0
    duration_each = plan.task_ref.meta.estimated_duration_sec / max(len(plan.steps), 1)
    for step in replacement_steps:
        step.estimated_cost_usd = each
        step.estimated_duration_sec = duration_each


__all__ = [
    "DynamicReplanner",
    "Plan",
    "ReplanDecision",
    "ReplanResult",
    "SunkCostEstimate",
]
