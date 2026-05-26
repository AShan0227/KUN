"""Feature activation audit missions for KUN V6 Control Plane.

This suite turns product capabilities into concrete trigger tasks.  It is not a
static code inventory: every case creates a real Control Plane mission/work item
or daemon condition, runs it, and records whether the expected feature actually
activated with auditable evidence.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field

from kun.control_plane.app_development import (
    KUN_AUTONOMOUS_APP_RUNNER_OWNER,
    AppCommandResult,
    AutonomousAppDevelopmentRunner,
)
from kun.control_plane.capability_evolution import (
    CapabilityCandidate,
    CapabilityEvaluation,
    build_capability_promotion,
)
from kun.control_plane.capability_execution import CapabilityExecutionPolicy
from kun.control_plane.concurrency import (
    FileResourceLockStore,
    RedisResourceLockStore,
    SQLiteResourceLockStore,
    WorkerPoolConfig,
)
from kun.control_plane.daemon import ControlPlaneDaemon
from kun.control_plane.daemon_service import build_daemon_worker_pool_service_install_plans
from kun.control_plane.external_sample_comparison import (
    KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER,
    ExternalSampleComparisonRunner,
)
from kun.control_plane.file_store import FileControlPlaneStore
from kun.control_plane.frontier50_external import (
    ExternalCommandResult,
    Frontier50ExternalRoundConfig,
    Frontier50ExternalRoundRunner,
)
from kun.control_plane.game_design_research import (
    KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER,
    GameDesignResearchRunner,
    ResearchSource,
)
from kun.control_plane.game_production import (
    KUN_GAME_PRODUCTION_RUNNER_OWNER,
    GameProductionCommandResult,
    GameProductionRunner,
)
from kun.control_plane.kun_runtime_runner import KunRuntimeTaskRunner, KunTaskExecutionOutput
from kun.control_plane.mission_director import MISSION_DIRECTOR_OWNER, MissionDirectorRunner
from kun.control_plane.productization import (
    ProductizationDogfoodRunner,
    build_productization_dogfood_mission,
    submit_productization_dogfood_mission,
)
from kun.control_plane.qi_ab import build_qi_ab_round_work_item
from kun.control_plane.runtime import InMemoryControlPlane, WorkItemResult
from kun.control_plane.runtime_followups import (
    ChainedControlPlaneRunner,
    NuoRuntimeRepairRunner,
    QiRuntimeGovernanceRunner,
)
from kun.control_plane.runtime_observation import build_runtime_observation_report
from kun.control_plane.self_improvement import (
    SELF_IMPROVEMENT_AUDIT_SUPPORT,
    SELF_IMPROVEMENT_STRATEGY_SUPPORT,
    NuoSelfImprovementAuditRunner,
    QiSelfImprovementStrategyRunner,
)
from kun.control_plane.v6 import (
    ArtifactManifest,
    ArtifactRecord,
    CapabilityProfile,
    CollaborationTicket,
    ExecutionContract,
    GateEvaluation,
    Mission,
    TaskPlan,
    WorkingContext,
    WorkItem,
)
from kun.watchtower.engine import RuleEngine
from kun.watchtower.rules import GuardRule, RuleTrigger

NOW = datetime(2026, 5, 21, 10, 0, tzinfo=UTC)


class FeatureActivationCase(BaseModel):
    """One audited capability activation case."""

    model_config = ConfigDict(extra="forbid")

    feature_id: str
    subsystem: str
    trigger_condition: str
    dependencies: list[str] = Field(default_factory=list)
    activated: bool
    evidence_refs: list[str] = Field(default_factory=list)
    generated_work_item_ids: list[str] = Field(default_factory=list)
    trigger_status: str = "unknown"
    trigger_available: bool = False
    runner_available: bool = False
    real_mission_e2e_passed: bool = False
    notes: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    evidence_scope: Literal["fixture", "real_mission", "synthetic_round", "static_probe"] = (
        "fixture"
    )


class FeatureActivationAuditReport(BaseModel):
    """Machine-readable result of the activation audit."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "kun-v6-feature-activation-audit-v1"
    suite_id: str
    generated_at: datetime
    output_dir: str
    cases: list[FeatureActivationCase]
    report_json_path: str
    report_markdown_path: str

    @computed_field
    @property
    def activated_count(self) -> int:
        return sum(1 for case in self.cases if case.activated)

    @computed_field
    @property
    def gap_count(self) -> int:
        return len(self.cases) - self.activated_count

    @computed_field
    @property
    def real_mission_count(self) -> int:
        return sum(1 for case in self.cases if case.evidence_scope == "real_mission")

    @computed_field
    @property
    def fixture_or_synthetic_count(self) -> int:
        return sum(1 for case in self.cases if case.evidence_scope != "real_mission")


class StaticRunner:
    runner_type: Literal["agent"] = "agent"
    runner_identity = "feature-activation-static-runner"

    def run(self, _work_item: WorkItem) -> WorkItemResult:
        artifact = ArtifactRecord(
            artifact_id=f"artifact-static-{_work_item.work_item_id}",
            kind="answer",
            path_or_uri=f"mem://feature-activation/{_work_item.work_item_id}",
            content_hash=f"hash-{_work_item.work_item_id}",
            created_by=self.runner_identity,
            mission_id=_work_item.mission_id,
            work_item_id=_work_item.work_item_id,
            supports=["feature_activation_static_runner"],
        )
        return WorkItemResult(
            status="done",
            summary="Feature activation static runner completed.",
            artifacts=[artifact],
            gate_evaluation=_pass_gate(_work_item, artifact.artifact_id),
        )


class PolicyAwareRunner(StaticRunner):
    runner_identity = "feature-activation-policy-aware-runner"

    def __init__(self) -> None:
        self.bound_policy: CapabilityExecutionPolicy | None = None

    def bind_capability_execution_policy(self, policy: CapabilityExecutionPolicy) -> None:
        self.bound_policy = policy


class ConcurrentProbeRunner(StaticRunner):
    runner_identity = "feature-activation-concurrent-probe-runner"

    def __init__(self, *, sleep_sec: float = 0.15) -> None:
        self.sleep_sec = sleep_sec
        self._lock = threading.Lock()
        self.current_running = 0
        self.max_running = 0

    def run(self, work_item: WorkItem) -> WorkItemResult:
        with self._lock:
            self.current_running += 1
            self.max_running = max(self.max_running, self.current_running)
        try:
            time.sleep(self.sleep_sec)
            return super().run(work_item)
        finally:
            with self._lock:
                self.current_running -= 1


def run_feature_activation_audit(
    *,
    output_dir: str | Path,
    suite_id: str = "kun-v6-feature-activation",
    now: datetime = NOW,
) -> FeatureActivationAuditReport:
    """Run all feature activation cases and persist JSON/Markdown evidence."""

    root = Path(output_dir).expanduser().resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    cases: list[FeatureActivationCase] = []
    for case_fn in _CASE_FUNCTIONS:
        try:
            cases.append(case_fn(root, now))
        except Exception as exc:  # pragma: no cover - report-path safety
            cases.append(
                FeatureActivationCase(
                    feature_id=case_fn.__name__.removeprefix("_case_"),
                    subsystem="unknown",
                    trigger_condition="case raised before it could declare a trigger",
                    dependencies=[],
                    activated=False,
                    trigger_status="error",
                    gaps=[f"{type(exc).__name__}: {exc}"],
                )
            )
    cases = [_with_activation_axes(case) for case in cases]
    json_path = root / "feature-activation-audit.json"
    md_path = root / "feature-activation-audit.md"
    report = FeatureActivationAuditReport(
        suite_id=suite_id,
        generated_at=now,
        output_dir=str(root),
        cases=cases,
        report_json_path=str(json_path),
        report_markdown_path=str(md_path),
    )
    json_path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    md_path.write_text(_markdown_report(report), encoding="utf-8")
    return report


def _with_activation_axes(case: FeatureActivationCase) -> FeatureActivationCase:
    trigger_available = case.activated and case.trigger_status not in {"error", "missing"}
    runner_available = case.activated and bool(case.evidence_refs or case.generated_work_item_ids)
    real_mission_e2e_passed = (
        case.evidence_scope == "real_mission" and case.activated and not case.gaps
    )
    return case.model_copy(
        update={
            "trigger_available": trigger_available,
            "runner_available": runner_available,
            "real_mission_e2e_passed": real_mission_e2e_passed,
        }
    )


def _case_info_gap_collaboration(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-activation-info-gap",
        owner="customer",
        objective="Build a complex task with missing platform and acceptance information.",
        task_type="product_development",
        status="planning",
    )
    plan = TaskPlan(
        plan_id="plan-activation-info-gap",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        info_gaps=["Need target platform.", "Need acceptance criteria."],
        acceptance_criteria=["do not execute until gaps are answered"],
        approval_status="draft",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.task_plans[plan.plan_id] = plan
    report = ControlPlaneDaemon(
        control_plane=control_plane, daemon_id="activation-info-gap"
    ).tick_once(
        mission_ids=[mission.mission_id],
        now=now,
        max_work_items=1,
        write_progress=False,
    )
    activated = (
        report.created_collaboration_ticket_ids
        and control_plane.missions[mission.mission_id].status == "waiting_human"
    )
    return FeatureActivationCase(
        feature_id="info_gap_human_collaboration",
        subsystem="human_collaboration",
        trigger_condition="TaskPlan.info_gaps exists while mission is planning/info_gap.",
        dependencies=["TaskPlan.info_gaps", "ControlPlaneDaemon._ensure_info_gap_collaboration"],
        activated=bool(activated),
        evidence_refs=list(report.created_collaboration_ticket_ids),
        generated_work_item_ids=[],
        trigger_status=control_plane.missions[mission.mission_id].status,
        notes=[
            "KUN must ask before creating a task plan/execution when required information is missing."
        ],
    )


def _case_acceptance_collaboration_cleanup(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-activation-acceptance",
        owner="customer",
        objective="Deliver a user-facing artifact.",
        task_type="product_development",
        status="delivering",
        current_plan_version="v2",
        artifact_manifest_refs=["manifest-activation-delivery"],
    )
    plan = TaskPlan(
        plan_id="plan-activation-acceptance",
        mission_id=mission.mission_id,
        version="v2",
        objective=mission.objective,
        acceptance_criteria=["human acceptance must be requested"],
        approval_status="approved",
    )
    manifest = ArtifactManifest(
        manifest_id="manifest-activation-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=["artifact-delivery"],
        primary_artifact_ref="artifact-delivery",
        evidence_refs=["artifact-evidence"],
        rollback_refs=["artifact-rollback"],
        created_by="kun",
        content_hash="hash-manifest",
        supports_delivery=True,
    )
    old_item = WorkItem(
        work_item_id="work-activation-old-plan",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="blocked",
        expected_output="obsolete work",
    )
    old_ticket = CollaborationTicket(
        ticket_id="collab-activation-old",
        mission_id=mission.mission_id,
        type="expert_input",
        role_needed="customer",
        why_needed="obsolete question",
        context_ref="ctx-v1",
        risk_if_skipped="old state pollutes cockpit",
        deadline=now,
        output_contract="obsolete answer",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.task_plans[plan.plan_id] = plan
    control_plane.artifact_manifests[manifest.manifest_id] = manifest
    control_plane.work_items[old_item.work_item_id] = old_item
    control_plane.collaboration_tickets[old_ticket.ticket_id] = old_ticket
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="activation-acceptance",
    ).tick_once(mission_ids=[mission.mission_id], now=now, write_progress=False)
    activated = (
        bool(report.created_collaboration_ticket_ids)
        and control_plane.work_items[old_item.work_item_id].status == "cancelled"
        and control_plane.collaboration_tickets[old_ticket.ticket_id].status == "cancelled"
    )
    return FeatureActivationCase(
        feature_id="delivery_acceptance_and_state_cleanup",
        subsystem="human_collaboration",
        trigger_condition="Mission is delivering with delivery manifest and stale work/tickets.",
        dependencies=["delivery manifest", "current_plan_version", "collaboration ticket store"],
        activated=activated,
        evidence_refs=[
            *report.created_collaboration_ticket_ids,
            *report.retired_work_item_ids,
            *report.retired_collaboration_ticket_ids,
        ],
        generated_work_item_ids=[],
        trigger_status=control_plane.missions[mission.mission_id].status,
        notes=["Acceptance ticket and stale-state cleanup must happen together."],
    )


def _case_mission_director_delivery_supervision(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane = InMemoryControlPlane()
    mission = Mission(
        mission_id="msn-activation-mission-director",
        owner="customer",
        objective="Ship a final product only after real user experience evidence.",
        task_type="product_development",
        status="delivering",
        current_plan_version="v1",
        artifact_manifest_refs=["manifest-activation-director-delivery"],
    )
    plan = TaskPlan(
        plan_id="plan-activation-mission-director",
        mission_id=mission.mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["human or target user accepts the final product"],
        decomposition=["build", "test", "playtest", "accept"],
        worker_plan=["kun builds", "mission-director supervises"],
        test_plan=["internal", "player perception"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id="contract-activation-mission-director",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        delivery_contract={
            "final_player_experience_required": True,
            "human_acceptance_required": True,
        },
    )
    delivery_artifact = ArtifactRecord(
        artifact_id="artifact-activation-director-delivery",
        kind="answer",
        path_or_uri="mem://activation/mission-director/delivery",
        content_hash="hash-activation-director-delivery",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["directly_playable_product"],
    )
    test_artifact = ArtifactRecord(
        artifact_id="artifact-activation-director-internal-test",
        kind="test_result",
        path_or_uri="mem://activation/mission-director/internal-test",
        content_hash="hash-activation-director-test",
        created_by="kun",
        mission_id=mission.mission_id,
        supports=["internal_test_passed"],
    )
    manifest = ArtifactManifest(
        manifest_id="manifest-activation-director-delivery",
        mission_id=mission.mission_id,
        kind="delivery",
        artifact_refs=[delivery_artifact.artifact_id, test_artifact.artifact_id],
        primary_artifact_ref=delivery_artifact.artifact_id,
        evidence_refs=[test_artifact.artifact_id],
        created_by="kun",
        content_hash="hash-activation-director-manifest",
        supports_delivery=True,
    )
    gate = GateEvaluation(
        gate_evaluation_id="gate-activation-director-runner-pass",
        mission_id=mission.mission_id,
        task_plan_version=plan.version,
        subject_ref="work-final-delivery",
        stage="acceptance",
        task_type="product_development",
        rubric_version="activation-fixture",
        metric_pack_version="activation-fixture",
        north_star_verdict="pass",
        result_quality=0.9,
        speed=0.8,
        cost=0.8,
        risk=0.2,
        evidence_quality=0.8,
        collaboration_quality=0.8,
        evidence_refs=[test_artifact.artifact_id],
        artifact_refs=[delivery_artifact.artifact_id],
        confidence=0.85,
        next_action="ready_to_deliver",
        next_state="delivering",
        created_by="kun",
    )
    control_plane.missions[mission.mission_id] = mission
    control_plane.task_plans[plan.plan_id] = plan
    control_plane.contracts[contract.contract_id] = contract
    control_plane.artifacts[delivery_artifact.artifact_id] = delivery_artifact
    control_plane.artifacts[test_artifact.artifact_id] = test_artifact
    control_plane.artifact_manifests[manifest.manifest_id] = manifest
    control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            MISSION_DIRECTOR_OWNER: MissionDirectorRunner(control_plane=control_plane)
        },
        daemon_id="activation-mission-director",
    )
    report = daemon.tick_once(
        mission_ids=[mission.mission_id],
        now=now,
        max_work_items=1,
        write_progress=False,
    )
    director_artifacts = [
        artifact.artifact_id
        for artifact in control_plane.artifacts.values()
        if "mission_director_review" in artifact.supports
    ]
    director_gates = [
        gate.gate_evaluation_id
        for gate in control_plane.gate_evaluations.values()
        if gate.created_by == MISSION_DIRECTOR_OWNER
    ]
    ran_director = any(
        work_item_id.startswith("work-mission-director-")
        for work_item_id in report.ran_work_item_ids
    )
    activated = (
        ran_director
        and bool(director_artifacts)
        and control_plane.missions[mission.mission_id].status in {"changing_plan", "waiting_human"}
    )
    return FeatureActivationCase(
        feature_id="mission_director_delivery_supervision",
        subsystem="mission_director",
        trigger_condition=(
            "A product mission is in delivery state with a passing runner gate but missing "
            "real player/human acceptance evidence."
        ),
        dependencies=[
            "MissionDirectorRunner",
            "delivery manifest",
            "acceptance gate",
            "human/player perception evidence contract",
        ],
        activated=bool(activated),
        evidence_refs=[*director_artifacts, *director_gates],
        generated_work_item_ids=[*report.created_work_item_ids, *report.ran_work_item_ids],
        trigger_status=control_plane.missions[mission.mission_id].status,
        notes=[
            "This proves Mission Director is activated by daemon scheduling and can outrank "
            "ordinary delivery work in the fixture."
        ],
    )


def _case_runtime_activation_preflight_snapshot(root: Path, now: datetime) -> FeatureActivationCase:
    workspace = root / "runtime-activation-workspace"
    workspace.mkdir(parents=True)
    (workspace / "app.py").write_text("print('hello')\n", encoding="utf-8")
    control_plane, _store, mission = _runtime(
        root / "runtime-activation.json",
        mission_id="msn-activation-runtime",
        workspace=workspace,
    )
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="activation-runtime",
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=1)
    item = control_plane.work_items["work-msn-activation-runtime"]
    supports = [
        support for artifact in control_plane.artifacts.values() for support in artifact.supports
    ]
    activated = (
        "runtime_feature_activation" in supports
        and bool(report.preflight_artifact_refs)
        and "workspace_snapshot" in supports
        and bool(item.rollback_refs)
        and any(lock.startswith("workspace:") for lock in item.resource_locks)
    )
    return FeatureActivationCase(
        feature_id="runtime_activation_preflight_checkpoint",
        subsystem="runtime_activation",
        trigger_condition="KUN execution/test work item has a workspace path and code/test wording.",
        dependencies=[
            "ExecutionContract.delivery_contract.workspace_path",
            "skill registry",
            "workspace snapshot",
        ],
        activated=activated,
        evidence_refs=[
            *report.activation_artifact_refs,
            *report.preflight_artifact_refs,
            *item.rollback_refs,
        ],
        generated_work_item_ids=list(report.ran_work_item_ids),
        trigger_status=item.status,
        notes=[
            "This checks capability binding, skill trigger scan, preflight, resource lock, checkpoint, rollback refs."
        ],
    )


def _case_worker_pool_resource_lock(root: Path, now: datetime) -> FeatureActivationCase:
    lock_store = FileResourceLockStore(root / "worker-locks.json")
    control_plane, store, mission = _runtime(
        root / "worker-lock-runtime.json",
        mission_id="msn-activation-worker-lock",
        workspace=root / "worker-lock-workspace",
    )
    item = control_plane.work_items["work-msn-activation-worker-lock"].model_copy(
        update={"resource_locks": ["workspace:activation-shared"]}
    )
    control_plane.work_items[item.work_item_id] = item
    store.put_work_item(item)
    holder_item = WorkItem(
        work_item_id="work-msn-activation-worker-lock-holder",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="running",
        lease="external-holder",
        heartbeat=now,
        timeout=now + timedelta(minutes=10),
        resource_locks=["workspace:activation-shared"],
        expected_output="Hold the shared activation resource.",
    )
    control_plane.work_items[holder_item.work_item_id] = holder_item
    store.put_work_item(holder_item)
    lock_store.acquire_many(
        resources=["workspace:activation-shared"],
        holder_id="external-holder",
        daemon_id="external-daemon",
        worker_id="external-worker",
        work_item=holder_item,
        now=now,
        ttl=timedelta(minutes=10),
    )
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="activation-worker-lock",
        worker_pool=WorkerPoolConfig(pool_id="activation-pool", machine_id="local", worker_count=2),
        resource_lock_store=lock_store,
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=1)
    activated = bool(report.resource_lock_skipped_work_item_ids and report.resource_lock_conflicts)
    return FeatureActivationCase(
        feature_id="worker_pool_resource_lock_wait",
        subsystem="concurrency",
        trigger_condition="Ready work item requires a resource already held by another daemon/worker.",
        dependencies=["WorkerPoolConfig", "FileResourceLockStore", "resource_locks"],
        activated=activated,
        evidence_refs=[conflict.resource_ref for conflict in report.resource_lock_conflicts],
        generated_work_item_ids=list(report.resource_lock_skipped_work_item_ids),
        trigger_status=report.worker_slots[0].status if report.worker_slots else "unknown",
        notes=["Lock conflict must be waiting/retry, not KUN capability failure."],
    )


def _case_parallel_worker_pool_isolated_execution(
    root: Path, now: datetime
) -> FeatureActivationCase:
    control_plane, _store, mission = _runtime(
        root / "parallel-worker-runtime.json",
        mission_id="msn-activation-parallel-workers",
        workspace=root / "parallel-worker-workspace-a",
    )
    second_mission = Mission(
        mission_id="msn-activation-parallel-workers-b",
        owner="kun",
        objective="Independent parallel worker-pool activation mission.",
        task_type="self_improvement",
        status="contracted",
    )
    second_plan = TaskPlan(
        plan_id="plan-msn-activation-parallel-workers-b",
        mission_id=second_mission.mission_id,
        version="v1",
        objective=second_mission.objective,
        acceptance_criteria=["second mission runs in another worker slot"],
        approval_status="approved",
    )
    second_contract = ExecutionContract(
        contract_id="contract-msn-activation-parallel-workers-b",
        mission_id=second_mission.mission_id,
        task_plan_version="v1",
        allowed_actions=["run parallel worker fixture"],
        forbidden_actions=["share writable workspace with the first fixture"],
        delivery_contract={"workspace_path": str(root / "parallel-worker-workspace-b")},
    )
    second_context = WorkingContext(
        working_context_id="ctx-msn-activation-parallel-workers-b",
        mission_id=second_mission.mission_id,
        task_plan_version="v1",
        audience="kun",
        scope="feature-activation",
        summary="Independent second mission for parallel worker-pool activation.",
        acceptance_criteria=second_plan.acceptance_criteria,
        constraints=["must stay in its own workspace"],
    )
    second = WorkItem(
        work_item_id="work-msn-activation-parallel-workers-b",
        mission_id=second_mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        priority=79,
        expected_output="Independent worker-pool execution fixture.",
    )
    control_plane.submit_mission(
        mission=second_mission,
        task_plan=second_plan,
        execution_contract=second_contract,
        working_context=second_context,
        work_items=[second],
    )
    runner = ConcurrentProbeRunner()
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": runner},
        daemon_id="activation-parallel-workers",
        worker_pool=WorkerPoolConfig(worker_count=2),
    ).tick_once(
        mission_ids=[mission.mission_id, second_mission.mission_id],
        now=now,
        max_work_items=2,
    )
    activated = (
        runner.max_running >= 2
        and set(report.ran_work_item_ids)
        == {"work-msn-activation-parallel-workers", second.work_item_id}
        and len(report.worker_slots) == 2
    )
    return FeatureActivationCase(
        feature_id="parallel_worker_pool_isolated_execution",
        subsystem="concurrency",
        trigger_condition="Two independent ready work items exist and worker_pool.worker_count >= 2.",
        dependencies=[
            "WorkerPoolConfig.worker_count",
            "ControlPlaneDaemon parallel executor",
            "runtime start/finish run boundary",
            "resource lock conflict filter",
        ],
        activated=activated,
        evidence_refs=[
            *report.ran_work_item_ids,
            *[slot.worker_id for slot in report.worker_slots],
        ],
        generated_work_item_ids=list(report.ran_work_item_ids),
        trigger_status="activated" if activated else "gap",
        notes=[
            "This verifies actual simultaneous runner execution, not just max_work_items_per_tick batching.",
            "Shared resources are still filtered by resource locks before worker assignment.",
        ],
        gaps=[] if activated else ["worker_pool_did_not_execute_independent_items_concurrently"],
    )


def _case_sqlite_multi_process_worker_pool_plan(root: Path, now: datetime) -> FeatureActivationCase:
    lock_path = root / "sqlite-worker-locks.sqlite3"
    store_a = SQLiteResourceLockStore(lock_path)
    store_b = SQLiteResourceLockStore(lock_path)
    item_a = WorkItem(
        work_item_id="work-sqlite-lock-a",
        mission_id="msn-activation-sqlite-lock",
        task_plan_version="v1",
        type="execution",
        owner="kun",
    )
    item_b = item_a.model_copy(update={"work_item_id": "work-sqlite-lock-b"})
    first = store_a.acquire_many(
        resources=["workspace:sqlite-shared"],
        holder_id="lease-sqlite-a",
        daemon_id="daemon-sqlite-a",
        worker_id="worker-a",
        work_item=item_a,
        now=now,
        ttl=timedelta(minutes=5),
    )
    second = store_b.acquire_many(
        resources=["workspace:sqlite-shared"],
        holder_id="lease-sqlite-b",
        daemon_id="daemon-sqlite-b",
        worker_id="worker-b",
        work_item=item_b,
        now=now + timedelta(seconds=1),
        ttl=timedelta(minutes=5),
    )
    pool_plan = build_daemon_worker_pool_service_install_plans(
        platform="systemd",
        working_directory=root,
        service_name="kun-v6-worker-pool",
        replica_count=2,
        store_path=root / "control-plane.json",
        resource_lock_path=lock_path,
        resource_lock_backend="sqlite",
    )
    activated = (
        first.acquired
        and not second.acquired
        and second.conflicts[0].holder_daemon_id == "daemon-sqlite-a"
        and pool_plan.replica_count == 2
        and len({plan.state_path for plan in pool_plan.plans}) == 2
        and len({plan.resource_lock_path for plan in pool_plan.plans}) == 1
    )
    return FeatureActivationCase(
        feature_id="sqlite_multi_process_worker_pool",
        subsystem="concurrency",
        trigger_condition="Multiple daemon processes share a SQLite resource-lock store.",
        dependencies=[
            "SQLiteResourceLockStore",
            "build_daemon_worker_pool_service_install_plans",
            "one state file per daemon replica",
        ],
        activated=activated,
        evidence_refs=[lock_path.as_posix(), *[plan.service_name for plan in pool_plan.plans]],
        generated_work_item_ids=[item_a.work_item_id, item_b.work_item_id],
        trigger_status="waiting_lock" if second.conflicts else "unknown",
        notes=[
            "This activates local multi-process coordination without requiring Redis/Kubernetes."
        ],
    )


class _AuditRedisPipeline:
    def __init__(self, redis: _AuditRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, str]] = []

    def __enter__(self) -> _AuditRedisPipeline:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def watch(self, *_keys: str) -> None:
        return None

    def mget(self, keys: list[str]) -> list[str | None]:
        return [self.redis.values.get(key) for key in keys]

    def unwatch(self) -> None:
        return None

    def multi(self) -> None:
        return None

    def set(self, key: str, value: str, *, px: int) -> None:
        self.commands.append((key, value))

    def execute(self) -> list[bool]:
        for key, value in self.commands:
            self.redis.values[key] = value
        return [True for _ in self.commands]


class _AuditRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def pipeline(self) -> _AuditRedisPipeline:
        return _AuditRedisPipeline(self)

    def scan_iter(self, pattern: str):
        prefix = pattern.removesuffix("*")
        return (key for key in list(self.values) if key.startswith(prefix))

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _case_redis_distributed_resource_lock_adapter(
    root: Path, now: datetime
) -> FeatureActivationCase:
    redis = _AuditRedis()
    store_a = RedisResourceLockStore(client=redis)
    store_b = RedisResourceLockStore(client=redis)
    item_a = WorkItem(
        work_item_id="work-redis-lock-a",
        mission_id="msn-activation-redis-lock",
        task_plan_version="v1",
        type="execution",
        owner="kun",
    )
    item_b = item_a.model_copy(update={"work_item_id": "work-redis-lock-b"})
    first = store_a.acquire_many(
        resources=["workspace:redis-shared"],
        holder_id="lease-redis-a",
        daemon_id="daemon-redis-a",
        worker_id="worker-a",
        work_item=item_a,
        now=now,
        ttl=timedelta(minutes=5),
    )
    second = store_b.acquire_many(
        resources=["workspace:redis-shared"],
        holder_id="lease-redis-b",
        daemon_id="daemon-redis-b",
        worker_id="worker-b",
        work_item=item_b,
        now=now + timedelta(seconds=1),
        ttl=timedelta(minutes=5),
    )
    activated = (
        first.acquired
        and not second.acquired
        and second.conflicts[0].holder_daemon_id == "daemon-redis-a"
    )
    return FeatureActivationCase(
        feature_id="redis_distributed_resource_lock_adapter",
        subsystem="concurrency",
        trigger_condition="Two worker processes/machines contend for one Redis resource lock.",
        dependencies=["RedisResourceLockStore", "resource lock backend=redis"],
        activated=activated,
        evidence_refs=[second.conflicts[0].resource_ref] if second.conflicts else [],
        generated_work_item_ids=[item_a.work_item_id, item_b.work_item_id],
        trigger_status="waiting_lock" if second.conflicts else "unknown",
        notes=[
            "This proves the cross-machine lock adapter contract without requiring a live Redis server."
        ],
    )


def _case_container_required_gate(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane, _store, mission = _runtime(
        root / "container-required-runtime.json",
        mission_id="msn-activation-container",
        workspace=root / "container-workspace",
    )
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="activation-container",
        sandbox_mode="container_required",
        container_runtime="docker",
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=1)
    item = control_plane.work_items["work-msn-activation-container"]
    supports = [
        support for artifact in control_plane.artifacts.values() for support in artifact.supports
    ]
    activated = item.status == "failed" and "container_sandbox_required" in supports
    return FeatureActivationCase(
        feature_id="container_required_blocks_unsupported_runner",
        subsystem="sandbox",
        trigger_condition="Daemon sandbox_mode=container_required while selected runner lacks container support.",
        dependencies=["sandbox_spec_for_work_item", "runner.supports_container_sandbox"],
        activated=activated,
        evidence_refs=[
            *report.run_refs,
            *[
                artifact.artifact_id
                for artifact in control_plane.artifacts.values()
                if "container_sandbox_required" in artifact.supports
            ],
        ],
        generated_work_item_ids=list(report.ran_work_item_ids),
        trigger_status=item.status,
        notes=[
            "This prevents workspace snapshot isolation from being misreported as container isolation."
        ],
    )


def _case_qi_nuo_observation_strategy_loop(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane, _store, mission = _runtime(
        root / "qi-nuo-runtime.json",
        mission_id="msn-activation-qi-nuo",
        workspace=root / "qi-nuo-workspace",
    )
    mission = control_plane.missions[mission.mission_id].model_copy(
        update={"task_type": "product_development"}
    )
    control_plane.missions[mission.mission_id] = mission
    if control_plane.store is not None:
        control_plane.store.put_mission(mission)
    failed = control_plane.work_items["work-msn-activation-qi-nuo"].model_copy(
        update={"status": "failed"}
    )
    control_plane.work_items[failed.work_item_id] = failed
    if control_plane.store is not None:
        control_plane.store.put_work_item(failed)
    control_plane.transition_mission(
        mission_id=mission.mission_id,
        target="running",
        actor="feature-activation-audit",
        reason="failed quality gate activation case needs runtime observation followups",
        subject_ref=failed.work_item_id,
    )
    control_plane.apply_gate(_failed_gate(mission, failed))
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner=_default_owner_runners(control_plane),
        daemon_id="activation-qi-nuo",
        worker_pool=WorkerPoolConfig(worker_count=3),
    )
    observation_tick = daemon.tick_once(
        mission_ids=[mission.mission_id],
        max_work_items=0,
        write_progress=True,
        now=now,
    )
    loop = daemon.run_loop(
        mission_ids=[mission.mission_id],
        max_ticks=5,
        max_work_items_per_tick=3,
        poll_interval_sec=0,
        sleeper=lambda _seconds: None,
        now_factory=lambda: now,
    )
    artifacts = control_plane.artifacts.values()
    activated = (
        any("qi_runtime_governance_report" in artifact.supports for artifact in artifacts)
        and any("nuo_runtime_repair_report" in artifact.supports for artifact in artifacts)
        and any("strategy_optimization_plan" in artifact.supports for artifact in artifacts)
    )
    return FeatureActivationCase(
        feature_id="qi_nuo_observation_strategy_loop",
        subsystem="qi_nuo_self_iteration",
        trigger_condition="Failed work item and failed quality gate exist without recovery refs.",
        dependencies=[
            "RuntimeObservationReport",
            "QiRuntimeGovernanceRunner",
            "NuoRuntimeRepairRunner",
            "KunRuntimeTaskRunner",
        ],
        activated=activated,
        evidence_refs=[
            artifact.artifact_id
            for artifact in artifacts
            if (
                artifact.supports
                and artifact.supports[0]
                in {"qi_runtime_governance_report", "nuo_runtime_repair_report"}
            )
            or "strategy_optimization_plan" in artifact.supports
        ],
        generated_work_item_ids=[
            *observation_tick.created_work_item_ids,
            *observation_tick.observation_followup_ids,
            *[
                wid
                for tick in loop.tick_reports
                for wid in [*tick.created_work_item_ids, *tick.ran_work_item_ids]
            ],
        ],
        trigger_status=control_plane.missions[mission.mission_id].status,
        notes=[
            "This verifies that weak execution opens Nuo/Qi work and KUN follow-up strategy tasks automatically."
        ],
    )


def _case_runtime_observation_hardening(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane, _store, mission = _runtime(
        root / "runtime-observation-hardening.json",
        mission_id="msn-activation-runtime-hardening",
        workspace=root / "runtime-observation-hardening-workspace",
    )
    rework_plan_version = "runtime-observation-v1-acceptance-rework-22222222"
    mission = mission.model_copy(
        update={
            "status": "awaiting_acceptance",
            "current_plan_version": rework_plan_version,
        }
    )
    control_plane.missions[mission.mission_id] = mission
    work_item = control_plane.work_items["work-msn-activation-runtime-hardening"].model_copy(
        update={
            "status": "done",
            "task_plan_version": rework_plan_version,
            "required_capability_refs": ["cap-observation-required"],
            "expected_output": "Capability must produce a behavior receipt.",
        }
    )
    control_plane.work_items[work_item.work_item_id] = work_item
    for index, plan_version in enumerate(
        [
            "runtime-observation-v1-acceptance-rework-11111111",
            rework_plan_version,
        ]
    ):
        gate = GateEvaluation(
            gate_evaluation_id=f"gate-observation-pressure-{index}",
            mission_id=mission.mission_id,
            task_plan_version=plan_version,
            subject_ref=f"ticket-observation-acceptance-{index}",
            stage="delivery",
            task_type=mission.task_type,
            rubric_version="kun-runtime-observation-hardening-v1",
            metric_pack_version="north-star-v6",
            north_star_verdict="partial",
            result_quality=0.72,
            speed=0.8,
            cost=0.8,
            risk=0.42,
            evidence_quality=0.7,
            collaboration_quality=0.75,
            hard_gate_failures=["human_acceptance_missing"],
            next_action="needs_plan_change",
            next_state="changing_plan",
            governance_signal="open_acceptance_requires_continued_product_pressure",
            created_by="feature-activation-audit",
        )
        control_plane.gate_evaluations[gate.gate_evaluation_id] = gate
    report = build_runtime_observation_report(
        control_plane=control_plane,
        mission_id=mission.mission_id,
        tick_report=object(),
        capability_policy=CapabilityExecutionPolicy(policy_id="policy-empty", built_at=now),
    )
    codes = {item.code for item in report.items}
    activated = {
        "capability_consumption_unproven",
        "mechanical_acceptance_rework_loop",
    }.issubset(codes)
    evidence_refs = [
        ref
        for item in report.items
        if item.code in {"capability_consumption_unproven", "mechanical_acceptance_rework_loop"}
        for ref in item.evidence_refs
    ]
    return FeatureActivationCase(
        feature_id="runtime_observation_hardening",
        subsystem="runtime_observation",
        trigger_condition=(
            "A mission has required-capability work without behavior receipt and repeated "
            "acceptance rework pressure."
        ),
        dependencies=[
            "capability_behavior_receipt observation",
            "mechanical acceptance rework loop observation",
            "Qi/Nuo/external-supervisor routing",
        ],
        activated=activated,
        evidence_refs=evidence_refs,
        trigger_status="observed" if activated else "gap",
        notes=[
            "This verifies KUN can surface metadata-only capability activation and mechanical "
            "rework loops before external supervision discovers them manually."
        ],
        gaps=[]
        if activated
        else ["runtime observation did not flag capability receipt and rework-loop gaps"],
        evidence_scope="static_probe",
    )


def _case_capability_dedupe_qi_governance(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane, _store, mission = _runtime(
        root / "capability-dedupe-runtime.json",
        mission_id="msn-activation-capability-dedupe",
        workspace=root / "capability-workspace",
    )
    for index, suffix in enumerate(("old", "new")):
        profile = CapabilityProfile(
            capability_id=f"cap-activation-duplicate-{suffix}",
            capability_name="Activation duplicate runtime capability",
            governance_key="activation-duplicate-runtime-capability",
            source_refs=[f"source-{suffix}"],
            source_versions=[f"v{index}"],
            evidence_refs=[f"artifact-evidence-{suffix}"],
            holdout_refs=[f"artifact-holdout-{suffix}"],
            regression_refs=[f"artifact-regression-{suffix}"],
            last_verified_at=now + timedelta(minutes=index),
            rollback_plan=["disable duplicate"],
            promotion_stage="production",
            runtime_enabled=True,
        )
        control_plane.capability_profiles[profile.capability_id] = profile
        control_plane.store.put_capability_profile(profile)
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            "kun": PolicyAwareRunner(),
            "qi": QiRuntimeGovernanceRunner(control_plane=control_plane),
        },
        daemon_id="activation-capability-dedupe",
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=1)
    second = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": QiRuntimeGovernanceRunner(control_plane=control_plane)},
        daemon_id="activation-capability-dedupe-qi",
    ).tick_once(mission_ids=[mission.mission_id], now=now + timedelta(seconds=1), max_work_items=2)
    activated = bool(report.retired_capability_profile_ids) and any(
        "qi_runtime_governance_report" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )
    return FeatureActivationCase(
        feature_id="capability_dedupe_routes_to_qi",
        subsystem="qi_capability_governance",
        trigger_condition="Two production runtime capabilities share a governance_key.",
        dependencies=[
            "CapabilityGovernanceReport",
            "daemon duplicate retirement",
            "Qi runtime governance runner",
        ],
        activated=activated,
        evidence_refs=[
            *report.retired_capability_profile_ids,
            *[
                artifact.artifact_id
                for artifact in control_plane.artifacts.values()
                if "qi_runtime_governance_report" in artifact.supports
            ],
        ],
        generated_work_item_ids=[*report.created_work_item_ids, *second.ran_work_item_ids],
        trigger_status="activated" if activated else "gap",
        notes=["Daemon may do the safety retirement, but Qi must record the governance decision."],
    )


def _case_task_vs_self_improvement_boundary(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane, _store, mission = _domain_runtime(
        root / "task-vs-self-improvement-runtime.json",
        mission_id="msn-activation-task-boundary",
        owner="kun",
        workspace=root / "task-boundary-workspace",
        work_ids=["work-task-boundary-user-delivery"],
    )
    candidate = CapabilityCandidate(
        candidate_id="cand-user-task-runtime-pollution",
        capability_name="User task must not enable KUN runtime defaults",
        source="real_task_review",
        source_ref=mission.mission_id,
        hypothesis=(
            "User mission learning can become a learning signal, but cannot directly "
            "enable a production runtime capability."
        ),
        target_task_types=["product_development"],
        evidence_refs=["artifact-user-task-learning-signal"],
        known_limits=["Only Qi/Nuo self_improvement governance can promote production defaults."],
        created_by="kun",
    )
    promotion = build_capability_promotion(
        candidate,
        [
            _capability_boundary_evaluation(stage, mission_id=mission.mission_id)
            for stage in ["replay", "holdout", "shadow", "canary", "production"]
        ],
        target_stage="production",
        capability_id="cap-user-task-runtime-pollution",
    )
    blocked = False
    try:
        control_plane.apply_capability_promotion(promotion, actor="kun")
    except ValueError as exc:
        blocked = "only allowed from self_improvement" in str(exc)
    activated = blocked and not control_plane.list_default_runtime_capabilities()
    return FeatureActivationCase(
        feature_id="task_vs_self_improvement_boundary",
        subsystem="qi_capability_governance",
        trigger_condition=(
            "A product_development user mission attempts to enable a production CapabilityProfile."
        ),
        dependencies=[
            "apply_capability_promotion boundary guard",
            "CapabilityProfile runtime_enabled production profile",
            "mission.task_type self_improvement requirement",
        ],
        activated=activated,
        evidence_refs=[promotion.promotion_id, promotion.gate_evaluation.gate_evaluation_id],
        generated_work_item_ids=[],
        trigger_status="blocked" if activated else "gap",
        notes=[
            "User task learning remains evidence for Qi/Nuo governance; it cannot mutate KUN "
            "production defaults directly."
        ],
        gaps=[] if activated else ["user task production capability promotion was not blocked"],
        evidence_scope="static_probe",
    )


def _case_governed_self_improvement_loop(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane, store, mission = _runtime(
        root / "governed-self-improvement-runtime.json",
        mission_id="msn-activation-governed-self-improvement",
        workspace=root / "governed-self-improvement-workspace",
    )
    seed_work = control_plane.work_items[f"work-{mission.mission_id}"].model_copy(
        update={
            "status": "done",
            "required_capability_refs": ["cap-activation-strategy"],
            "expected_output": (
                "Use the activated strategy capability and produce a behavior receipt."
            ),
        }
    )
    control_plane.work_items[seed_work.work_item_id] = seed_work
    store.put_work_item(seed_work)
    control_plane.missions[mission.mission_id] = mission.model_copy(
        update={"status": "running", "current_plan_version": "v1"}
    )
    store.put_mission(control_plane.missions[mission.mission_id])

    runners = {
        "nuo": ChainedControlPlaneRunner(
            runner_identity="activation-self-improvement-nuo-router",
            runners=[NuoSelfImprovementAuditRunner(control_plane=control_plane)],
        ),
        "qi": ChainedControlPlaneRunner(
            runner_identity="activation-self-improvement-qi-router",
            runners=[QiSelfImprovementStrategyRunner(control_plane=control_plane)],
        ),
    }
    first = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner=runners,
        daemon_id="activation-governed-self-improvement-1",
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=1)
    second = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner=runners,
        daemon_id="activation-governed-self-improvement-2",
    ).tick_once(mission_ids=[mission.mission_id], now=now + timedelta(seconds=1), max_work_items=1)

    audit_artifacts = [
        artifact.artifact_id
        for artifact in control_plane.artifacts.values()
        if SELF_IMPROVEMENT_AUDIT_SUPPORT in artifact.supports
    ]
    strategy_artifacts = [
        artifact.artifact_id
        for artifact in control_plane.artifacts.values()
        if SELF_IMPROVEMENT_STRATEGY_SUPPORT in artifact.supports
    ]
    replay_profiles = [
        profile.capability_id
        for profile in control_plane.capability_profiles.values()
        if profile.capability_id.startswith("cap-self-improvement-")
        and profile.promotion_stage == "replay"
        and not profile.runtime_enabled
    ]
    layer_supports = {
        support
        for artifact in control_plane.artifacts.values()
        for support in artifact.supports
        if support.startswith("rsi_layer:")
    }
    kun_followups = [
        item.work_item_id
        for item in control_plane.work_items.values()
        if item.work_item_id.startswith("work-kun-self-improvement-implementation-")
    ]
    activated = bool(
        audit_artifacts
        and strategy_artifacts
        and replay_profiles
        and kun_followups
        and {
            "rsi_layer:self_evaluation",
            "rsi_layer:strategy_search",
            "rsi_layer:safe_experimentation",
            "rsi_layer:learning_signal",
            "rsi_layer:capability_governance",
        }.issubset(layer_supports)
    )
    return FeatureActivationCase(
        feature_id="governed_self_improvement_loop",
        subsystem="qi_nuo_self_iteration",
        trigger_condition=(
            "A done KUN work item used required capabilities but has no behavior receipt."
        ),
        dependencies=[
            "NuoSelfImprovementAuditRunner",
            "QiSelfImprovementStrategyRunner",
            "daemon self-improvement audit scheduling",
            "runtime_enabled=false replay capability profile",
        ],
        activated=activated,
        evidence_refs=[*audit_artifacts, *strategy_artifacts, *replay_profiles, *layer_supports],
        generated_work_item_ids=[
            *first.created_work_item_ids,
            *first.ran_work_item_ids,
            *second.created_work_item_ids,
            *second.ran_work_item_ids,
            *kun_followups,
        ],
        trigger_status="activated" if activated else "gap",
        notes=[
            "This proves Nuo can audit a KUN capability-consumption gap, Qi can generate "
            "strategy candidates, and the selected candidate stays replay-only."
        ],
        gaps=[]
        if activated
        else ["governed self-improvement audit/strategy/replay candidate chain did not complete"],
        evidence_scope="fixture",
    )


def _capability_boundary_evaluation(stage: str, *, mission_id: str) -> CapabilityEvaluation:
    payload: dict[str, object] = {
        "evaluation_id": f"eval-task-boundary-{stage}",
        "candidate_id": "cand-user-task-runtime-pollution",
        "stage": stage,
        "mission_id": mission_id,
        "task_plan_version": "v1",
        "subject_ref": f"work-task-boundary-{stage}",
        "passed": True,
        "result_quality": 0.91,
        "speed": 0.75,
        "cost": 0.7,
        "risk": 0.2,
        "evidence_refs": [f"artifact-task-boundary-evidence-{stage}"],
        "artifact_refs": [f"artifact-task-boundary-report-{stage}"],
        "review_refs": [f"artifact-task-boundary-review-{stage}"],
    }
    if stage in {"holdout", "canary", "production"}:
        payload["holdout_refs"] = ["artifact-task-boundary-holdout"]
    if stage in {"canary", "production"}:
        payload["regression_refs"] = ["artifact-task-boundary-regression"]
        payload["rollback_plan"] = ["disable rejected user-task runtime profile"]
    return CapabilityEvaluation.model_validate(payload)


def _case_merge_conflict_governance(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane, _store, mission = _runtime(
        root / "merge-runtime.json",
        mission_id="msn-activation-merge",
        workspace=root / "merge-workspace",
    )
    upstream_a = WorkItem(
        work_item_id="work-merge-upstream-a",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        status="done",
    )
    upstream_b = upstream_a.model_copy(update={"work_item_id": "work-merge-upstream-b"})
    merge = WorkItem(
        work_item_id="work-merge-conflict",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="merge",
        owner="kun",
        dependencies=[upstream_a.work_item_id, upstream_b.work_item_id],
        expected_output="Merge conflicting upstream artifacts.",
    )
    control_plane.work_items = {
        upstream_a.work_item_id: upstream_a,
        upstream_b.work_item_id: upstream_b,
        merge.work_item_id: merge,
    }
    if control_plane.store is not None:
        control_plane.store.put_work_item(upstream_a)
        control_plane.store.put_work_item(upstream_b)
        control_plane.store.put_work_item(merge)
    for item in (upstream_a, upstream_b):
        artifact = ArtifactRecord(
            artifact_id=f"artifact-{item.work_item_id}",
            kind="diff",
            path_or_uri="repo://src/app.ts",
            content_hash=f"hash-{item.work_item_id}",
            created_by=item.work_item_id,
            mission_id=mission.mission_id,
            work_item_id=item.work_item_id,
            supports=["writes:src/app.ts"],
            source_quality="primary",
        )
        control_plane.artifacts[artifact.artifact_id] = artifact
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": KunRuntimeTaskRunner(control_plane=control_plane)},
        daemon_id="activation-merge",
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=1)
    gate = control_plane.gate_evaluations.get("gate-kun-merge-work-merge-conflict")
    activated = gate is not None and "overlapping_artifact_output" in gate.hard_gate_failures
    return FeatureActivationCase(
        feature_id="merge_conflict_governance",
        subsystem="merge_governance",
        trigger_condition="Merge work item depends on two artifacts that write the same path.",
        dependencies=["KunRuntimeTaskRunner merge path", "build_merge_governance_report"],
        activated=activated,
        evidence_refs=[gate.gate_evaluation_id] if gate else [],
        generated_work_item_ids=list(report.ran_work_item_ids),
        trigger_status=control_plane.work_items[merge.work_item_id].status,
        notes=[
            "Merge conflict must block delivery instead of hiding behind artifact concatenation."
        ],
    )


def _case_workspace_rollback(root: Path, now: datetime) -> FeatureActivationCase:
    workspace = root / "rollback-workspace"
    workspace.mkdir(parents=True)
    target = workspace / "state.txt"
    target.write_text("v1", encoding="utf-8")
    control_plane, _store, mission = _runtime(
        root / "rollback-runtime.json",
        mission_id="msn-activation-rollback",
        workspace=workspace,
    )
    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="activation-rollback",
    )
    first = daemon.tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=1)
    snapshot_refs = list(control_plane.work_items["work-msn-activation-rollback"].rollback_refs)
    target.write_text("v2", encoding="utf-8")
    (workspace / "extra.txt").write_text("extra", encoding="utf-8")
    rollback = WorkItem(
        work_item_id="work-activation-rollback",
        mission_id=mission.mission_id,
        task_plan_version="v1",
        type="rollback",
        owner="kun",
        idempotency_key="feature-activation-rollback",
        expected_output="Restore workspace snapshot.",
        rollback_refs=snapshot_refs,
    )
    control_plane.work_items[rollback.work_item_id] = rollback
    if control_plane.store is not None:
        control_plane.store.put_work_item(rollback)
    second = ControlPlaneDaemon(
        control_plane=control_plane,
        daemon_id="activation-rollback-restore",
    ).tick_once(mission_ids=[mission.mission_id], now=now + timedelta(minutes=1), max_work_items=1)
    activated = (
        target.read_text(encoding="utf-8") == "v1" and not (workspace / "extra.txt").exists()
    )
    return FeatureActivationCase(
        feature_id="workspace_snapshot_and_rollback",
        subsystem="rollback",
        trigger_condition="Workspace work item creates a snapshot, then rollback work item references it.",
        dependencies=["create_workspace_snapshot", "WorkspaceRollbackRunner", "rollback_refs"],
        activated=activated,
        evidence_refs=[
            *snapshot_refs,
            *[
                artifact.artifact_id
                for artifact in control_plane.artifacts.values()
                if "workspace_restore" in artifact.supports
            ],
        ],
        generated_work_item_ids=[*first.ran_work_item_ids, *second.ran_work_item_ids],
        trigger_status=control_plane.work_items[rollback.work_item_id].status,
        notes=["Rollback must restore files and remove post-snapshot extras."],
    )


def _case_watchtower_bridge(root: Path, now: datetime) -> FeatureActivationCase:
    control_plane, _store, mission = _runtime(
        root / "watchtower-runtime.json",
        mission_id="msn-activation-watchtower",
        workspace=root / "watchtower-workspace",
    )
    rule = GuardRule(
        id="activation_gate_pass",
        kind="guard",
        trigger=RuleTrigger(
            event_type="control_plane.gate_evaluated",
            when="event['payload']['north_star_verdict'] == 'pass'",
        ),
    )
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"kun": StaticRunner()},
        daemon_id="activation-watchtower",
        rule_engine=RuleEngine([rule]),
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=1)
    activated = "activation_gate_pass" in report.watchtower_fired_rule_ids
    return FeatureActivationCase(
        feature_id="watchtower_runtime_bridge",
        subsystem="watchtower",
        trigger_condition="A work item produces a GateEvaluation consumed by Watchtower.",
        dependencies=["V6 Watchtower event namespace", "RuleEngine"],
        activated=activated,
        evidence_refs=list(report.watchtower_fired_rule_ids),
        generated_work_item_ids=list(report.ran_work_item_ids),
        trigger_status="activated" if activated else "gap",
        notes=["Runtime rules must see Control Plane gates, not a separate telemetry island."],
    )


def _case_external_sample_comparison(root: Path, now: datetime) -> FeatureActivationCase:
    source = root / "external-sample" / "genesis"
    target = root / "external-sample" / "kun"
    output = root / "external-sample" / "comparison"
    (source / "docs").mkdir(parents=True)
    (target / "docs").mkdir(parents=True)
    (source / "docs" / "architecture.md").write_text(
        "# Genesis\n\nGateway sessions tools.\n\nHall of Fame.\n\nConsult injection.",
        encoding="utf-8",
    )
    (target / "docs" / "kun.md").write_text(
        "# KUN\n\nGateway sessions tools are handled through Control Plane.",
        encoding="utf-8",
    )
    control_plane, _store, mission = _runtime(
        root / "external-sample-runtime.json",
        mission_id="msn-activation-external-sample",
        workspace=target,
    )
    contract = control_plane.contracts[f"contract-{mission.mission_id}"].model_copy(
        update={
            "evidence_policy": {
                "external_sample_comparison": {
                    "source_name": "Genesis",
                    "source_repo_path": str(source),
                    "target_repo_path": str(target),
                    "output_dir": str(output),
                    "max_source_files": 20,
                    "max_target_files": 20,
                }
            }
        }
    )
    control_plane.contracts[contract.contract_id] = contract
    work_item = control_plane.work_items[f"work-{mission.mission_id}"].model_copy(
        update={
            "type": "research",
            "owner": KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER,
            "expected_output": "Compare external sample with KUN and produce Qi governance actions.",
        }
    )
    control_plane.work_items = {work_item.work_item_id: work_item}
    if control_plane.store is not None:
        control_plane.store.put_execution_contract(contract)
        control_plane.store.put_work_item(work_item)
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            KUN_EXTERNAL_SAMPLE_COMPARISON_RUNNER_OWNER: ExternalSampleComparisonRunner(
                control_plane=control_plane
            )
        },
        daemon_id="activation-external-sample",
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=1)
    activated = (output / "qi-governance-actions.json").exists() and any(
        "external_sample_governance_plan" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )
    return FeatureActivationCase(
        feature_id="external_sample_comparison_qi_governance",
        subsystem="external_sample_learning",
        trigger_condition="Research work item owned by external-sample runner with source/target repo paths.",
        dependencies=[
            "ExternalSampleComparisonRunner",
            "evidence_policy.external_sample_comparison",
            "Qi governance action output",
        ],
        activated=activated,
        evidence_refs=[
            *report.ran_work_item_ids,
            *[
                artifact.artifact_id
                for artifact in control_plane.artifacts.values()
                if "external_sample_governance_plan" in artifact.supports
            ],
        ],
        generated_work_item_ids=list(report.ran_work_item_ids),
        trigger_status=control_plane.work_items[work_item.work_item_id].status,
        notes=["External samples become KUN-native governance actions, not copied code."],
    )


def _case_autonomous_app_development_runner(root: Path, now: datetime) -> FeatureActivationCase:
    project = root / "app-dev-project"
    control_plane, _store, mission = _domain_runtime(
        root / "app-dev-runtime.json",
        mission_id="msn-activation-app-dev",
        owner=KUN_AUTONOMOUS_APP_RUNNER_OWNER,
        workspace=project,
        work_ids=[
            "work-huohutu-00-kun-autonomous-runner-activation",
            "work-huohutu-01-mvp-scope-worlds",
            "work-huohutu-02-app-scaffold",
            "work-huohutu-03-core-game-loop",
            "work-huohutu-04-ui-safety-report",
            "work-huohutu-05-qa-delivery",
        ],
    )
    commands: list[list[str]] = []

    def fake_command(command: list[str], _cwd: Path, _timeout_sec: int) -> AppCommandResult:
        commands.append(command)
        return AppCommandResult(exit_code=0, stdout="ok", stderr="")

    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            KUN_AUTONOMOUS_APP_RUNNER_OWNER: AutonomousAppDevelopmentRunner(
                control_plane=control_plane,
                command_runner=fake_command,
            )
        },
        daemon_id="activation-app-dev",
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=10)
    activated = (project / "src" / "App.tsx").exists() and (
        project / "docs" / "delivery-report.md"
    ).exists()
    return FeatureActivationCase(
        feature_id="autonomous_app_development_runner",
        subsystem="app_development",
        trigger_condition="A product-development work queue is owned by the autonomous app runner.",
        dependencies=[
            "AutonomousAppDevelopmentRunner",
            "delivery_contract.project_path",
            "npm command boundary",
        ],
        activated=activated,
        evidence_refs=[
            *report.ran_work_item_ids,
            *[" ".join(command) for command in commands],
        ],
        generated_work_item_ids=list(report.ran_work_item_ids),
        trigger_status=control_plane.missions[mission.mission_id].status,
        notes=[
            "Uses fake npm commands; verifies runner activation and project materialization without resuming the game task."
        ],
    )


def _case_game_design_research_runner(root: Path, now: datetime) -> FeatureActivationCase:
    project = root / "game-design-project"
    final_project = root / "game-design-final"
    control_plane, _store, mission = _domain_runtime(
        root / "game-design-runtime.json",
        mission_id="msn-activation-game-design",
        owner=KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER,
        workspace=project,
        final_workspace=final_project,
        task_plan_version="mvp-v2-research-first",
        work_ids=[
            "work-huohutu-v2-00-research-source-corpus",
            "work-huohutu-v2-02-game-design-synthesis",
            "work-huohutu-v2-03-research-design-gate",
        ],
    )
    sources = [
        ResearchSource(
            source_id=f"source-{idx}",
            title=f"Source {idx}",
            url=f"https://example.com/source-{idx}",
            category="benchmark",
            organization="Example",
            design_takeaway=f"Takeaway {idx}",
        ).model_dump(mode="json")
        for idx in range(12)
    ]
    contract_id = f"contract-{mission.mission_id}"
    contract = control_plane.contracts[contract_id].model_copy(
        update={
            "evidence_policy": {"research_sources": sources},
            "delivery_contract": {
                "project_path": str(project),
                "final_project_path": str(final_project),
            },
        }
    )
    control_plane.contracts[contract_id] = contract
    if control_plane.store is not None:
        control_plane.store.put_execution_contract(contract)

    def fake_fetcher(source: ResearchSource) -> tuple[str, str, str]:
        return "fetched", source.title, f"{source.organization} says {source.design_takeaway}"

    def fake_command(command: list[str], _cwd: Path, _timeout_sec: int) -> AppCommandResult:
        return AppCommandResult(exit_code=0, stdout="ok", stderr="")

    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            KUN_GAME_DESIGN_RESEARCH_RUNNER_OWNER: GameDesignResearchRunner(
                control_plane=control_plane,
                fetcher=fake_fetcher,
            ),
            KUN_AUTONOMOUS_APP_RUNNER_OWNER: AutonomousAppDevelopmentRunner(
                control_plane=control_plane,
                command_runner=fake_command,
            ),
        },
        daemon_id="activation-game-design",
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=12)
    activated = (project / "docs" / "game-design-spec.md").exists() and (
        final_project / "docs" / "delivery-report.md"
    ).exists()
    return FeatureActivationCase(
        feature_id="game_design_research_to_app_runner",
        subsystem="research_first_product_development",
        trigger_condition="Research gate work item owned by game-design runner passes and queues app runner.",
        dependencies=[
            "GameDesignResearchRunner",
            "research source corpus",
            "AutonomousAppDevelopmentRunner",
        ],
        activated=activated,
        evidence_refs=list(report.ran_work_item_ids),
        generated_work_item_ids=[*report.created_work_item_ids, *report.ran_work_item_ids],
        trigger_status=control_plane.missions[mission.mission_id].status,
        notes=["This is a small activation fixture, not the paused game delivery task."],
    )


def _case_game_production_runner(root: Path, now: datetime) -> FeatureActivationCase:
    project = root / "game-production-project"
    control_plane, _store, mission = _domain_runtime(
        root / "game-production-runtime.json",
        mission_id="msn-activation-game-production",
        owner=KUN_GAME_PRODUCTION_RUNNER_OWNER,
        workspace=project,
        work_ids=[
            "work-huohutu-v3-01-interaction-design",
            "work-huohutu-v3-02-game-production",
            "work-huohutu-v3-03-internal-test",
            "work-huohutu-v3-04-final-delivery",
        ],
    )
    commands: list[list[str]] = []

    def fake_command(
        command: list[str], _cwd: Path, _timeout_sec: int
    ) -> GameProductionCommandResult:
        commands.append(command)
        return GameProductionCommandResult(exit_code=0, stdout="ok", stderr="")

    daemon = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={
            KUN_GAME_PRODUCTION_RUNNER_OWNER: GameProductionRunner(
                control_plane=control_plane,
                command_runner=fake_command,
            )
        },
        daemon_id="activation-game-production",
    )
    reports = [
        daemon.tick_once(
            mission_ids=[mission.mission_id],
            now=now + timedelta(seconds=index),
            max_work_items=8,
        )
        for index in range(len(control_plane.work_items))
    ]
    ran_work_item_ids = [
        work_item_id for report in reports for work_item_id in report.ran_work_item_ids
    ]
    activated = (project / "src" / "App.tsx").exists() and (
        project / "docs" / "final-playable-delivery.md"
    ).exists()
    return FeatureActivationCase(
        feature_id="game_production_runner_internal_delivery",
        subsystem="game_production",
        trigger_condition="Game production work queue is owned by the game production runner.",
        dependencies=[
            "GameProductionRunner",
            "project_path",
            "internal/user simulation command hooks",
        ],
        activated=activated,
        evidence_refs=ran_work_item_ids,
        generated_work_item_ids=ran_work_item_ids,
        trigger_status=control_plane.missions[mission.mission_id].status,
        notes=[
            "The user-facing game task remains paused; this only activates the runner path on a fixture."
        ],
    )


def _case_qi_ab_external_runner(root: Path, now: datetime) -> FeatureActivationCase:
    workdir = root / "frontier50"
    run_tag = "activation-ab"
    round_dir = (
        workdir
        / "fair_ab_outputs"
        / "real_comparator_ab_external_live"
        / "groups"
        / "group-01"
        / run_tag
    )
    round_dir.mkdir(parents=True)
    (round_dir / "report.json").write_text(
        json.dumps(
            {
                "status": "ok",
                "rankings": [
                    {"agent_ref": "kun", "avg_overall_score": 0.92},
                    {"agent_ref": "hermes", "avg_overall_score": 0.86},
                ],
            }
        ),
        encoding="utf-8",
    )
    (round_dir / "comparator_health.json").write_text(
        json.dumps({"comparator_unhealthy": False}), encoding="utf-8"
    )
    (round_dir / "repair_tickets.json").write_text("[]", encoding="utf-8")
    (round_dir / "runs.jsonl").write_text(
        "\n".join(json.dumps({"run": idx}) for idx in range(20)) + "\n", encoding="utf-8"
    )
    (round_dir / "reviews.jsonl").write_text(
        "\n".join(json.dumps({"review": idx}) for idx in range(45)) + "\n", encoding="utf-8"
    )
    control_plane, _store, mission = _runtime(
        root / "ab-runtime.json",
        mission_id="msn-activation-ab",
        workspace=workdir,
    )
    work_item = build_qi_ab_round_work_item(
        mission_id=mission.mission_id,
        task_plan_version="v1",
        round_id="round-01",
        task_ids=[f"t{idx}" for idx in range(5)],
    )
    control_plane.work_items = {work_item.work_item_id: work_item}
    if control_plane.store is not None:
        control_plane.store.put_work_item(work_item)

    def fake_executor(
        _command: Path, _env: dict[str, str], _cwd: Path, _timeout: int
    ) -> ExternalCommandResult:
        return ExternalCommandResult(exit_code=0, stdout="ok", stderr="")

    runner = Frontier50ExternalRoundRunner(
        config=Frontier50ExternalRoundConfig(
            workdir=workdir,
            command=workdir / "run.sh",
            round_index=1,
            round_id="round-01",
            run_tag=run_tag,
        ),
        mission_id=mission.mission_id,
        task_plan_version="v1",
        task_ids=[f"t{idx}" for idx in range(5)],
        executor=fake_executor,
    )
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"qi": runner},
        daemon_id="activation-ab",
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=1)
    activated = any(ref.startswith("manifest-qi-ab-") for ref in control_plane.artifact_manifests)
    return FeatureActivationCase(
        feature_id="qi_ab_frontier50_external_runner",
        subsystem="qi_ab_regression",
        trigger_condition="Qi test work item mentions Frontier50/AB and a round directory exists.",
        dependencies=[
            "Frontier50ExternalRoundRunner",
            "report.json",
            "comparator_health.json",
            "runs/reviews jsonl",
        ],
        activated=activated,
        evidence_refs=[*report.ran_work_item_ids, *control_plane.artifact_manifests.keys()],
        generated_work_item_ids=list(report.ran_work_item_ids),
        trigger_status=control_plane.work_items[work_item.work_item_id].status,
        notes=[
            "Runs against a synthetic round fixture; real AB rounds remain paused unless explicitly requested."
        ],
    )


def _case_productization_dogfood_runner(root: Path, now: datetime) -> FeatureActivationCase:
    store = FileControlPlaneStore(root / "productization-runtime.json")
    control_plane = InMemoryControlPlane(store=store)
    mission = submit_productization_dogfood_mission(
        control_plane,
        build_productization_dogfood_mission(mission_id="msn-activation-productization"),
    )
    runner = ProductizationDogfoodRunner(control_plane=control_plane)
    report = ControlPlaneDaemon(
        control_plane=control_plane,
        runners_by_owner={"control-plane": runner, "qi": runner, "nuo": runner},
        daemon_id="activation-productization",
    ).tick_once(mission_ids=[mission.mission_id], now=now, max_work_items=2)
    activated = (
        bool(report.ran_work_item_ids)
        and control_plane.work_items[report.ran_work_item_ids[0]].status == "done"
    )
    return FeatureActivationCase(
        feature_id="productization_dogfood_runner",
        subsystem="control_plane_productization",
        trigger_condition="Canonical KUN V6 productization dogfood mission is submitted.",
        dependencies=["ProductizationDogfoodRunner", "canonical productization work items"],
        activated=activated,
        evidence_refs=list(report.ran_work_item_ids),
        generated_work_item_ids=list(report.ran_work_item_ids),
        trigger_status=control_plane.missions[mission.mission_id].status,
        notes=[
            "This activates the canonical productization queue without running unrelated game work."
        ],
    )


_CASE_FUNCTIONS: list[Callable[[Path, datetime], FeatureActivationCase]] = [
    _case_info_gap_collaboration,
    _case_acceptance_collaboration_cleanup,
    _case_mission_director_delivery_supervision,
    _case_runtime_activation_preflight_snapshot,
    _case_worker_pool_resource_lock,
    _case_parallel_worker_pool_isolated_execution,
    _case_sqlite_multi_process_worker_pool_plan,
    _case_redis_distributed_resource_lock_adapter,
    _case_container_required_gate,
    _case_qi_nuo_observation_strategy_loop,
    _case_runtime_observation_hardening,
    _case_capability_dedupe_qi_governance,
    _case_task_vs_self_improvement_boundary,
    _case_governed_self_improvement_loop,
    _case_merge_conflict_governance,
    _case_workspace_rollback,
    _case_watchtower_bridge,
    _case_external_sample_comparison,
    _case_autonomous_app_development_runner,
    _case_game_design_research_runner,
    _case_game_production_runner,
    _case_qi_ab_external_runner,
    _case_productization_dogfood_runner,
]


def _runtime(
    store_path: Path,
    *,
    mission_id: str,
    workspace: Path,
) -> tuple[InMemoryControlPlane, FileControlPlaneStore, Mission]:
    workspace.mkdir(parents=True, exist_ok=True)
    store = FileControlPlaneStore(store_path)
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id=mission_id,
        owner="kun",
        objective=f"Feature activation mission {mission_id}",
        task_type="self_improvement",
        status="contracted",
    )
    plan = TaskPlan(
        plan_id=f"plan-{mission_id}",
        mission_id=mission_id,
        version="v1",
        objective=mission.objective,
        acceptance_criteria=["feature activates with auditable evidence"],
        constraints=["do not use a passive code check as evidence"],
        evidence_plan=["runtime artifacts", "gate evaluations", "follow-up work item ids"],
        test_plan=["daemon tick or loop must run the trigger"],
        approval_status="approved",
    )
    contract = ExecutionContract(
        contract_id=f"contract-{mission_id}",
        mission_id=mission_id,
        task_plan_version="v1",
        allowed_actions=["run feature activation task"],
        forbidden_actions=["mutate competitor agents"],
        delivery_contract={"workspace_path": str(workspace)},
    )
    context = WorkingContext(
        working_context_id=f"ctx-{mission_id}",
        mission_id=mission_id,
        task_plan_version="v1",
        audience="kun",
        scope="feature-activation",
        summary=f"Activation context for {mission_id}.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work_item = WorkItem(
        work_item_id=f"work-{mission_id}",
        mission_id=mission_id,
        task_plan_version="v1",
        type="execution",
        owner="kun",
        priority=80,
        expected_output="Run code/test/app activation task and record evidence.",
    )
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=[work_item],
    )
    return control_plane, store, mission


def _default_owner_runners(control_plane: InMemoryControlPlane) -> dict[str, object]:
    return {
        "kun": KunRuntimeTaskRunner(control_plane=control_plane, executor=_executor),
        "qi": ChainedControlPlaneRunner(
            runner_identity="feature-activation-qi-router",
            runners=[
                QiSelfImprovementStrategyRunner(control_plane=control_plane),
                QiRuntimeGovernanceRunner(control_plane=control_plane),
            ],
        ),
        "nuo": ChainedControlPlaneRunner(
            runner_identity="feature-activation-nuo-router",
            runners=[
                NuoSelfImprovementAuditRunner(control_plane=control_plane),
                NuoRuntimeRepairRunner(control_plane=control_plane),
            ],
        ),
    }


def _domain_runtime(
    store_path: Path,
    *,
    mission_id: str,
    owner: str,
    workspace: Path,
    work_ids: list[str],
    final_workspace: Path | None = None,
    task_plan_version: str = "v1",
) -> tuple[InMemoryControlPlane, FileControlPlaneStore, Mission]:
    workspace.mkdir(parents=True, exist_ok=True)
    if final_workspace is not None:
        final_workspace.mkdir(parents=True, exist_ok=True)
    store = FileControlPlaneStore(store_path)
    control_plane = InMemoryControlPlane(store=store)
    mission = Mission(
        mission_id=mission_id,
        owner="kun",
        objective=f"Domain runner activation mission {mission_id}",
        task_type="product_development",
        status="contracted",
        risk_level="high",
    )
    plan = TaskPlan(
        plan_id=f"plan-{mission_id}",
        mission_id=mission_id,
        version=task_plan_version,
        objective=mission.objective,
        acceptance_criteria=["domain runner activates and writes delivery evidence"],
        constraints=["fixture only; do not resume paused game task"],
        approval_status="approved_with_limits",
    )
    contract = ExecutionContract(
        contract_id=f"contract-{mission_id}",
        mission_id=mission_id,
        task_plan_version=task_plan_version,
        allowed_actions=["write_fixture_project", "run_fake_build_commands"],
        forbidden_actions=["resume_paused_user_game_task"],
        delivery_contract={
            "project_path": str(workspace),
            **(
                {"production_mode": "gameful_playtest"}
                if owner == KUN_GAME_PRODUCTION_RUNNER_OWNER
                else {}
            ),
            **({"final_project_path": str(final_workspace)} if final_workspace else {}),
        },
    )
    context = WorkingContext(
        working_context_id=f"ctx-{mission_id}",
        mission_id=mission_id,
        task_plan_version=task_plan_version,
        audience=owner,
        scope="feature-activation-domain-runner",
        summary=f"Domain runner activation context for {owner}.",
        acceptance_criteria=plan.acceptance_criteria,
        constraints=plan.constraints,
    )
    work_items: list[WorkItem] = []
    previous: str | None = None
    for index, work_id in enumerate(work_ids):
        if "runner-activation" in work_id:
            item_type = "governance"
        elif "research-source" in work_id or "design-synthesis" in work_id:
            item_type = "research"
        elif "design-gate" in work_id:
            item_type = "review"
        elif "qa" in work_id or "test" in work_id:
            item_type = "test"
        elif "delivery" in work_id:
            item_type = "merge"
        else:
            item_type = "execution"
        work_items.append(
            WorkItem(
                work_item_id=work_id,
                mission_id=mission_id,
                task_plan_version=task_plan_version,
                type=item_type,
                owner=owner,
                dependencies=[previous] if previous else [],
                expected_output=f"Activate domain runner step {index}: {work_id}.",
            )
        )
        previous = work_id
    control_plane.submit_mission(
        mission=mission,
        task_plan=plan,
        execution_contract=contract,
        working_context=context,
        work_items=work_items,
    )
    return control_plane, store, mission


def _executor(prompt: str) -> KunTaskExecutionOutput:
    return KunTaskExecutionOutput(
        status="done",
        answer="Feature activation KUN runtime executor produced auditable output.",
        duration_sec=0.01,
        raw={"prompt_excerpt": prompt[:160]},
    )


def _pass_gate(work_item: WorkItem, artifact_ref: str) -> GateEvaluation:
    return GateEvaluation(
        gate_evaluation_id=f"gate-{work_item.work_item_id}",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="workitem",
        task_type="self_improvement",
        rubric_version="feature-activation-v1",
        metric_pack_version="feature-activation-v1",
        north_star_verdict="pass",
        result_quality=0.86,
        speed=0.8,
        cost=0.86,
        risk=0.18,
        evidence_quality=0.82,
        collaboration_quality=0.76,
        thresholds={"result_quality": 0.8},
        evidence_refs=[artifact_ref],
        artifact_refs=[artifact_ref],
        source_freshness="fresh",
        responsibility_scope="kun_auto",
        confidence=0.82,
        next_action="continue",
        next_state="running",
        governance_signal="feature_activation_static_pass",
        created_by=StaticRunner.runner_identity,
    )


def _failed_gate(mission: Mission, work_item: WorkItem) -> GateEvaluation:
    return GateEvaluation(
        gate_evaluation_id=f"gate-failed-{work_item.work_item_id}",
        mission_id=mission.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="workitem",
        task_type=mission.task_type,
        rubric_version="feature-activation-failure-v1",
        metric_pack_version="feature-activation-failure-v1",
        north_star_verdict="fail",
        result_quality=0.3,
        speed=0.7,
        cost=0.8,
        risk=0.68,
        evidence_quality=0.3,
        collaboration_quality=0.4,
        thresholds={"result_quality": 0.8},
        hard_gate_failures=["feature_activation_forced_gap"],
        evidence_refs=[work_item.work_item_id],
        failure_category="delivery_failure",
        responsibility_scope="mixed",
        confidence=0.78,
        next_action="needs_repair",
        next_state="repairing",
        governance_signal="feature_activation_forced_gap",
        created_by="feature-activation-audit",
    )


def _markdown_report(report: FeatureActivationAuditReport) -> str:
    lines = [
        "# KUN V6 Feature Activation Audit",
        "",
        f"- Suite: `{report.suite_id}`",
        f"- Generated: `{report.generated_at.isoformat()}`",
        f"- Activated: `{report.activated_count}/{len(report.cases)}`",
        f"- Gaps: `{report.gap_count}`",
        f"- Real mission evidence: `{report.real_mission_count}`",
        f"- Fixture or synthetic evidence: `{report.fixture_or_synthetic_count}`",
        "",
        "| Feature | Subsystem | Scope | Trigger | Runner | Real E2E | Activated | Evidence | Gaps |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in report.cases:
        lines.append(
            "| "
            + " | ".join(
                [
                    case.feature_id,
                    case.subsystem,
                    case.evidence_scope,
                    "yes" if case.trigger_available else "no",
                    "yes" if case.runner_available else "no",
                    "yes" if case.real_mission_e2e_passed else "no",
                    "yes" if case.activated else "no",
                    ", ".join(case.evidence_refs[:5]).replace("|", "/") or "-",
                    "; ".join(case.gaps).replace("|", "/") or "-",
                ]
            )
            + " |"
        )
    lines.append("")
    lines.append("## Trigger Map")
    for case in report.cases:
        lines.extend(
            [
                "",
                f"### {case.feature_id}",
                f"- Trigger: {case.trigger_condition}",
                f"- Dependencies: {', '.join(case.dependencies) or '-'}",
                f"- Generated work: {', '.join(case.generated_work_item_ids) or '-'}",
                f"- Status: {case.trigger_status}",
                f"- Notes: {'; '.join(case.notes) or '-'}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "FeatureActivationAuditReport",
    "FeatureActivationCase",
    "run_feature_activation_audit",
]
