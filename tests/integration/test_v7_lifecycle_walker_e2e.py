"""V7 Phase X.C.LIFECYCLE-WALKER — 9 阶段全流程 e2e against real PG.

V7 §15 capability lifecycle has 9 阶段:

    OBSERVATION → CANDIDATE → REPLAY → HOLDOUT → SHADOW →
    CANARY → PRODUCTION → MONITOR → ROLLBACK → RETIRE

`CapabilityLifecycleService` enforces V7 §12.2 / §12.3 invariants at the
service layer, and ``lifecycle_transitions`` table CHECK constraints are the
bottom-of-stack double-保险 (alembic 0015 + 0017).

What was missing before this file: nobody had ever walked a real synthetic
capability through all 9 stages end-to-end against real Postgres, reading
back each transition row to confirm the chain. The integration test for the
DB writer (test_integration_capability_lifecycle_db.py) uses a fake session,
so PG CHECK violation behavior is verified in isolation by
test_v7_xb_pg_check_constraints.py — but the **full chain** had no proof.

This file is the 全流程演示 evidence:

    1. service.transition() × 8 (full happy chain to MONITOR)
    2. each emits real Row to PG via make_lifecycle_transition_emitter
    3. read back via list_recent_lifecycle_transitions (cockpit reader)
    4. verify chain order + every invariant honored
    5. also exercise rollback branch + retire terminal state
    6. negative tests: skip-stage / production-without-approval /
       replay-without-3-evidence all raise at service layer
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.api.cockpit_readers import list_recent_lifecycle_transitions
from kun.governance.capability_lifecycle import (
    CapabilityLifecycleError,
    CapabilityLifecycleService,
    CapabilityLifecycleStage,
)
from kun.integration.capability_lifecycle_db import (
    make_lifecycle_transition_emitter,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _reset_engines_between_tests() -> Any:
    """Fresh sessionmaker per test (same fix as PG CHECK / crash-resume tests)."""
    import kun.core.db as db_mod

    db_mod._sessionmaker = None  # type: ignore[attr-defined]
    db_mod._admin_sessionmaker = None  # type: ignore[attr-defined]
    db_mod._engine = None  # type: ignore[attr-defined]
    db_mod._admin_engine = None  # type: ignore[attr-defined]
    yield
    db_mod._sessionmaker = None  # type: ignore[attr-defined]
    db_mod._admin_sessionmaker = None  # type: ignore[attr-defined]
    db_mod._engine = None  # type: ignore[attr-defined]
    db_mod._admin_engine = None  # type: ignore[attr-defined]


async def _skip_if_no_pg() -> None:
    """Skip the test if PG isn't reachable — same pattern as crash-resume e2e."""
    try:
        from kun.core.db import session_scope
        from sqlalchemy import text

        async with session_scope(tenant_id="t-lcwalk-probe") as s:
            await s.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PG unavailable: {type(e).__name__}: {e}")


# ============================================================
# Full 9-stage walk — happy chain to PRODUCTION → MONITOR
# ============================================================


async def test_walk_full_lifecycle_observation_to_monitor_against_real_pg() -> None:
    """**The wiring proof**: walk a synthetic capability through 8 transitions
    (OBSERVATION → CANDIDATE → REPLAY → HOLDOUT → SHADOW → CANARY →
    PRODUCTION → MONITOR), each emitted to real PG, then read back via
    cockpit reader to confirm the chain is intact + ordered.
    """
    await _skip_if_no_pg()

    tenant_id = "t-lcwalk-full"
    capability_id = f"cap-walk-{id(test_walk_full_lifecycle_observation_to_monitor_against_real_pg)}"

    service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter(tenant_id),
    )

    # Step 1: OBSERVATION → CANDIDATE
    t1 = await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.OBSERVATION,
        to_stage=CapabilityLifecycleStage.CANDIDATE,
        decision_rationale="opportunity surfaced from dogfood",
    )
    assert t1.from_stage == CapabilityLifecycleStage.OBSERVATION
    assert t1.to_stage == CapabilityLifecycleStage.CANDIDATE

    # Step 2: CANDIDATE → REPLAY (needs 3 evidence kinds per V7 §12.3)
    three_evidence = [
        "strategy_replay_report:rr-walk-1",
        "process_audit:pa-walk-1",
        "capability_candidate:cc-walk-1",
    ]
    t2 = await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.CANDIDATE,
        to_stage=CapabilityLifecycleStage.REPLAY,
        evidence_refs=three_evidence,
        decision_rationale="3 evidence kinds collected, ready to replay",
        metrics_snapshot={"replay_planned_traces": 500},
    )
    assert t2.evidence_refs == three_evidence

    # Step 3: REPLAY → HOLDOUT
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.REPLAY,
        to_stage=CapabilityLifecycleStage.HOLDOUT,
        evidence_refs=["strategy_replay_report:rr-walk-1"],
        decision_rationale="replay no regression vs baseline",
        metrics_snapshot={
            "baseline_score": 0.78,
            "candidate_score": 0.82,
            "delta_pct": 5.1,
        },
    )

    # Step 4: HOLDOUT → SHADOW
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.HOLDOUT,
        to_stage=CapabilityLifecycleStage.SHADOW,
        decision_rationale="holdout test on 200 traces, +4.2% delta",
        metrics_snapshot={"holdout_traces": 200, "holdout_delta_pct": 4.2},
    )

    # Step 5: SHADOW → CANARY
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.SHADOW,
        to_stage=CapabilityLifecycleStage.CANARY,
        decision_rationale="shadow parallel 48h, no drift, no error spike",
        metrics_snapshot={"shadow_hours": 48, "shadow_error_rate": 0.001},
    )

    # Step 6: CANARY → PRODUCTION (needs user_approval_ticket per V7 §12.2)
    approval_ticket = "tk-prod-flip-walk-001"
    t6 = await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.CANARY,
        to_stage=CapabilityLifecycleStage.PRODUCTION,
        user_approval_ticket_id=approval_ticket,
        evidence_refs=["canary_metrics:cm-walk-1"],
        decision_rationale="canary 10% × 3 days, no regression, user approved",
        metrics_snapshot={"canary_pct": 10, "canary_days": 3},
    )
    assert t6.user_approval_ticket_id == approval_ticket

    # Step 7: PRODUCTION → MONITOR
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.PRODUCTION,
        to_stage=CapabilityLifecycleStage.MONITOR,
        decision_rationale="flipped to production, now continuous monitoring",
    )

    # ============================================================
    # Read back from PG — full chain should be intact
    # ============================================================
    result = await list_recent_lifecycle_transitions(
        tenant_id=tenant_id,
        capability_id=capability_id,
        limit=20,
    )
    assert result.error_kind is None, (
        f"Reader failed: kind={result.error_kind}, detail={result.error_detail}"
    )
    assert len(result.rows) == 7, (
        f"Expected 7 transitions in PG, got {len(result.rows)}: {result.rows}"
    )

    # Reader returns DESC by decided_at — newest first.
    stages_seen = [(r["from_stage"], r["to_stage"]) for r in result.rows]
    # Reverse to chronological
    chain_chronological = list(reversed(stages_seen))
    assert chain_chronological == [
        ("observation", "candidate"),
        ("candidate", "replay"),
        ("replay", "holdout"),
        ("holdout", "shadow"),
        ("shadow", "canary"),
        ("canary", "production"),
        ("production", "monitor"),
    ], f"Chain order wrong: {chain_chronological}"

    # The production transition row must have approval ticket
    prod_row = next(r for r in result.rows if r["to_stage"] == "production")
    assert prod_row["user_approval_ticket_id"] == approval_ticket

    # The replay transition row must have ≥1 evidence
    replay_row = next(r for r in result.rows if r["to_stage"] == "replay")
    assert len(replay_row["evidence_refs"]) >= 1


# ============================================================
# Rollback branch — PRODUCTION → ROLLBACK → RETIRE
# ============================================================


async def test_rollback_branch_from_production_terminates_in_retire() -> None:
    """V7 §15 rollback branch:
    after PRODUCTION, can transition to ROLLBACK (regression detected),
    then ROLLBACK → RETIRE (终态)."""
    await _skip_if_no_pg()

    tenant_id = "t-lcwalk-rollback"
    capability_id = f"cap-rb-{id(test_rollback_branch_from_production_terminates_in_retire)}"

    service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter(tenant_id),
    )

    # Quick walk to PRODUCTION (no read-back yet)
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.OBSERVATION,
        to_stage=CapabilityLifecycleStage.CANDIDATE,
    )
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.CANDIDATE,
        to_stage=CapabilityLifecycleStage.REPLAY,
        evidence_refs=[
            "strategy_replay_report:rr-rb",
            "process_audit:pa-rb",
            "capability_candidate:cc-rb",
        ],
    )
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.REPLAY,
        to_stage=CapabilityLifecycleStage.HOLDOUT,
        evidence_refs=["strategy_replay_report:rr-rb"],
    )
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.HOLDOUT,
        to_stage=CapabilityLifecycleStage.SHADOW,
    )
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.SHADOW,
        to_stage=CapabilityLifecycleStage.CANARY,
    )
    await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.CANARY,
        to_stage=CapabilityLifecycleStage.PRODUCTION,
        user_approval_ticket_id="tk-rb-prod-001",
        evidence_refs=["canary_metrics:cm-rb"],
    )

    # Now regression detected → ROLLBACK
    rollback_record = await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.PRODUCTION,
        to_stage=CapabilityLifecycleStage.ROLLBACK,
        decision_rationale="error_rate spiked 5x baseline within 6h of prod flip",
        metrics_snapshot={"error_rate_x_baseline": 5.1},
    )
    assert rollback_record.to_stage == CapabilityLifecycleStage.ROLLBACK

    # ROLLBACK → RETIRE (终态)
    retire_record = await service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.ROLLBACK,
        to_stage=CapabilityLifecycleStage.RETIRE,
        decision_rationale="rolled back + root cause identified, retire candidate",
    )
    assert retire_record.to_stage == CapabilityLifecycleStage.RETIRE

    # Confirm rollback + retire transitions both in PG
    result = await list_recent_lifecycle_transitions(
        tenant_id=tenant_id,
        capability_id=capability_id,
        limit=20,
    )
    assert result.error_kind is None
    to_stages = {r["to_stage"] for r in result.rows}
    assert "rollback" in to_stages
    assert "retire" in to_stages

    # RETIRE is 终态 — service blocks any further transition
    with pytest.raises(CapabilityLifecycleError, match="Illegal transition"):
        await service.transition(
            capability_id=capability_id,
            from_stage=CapabilityLifecycleStage.RETIRE,
            to_stage=CapabilityLifecycleStage.OBSERVATION,
        )


# ============================================================
# V7 §12.2 invariant — production flip needs user approval
# ============================================================


async def test_canary_to_production_without_approval_raises_at_service() -> None:
    """V7 §12.2: CANARY → PRODUCTION without user_approval_ticket_id must
    raise CapabilityLifecycleError at the service layer — request never
    even reaches PG."""
    # No PG dependency — service-layer validation only.
    service = CapabilityLifecycleService()  # no emitter, no DB needed

    with pytest.raises(CapabilityLifecycleError, match="user approval"):
        await service.transition(
            capability_id="cap-no-approval",
            from_stage=CapabilityLifecycleStage.CANARY,
            to_stage=CapabilityLifecycleStage.PRODUCTION,
            evidence_refs=["canary_metrics:cm-x"],
            # user_approval_ticket_id intentionally omitted
        )


# ============================================================
# V7 §12.3 invariant — replay entry needs 3 evidence kinds
# ============================================================


async def test_candidate_to_replay_without_three_evidence_raises() -> None:
    """V7 §12.3: CANDIDATE → REPLAY must have all 3 evidence kinds
    (strategy_replay_report / process_audit / capability_candidate).
    Missing any one raises at service layer."""
    service = CapabilityLifecycleService()

    # Only 2 of 3 kinds present
    with pytest.raises(CapabilityLifecycleError, match="three evidence"):
        await service.transition(
            capability_id="cap-2-evidence",
            from_stage=CapabilityLifecycleStage.CANDIDATE,
            to_stage=CapabilityLifecycleStage.REPLAY,
            evidence_refs=[
                "strategy_replay_report:rr-x",
                "process_audit:pa-x",
                # capability_candidate missing
            ],
        )


# ============================================================
# Adjacency invariant — no skipping stages
# ============================================================


@pytest.mark.parametrize(
    "from_stage,to_stage",
    [
        # Skip CANDIDATE altogether
        (CapabilityLifecycleStage.OBSERVATION, CapabilityLifecycleStage.REPLAY),
        # Skip HOLDOUT
        (CapabilityLifecycleStage.REPLAY, CapabilityLifecycleStage.SHADOW),
        # Skip SHADOW
        (CapabilityLifecycleStage.HOLDOUT, CapabilityLifecycleStage.CANARY),
        # Skip CANARY (back-door to PRODUCTION)
        (CapabilityLifecycleStage.SHADOW, CapabilityLifecycleStage.PRODUCTION),
        # Skip the whole acceptance ladder
        (CapabilityLifecycleStage.CANDIDATE, CapabilityLifecycleStage.PRODUCTION),
    ],
)
async def test_skipping_stages_raises_at_service_layer(
    from_stage: CapabilityLifecycleStage,
    to_stage: CapabilityLifecycleStage,
) -> None:
    """V7 §15: 不允许跳级. Every non-adjacent transition raises before any
    DB write — no orphaned 'partial walk' rows possible."""
    service = CapabilityLifecycleService()

    with pytest.raises(CapabilityLifecycleError, match="Illegal transition"):
        await service.transition(
            capability_id="cap-skip",
            from_stage=from_stage,
            to_stage=to_stage,
            # provide evidence + approval so the failure is purely 'skipping'
            evidence_refs=[
                "strategy_replay_report:rr",
                "process_audit:pa",
                "capability_candidate:cc",
                "canary_metrics:cm",
            ],
            user_approval_ticket_id="tk-x",
        )


# ============================================================
# Stage filtering — cockpit reader can scope to a single capability + stage
# ============================================================


async def test_cockpit_reader_filters_by_to_stage_against_real_pg() -> None:
    """Reader's to_stage filter narrows to one stage — used by the cockpit
    UI to show only PRODUCTION flips, only ROLLBACKs, etc."""
    await _skip_if_no_pg()

    tenant_id = "t-lcwalk-filter"
    cap_a = f"cap-filt-a-{id(test_cockpit_reader_filters_by_to_stage_against_real_pg)}"

    service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter(tenant_id),
    )

    # 2 transitions for cap_a — only the 2nd ends in CANDIDATE
    await service.transition(
        capability_id=cap_a,
        from_stage=CapabilityLifecycleStage.OBSERVATION,
        to_stage=CapabilityLifecycleStage.CANDIDATE,
    )
    await service.transition(
        capability_id=cap_a,
        from_stage=CapabilityLifecycleStage.CANDIDATE,
        to_stage=CapabilityLifecycleStage.REPLAY,
        evidence_refs=[
            "strategy_replay_report:f-rr",
            "process_audit:f-pa",
            "capability_candidate:f-cc",
        ],
    )

    # Filter: only to_stage='replay' transitions for cap_a
    result = await list_recent_lifecycle_transitions(
        tenant_id=tenant_id,
        capability_id=cap_a,
        to_stage="replay",
        limit=10,
    )
    assert result.error_kind is None
    assert len(result.rows) == 1
    assert result.rows[0]["to_stage"] == "replay"
    assert result.rows[0]["capability_id"] == cap_a
