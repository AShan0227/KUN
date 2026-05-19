from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

import pytest
from kun.control_plane import (
    ExecutionContract,
    FileControlPlaneStore,
    InMemoryControlPlane,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
    WorkItemResult,
    apply_frontier50_round_summary,
    build_frontier50_campaign_plan,
    build_user_progress_summary,
    initial_frontier50_work_item,
)
from kun.control_plane.qi_ab import QiABRoundSummary


class StaticRunner:
    runner_type: Literal["agent"] = "agent"
    runner_identity = "qi-frontier50-contract-runner"

    def __init__(self, handler: Callable[[WorkItem], WorkItemResult]) -> None:
        self._handler = handler

    def run(self, work_item: WorkItem) -> WorkItemResult:
        return self._handler(work_item)


def _task_ids(count: int = 50) -> list[str]:
    return [f"task-{index:02d}" for index in range(1, count + 1)]


def _mission() -> Mission:
    return Mission(
        mission_id="msn-e2e",
        owner="customer",
        objective="Run Frontier50 under KUN V6 Control Plane",
        task_type="self_improvement",
        status="contracted",
    )


def _plan() -> TaskPlan:
    return TaskPlan(
        plan_id="plan-e2e",
        mission_id="msn-e2e",
        version="v1",
        objective="Run Frontier50 and only progress after KUN passes same-task replay.",
        acceptance_criteria=["Comparator must be healthy.", "KUN must pass same-task replay."],
        constraints=["Do not modify OpenClaw, Hermes, or GPT-5.5 direct."],
        evidence_plan=["Persist answers, reviews, report, health, and repair tickets."],
        decomposition=["round-01", "repair-or-next-round"],
        worker_plan=["Qi runs AB; Nuo diagnoses pollution; KUN receives repair only after valid gap."],
        merge_plan=["Merge round artifacts into one manifest."],
        test_plan=["Gate invalid rounds before ranking.", "Same-task retest before next round."],
        rollback_plan=["Invalidate polluted round and rerun after system repair."],
        approval_status="approved",
    )


def _contract() -> ExecutionContract:
    return ExecutionContract(
        contract_id="contract-e2e",
        mission_id="msn-e2e",
        task_plan_version="v1",
        allowed_actions=["run_ab_round", "diagnose_pollution", "repair", "retest"],
        forbidden_actions=["optimize_control_agents"],
    )


def _context() -> WorkingContext:
    return WorkingContext(
        working_context_id="ctx-e2e",
        mission_id="msn-e2e",
        task_plan_version="v1",
        audience="operator",
        scope="frontier50",
        summary="Frontier50 must be gated by comparator health and same-task retest.",
        acceptance_criteria=["Comparator must be healthy.", "KUN must pass same-task replay."],
        constraints=["Only KUN can be repaired during the current AB stage."],
    )


@pytest.mark.unit
def test_frontier50_polluted_round_blocks_next_round_and_survives_restart(tmp_path: Path) -> None:
    store_path = tmp_path / "control-plane.json"
    runtime = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    campaign = build_frontier50_campaign_plan(
        mission_id="msn-e2e",
        task_plan_version="v1",
        task_ids=_task_ids(),
    )
    runtime.submit_mission(
        mission=_mission(),
        task_plan=_plan(),
        execution_contract=_contract(),
        working_context=_context(),
        work_items=[initial_frontier50_work_item(campaign)],
    )

    def polluted_round(work_item: WorkItem) -> WorkItemResult:
        summary = QiABRoundSummary(
            mission_id="msn-e2e",
            task_plan_version="v1",
            round_id="round-01",
            work_item_id=work_item.work_item_id,
            task_ids=campaign.rounds[0].task_ids,
            answer_refs=[f"answer-{index}" for index in range(20)],
            review_refs=[f"review-{index}" for index in range(45)],
            report_ref="report-round-01",
            health_ref="health-round-01",
            comparator_healthy=False,
            kun_gate_passed=True,
            kun_result_quality=0.9,
        )
        decision = apply_frontier50_round_summary(plan=campaign, summary=summary)
        return decision.round_contract.work_item_result

    runtime.run_next_ready(mission_id="msn-e2e", runner=StaticRunner(polluted_round))

    progress = runtime.progress_report("msn-e2e")
    user_summary = build_user_progress_summary(progress)

    assert progress.status == "repairing"
    assert progress.latest_failure_category == "environment_failure"
    assert user_summary.quality_gate_status == "invalid"
    assert "不能算作能力失败" in user_summary.blocking_reason
    assert "work-qi-ab-round-02" not in runtime.work_items
    assert runtime.work_items["work-qi-ab-repair-round-01"].owner == "nuo"

    restored = InMemoryControlPlane(store=FileControlPlaneStore(store_path))
    restored_progress = restored.progress_report("msn-e2e")

    assert restored_progress.status == "repairing"
    assert restored.work_items["work-qi-ab-repair-round-01"].owner == "nuo"
    assert restored.working_contexts["ctx-e2e"] == _context()
