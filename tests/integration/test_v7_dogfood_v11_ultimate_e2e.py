"""V7 Phase X.C.DOGFOOD-V11 — ultimate e2e 串 7 件 against real PG.

The capstone test. Walks one synthetic "ship feature X" mission through
the **full V7 Phase X production-loop** end-to-end, exercising every
subsystem we built in Phase X.B + X.C:

  1. Mission Director review   → mission_alignment_reviews +1 row
  2. Trifecta coordinator      → 3 parallel lines, cost multiplier surfaced
  3. Lifecycle 9-stage walk    → lifecycle_transitions +8 rows
  4. CollaborationTicket gate  → production flip unblocked by approval
  5. Auditor report (P2)       → auditor_reports +1 row, allow_release=true
  6. Ensemble call recorded    → ensemble_calls +1 row (synth, no live LLM)
  7. Checkpoint write + crash + resume → task_checkpoints +3 rows, latest
                                          recovered after `del service`

This is V7 §16 production-loop **闭环 capstone evidence**. Every row
landing in real PG via the same code paths production daemons use.

Scenario:
    Mission "ship-feature-X" (mission_id derived from test id).
    Capability `cap-dogfood-v11-<id>` walks observation→monitor.
    A CollaborationTicket gates the canary→production flip.
    An AuditorReport on the capability surfaces a P2 risk but allows
    release. The trifecta coordinator evaluates at the SHADOW→CANARY
    milestone. Three checkpoints are written through the walk; after
    "crash" (discard the service), a fresh reader recovers the latest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kun.agents.executor.checkpoint import TaskCheckpoint
from kun.agents.mission_director.service import (
    AlignmentVerdict,
    MissionAlignmentReview,
)
from kun.agents.trifecta import (
    TrifectaCoordinator,
    TrifectaState,
)
from kun.api.cockpit_readers import (
    list_recent_auditor_reports,
    list_recent_lifecycle_transitions,
    list_recent_mission_reviews,
)
from kun.control_plane.collaboration import (
    CollaborationResponse,
    InMemoryCollaborationQueue,
)
from kun.control_plane.v6 import CollaborationTicket
from kun.governance.capability_lifecycle import (
    CapabilityLifecycleService,
    CapabilityLifecycleStage,
)
from kun.integration.auditor_report_db import (
    AuditorReport,
    write_auditor_report,
)
from kun.integration.capability_lifecycle_db import (
    make_lifecycle_transition_emitter,
)
from kun.integration.checkpoint_db import (
    make_checkpoint_reader,
    make_checkpoint_writer,
)
from kun.integration.ensemble_invoker import (
    EnsembleCallRecord,
    write_ensemble_call,
)
from kun.integration.mission_director_db import write_mission_review

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _reset_engines_between_tests() -> Any:
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
    try:
        from kun.core.db import session_scope
        from sqlalchemy import text

        async with session_scope(tenant_id="t-v11-probe") as s:
            await s.execute(text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"PG unavailable: {type(e).__name__}: {e}")


# ============================================================
# THE CAPSTONE — one big walk
# ============================================================


async def test_dogfood_v11_full_production_loop_chain_lands_in_real_pg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The capstone**: one synthetic mission walks every Phase X.B + X.C
    subsystem; verify all 4 X.B tables + lifecycle + checkpoints have the
    rows we expect, and the production flip is gated on a CollaborationTicket
    approval. Failure of any link breaks the V7 §16 loop closure."""
    await _skip_if_no_pg()
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")

    tenant_id = "t-v11-capstone"
    test_uid = id(test_dogfood_v11_full_production_loop_chain_lands_in_real_pg)
    mission_id = f"msn-v11-{test_uid}"
    task_id = f"tk-v11-{test_uid}"
    capability_id = f"cap-v11-{test_uid}"
    ticket_id = f"tk-collab-v11-{test_uid}"
    task_plan_version = "v1.0.0-v11"

    # ============================================================
    # PIECE 1 — Mission Director review (writes mission_alignment_reviews)
    # ============================================================
    review = MissionAlignmentReview(
        review_id=f"mar-v11-{test_uid}",
        task_id=task_id,
        task_plan_version=task_plan_version,
        reviewed_at=datetime.now(UTC),
        verdict=AlignmentVerdict.OK,
        alignment_score=0.92,
        findings=[
            "info_gap coverage 85%, on track",
            "all decomposed work_items have evidence refs",
        ],
        info_gap_coverage=0.85,
        decomposition_coverage=1.0,
        evidence_coverage=0.95,
        plan_change_proposed=False,
        plan_change_proposal_id=None,
    )
    review_id = await write_mission_review(
        tenant_id=tenant_id, review=review
    )
    assert review_id == review.review_id

    # ============================================================
    # PIECE 2 — Trifecta coordinator runs at SHADOW→CANARY milestone
    # ============================================================
    async def _past(
        _task_id: str, _recent: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        return (
            [
                {
                    "finding": "rollback in prior canary linked to fixture cleanup race",
                    "case_id": "bug_root_cause_cases:brc-v11-1",
                }
            ],
            0.002,
            None,
        )

    async def _present(
        _task_id: str, _cur: dict[str, Any]
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        return (
            [
                {"critique": "shadow latency p99 within 1.1x baseline — proceed"},
                {"critique": "no error spike in last 6h shadow window"},
            ],
            0.006,
            None,
        )

    async def _future(
        _task_id: str, _plan: dict[str, Any], n: int
    ) -> tuple[list[dict[str, Any]], float, str | None]:
        return (
            [
                {"candidate": f"plan-{i}", "metric": 0.80 + 0.02 * i}
                for i in range(n)
            ],
            0.022,
            None,
        )

    trifecta = TrifectaCoordinator(
        past_hook=_past, present_hook=_present, future_hook=_future
    )
    trifecta_report = await trifecta.run(
        task_id=task_id,
        recent_steps=[{"step": "shadow-soak"}],
        current_step={"step": "canary-decision"},
        future_plan={"goal": "stable prod flip"},
        n_future_candidates=3,
        baseline_cost_usd=0.01,
    )
    assert trifecta_report.past.state == TrifectaState.OK
    assert trifecta_report.present.state == TrifectaState.OK
    assert trifecta_report.future.state == TrifectaState.OK
    assert trifecta_report.n_findings == 1 + 2 + 3  # 6 findings total
    assert trifecta_report.total_cost_usd == pytest.approx(0.030)
    assert trifecta_report.cost_multiplier_vs_baseline == pytest.approx(3.0)

    # ============================================================
    # PIECE 3 — Capability lifecycle walk (writes lifecycle_transitions)
    # ============================================================
    lifecycle_service = CapabilityLifecycleService(
        transition_emitter=make_lifecycle_transition_emitter(tenant_id),
    )

    await lifecycle_service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.OBSERVATION,
        to_stage=CapabilityLifecycleStage.CANDIDATE,
        decision_rationale="v11 dogfood opportunity",
    )
    await lifecycle_service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.CANDIDATE,
        to_stage=CapabilityLifecycleStage.REPLAY,
        evidence_refs=[
            "strategy_replay_report:rr-v11",
            "process_audit:pa-v11",
            "capability_candidate:cc-v11",
        ],
    )
    await lifecycle_service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.REPLAY,
        to_stage=CapabilityLifecycleStage.HOLDOUT,
        evidence_refs=["strategy_replay_report:rr-v11"],
    )
    await lifecycle_service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.HOLDOUT,
        to_stage=CapabilityLifecycleStage.SHADOW,
    )
    await lifecycle_service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.SHADOW,
        to_stage=CapabilityLifecycleStage.CANARY,
    )

    # ============================================================
    # PIECE 4 — CollaborationTicket gates the production flip
    # ============================================================
    queue = InMemoryCollaborationQueue()
    queue.submit(
        CollaborationTicket(
            ticket_id=ticket_id,
            mission_id=mission_id,
            type="approval",
            role_needed="release_owner",
            why_needed=f"V7 §12.2 production flip for {capability_id}",
            decision_options=["approve", "hold"],
            recommended_option="approve",
            context_ref=f"capability:{capability_id}",
            risk_if_skipped="opportunity cost / canary drift",
            deadline=datetime.now(UTC) + timedelta(hours=24),
            fallback_policy={"option": "hold", "reason": "default hold"},
            output_contract="approve|hold + rationale",
        )
    )
    queue.respond(
        CollaborationResponse(
            ticket_id=ticket_id,
            responder="release_owner_alice",
            selected_option="approve",
            answer="Canary green 72h, trifecta past/present/future all OK",
        )
    )
    assert queue.tickets[ticket_id].status == "answered"

    # Approval-driven CANARY → PRODUCTION
    await lifecycle_service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.CANARY,
        to_stage=CapabilityLifecycleStage.PRODUCTION,
        user_approval_ticket_id=ticket_id,
        evidence_refs=["canary_metrics:cm-v11"],
        decision_rationale=f"approved via ticket {ticket_id}",
    )
    # PRODUCTION → MONITOR
    await lifecycle_service.transition(
        capability_id=capability_id,
        from_stage=CapabilityLifecycleStage.PRODUCTION,
        to_stage=CapabilityLifecycleStage.MONITOR,
    )

    # ============================================================
    # PIECE 5 — Auditor report on the capability (P2, allow release)
    # ============================================================
    auditor_report = AuditorReport(
        report_id=f"ar-v11-{test_uid}",
        audited_capability=capability_id,
        audited_at=datetime.now(UTC),
        auditor_provider="openai/gpt-5.5+anthropic/qwen2.5:14b-cross-family",
        design_promise=f"capability {capability_id} ships feature-X via gated rollout",
        real_code_path="kun/governance/capability_lifecycle.py + integration/capability_lifecycle_db.py",
        bypass_methods=[
            "approval ticket replay (mitigated by 'closed' terminal state guard)",
        ],
        min_repro_steps="run dogfood_v11 e2e, inspect lifecycle_transitions row",
        risk_level="P2",
        must_fix=[],
        acceptance_tests=[
            "tests/integration/test_v7_lifecycle_walker_e2e.py",
            "tests/integration/test_v7_collab_human_in_loop_e2e.py",
            "tests/integration/test_v7_dogfood_v11_ultimate_e2e.py",
        ],
        allow_release=True,
        rationale="P2 — low residual risk, gate + invariants verified; release approved",
    )
    auditor_id = await write_auditor_report(
        tenant_id=tenant_id, report=auditor_report
    )
    assert auditor_id == auditor_report.report_id

    # ============================================================
    # PIECE 6 — Ensemble call recorded (synth — no live LLM)
    # ============================================================
    ensemble_record = EnsembleCallRecord(
        call_id=f"ec-v11-{test_uid}",
        invoked_at=datetime.now(UTC),
        purpose="dogfood-v11-shadow-decision",
        providers=[
            {"name": "openai", "model_id": "gpt-5.5", "family": "openai"},
            {
                "name": "ollama",
                "model_id": "qwen2.5:14b-instruct-q4_K_M",
                "family": "qwen",
            },
        ],
        consensus_strategy="majority_vote",
        divergence_score=0.18,
        divergence_signals=["minor wording difference in rationale"],
        consensus_provider="openai/gpt-5.5",
        total_cost_usd=0.0125,
        failure_count=0,
        n_providers_total=2,
        request_hash="dogfood-v11-shadow-hash-abc",
        consensus_content_excerpt="shadow latency p99 within tolerance, proceed",
    )
    ensemble_call_id = await write_ensemble_call(
        tenant_id=tenant_id, record=ensemble_record
    )
    assert ensemble_call_id == ensemble_record.call_id

    # ============================================================
    # PIECE 7 — Checkpoint write × 3 + crash + resume
    # ============================================================
    checkpoint_writer = make_checkpoint_writer()
    for step in (1, 2, 3):
        cp = TaskCheckpoint(
            checkpoint_id=f"tcp-v11-{test_uid}-{step}",
            task_id=task_id,
            step_idx=step,
            sequence=step,
            conversation_snapshot=[
                {"role": "system", "content": f"v11 step {step} system"},
                {"role": "user", "content": f"v11 step {step} ask"},
            ],
            working_state={"step": step, "progress_pct": step * 33},
            artifact_refs=[f"artifact:v11-step-{step}"],
            goal_anchor_id=None,
            last_self_report=None,
            cost_usd_so_far=0.01 * step + ensemble_record.total_cost_usd,
            tokens_used_so_far=120 * step,
            status="active",  # type: ignore[arg-type]
            rationale=f"dogfood v11 step {step} sealed",
        )
        await checkpoint_writer(cp.to_row_payload(tenant_id))

    # SIMULATE CRASH — discard everything in-memory, fresh reader
    del lifecycle_service, queue, trifecta, trifecta_report
    reader = make_checkpoint_reader()
    latest = await reader(tenant_id, task_id)

    assert latest is not None, "crash-resume broken: checkpoint reader returned None"
    assert latest.step_idx == 3
    assert latest.sequence == 3
    assert latest.working_state["step"] == 3
    assert latest.cost_usd_so_far == pytest.approx(
        0.03 + ensemble_record.total_cost_usd, rel=1e-6
    )

    # ============================================================
    # CAPSTONE ASSERTIONS — every X.B/X.C table has the expected rows
    # ============================================================

    # 4 X.B tables + lifecycle_transitions via cockpit readers
    mar_result = await list_recent_mission_reviews(
        tenant_id=tenant_id, task_id=task_id, limit=10
    )
    assert mar_result.error_kind is None
    assert len(mar_result.rows) == 1
    assert mar_result.rows[0]["verdict"] == "ok"
    assert mar_result.rows[0]["alignment_score"] == pytest.approx(0.92)

    lct_result = await list_recent_lifecycle_transitions(
        tenant_id=tenant_id, capability_id=capability_id, limit=20
    )
    assert lct_result.error_kind is None
    # 7 transitions total: 5 (obs→canary) + 1 (canary→prod) + 1 (prod→monitor)
    assert len(lct_result.rows) == 7
    chain = [(r["from_stage"], r["to_stage"]) for r in reversed(lct_result.rows)]
    assert chain == [
        ("observation", "candidate"),
        ("candidate", "replay"),
        ("replay", "holdout"),
        ("holdout", "shadow"),
        ("shadow", "canary"),
        ("canary", "production"),
        ("production", "monitor"),
    ]
    # Production row must carry the approval ticket id
    prod_row = next(r for r in lct_result.rows if r["to_stage"] == "production")
    assert prod_row["user_approval_ticket_id"] == ticket_id

    ar_result = await list_recent_auditor_reports(
        tenant_id=tenant_id,
        audited_capability=capability_id,
        limit=10,
    )
    assert ar_result.error_kind is None
    assert len(ar_result.rows) == 1
    assert ar_result.rows[0]["risk_level"] == "P2"
    assert ar_result.rows[0]["allow_release"] is True


# ============================================================
# Independent assertion — ensemble + checkpoint rows reachable
# ============================================================


async def test_dogfood_v11_ensemble_and_checkpoint_rows_are_persistent() -> None:
    """Standalone read-back: write 1 ensemble call + 2 checkpoints, confirm
    they survive a fresh session — proves the v11 capstone's writes are
    genuinely durable (not session-scoped artifacts)."""
    await _skip_if_no_pg()

    tenant_id = "t-v11-durable"
    test_uid = id(test_dogfood_v11_ensemble_and_checkpoint_rows_are_persistent)
    task_id = f"tk-v11-d-{test_uid}"

    # Write 1 ensemble call + 2 checkpoints (in same session group)
    await write_ensemble_call(
        tenant_id=tenant_id,
        record=EnsembleCallRecord(
            call_id=f"ec-v11-d-{test_uid}",
            invoked_at=datetime.now(UTC),
            purpose="durability-probe",
            providers=[
                {"name": "openai", "model_id": "gpt-5.5", "family": "openai"},
                {
                    "name": "ollama",
                    "model_id": "qwen2.5:14b",
                    "family": "qwen",
                },
            ],
            consensus_strategy="majority_vote",
            divergence_score=0.1,
            divergence_signals=[],
            consensus_provider="openai/gpt-5.5",
            total_cost_usd=0.005,
            failure_count=0,
            n_providers_total=2,
            request_hash="v11-d-hash",
        ),
    )

    writer = make_checkpoint_writer()
    for step in (1, 2):
        cp = TaskCheckpoint(
            checkpoint_id=f"tcp-v11-d-{test_uid}-{step}",
            task_id=task_id,
            step_idx=step,
            sequence=step,
            conversation_snapshot=[
                {"role": "user", "content": f"durability check {step}"}
            ],
            working_state={"step": step},
            artifact_refs=[],
            goal_anchor_id=None,
            last_self_report=None,
            cost_usd_so_far=0.001 * step,
            tokens_used_so_far=10 * step,
            status="active",  # type: ignore[arg-type]
            rationale=f"durability probe step {step}",
        )
        await writer(cp.to_row_payload(tenant_id))

    # Reset all in-process DB state (simulate process restart)
    import kun.core.db as db_mod

    db_mod._sessionmaker = None  # type: ignore[attr-defined]
    db_mod._admin_sessionmaker = None  # type: ignore[attr-defined]
    db_mod._engine = None  # type: ignore[attr-defined]
    db_mod._admin_engine = None  # type: ignore[attr-defined]

    # Fresh reader for checkpoints
    fresh_reader = make_checkpoint_reader()
    latest = await fresh_reader(tenant_id, task_id)
    assert latest is not None
    assert latest.sequence == 2

    # Direct ensemble row probe (no public list-reader yet; admin query)
    from kun.core.db import get_admin_sessionmaker
    from kun.core.orm import EnsembleCallRow
    from sqlalchemy import select

    sm = get_admin_sessionmaker()
    async with sm() as s:
        result = await s.execute(
            select(EnsembleCallRow).where(
                EnsembleCallRow.tenant_id == tenant_id,
                EnsembleCallRow.call_id == f"ec-v11-d-{test_uid}",
            )
        )
        row = result.scalar_one_or_none()
    assert row is not None
    # NUMERIC(10,6) comes back as Decimal — cast for approx compare
    assert float(row.total_cost_usd) == pytest.approx(0.005)
    assert row.n_providers_total == 2


# ============================================================
# Auditor report invariant — P0 with allow_release=True must raise
# ============================================================


async def test_dogfood_v11_p0_auditor_report_blocks_release_at_construction() -> None:
    """V7 §16.6 invariant double-check inside the v11 capstone context:
    even at the dataclass __post_init__ layer, P0 + allow_release=True
    raises ValueError. This protects the v11 chain from a bypass at the
    auditor-emit step."""
    with pytest.raises(ValueError, match="P0"):
        AuditorReport(
            report_id="ar-v11-p0-bad",
            audited_capability="cap-v11-p0-bad",
            audited_at=datetime.now(UTC),
            auditor_provider="test",
            design_promise="X",
            real_code_path="Y",
            risk_level="P0",
            allow_release=True,  # invalid
        )
