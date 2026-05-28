"""Dogfood v10 — trigger ALL 4 X.B tables through real production code paths.

Dogfood v9 only触发了 ensemble_calls (via LongTaskOrchestrator). The other 3
tables (mission_alignment_reviews / lifecycle_transitions / auditor_reports)
stayed flat because the task type didn't go through their wiring paths.

This dogfood drives each table's PRODUCTION code path directly (no LLM cost):
  - GateService.admit(approve)
      → V7 lifecycle bridge → lifecycle_transitions +1
      → chained heuristic auditor bridge → auditor_reports +1
  - V6 control_plane.MissionDirectorRunner.run(work_item type=review)
      → V7 mission director bridge → mission_alignment_reviews +1
  - LocalLLMProvider + ensemble_invoke (1 short call)
      → ensemble_calls +1 (also proves real LLM path still works)

Expected end state: all 4 X.B tables get ≥+1 row from real production path.

Cost: ~$0.05 (single short LLM call). Mostly free — just direct Python calls
to production code, no long-task orchestration.

Run: .venv/bin/python scripts/dogfood_v10_trigger_xb_tables.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Load .env
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

os.environ.setdefault("KUN_V7_ENSEMBLE_ENABLED", "true")
os.environ.setdefault("KUN_V7_ENSEMBLE_TIERS", "top")
os.environ.setdefault(
    "KUN_V7_ENSEMBLE_LOCAL_MODEL_ID", "qwen2.5:14b-instruct-q4_K_M"
)
os.environ.setdefault(
    "KUN_V7_ENSEMBLE_LOCAL_BASE_URL", "http://localhost:11434/v1"
)


def _hdr(t: str) -> None:
    print(f"\n{'=' * 70}\n  {t}\n{'=' * 70}")


async def _count(table: str) -> int:
    from kun.core.db import get_admin_sessionmaker
    from kun.core.orm import (
        AuditorReportRow,
        EnsembleCallRow,
        LifecycleTransitionRow,
        MissionAlignmentReviewRow,
    )
    from sqlalchemy import func, select

    table_to_orm = {
        "mission_alignment_reviews": MissionAlignmentReviewRow,
        "lifecycle_transitions": LifecycleTransitionRow,
        "auditor_reports": AuditorReportRow,
        "ensemble_calls": EnsembleCallRow,
    }
    sm = get_admin_sessionmaker()
    async with sm() as s:
        result = await s.execute(
            select(func.count()).select_from(table_to_orm[table])
        )
        return int(result.scalar() or 0)


async def _snapshot() -> dict[str, int]:
    return {
        t: await _count(t)
        for t in (
            "mission_alignment_reviews",
            "lifecycle_transitions",
            "auditor_reports",
            "ensemble_calls",
        )
    }


# ============================================================
# Step 1 — GateService.admit triggers lifecycle + auditor
# ============================================================


async def step1_gate_admit() -> None:
    """Real GateService.admit with passing inputs → fires V7 bridges chain."""
    from kun.agents.gate.service import GateService

    print("  → Constructing real GateService...")
    service = GateService()

    print("  → admit() with passing rule_results (R1+R2+R3+R4 all pass)...")
    decision = await service.admit(
        experiment={
            "experiment_id": f"ex-dogfood-v10-{int(time.time())}",
            "target_module": "kun.dogfood.v10.synthetic",
            "rationale": "Dogfood v10 — trigger lifecycle + auditor bridges",
            "rollback_on": ["pass_rate < 0.9"],
        },
        test_report={
            "pass_rate": 0.97,
            "covered_rules": ["R1", "R2", "R3"],
        },
        diagnostic_record={
            "diagnostic_id": "dx-v10",
            "root_cause": "test-driven",
            "verdict": "ok",
        },
        debrief={
            "debrief_id": "db-v10",
            "evidence_quality": 0.92,
            "summary": "ok",
        },
        tenant_id=os.environ.get("KUN_DEFAULT_TENANT_ID", "u-sylvan"),
    )
    print(f"  ← GateDecision: verdict={decision.verdict} "
          f"capability_id={decision.capability_id}")
    if decision.verdict != "approve":
        print(f"  ⚠️  verdict not 'approve', reasons: {decision.reasons}")
    return decision


# ============================================================
# Step 2 — V6 MissionDirectorRunner.run triggers V7 bridge
# ============================================================


async def step2_mission_director_run() -> None:
    """Real V6 control_plane.MissionDirectorRunner.run() with synthetic mission
    + review work_item → fires X.B.MF-1 V7 bridge → mission_alignment_reviews +1.
    """
    from kun.control_plane import (
        MISSION_DIRECTOR_OWNER,
        ArtifactManifest,
        ArtifactRecord,
        ExecutionContract,
        GateEvaluation,
        InMemoryControlPlane,
        Mission,
        MissionDirectorModelConfig,
        MissionDirectorRunner,
        TaskPlan,
        WorkingContext,
        WorkItem,
    )

    cp = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-dogfood-v10",
        owner="user",
        objective="Dogfood v10 — drive V6 MissionDirectorRunner",
        task_type="product_development",
        status="contracted",
        current_plan_version="v10",
    )
    plan = TaskPlan(
        plan_id="plan-v10",
        mission_id=mission.mission_id,
        version="v10",
        objective=mission.objective,
        info_gaps=[],
        acceptance_criteria=["v10 fires V7 bridge"],
        decomposition=["build", "verify"],
        worker_plan=["mission-director"],
        test_plan=["this"],
        rollback_plan=["disable bridge env"],
        evidence_plan=["mission_alignment_reviews row"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-v10",
        mission_id=mission.mission_id,
        task_plan_version="v10",
        allowed_actions=["write_project"],
        delivery_contract={},
    )
    context = WorkingContext(
        working_context_id="ctx-v10",
        mission_id=mission.mission_id,
        task_plan_version="v10",
        audience="mission-director",
        scope="dogfood v10",
        summary="V10 — trigger MD bridge.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=["bridge must fire"],
    )
    cp.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[],
    )

    delivery = ArtifactRecord(
        artifact_id="art-v10-delivery",
        kind="answer",
        path_or_uri="mem://v10",
        content_hash="hash-v10",
        created_by="dogfood-v10",
        mission_id=mission.mission_id,
        supports=["v10_artifact"],
    )
    evidence = ArtifactRecord(
        artifact_id="art-v10-evidence",
        kind="test_result",
        path_or_uri="mem://v10-test",
        content_hash="hash-v10-test",
        created_by="dogfood-v10",
        mission_id=mission.mission_id,
        supports=["internal_test_passed"],
    )
    cp.artifacts[delivery.artifact_id] = delivery
    cp.artifacts[evidence.artifact_id] = evidence
    manifest = ArtifactManifest(
        manifest_id="manifest-v10",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=[delivery.artifact_id, evidence.artifact_id],
        primary_artifact_ref=delivery.artifact_id,
        evidence_refs=[evidence.artifact_id],
        created_by="dogfood-v10",
        content_hash="hash-mani",
        supports_delivery=True,
    )
    cp.artifact_manifests[manifest.manifest_id] = manifest
    cp.gate_evaluations["gate-v10"] = GateEvaluation(
        gate_evaluation_id="gate-v10",
        mission_id=mission.mission_id,
        task_plan_version="v10",
        subject_ref="work-v10",
        stage="acceptance",
        task_type="product_development",
        rubric_version="v10",
        metric_pack_version="v10",
        north_star_verdict="pass",
        result_quality=0.9,
        speed=0.8,
        cost=0.8,
        risk=0.2,
        evidence_quality=0.8,
        collaboration_quality=0.8,
        evidence_refs=[evidence.artifact_id],
        artifact_refs=[delivery.artifact_id],
        confidence=0.85,
        next_action="ready_to_deliver",
        next_state="delivering",
        created_by="dogfood-v10",
    )
    cp.missions[mission.mission_id] = cp.missions[mission.mission_id].model_copy(
        update={"status": "delivering", "artifact_manifest_refs": [manifest.manifest_id]}
    )

    work_item = WorkItem(
        work_item_id="work-v10-review",
        mission_id=mission.mission_id,
        task_plan_version="v10",
        type="review",
        owner=MISSION_DIRECTOR_OWNER,
        expected_output="Review for dogfood v10",
    )

    runner = MissionDirectorRunner(
        control_plane=cp,
        model_config=MissionDirectorModelConfig(
            model_id="dogfood-v10-md",
            model_tier="top",
            provider="test-provider",
        ),
    )
    print("  → MissionDirectorRunner.run(work_item type=review)...")
    result = runner.run(work_item)
    print(f"  ← WorkItemResult: status={result.status} "
          f"gate.next_action={(result.gate_evaluation or {}).next_action if result.gate_evaluation else 'none'}")

    # Bridge fires fire-and-forget in a thread. Give it ~3s to land.
    print("  → Waiting 3s for fire-and-forget bridge thread to flush to PG...")
    await asyncio.sleep(3.0)


# ============================================================
# Step 3 — Single ensemble call (proves ensemble path still healthy)
# ============================================================


async def step3_ensemble_call() -> None:
    """Single short cross-family ensemble call → +1 ensemble_calls row."""
    from kun.integration.ensemble_invoker_factory import (
        build_ensemble_invoker_from_settings,
    )
    from kun.interface.llm.router import get_router

    router = get_router()
    invoker = build_ensemble_invoker_from_settings(
        router=router,
        purpose="execution",
        tenant_id=os.environ.get("KUN_DEFAULT_TENANT_ID", "u-sylvan"),
    )
    if invoker is None:
        print("  ⚠️  Factory returned None — ensemble invoker not built")
        return

    print("  → Real ensemble invoke (gpt-5.5 + Qwen, 1 short prompt)...")
    t0 = time.perf_counter()
    step = await invoker(
        [
            {"role": "system", "content": "Reply in one Chinese sentence."},
            {"role": "user", "content": "用一句话说: 多 LLM 一起干的好处?"},
        ]
    )
    elapsed = time.perf_counter() - t0
    print(f"  ← Response: {step.content[:120]!r}")
    print(f"  ← Took {elapsed:.1f}s, cost ${step.cost_usd:.4f}")


# ============================================================
# Main
# ============================================================


async def main() -> int:
    _hdr("DOGFOOD V10 — trigger ALL 4 X.B production tables")
    print(f"Started: {datetime.now(UTC).isoformat()}")

    _hdr("Before-snapshot")
    before = await _snapshot()
    for t, n in before.items():
        print(f"  {t:35s} rows={n}")

    _hdr("Step 1/3 — GateService.admit (triggers lifecycle + auditor bridges)")
    try:
        await step1_gate_admit()
    except Exception as e:
        print(f"  ❌ step1 failed: {type(e).__name__}: {e}")

    _hdr("Step 2/3 — V6 MissionDirectorRunner.run (triggers V7 MD bridge)")
    try:
        await step2_mission_director_run()
    except Exception as e:
        print(f"  ❌ step2 failed: {type(e).__name__}: {e}")

    _hdr("Step 3/3 — Single ensemble_invoke (proves ensemble still healthy)")
    try:
        await step3_ensemble_call()
    except Exception as e:
        print(f"  ❌ step3 failed: {type(e).__name__}: {e}")

    _hdr("After-snapshot + delta")
    after = await _snapshot()
    deltas: dict[str, int] = {}
    for t, n_after in after.items():
        n_before = before[t]
        delta = n_after - n_before
        deltas[t] = delta
        marker = f" ← +{delta} NEW" if delta > 0 else ""
        print(f"  {t:35s} {n_before:3d} → {n_after:3d}{marker}")

    _hdr("VERDICT")
    grew = [t for t, d in deltas.items() if d > 0]
    if len(grew) == 4:
        print("  ✅ PASS — ALL 4 X.B tables真 wrote new rows from production paths")
    elif len(grew) >= 1:
        print(f"  ⚠️  PARTIAL — {len(grew)}/4 tables grew: {grew}")
        print(f"  📋 No-grow tables: {[t for t in deltas if deltas[t] == 0]}")
    else:
        print("  ❌ FAIL — no tables grew")
    return 0 if len(grew) >= 3 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
