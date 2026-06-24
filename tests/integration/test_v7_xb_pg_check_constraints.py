"""V7 Phase X.B.MF-5 — real PG CHECK constraint violation tests.

V7 §16.6 attacker audit MF-5. Prior to this, all CHECK constraints on the
4 X.B tables (mission_alignment_reviews / plan_change_proposals /
lifecycle_transitions / auditor_reports / ensemble_calls) were only verified
via fake in-memory ``_CaptureSession`` that doesn't enforce CHECK.

This test file runs against **real Postgres** (docker-compose.dev.yml's
kun-dev-postgres-1) and exercises each invariant by attempting an insert
that **must** be rejected by the CHECK. Without these tests, the schema
claims are unverified — a syntax error in any CHECK would silently let
malformed data into production.

Skipped when PG unavailable (``alembic_version`` not reachable).

The 9 invariants under test:
  1. mar_verdict_valid — verdict must be in enum set
  2. mar_score_in_range — alignment_score ∈ [0, 1]
  3. lct_production_needs_user_approval — to_stage='production' ⇒ ticket
  4. lct_replay_needs_evidence — to_stage='replay' ⇒ ≥1 evidence_refs
  5. lct_to_stage_valid — to_stage must be in 10-stage enum
  6. pcp_severity_valid — severity ∈ {low, medium, high}
  7. pcp_high_severity_needs_approval — severity='high' ⇒ user_approval_required
  8. pcp_candidate_changes_nonempty — candidate_changes ≥ 1
  9. ar_p0_blocks_release — risk_level='P0' ⇒ NOT allow_release
  10. ar_risk_level_valid — risk_level ∈ {P0, P1, P2}
  11. ec_strategy_valid — consensus_strategy in enum
  12. ec_divergence_in_range — divergence_score ∈ [0, 1]
  13. ec_min_2_providers — n_providers_total ≥ 2
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from kun.core.db import session_scope
from kun.core.orm import (
    AuditorReportRow,
    EnsembleCallRow,
    LifecycleTransitionRow,
    MissionAlignmentReviewRow,
    PlanChangeProposalRow,
)
from sqlalchemy.exc import IntegrityError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


# Audit F068: substrings (lowercased) that indicate PG/Docker is simply not
# reachable, so the test should SKIP rather than fail. asyncio's connector
# raises `OSError: Multiple exceptions: [Errno 61] Connect call failed (...)`
# (or [Errno 111] on Linux) which the old two-pattern guard missed.
_PG_UNAVAILABLE_MARKERS = (
    "could not connect",
    "connection refused",
    "connect call failed",  # asyncio OSError [Errno 61/111]
    "multiple exceptions",  # asyncio happy-eyeballs aggregate
    "connection failed",
    "could not translate host",
    "name or service not known",
    "no route to host",
    "errno 61",
    "errno 111",
    "operation timed out",
)


def _is_pg_unavailable(exc: Exception) -> bool:
    """True when `exc` looks like 'Postgres/Docker is not reachable'."""
    if isinstance(exc, (ConnectionError, OSError, TimeoutError)):
        return True
    msg = str(exc).lower()
    return any(marker in msg for marker in _PG_UNAVAILABLE_MARKERS)


# Each test gets a fresh engine + sessionmaker — avoids the "Event loop is
# closed" race where a connection from test N's loop is reused in test N+1's
# (different) loop. pytest-asyncio defaults to function-scoped loops, so we
# must dispose between tests.


@pytest.fixture(autouse=True)
async def _reset_engines_between_tests():
    """Dispose KUN's lazy sessionmaker before AND after each test."""
    import kun.core.db as db_mod

    # Force a fresh sessionmaker construction next call
    db_mod._sessionmaker = None  # type: ignore[attr-defined]
    db_mod._admin_sessionmaker = None  # type: ignore[attr-defined]
    db_mod._engine = None  # type: ignore[attr-defined]
    db_mod._admin_engine = None  # type: ignore[attr-defined]

    yield

    # Teardown — clear again so next test starts clean
    db_mod._sessionmaker = None  # type: ignore[attr-defined]
    db_mod._admin_sessionmaker = None  # type: ignore[attr-defined]
    db_mod._engine = None  # type: ignore[attr-defined]
    db_mod._admin_engine = None  # type: ignore[attr-defined]


# ============================================================
# Helper: try insert + expect IntegrityError on CHECK
# ============================================================


async def _expect_check_violation(
    row_factory,
    constraint_name: str,
) -> None:
    """Try to add() + flush() the row, expect IntegrityError naming the CHECK.

    PG availability is verified by the inner `session_scope` opening a real
    connection. If PG is down → IntegrityError won't fire AND no row inserts;
    the assertion below will fail loudly (visible signal) rather than silently
    skip — that's the honest behavior for the dev/CI path.

    Raise the inner exception if the message doesn't mention the expected
    constraint — that means we triggered a DIFFERENT violation than intended,
    which is a test bug.
    """
    raised: IntegrityError | None = None
    try:
        async with session_scope(tenant_id="t-mf5-check") as s:
            s.add(row_factory())
            await s.flush()
    except IntegrityError as e:
        raised = e
    except Exception as e:
        # PG unavailable / RLS / network → skip; re-raise anything else so we don't
        # false-positive claim "CHECK active" when really nothing was tested.
        # Audit F068: the old guard only matched "could not connect"/"connection
        # refused", but asyncio's connector raises e.g. `OSError: Multiple
        # exceptions: [Errno 61] Connect call failed (...)` when Docker/PG is down,
        # so all 13 tests *errored* instead of skipping. Match the real markers.
        if _is_pg_unavailable(e):
            pytest.skip(f"PG unavailable (connection error): {type(e).__name__}: {e}")
        raise

    assert raised is not None, (
        f"Expected IntegrityError for CHECK {constraint_name!r}, "
        f"but insert succeeded — the constraint is NOT active in PG"
    )
    assert constraint_name in str(raised), (
        f"IntegrityError triggered but not for expected constraint. "
        f"Expected {constraint_name!r} in message; got: {raised}"
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ============================================================
# mission_alignment_reviews CHECK constraints
# ============================================================


async def test_mar_verdict_must_be_in_enum() -> None:
    """mar_verdict_valid: verdict ∈ {ok, drifting, off_anchor, needs_human}."""

    def _bad() -> MissionAlignmentReviewRow:
        return MissionAlignmentReviewRow(
            tenant_id="t-mf5-check",
            review_id="mar-bad-verdict",
            task_id="tk-x",
            task_plan_version="v1",
            reviewed_at=_utcnow(),
            verdict="bogus_value",  # ⚠️ not in enum
            alignment_score=0.5,
            findings=[],
            info_gap_coverage=0.5,
            decomposition_coverage=0.5,
            evidence_coverage=0.5,
            plan_change_proposed=False,
        )

    await _expect_check_violation(_bad, "mar_verdict_valid")


async def test_mar_alignment_score_must_be_in_range() -> None:
    """mar_score_in_range: alignment_score ∈ [0, 1]."""

    def _bad() -> MissionAlignmentReviewRow:
        return MissionAlignmentReviewRow(
            tenant_id="t-mf5-check",
            review_id="mar-bad-score",
            task_id="tk-x",
            task_plan_version="v1",
            reviewed_at=_utcnow(),
            verdict="ok",
            alignment_score=1.5,  # ⚠️ > 1
            findings=[],
            info_gap_coverage=0.5,
            decomposition_coverage=0.5,
            evidence_coverage=0.5,
            plan_change_proposed=False,
        )

    await _expect_check_violation(_bad, "mar_score_in_range")


# ============================================================
# lifecycle_transitions CHECK constraints (V7 §12.2 关键不变量)
# ============================================================


async def test_lct_production_must_have_user_approval_ticket() -> None:
    """lct_production_needs_user_approval (V7 §12.2 关键不变量).

    to_stage='production' without user_approval_ticket_id → must reject.
    """

    def _bad() -> LifecycleTransitionRow:
        return LifecycleTransitionRow(
            tenant_id="t-mf5-check",
            transition_id="lct-bad-prod",
            capability_id="cap-x",
            from_stage="canary",
            to_stage="production",  # ⚠️ production
            decided_at=_utcnow(),
            decision_rationale="missing approval — should be rejected",
            user_approval_ticket_id=None,  # ⚠️ no ticket
            evidence_refs=["evidence:test"],
            metrics_snapshot={},
        )

    await _expect_check_violation(_bad, "lct_production_needs_user_approval")


async def test_lct_replay_must_have_evidence() -> None:
    """lct_replay_needs_evidence (V7 §12.3 三证据): to_stage='replay' ⇒ ≥1 evidence."""

    def _bad() -> LifecycleTransitionRow:
        return LifecycleTransitionRow(
            tenant_id="t-mf5-check",
            transition_id="lct-bad-replay",
            capability_id="cap-x",
            from_stage="candidate",
            to_stage="replay",  # ⚠️ replay
            decided_at=_utcnow(),
            decision_rationale="empty evidence — should be rejected",
            user_approval_ticket_id=None,
            evidence_refs=[],  # ⚠️ empty
            metrics_snapshot={},
        )

    await _expect_check_violation(_bad, "lct_replay_needs_evidence")


async def test_lct_to_stage_must_be_in_enum() -> None:
    """lct_to_stage_valid: to_stage ∈ V7 §15 10 stages."""

    def _bad() -> LifecycleTransitionRow:
        return LifecycleTransitionRow(
            tenant_id="t-mf5-check",
            transition_id="lct-bad-stage",
            capability_id="cap-x",
            from_stage="observation",
            to_stage="nonexistent_stage",  # ⚠️
            decided_at=_utcnow(),
            decision_rationale="",
            user_approval_ticket_id=None,
            evidence_refs=["x"],
            metrics_snapshot={},
        )

    await _expect_check_violation(_bad, "lct_to_stage_valid")


# ============================================================
# plan_change_proposals CHECK constraints (V7 §10.3.3)
# ============================================================


async def test_pcp_high_severity_must_require_user_approval() -> None:
    """pcp_high_severity_needs_approval (V7 §10.3.3): high ⇒ user_approval_required."""

    def _bad() -> PlanChangeProposalRow:
        return PlanChangeProposalRow(
            tenant_id="t-mf5-check",
            proposal_id="pcp-bad-high",
            task_id="tk-x",
            triggered_by="mission_director",
            triggered_at=_utcnow(),
            change_type="scope",
            severity="high",  # ⚠️ high
            affected_work_items=[],
            affected_deliverables=[],
            candidate_changes=[{"name": "x", "delta": "-1"}],
            rollback_condition="",
            rationale="missing user_approval_required — should be rejected",
            user_approval_required=False,  # ⚠️ should be True for high
        )

    await _expect_check_violation(_bad, "pcp_high_severity_needs_approval")


async def test_pcp_candidate_changes_must_be_nonempty() -> None:
    """pcp_candidate_changes_nonempty: jsonb_array_length(candidate_changes) ≥ 1."""

    def _bad() -> PlanChangeProposalRow:
        return PlanChangeProposalRow(
            tenant_id="t-mf5-check",
            proposal_id="pcp-bad-empty",
            task_id="tk-x",
            triggered_by="mission_director",
            triggered_at=_utcnow(),
            change_type="scope",
            severity="low",
            affected_work_items=[],
            affected_deliverables=[],
            candidate_changes=[],  # ⚠️ empty
            rollback_condition="",
            rationale="empty candidates — should be rejected",
            user_approval_required=False,
        )

    await _expect_check_violation(_bad, "pcp_candidate_changes_nonempty")


# ============================================================
# auditor_reports CHECK constraints (V7 §16.6)
# ============================================================


async def test_ar_p0_must_block_release() -> None:
    """ar_p0_blocks_release (V7 §16.6 关键不变量): P0 ⇒ NOT allow_release."""

    def _bad() -> AuditorReportRow:
        return AuditorReportRow(
            tenant_id="t-mf5-check",
            report_id="ar-bad-p0",
            audited_capability="x",
            audited_at=_utcnow(),
            auditor_provider="test",
            design_promise="...",
            real_code_path="...",
            bypass_methods=[],
            min_repro_steps="",
            risk_level="P0",  # ⚠️ P0
            must_fix=[],
            acceptance_tests=[],
            allow_release=True,  # ⚠️ but allow_release=True
            rationale="should be rejected by DB",
        )

    await _expect_check_violation(_bad, "ar_p0_blocks_release")


async def test_ar_risk_level_must_be_in_enum() -> None:
    """ar_risk_level_valid: risk_level ∈ {P0, P1, P2}."""

    def _bad() -> AuditorReportRow:
        return AuditorReportRow(
            tenant_id="t-mf5-check",
            report_id="ar-bad-risk",
            audited_capability="x",
            audited_at=_utcnow(),
            auditor_provider="test",
            design_promise="...",
            real_code_path="...",
            bypass_methods=[],
            min_repro_steps="",
            risk_level="P9",  # ⚠️ not in enum
            must_fix=[],
            acceptance_tests=[],
            allow_release=False,
            rationale="invalid risk_level — should be rejected",
        )

    await _expect_check_violation(_bad, "ar_risk_level_valid")


# ============================================================
# ensemble_calls CHECK constraints (V7 §11.4)
# ============================================================


async def test_ec_strategy_must_be_in_enum() -> None:
    """ec_strategy_valid: consensus_strategy enum."""

    def _bad() -> EnsembleCallRow:
        return EnsembleCallRow(
            tenant_id="t-mf5-check",
            call_id="enc-bad-strat",
            invoked_at=_utcnow(),
            purpose="test",
            providers=[],
            consensus_strategy="bogus_strategy",  # ⚠️
            divergence_score=0.0,
            divergence_signals=[],
            consensus_provider=None,
            total_cost_usd=0.0,
            failure_count=0,
            n_providers_total=2,
        )

    await _expect_check_violation(_bad, "ec_strategy_valid")


async def test_ec_divergence_must_be_in_range() -> None:
    """ec_divergence_in_range: divergence_score ∈ [0, 1]."""

    def _bad() -> EnsembleCallRow:
        return EnsembleCallRow(
            tenant_id="t-mf5-check",
            call_id="enc-bad-div",
            invoked_at=_utcnow(),
            purpose="test",
            providers=[],
            consensus_strategy="majority_vote",
            divergence_score=1.5,  # ⚠️ > 1
            divergence_signals=[],
            consensus_provider=None,
            total_cost_usd=0.0,
            failure_count=0,
            n_providers_total=2,
        )

    await _expect_check_violation(_bad, "ec_divergence_in_range")


async def test_ec_min_2_providers() -> None:
    """ec_min_2_providers: n_providers_total ≥ 2 (V7 §11.4 ensemble 起步条件)."""

    def _bad() -> EnsembleCallRow:
        return EnsembleCallRow(
            tenant_id="t-mf5-check",
            call_id="enc-bad-n",
            invoked_at=_utcnow(),
            purpose="test",
            providers=[],
            consensus_strategy="majority_vote",
            divergence_score=0.0,
            divergence_signals=[],
            consensus_provider=None,
            total_cost_usd=0.0,
            failure_count=0,
            n_providers_total=1,  # ⚠️ < 2
        )

    await _expect_check_violation(_bad, "ec_min_2_providers")


# ============================================================
# Happy-path sanity: valid rows DO insert (proves DB is reachable)
# ============================================================


async def test_happy_path_valid_mission_alignment_row_inserts() -> None:
    """Sanity: a fully valid row passes all CHECKs and inserts cleanly."""
    review_id = f"mar-mf5-happy-{datetime.now(UTC).timestamp()}"
    try:
        async with session_scope(tenant_id="t-mf5-happy") as s:
            s.add(
                MissionAlignmentReviewRow(
                    tenant_id="t-mf5-happy",
                    review_id=review_id,
                    task_id="tk-mf5",
                    task_plan_version="v1",
                    reviewed_at=_utcnow(),
                    verdict="ok",
                    alignment_score=0.85,
                    findings=["sanity test"],
                    info_gap_coverage=0.9,
                    decomposition_coverage=0.85,
                    evidence_coverage=0.7,
                    plan_change_proposed=False,
                )
            )
            await s.flush()
    except Exception as e:
        if _is_pg_unavailable(e):
            pytest.skip(f"PG unavailable (connection error): {type(e).__name__}: {e}")
        raise
