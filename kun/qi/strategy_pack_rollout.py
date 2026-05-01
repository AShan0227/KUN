"""Guarded rollout planning for Qi StrategyPack drafts.

This is the bridge after evidence review and before any production adoption.
It creates a human-reviewable shadow/canary plan, but deliberately does not
create or activate production experiments by itself.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.context.assets import LayeredAsset
from kun.context.storage import AssetStore, get_store

RolloutPlanStatus = Literal["blocked", "needs_review", "shadow_plan"]


class StrategyPackRolloutPhase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: Literal["shadow", "canary", "rollout", "stable"]
    rollout_percent: int = 0
    min_runs: int = 0
    min_success_rate: float = 0.0
    max_cost_regression_pct: float = 0.0
    max_latency_regression_pct: float = 0.0
    rollback_on_guardrail_breach: bool = True


class StrategyPackRolloutPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str
    draft_id: str
    proposed_pack_id: str = "unknown"
    status: RolloutPlanStatus
    phases: list[StrategyPackRolloutPhase] = Field(default_factory=list)
    guardrails: dict[str, Any] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    requires_human_approval: bool = True
    promotion_allowed: Literal[False] = False
    production_action: Literal[False] = False


class StrategyPackRolloutPlanReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scanned: int = 0
    planned: int = 0
    blocked: int = 0
    updated: int = 0
    dry_run: bool = True
    plans: list[StrategyPackRolloutPlan] = Field(default_factory=list)
    production_action: Literal[False] = False


def build_strategy_pack_rollout_plan(asset: LayeredAsset) -> StrategyPackRolloutPlan:
    metadata = asset.l1_metadata
    draft_id = str(metadata.get("draft_id") or asset.asset_id)
    proposed_pack_id = str(metadata.get("proposed_pack_id") or "unknown")
    plan_id = _plan_id(draft_id, proposed_pack_id)
    if metadata.get("source") != "qi.idle_replay.strategy_pack_draft":
        return StrategyPackRolloutPlan(
            plan_id=plan_id,
            draft_id=draft_id,
            proposed_pack_id=proposed_pack_id,
            status="blocked",
            reasons=["asset_is_not_qi_strategy_pack_draft"],
        )
    review_status = str(metadata.get("qi_review_status") or "")
    if review_status != "ready_for_human_review":
        return StrategyPackRolloutPlan(
            plan_id=plan_id,
            draft_id=draft_id,
            proposed_pack_id=proposed_pack_id,
            status="blocked",
            reasons=[f"review_status_not_ready:{review_status or 'missing'}"],
        )
    risk = str(metadata.get("qi_review_risk") or "low")
    guardrails = _guardrails(risk=risk)
    return StrategyPackRolloutPlan(
        plan_id=plan_id,
        draft_id=draft_id,
        proposed_pack_id=proposed_pack_id,
        status="shadow_plan",
        phases=_phases_for_risk(risk),
        guardrails=guardrails,
        reasons=[
            "review_ready_but_requires_human_approval",
            "shadow_first_no_user_visible_change",
            "canary_requires_guardrail_pass",
        ],
    )


async def plan_strategy_pack_rollouts(
    *,
    tenant_id: str,
    store: AssetStore | None = None,
    dry_run: bool = True,
    limit: int = 1000,
) -> StrategyPackRolloutPlanReport:
    store = store or get_store()
    assets = await store.list(tenant_id=tenant_id, asset_kind="methodology", limit=limit)
    draft_assets = [
        asset
        for asset in assets
        if asset.l1_metadata.get("source") == "qi.idle_replay.strategy_pack_draft"
    ]
    plans = [build_strategy_pack_rollout_plan(asset) for asset in draft_assets]
    updated = 0
    if not dry_run:
        for asset, plan in zip(draft_assets, plans, strict=True):
            if _apply_rollout_plan(asset, plan):
                await store.put(asset)
                updated += 1
    return StrategyPackRolloutPlanReport(
        scanned=len(draft_assets),
        planned=sum(1 for plan in plans if plan.status == "shadow_plan"),
        blocked=sum(1 for plan in plans if plan.status == "blocked"),
        updated=updated,
        dry_run=dry_run,
        plans=plans,
    )


def _apply_rollout_plan(asset: LayeredAsset, plan: StrategyPackRolloutPlan) -> bool:
    payload = plan.model_dump(mode="json")
    if asset.l1_metadata.get("qi_rollout_plan") == payload:
        return False
    asset.l1_metadata["qi_rollout_plan"] = payload
    asset.l1_metadata["qi_rollout_plan_status"] = plan.status
    asset.l1_metadata["promotion_allowed"] = False
    asset.l1_metadata["production_action"] = False
    plan_tag = "qi_rollout:shadow_plan" if plan.status == "shadow_plan" else "qi_rollout:blocked"
    asset.tags = sorted(
        {
            *[tag for tag in asset.tags if not str(tag).startswith("qi_rollout:")],
            plan_tag,
        }
    )
    return True


def _phases_for_risk(risk: str) -> list[StrategyPackRolloutPhase]:
    if risk in {"high", "critical"}:
        return [
            StrategyPackRolloutPhase(
                phase="shadow",
                rollout_percent=0,
                min_runs=30,
                min_success_rate=0.72,
                max_cost_regression_pct=5,
                max_latency_regression_pct=10,
            ),
            StrategyPackRolloutPhase(
                phase="canary",
                rollout_percent=1,
                min_runs=80,
                min_success_rate=0.78,
                max_cost_regression_pct=0,
                max_latency_regression_pct=5,
            ),
            StrategyPackRolloutPhase(
                phase="rollout",
                rollout_percent=10,
                min_runs=200,
                min_success_rate=0.82,
                max_cost_regression_pct=0,
                max_latency_regression_pct=5,
            ),
        ]
    return [
        StrategyPackRolloutPhase(
            phase="shadow",
            rollout_percent=0,
            min_runs=10,
            min_success_rate=0.65,
            max_cost_regression_pct=10,
            max_latency_regression_pct=15,
        ),
        StrategyPackRolloutPhase(
            phase="canary",
            rollout_percent=5,
            min_runs=40,
            min_success_rate=0.70,
            max_cost_regression_pct=5,
            max_latency_regression_pct=10,
        ),
        StrategyPackRolloutPhase(
            phase="rollout",
            rollout_percent=25,
            min_runs=100,
            min_success_rate=0.74,
            max_cost_regression_pct=0,
            max_latency_regression_pct=8,
        ),
    ]


def _guardrails(*, risk: str) -> dict[str, Any]:
    return {
        "must_improve_or_match": [
            "success_rate",
            "user_satisfaction",
            "verification_pass_rate",
        ],
        "must_not_regress": [
            "cross_tenant_access",
            "unauthorized_world_action",
            "budget_overrun",
            "rollback_failure",
        ],
        "human_review_required_before_canary": True,
        "auto_rollback_on_guardrail_breach": True,
        "risk": risk,
    }


def _plan_id(draft_id: str, proposed_pack_id: str) -> str:
    digest = hashlib.sha256(f"{draft_id}:{proposed_pack_id}".encode()).hexdigest()
    return f"qsp_plan_{digest[:16]}"


__all__ = [
    "StrategyPackRolloutPhase",
    "StrategyPackRolloutPlan",
    "StrategyPackRolloutPlanReport",
    "build_strategy_pack_rollout_plan",
    "plan_strategy_pack_rollouts",
]
