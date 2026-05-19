"""Frontier50 campaign orchestration rules for Qi under KUN V6."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.qi_ab import (
    QiABRoundControlPlaneContract,
    QiABRoundSummary,
    build_qi_ab_round_contract,
    build_qi_ab_round_work_item,
)
from kun.control_plane.v6 import WorkItem

Frontier50RoundStatus = Literal["pending", "running", "passed", "repairing", "invalid"]


class Frontier50RoundSpec(BaseModel):
    """One five-task Frontier50 round."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    round_id: str
    task_ids: list[str] = Field(min_length=1, max_length=5)
    status: Frontier50RoundStatus = "pending"
    latest_work_item_id: str | None = None
    same_task_retest_required: bool = False


class Frontier50CampaignPlan(BaseModel):
    """Ten-round campaign plan with strict next-round gating."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mission_id: str
    task_plan_version: str
    rounds: list[Frontier50RoundSpec] = Field(min_length=1, max_length=10)

    @property
    def completed_round_count(self) -> int:
        return sum(1 for item in self.rounds if item.status == "passed")

    @property
    def all_passed(self) -> bool:
        return self.completed_round_count == len(self.rounds)


class Frontier50CampaignDecision(BaseModel):
    """Campaign-level decision after one Qi AB round contract."""

    model_config = ConfigDict(extra="forbid")

    plan: Frontier50CampaignPlan
    round_contract: QiABRoundControlPlaneContract
    queued_work_items: list[WorkItem] = Field(default_factory=list)
    campaign_complete: bool = False
    reason: str


def build_frontier50_campaign_plan(
    *,
    mission_id: str,
    task_plan_version: str,
    task_ids: list[str],
    tasks_per_round: int = 5,
) -> Frontier50CampaignPlan:
    """Build a deterministic Frontier50 campaign from 50 task ids."""

    if tasks_per_round <= 0:
        raise ValueError("tasks_per_round must be positive")
    if len(task_ids) % tasks_per_round != 0:
        raise ValueError("task_ids must divide evenly into rounds")
    rounds = [
        Frontier50RoundSpec(
            round_id=f"round-{index + 1:02d}",
            task_ids=task_ids[index * tasks_per_round : (index + 1) * tasks_per_round],
        )
        for index in range(len(task_ids) // tasks_per_round)
    ]
    return Frontier50CampaignPlan(
        mission_id=mission_id,
        task_plan_version=task_plan_version,
        rounds=rounds,
    )


def initial_frontier50_work_item(plan: Frontier50CampaignPlan) -> WorkItem:
    """Queue the first Frontier50 round."""

    first = plan.rounds[0]
    return build_qi_ab_round_work_item(
        mission_id=plan.mission_id,
        task_plan_version=plan.task_plan_version,
        round_id=first.round_id,
        task_ids=first.task_ids,
    )


def apply_frontier50_round_summary(
    *,
    plan: Frontier50CampaignPlan,
    summary: QiABRoundSummary,
) -> Frontier50CampaignDecision:
    """Apply one round result and decide repair/retest/next-round work."""

    round_index = _round_index(plan, summary.round_id)
    current_round = plan.rounds[round_index]
    next_round = plan.rounds[round_index + 1] if round_index + 1 < len(plan.rounds) else None
    contract = build_qi_ab_round_contract(
        summary,
        next_round_id=next_round.round_id if next_round else None,
        next_round_task_ids=next_round.task_ids if next_round else (),
    )
    if contract.verdict == "pass":
        updated_round = current_round.model_copy(
            update={
                "status": "passed",
                "latest_work_item_id": summary.work_item_id,
                "same_task_retest_required": False,
            }
        )
        queued = [contract.next_round_work_item] if contract.next_round_work_item else []
        reason = "round passed; next round queued" if queued else "final round passed"
    elif contract.verdict == "repair":
        updated_round = current_round.model_copy(
            update={
                "status": "repairing",
                "latest_work_item_id": summary.work_item_id,
                "same_task_retest_required": True,
            }
        )
        queued = [contract.repair_work_item] if contract.repair_work_item else []
        reason = "KUN repair required; same-task retest must pass before next round"
    else:
        updated_round = current_round.model_copy(
            update={
                "status": "invalid",
                "latest_work_item_id": summary.work_item_id,
                "same_task_retest_required": True,
            }
        )
        queued = [contract.repair_work_item] if contract.repair_work_item else []
        reason = "round invalid; repair system pollution or missing artifacts and rerun same round"

    updated_rounds = [
        updated_round if item.round_id == current_round.round_id else item for item in plan.rounds
    ]
    updated_plan = plan.model_copy(update={"rounds": updated_rounds})
    return Frontier50CampaignDecision(
        plan=updated_plan,
        round_contract=contract,
        queued_work_items=[item for item in queued if item is not None],
        campaign_complete=updated_plan.all_passed,
        reason=reason,
    )


def _round_index(plan: Frontier50CampaignPlan, round_id: str) -> int:
    for index, item in enumerate(plan.rounds):
        if item.round_id == round_id:
            return index
    raise ValueError(f"unknown Frontier50 round {round_id}")
