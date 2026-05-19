from __future__ import annotations

import pytest
from kun.control_plane.capability_evolution import (
    CapabilityCandidate,
    CapabilityEvaluation,
    CapabilitySource,
    build_capability_promotion,
    build_capability_rollback,
)
from kun.control_plane.nuo import NuoObservation, diagnose_nuo_health
from kun.control_plane.runtime import InMemoryControlPlane
from kun.control_plane.store import InMemoryControlPlaneStore
from pydantic import ValidationError

pytestmark = pytest.mark.unit


def _candidate(**overrides: object) -> CapabilityCandidate:
    payload: dict[str, object] = {
        "candidate_id": "cand-ab-gap-repair",
        "capability_name": "Evidence-backed repair planning",
        "source": "ab_gap",
        "source_ref": "qi-ab-round-17",
        "hypothesis": "Require repair plans to bind symptoms to replay evidence.",
        "target_task_types": ["self_improvement", "product_development"],
        "proposed_change_refs": ["artifact-diff-repair-planner"],
        "evidence_refs": ["artifact-gap-analysis"],
        "known_limits": ["Does not apply to unauthorized external actions."],
    }
    payload.update(overrides)
    return CapabilityCandidate.model_validate(payload)


def _evaluation(stage: str, **overrides: object) -> CapabilityEvaluation:
    payload: dict[str, object] = {
        "evaluation_id": f"eval-{stage}",
        "candidate_id": "cand-ab-gap-repair",
        "stage": stage,
        "mission_id": "msn-capability-evolution",
        "task_plan_version": "v6",
        "subject_ref": f"work-capability-{stage}",
        "passed": True,
        "result_quality": 0.91,
        "speed": 0.72,
        "cost": 0.68,
        "risk": 0.2,
        "evidence_refs": [f"artifact-evidence-{stage}"],
        "artifact_refs": [f"artifact-report-{stage}"],
        "review_refs": [f"artifact-review-{stage}"],
    }
    if stage in {"holdout", "canary", "production"}:
        payload["holdout_refs"] = ["artifact-holdout-suite"]
    if stage in {"canary", "production"}:
        payload["regression_refs"] = ["artifact-regression-suite"]
        payload["rollback_plan"] = ["disable capability profile", "restore previous runtime routing"]
    payload.update(overrides)
    return CapabilityEvaluation.model_validate(payload)


@pytest.mark.parametrize(
    "source",
    [
        "ab_gap",
        "real_task_review",
        "enterprise_project",
        "open_source_project",
        "expert_input",
    ],
)
def test_capability_candidate_accepts_all_v6_sources(source: CapabilitySource) -> None:
    candidate = _candidate(source=source, source_ref=f"source-{source}")

    assert candidate.source == source
    assert candidate.created_by == "qi"
    assert "self_improvement" in candidate.target_task_types


def test_replay_promotion_outputs_capability_profile() -> None:
    promotion = build_capability_promotion(
        _candidate(),
        [_evaluation("replay")],
        target_stage="replay",
        capability_id="cap-repair-planning",
    )

    assert promotion.decision == "approved"
    assert promotion.target_stage == "replay"
    assert promotion.capability_profile is not None
    assert promotion.capability_profile.capability_id == "cap-repair-planning"
    assert promotion.capability_profile.promotion_stage == "replay"
    assert promotion.capability_profile.evidence_refs == [
        "artifact-gap-analysis",
        "artifact-evidence-replay",
    ]
    assert promotion.gate_evaluation.next_action == "promote_candidate"
    assert promotion.gate_evaluation.next_state == "learning_writeback"


def test_production_promotion_requires_full_stage_chain_and_outputs_profile() -> None:
    promotion = build_capability_promotion(
        _candidate(),
        [
            _evaluation("replay"),
            _evaluation("holdout"),
            _evaluation("shadow"),
            _evaluation("canary"),
            _evaluation("production"),
        ],
        target_stage="production",
    )

    assert promotion.decision == "approved"
    assert promotion.capability_profile is not None
    profile = promotion.capability_profile
    assert profile.promotion_stage == "production"
    assert profile.evidence_refs
    assert profile.holdout_refs == ["artifact-holdout-suite"]
    assert profile.regression_refs == ["artifact-regression-suite"]
    assert profile.rollback_plan == ["disable capability profile", "restore previous runtime routing"]
    assert promotion.gate_evaluation.learning_eligibility == "ready_for_shadow"
    assert promotion.gate_evaluation.score_breakdown["holdout_ref_count"] == 1.0
    assert promotion.gate_evaluation.score_breakdown["regression_ref_count"] == 1.0


def test_runtime_default_capabilities_only_load_production_profiles() -> None:
    store = InMemoryControlPlaneStore()
    runtime = InMemoryControlPlane(store=store)

    replay_promotion = build_capability_promotion(
        _candidate(),
        [_evaluation("replay")],
        target_stage="replay",
        capability_id="cap-replay-only",
    )
    replay_profile = runtime.apply_capability_promotion(replay_promotion)

    assert replay_profile is not None
    assert replay_profile.promotion_stage == "replay"
    assert runtime.list_default_runtime_capabilities() == []
    assert store.list_capability_profiles()[0].capability_id == "cap-replay-only"

    recovered_runtime = InMemoryControlPlane(store=store)
    assert recovered_runtime.list_default_runtime_capabilities() == []

    production_promotion = build_capability_promotion(
        _candidate(),
        [
            _evaluation("replay"),
            _evaluation("holdout"),
            _evaluation("shadow"),
            _evaluation("canary"),
            _evaluation("production"),
        ],
        target_stage="production",
        capability_id="cap-production-default",
    )
    production_profile = recovered_runtime.apply_capability_promotion(production_promotion)

    assert production_profile is not None
    assert production_profile.promotion_stage == "production"
    assert [
        profile.capability_id
        for profile in recovered_runtime.list_default_runtime_capabilities()
    ] == ["cap-production-default"]
    promotion_events = [
        event
        for event in recovered_runtime.ledger_events.values()
        if event.event_type == "promotion"
    ]
    assert promotion_events[-1].payload["default_runtime_enabled"] is True


def test_runtime_rolls_back_failed_production_capability_from_default_path() -> None:
    store = InMemoryControlPlaneStore()
    runtime = InMemoryControlPlane(store=store)
    promotion = build_capability_promotion(
        _candidate(),
        [
            _evaluation("replay"),
            _evaluation("holdout"),
            _evaluation("shadow"),
            _evaluation("canary"),
            _evaluation("production"),
        ],
        target_stage="production",
        capability_id="cap-production-default",
    )
    profile = runtime.apply_capability_promotion(promotion)
    assert profile is not None
    assert runtime.list_default_runtime_capabilities()

    rollback = build_capability_rollback(
        profile,
        _evaluation(
            "production",
            passed=False,
            result_quality=0.72,
            hard_gate_failures=["production_runtime_regression"],
            evidence_refs=["artifact-production-failure"],
            review_refs=["artifact-gpt55-supervision"],
        ),
        reason="real dogfood regression reduced delivery quality",
    )
    rolled_back = runtime.apply_capability_rollback(rollback)

    assert rolled_back.runtime_enabled is False
    assert rolled_back.rolled_back_at is not None
    assert rolled_back.rollback_reason == "real dogfood regression reduced delivery quality"
    assert runtime.list_default_runtime_capabilities() == []

    recovered_runtime = InMemoryControlPlane(store=store)
    assert recovered_runtime.capability_profiles["cap-production-default"].runtime_enabled is False
    assert recovered_runtime.list_default_runtime_capabilities() == []
    rollback_events = [
        event
        for event in recovered_runtime.ledger_events.values()
        if event.event_type == "rollback"
    ]
    assert rollback_events
    assert rollback_events[-1].payload["default_runtime_enabled"] is False
    assert recovered_runtime.gate_evaluations[
        rollback.gate_evaluation.gate_evaluation_id
    ].next_action == "rollback_capability"


def test_production_is_blocked_without_replay_or_holdout_evidence() -> None:
    promotion = build_capability_promotion(
        _candidate(),
        [
            _evaluation("shadow"),
            _evaluation("canary"),
            _evaluation("production"),
        ],
        target_stage="production",
    )

    assert promotion.decision == "blocked"
    assert promotion.capability_profile is None
    assert promotion.blocked_by_nuo is False
    assert promotion.gate_evaluation.failure_category == "model_quality_failure"
    assert {
        "replay_evaluation_missing_or_failed",
        "holdout_evaluation_missing_or_failed",
    }.issubset(set(promotion.hard_gate_failures))
    assert promotion.gate_evaluation.learning_eligibility == "blocked"


def test_nuo_block_prevents_capability_profile_output() -> None:
    nuo_report = diagnose_nuo_health(
        NuoObservation(
            mission_id="msn-capability-evolution",
            task_plan_version="v6",
            subject_ref="cand-ab-gap-repair",
            task_type="self_improvement",
            output_text="[stub echo] claimed replay result",
            report_ref="artifact-report",
            review_count=1,
            expected_review_count=1,
        )
    )

    promotion = build_capability_promotion(
        _candidate(),
        [_evaluation("replay")],
        target_stage="replay",
        nuo_report=nuo_report,
    )

    assert promotion.decision == "blocked"
    assert promotion.blocked_by_nuo is True
    assert promotion.capability_profile is None
    assert promotion.nuo_finding_refs == ["nuo-cand-ab-gap-repair-stub_echo"]
    assert promotion.hard_gate_failures == ["stub_echo"]
    assert promotion.gate_evaluation.failure_category == "tool_failure"
    assert promotion.gate_evaluation.next_action == "needs_repair"


def test_evaluation_validator_blocks_low_quality_speed_cost_tradeoff() -> None:
    with pytest.raises(ValidationError, match="cannot offset low result quality"):
        _evaluation("replay", result_quality=0.79, speed=1.0, cost=1.0)


def test_canary_and_production_evaluations_require_regression_and_rollback() -> None:
    with pytest.raises(ValidationError, match="require holdout, regression, and rollback"):
        _evaluation(
            "production",
            holdout_refs=["artifact-holdout-suite"],
            regression_refs=[],
            rollback_plan=[],
        )
