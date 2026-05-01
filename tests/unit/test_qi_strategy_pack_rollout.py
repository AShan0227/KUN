from __future__ import annotations

import pytest
from kun.context.assets import LayeredAsset
from kun.context.storage import InMemoryAssetStore
from kun.qi.strategy_pack_rollout import (
    build_strategy_pack_rollout_plan,
    create_strategy_pack_shadow_experiment,
    plan_strategy_pack_rollouts,
)


def _reviewed_asset(
    *,
    tenant_id: str = "t-1",
    review_status: str = "ready_for_human_review",
    risk: str = "low",
    human_approved: bool = False,
) -> LayeredAsset:
    return LayeredAsset.build(
        "methodology",
        tenant_id,
        metadata={
            "source": "qi.idle_replay.strategy_pack_draft",
            "draft_id": "spd-1",
            "proposed_pack_id": "qi_marketing_pack",
            "qi_review_status": review_status,
            "qi_review_risk": risk,
            "production_action": False,
            "qi_rollout_human_approved": human_approved,
        },
        summary="reviewed strategy draft",
        tags=["qi", "strategy_pack_draft", f"qi_review:{review_status}"],
    )


def test_ready_strategy_draft_gets_shadow_first_rollout_plan() -> None:
    asset = _reviewed_asset()

    plan = build_strategy_pack_rollout_plan(asset)

    assert plan.status == "shadow_plan"
    assert plan.production_action is False
    assert plan.promotion_allowed is False
    assert plan.requires_human_approval is True
    assert plan.phases[0].phase == "shadow"
    assert plan.phases[0].rollout_percent == 0
    assert plan.guardrails["auto_rollback_on_guardrail_breach"] is True


def test_not_ready_strategy_draft_rollout_plan_is_blocked() -> None:
    asset = _reviewed_asset(review_status="needs_evidence")

    plan = build_strategy_pack_rollout_plan(asset)

    assert plan.status == "blocked"
    assert "review_status_not_ready:needs_evidence" in plan.reasons


@pytest.mark.asyncio
async def test_plan_strategy_pack_rollouts_writes_review_only_plan() -> None:
    store = InMemoryAssetStore()
    asset = _reviewed_asset(risk="high")
    await store.put(asset)

    report = await plan_strategy_pack_rollouts(
        tenant_id="t-1",
        store=store,
        dry_run=False,
    )

    updated = await store.get(asset.asset_id, tenant_id="t-1")
    assert report.scanned == 1
    assert report.planned == 1
    assert report.updated == 1
    assert updated is not None
    assert updated.l1_metadata["qi_rollout_plan_status"] == "shadow_plan"
    assert updated.l1_metadata["qi_rollout_plan"]["phases"][0]["phase"] == "shadow"
    assert updated.l1_metadata["qi_rollout_plan"]["phases"][1]["rollout_percent"] == 1
    assert updated.l1_metadata["production_action"] is False
    assert "qi_rollout:shadow_plan" in updated.tags


@pytest.mark.asyncio
async def test_strategy_pack_experiment_bridge_requires_human_approval() -> None:
    store = InMemoryAssetStore()
    asset = _reviewed_asset()
    await store.put(asset)
    await plan_strategy_pack_rollouts(tenant_id="t-1", store=store, dry_run=False)

    report = await create_strategy_pack_shadow_experiment(
        tenant_id="t-1",
        draft_id="spd-1",
        store=store,
        dry_run=True,
    )

    assert report.status == "blocked"
    assert report.production_action is False
    assert "human_approval_required_before_experiment_draft" in report.reasons


@pytest.mark.asyncio
async def test_strategy_pack_experiment_bridge_creates_draft_only_after_approval() -> None:
    store = InMemoryAssetStore()
    asset = _reviewed_asset(human_approved=True)
    await store.put(asset)
    await plan_strategy_pack_rollouts(tenant_id="t-1", store=store, dry_run=False)
    created_payloads: list[dict[str, object]] = []

    async def fake_creator(
        tenant_id: str,
        experiment_id: str,
        control_variant: dict[str, object],
        treatment_variant: dict[str, object],
        guardrails: dict[str, object],
    ) -> bool:
        created_payloads.append(
            {
                "tenant_id": tenant_id,
                "experiment_id": experiment_id,
                "control_variant": control_variant,
                "treatment_variant": treatment_variant,
                "guardrails": guardrails,
            }
        )
        return True

    report = await create_strategy_pack_shadow_experiment(
        tenant_id="t-1",
        draft_id="spd-1",
        store=store,
        dry_run=False,
        experiment_creator=fake_creator,
    )
    updated = await store.get(asset.asset_id, tenant_id="t-1")

    assert report.status == "experiment_draft_created"
    assert report.rollout_percent == 0
    assert report.production_action is False
    assert created_payloads[0]["tenant_id"] == "t-1"
    treatment = created_payloads[0]["treatment_variant"]
    assert isinstance(treatment, dict)
    assert treatment["shadow_only"] is True
    assert treatment["production_action"] is False
    assert updated is not None
    assert updated.l1_metadata["qi_rollout_experiment_status"] == "draft_created"
    assert updated.l1_metadata["production_action"] is False
