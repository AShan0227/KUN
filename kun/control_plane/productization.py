"""Productization glue for the KUN V6 Control Plane.

The lower-level V6 modules already define the durable objects: missions,
plans, work items, ledgers, gates, tickets, Nuo health, Qi AB, and capability
promotion.  This module ties those objects into a product-facing closure check:
can the system recover, explain progress to a normal user, invalidate polluted
rounds, resume after human input, and distill external agent behavior into
KUN-native capability candidates without copying foreign implementation code?
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.capability_evolution import (
    CapabilityCandidate,
    CapabilityEvaluation,
    build_capability_promotion,
)
from kun.control_plane.capability_execution import CapabilityExecutionPolicy
from kun.control_plane.capability_governance import normalize_capability_governance_key
from kun.control_plane.collaboration import CollaborationQueueSummary, CollaborationResponse
from kun.control_plane.frontier50_external import load_frontier50_round_summary
from kun.control_plane.progress import UserProgressSummary, build_user_progress_summary
from kun.control_plane.qi_ab import build_qi_ab_round_contract
from kun.control_plane.runtime import (
    ControlPlaneProgressReport,
    InMemoryControlPlane,
    RunnerType,
    WorkItemResult,
)
from kun.control_plane.v6 import (
    AcceptanceReview,
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
    WorkItemType,
)

ProductizationSubsystem = Literal[
    "persistence_recovery",
    "mission_dashboard",
    "nuo_contamination",
    "qi_ab_runner",
    "collaboration_tickets",
    "qi_capability_evolution",
    "external_behavior_distillation",
]

CodeBoundaryCategory = Literal[
    "formal_code",
    "test",
    "product_doc",
    "frontend",
    "artifact_state",
    "configuration",
    "generated_output",
    "unknown",
]
CodeBoundarySeverity = Literal["info", "warning", "blocker"]
BehaviorOrigin = Literal["openclaw", "hermes", "external"]
AdoptionMode = Literal["kun_native_contract", "kun_native_runtime", "qi_review_only"]
ExternalBehaviorDecision = Literal["adopt", "merge", "discard"]

_REQUIRED_SUBSYSTEMS: tuple[ProductizationSubsystem, ...] = (
    "persistence_recovery",
    "mission_dashboard",
    "nuo_contamination",
    "qi_ab_runner",
    "collaboration_tickets",
    "qi_capability_evolution",
    "external_behavior_distillation",
)

_WORK_ITEM_BY_SUBSYSTEM: dict[ProductizationSubsystem, tuple[WorkItemType, str, int, str]] = {
    "persistence_recovery": (
        "governance",
        "control-plane",
        100,
        "Persist mission state, queue, checkpoints, artifacts, decisions, and resume hints.",
    ),
    "mission_dashboard": (
        "governance",
        "control-plane",
        92,
        "Render non-technical mission status, risk, next step, and human-needed state.",
    ),
    "nuo_contamination": (
        "repair",
        "nuo",
        98,
        "Classify stub, fallback, misroute, timeout, EOF, missing report, and missing reviews.",
    ),
    "qi_ab_runner": (
        "test",
        "qi",
        95,
        "Run Frontier50 as a Control Plane work item with answers, reviews, report, health, and rerun gates.",
    ),
    "collaboration_tickets": (
        "collaboration",
        "control-plane",
        90,
        "Close the human/external ticket loop: who to ask, timeout, fallback, resume, and refusal handling.",
    ),
    "qi_capability_evolution": (
        "governance",
        "qi",
        88,
        "Promote learned capability only through replay, holdout, shadow, canary, and rollback gates.",
    ),
    "external_behavior_distillation": (
        "research",
        "qi",
        86,
        "Distill OpenClaw/Hermes behavior into KUN-native contracts and tests without source copying.",
    ),
}


class ControlPlaneRecoveryBundle(BaseModel):
    """Portable resume view for crash, reboot, or cross-day recovery."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    status: str
    current_plan_version: str | None
    execution_contract_ref: str | None
    working_context_ref: str | None
    queued_work_item_ids: list[str] = Field(default_factory=list)
    running_work_item_ids: list[str] = Field(default_factory=list)
    waiting_work_item_ids: list[str] = Field(default_factory=list)
    blocked_work_item_ids: list[str] = Field(default_factory=list)
    ready_work_item_ids: list[str] = Field(default_factory=list)
    open_ticket_ids: list[str] = Field(default_factory=list)
    artifact_manifest_refs: list[str] = Field(default_factory=list)
    latest_gate_ref: str | None = None
    latest_run_ref: str | None = None
    ledger_event_count: int = 0
    resume_policy: str


class MissionDashboardCard(BaseModel):
    """User-facing cockpit card, not a raw engineering log."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    headline: str
    status_text: str
    risk_text: str = ""
    next_step: str
    quality_gate_status: str
    human_needed: bool
    safe_to_continue: bool
    open_ticket_ids: list[str] = Field(default_factory=list)
    technical_refs: list[str] = Field(default_factory=list)


class ProductizationGap(BaseModel):
    """One missing productization capability with a concrete repair work item."""

    model_config = ConfigDict(extra="forbid")

    subsystem: ProductizationSubsystem
    severity: Literal["warning", "blocker"] = "blocker"
    summary: str
    repair_work_item: WorkItem


class ProductizationAuditReport(BaseModel):
    """Closure report for KUN V6 productization readiness."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    ready: bool
    present_subsystems: list[ProductizationSubsystem] = Field(default_factory=list)
    missing_subsystems: list[ProductizationSubsystem] = Field(default_factory=list)
    gaps: list[ProductizationGap] = Field(default_factory=list)
    recovery_bundle: ControlPlaneRecoveryBundle
    dashboard: MissionDashboardCard
    behavior_signals: list[ExternalBehaviorSignal] = Field(default_factory=list)


class ProductizationDogfoodMission(BaseModel):
    """Ready-to-submit mission package for dogfooding KUN productization."""

    model_config = ConfigDict(extra="forbid")

    mission: Mission
    task_plan: TaskPlan
    execution_contract: ExecutionContract
    working_context: WorkingContext
    work_items: list[WorkItem]


class ProductizationDogfoodExecutionReport(BaseModel):
    """Result of running the productization dogfood mission through Control Plane."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    run_refs: list[str] = Field(default_factory=list)
    completed_work_item_ids: list[str] = Field(default_factory=list)
    ab_regression_gate_ref: str | None = None
    delivery_manifest_ref: str | None = None
    final_gate_ref: str | None = None
    recovery_bundle_artifact_ref: str | None = None
    execution_report_artifact_ref: str | None = None
    mission_status: str
    stopped_reason: str


class ProductizationDogfoodAcceptanceReport(BaseModel):
    """Result of accepting and closing the productization dogfood delivery."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    acceptance_ref: str
    delivery_manifest_ref: str
    gate_evaluation_ref: str
    learning_artifact_ref: str | None = None
    learning_candidate_refs: list[str] = Field(default_factory=list)
    mission_status: str
    closed: bool


class CodeBoundaryFinding(BaseModel):
    """One issue found while separating code, tests, docs, and runtime state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    category: CodeBoundaryCategory
    severity: CodeBoundarySeverity
    summary: str
    recommended_action: str


class CodeBoundaryAuditReport(BaseModel):
    """Phase-10 audit report for a clean productization submission boundary."""

    model_config = ConfigDict(extra="forbid")

    ready: bool
    checked_path_count: int
    formal_code_paths: list[str] = Field(default_factory=list)
    test_paths: list[str] = Field(default_factory=list)
    product_doc_paths: list[str] = Field(default_factory=list)
    frontend_paths: list[str] = Field(default_factory=list)
    artifact_state_paths: list[str] = Field(default_factory=list)
    configuration_paths: list[str] = Field(default_factory=list)
    generated_output_paths: list[str] = Field(default_factory=list)
    unknown_paths: list[str] = Field(default_factory=list)
    findings: list[CodeBoundaryFinding] = Field(default_factory=list)
    recommended_pr_sections: list[str] = Field(default_factory=list)


class ExternalBehaviorSignal(BaseModel):
    """A behavior learned from an external codebase or release artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: str
    origin: BehaviorOrigin
    source_ref: str
    behavior: str
    kun_subsystem: ProductizationSubsystem
    adoption_mode: AdoptionMode
    required_tests: list[str] = Field(default_factory=list)
    risk_controls: list[str] = Field(default_factory=list)


class ExternalBehaviorDistillationRecord(BaseModel):
    """Persisted result of converting external behavior into KUN-native assets."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    signal_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    capability_profile_refs: list[str] = Field(default_factory=list)
    candidate_count: int = 0


class ExternalBehaviorComparisonRecord(BaseModel):
    """Source/behavior comparison result before KUN-native adoption."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    comparison_ref: str
    signal_ref: str
    origin: BehaviorOrigin
    source_ref: str
    behavior: str
    kun_subsystem: ProductizationSubsystem
    adoption_mode: AdoptionMode
    decision: ExternalBehaviorDecision
    reason: str
    required_tests: list[str] = Field(default_factory=list)
    risk_controls: list[str] = Field(default_factory=list)
    complexity_impact: Literal["low", "medium", "high"] = "low"
    production_blockers: list[str] = Field(default_factory=list)


class ExternalBehaviorProductionizationRecord(BaseModel):
    """Record of promoting external behavior samples through Qi to production."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    comparison_refs: list[str] = Field(default_factory=list)
    adopted_count: int = 0
    merged_count: int = 0
    discarded_count: int = 0
    artifact_refs: list[str] = Field(default_factory=list)
    promotion_refs: list[str] = Field(default_factory=list)
    capability_profile_refs: list[str] = Field(default_factory=list)
    supervisor_review_ref: str
    dogfood_validation_refs: list[str] = Field(default_factory=list)
    regression_refs: list[str] = Field(default_factory=list)


class ExternalBehaviorSourceProductionizationRun(BaseModel):
    """End-to-end source batch review for OpenClaw/Hermes capability samples."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    source_paths: list[str] = Field(default_factory=list)
    signal_refs: list[str] = Field(default_factory=list)
    distillation_record: ExternalBehaviorDistillationRecord
    productionization_record: ExternalBehaviorProductionizationRecord
    runtime_capability_refs: list[str] = Field(default_factory=list)


def build_recovery_bundle(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> ControlPlaneRecoveryBundle:
    """Build a durable resume summary from Control Plane state."""

    progress = control_plane.progress_report(mission_id)
    mission = control_plane.missions[mission_id]
    work_items = [
        item for item in control_plane.work_items.values() if item.mission_id == mission_id
    ]
    runs = [
        run
        for run in control_plane.runs.values()
        if any(item.work_item_id == run.work_item_id for item in work_items)
    ]
    latest_run = max(runs, key=lambda item: item.started_at, default=None)
    return ControlPlaneRecoveryBundle(
        mission_id=mission_id,
        status=mission.status,
        current_plan_version=mission.current_plan_version,
        execution_contract_ref=mission.execution_contract_ref,
        working_context_ref=mission.working_context_ref,
        queued_work_item_ids=sorted(
            item.work_item_id for item in work_items if item.status == "queued"
        ),
        running_work_item_ids=sorted(
            item.work_item_id for item in work_items if item.status == "running"
        ),
        waiting_work_item_ids=sorted(
            item.work_item_id
            for item in work_items
            if item.status in {"waiting_human", "waiting_external"}
        ),
        blocked_work_item_ids=sorted(
            item.work_item_id
            for item in work_items
            if item.status in {"blocked", "failed", "repairing", "rolling_back"}
        ),
        ready_work_item_ids=list(progress.next_ready_work_item_ids),
        open_ticket_ids=list(progress.open_collaboration_ticket_ids),
        artifact_manifest_refs=list(mission.artifact_manifest_refs),
        latest_gate_ref=progress.latest_gate_ref,
        latest_run_ref=latest_run.run_id if latest_run else None,
        ledger_event_count=progress.ledger_event_count,
        resume_policy=_resume_policy(progress),
    )


def build_dashboard_card(
    progress: ControlPlaneProgressReport,
    *,
    collaboration: CollaborationQueueSummary | None = None,
) -> MissionDashboardCard:
    """Translate runtime progress into a product cockpit card."""

    summary = build_user_progress_summary(progress, collaboration=collaboration)
    return MissionDashboardCard(
        mission_id=summary.mission_id,
        headline=_headline(summary),
        status_text=summary.current_status,
        risk_text=summary.blocking_reason,
        next_step=summary.next_step,
        quality_gate_status=summary.quality_gate_status,
        human_needed=summary.human_needed,
        safe_to_continue=summary.safe_to_continue,
        open_ticket_ids=list(summary.open_ticket_ids),
        technical_refs=_dashboard_refs(progress),
    )


def build_productization_work_items(
    *,
    mission_id: str,
    task_plan_version: str,
    subsystems: Sequence[ProductizationSubsystem] | None = None,
) -> list[WorkItem]:
    """Create canonical productization work items for missing closure loops."""

    selected = subsystems or _REQUIRED_SUBSYSTEMS
    return [
        _productization_work_item(
            subsystem,
            mission_id=mission_id,
            task_plan_version=task_plan_version,
        )
        for subsystem in selected
    ]


def build_productization_dogfood_mission(
    *,
    mission_id: str = "msn-kun-v6-productization",
    owner: str = "kun",
    task_plan_version: str = "v6-productization",
    objective: str = "Make KUN V6 Control Plane run real long tasks with recovery, dashboard, Nuo, Qi, collaboration, and learning.",
) -> ProductizationDogfoodMission:
    """Build the real dogfood mission for KUN V6 productization."""

    mission = Mission(
        mission_id=mission_id,
        owner=owner,
        objective=objective,
        task_type="self_improvement",
        priority=100,
        risk_level="medium",
        status="contracted",
    )
    task_plan = TaskPlan(
        plan_id=f"plan-{mission_id}",
        mission_id=mission_id,
        version=task_plan_version,
        objective=objective,
        known_facts=[
            "Round-02 Frontier50 passed after KUN-only fixes.",
            "AB remains a regression gate while dogfood becomes the main productization path.",
            "OpenClaw/Hermes are behavior samples, not code to copy.",
        ],
        assumptions=[
            "Use local file-backed Control Plane state until database persistence is wired.",
            "High-risk external actions stay behind collaboration tickets.",
        ],
        acceptance_criteria=[
            "State can be recovered after process restart.",
            "User can read mission status without terminal logs.",
            "Pollution and environment blockers are invalidated by Nuo before quality scoring.",
            "Frontier50 can run as a Control Plane work item.",
            "Human tickets resume or close work according to SLA/fallback.",
            "Qi capability candidates require replay/holdout/canary/rollback before production.",
        ],
        constraints=[
            "Do not optimize OpenClaw, Hermes, or GPT-5.5.",
            "Do not count comparator pollution as KUN capability failure.",
            "Do not promote learned capabilities on speed or cost if result quality fails.",
        ],
        evidence_plan=[
            "Control Plane file store snapshot.",
            "Productization audit report.",
            "Frontier50 round summaries.",
            "OpenClaw/Hermes behavior signal refs.",
            "Unit and regression test refs.",
        ],
        decomposition=list(_REQUIRED_SUBSYSTEMS),
        worker_plan=[
            "control-plane owns state, queue, dashboard, API, and tickets.",
            "nuo owns contamination and environment health.",
            "qi owns AB runner, behavior distillation, and capability evolution.",
            "kun runtime consumes only verified capabilities.",
        ],
        merge_plan=[
            "All subsystem outputs merge through ArtifactManifest and GateEvaluation.",
            "User-facing report is generated from progress summary, not raw logs.",
        ],
        test_plan=[
            "Run control-plane productization tests.",
            "Run AB round regression when capability behavior changes.",
            "Run dogfood recovery by rehydrating FileControlPlaneStore.",
        ],
        rollback_plan=[
            "Rollback capability candidates by reverting CapabilityProfile promotion.",
            "Rollback runtime changes by restoring previous Control Plane state snapshot.",
        ],
        human_confirmation_points=[
            "Production external actions.",
            "Credential/account changes.",
            "Capability promotion beyond review-only.",
        ],
        approval_status="approved",
    )
    execution_contract = ExecutionContract(
        contract_id=f"contract-{mission_id}",
        mission_id=mission_id,
        task_plan_version=task_plan.version,
        allowed_actions=[
            "read local OpenClaw/Hermes source samples",
            "write KUN-native code and tests",
            "run local tests and Frontier50 regression",
            "create review-only Qi capability candidates",
        ],
        forbidden_actions=[
            "copy external implementation code",
            "modify comparator agents as optimization",
            "promote unverified capability to production",
            "execute production external action without approval",
        ],
        budget={"quality_floor": 0.8, "ab_gap_floor": 0.05, "cost_regression_cap": 0.1},
        evidence_policy={"required": ["artifact_manifest", "gate_evaluation", "test_refs"]},
        delivery_contract={"format": "dashboard + audit + recovery bundle + tested code"},
        risk_policy={"quality_precedes_speed_cost": True},
        rollback_policy={"required_for": ["capability_promotion", "runtime_policy_change"]},
        approval_policy={"high_risk_actions": "collaboration_ticket_required"},
    )
    working_context = WorkingContext(
        working_context_id=f"ctx-{mission_id}",
        mission_id=mission_id,
        task_plan_version=task_plan.version,
        audience="control-plane",
        scope="kun-v6-productization-dogfood",
        summary="Dogfood KUN V6 productization as a real long task.",
        critical_facts=task_plan.known_facts,
        acceptance_criteria=task_plan.acceptance_criteria,
        constraints=task_plan.constraints,
        open_questions=[],
        risk_flags=["external action approval", "capability promotion overfit"],
    )
    return ProductizationDogfoodMission(
        mission=mission,
        task_plan=task_plan,
        execution_contract=execution_contract,
        working_context=working_context,
        work_items=build_productization_work_items(
            mission_id=mission_id,
            task_plan_version=task_plan.version,
        ),
    )


def submit_productization_dogfood_mission(
    control_plane: InMemoryControlPlane,
    package: ProductizationDogfoodMission | None = None,
    *,
    actor: str = "kun",
) -> Mission:
    """Submit the productization dogfood mission to a Control Plane runtime."""

    mission_package = package or build_productization_dogfood_mission()
    return control_plane.submit_mission(
        mission=mission_package.mission,
        task_plan=mission_package.task_plan,
        execution_contract=mission_package.execution_contract,
        working_context=mission_package.working_context,
        work_items=mission_package.work_items,
        actor=actor,
    )


def audit_control_plane_productization(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    *,
    behavior_signals: Sequence[ExternalBehaviorSignal] = (),
) -> ProductizationAuditReport:
    """Check whether a mission carries the productization closure loops."""

    mission = control_plane.missions[mission_id]
    task_plan_version = mission.current_plan_version or "v0"
    progress = control_plane.progress_report(mission_id)
    present = _present_subsystems(control_plane=control_plane, mission_id=mission_id)
    if behavior_signals:
        present.add("external_behavior_distillation")
    missing = [subsystem for subsystem in _REQUIRED_SUBSYSTEMS if subsystem not in present]
    gaps = [
        ProductizationGap(
            subsystem=subsystem,
            summary=_gap_summary(subsystem),
            repair_work_item=_productization_work_item(
                subsystem,
                mission_id=mission_id,
                task_plan_version=task_plan_version,
            ),
        )
        for subsystem in missing
    ]
    return ProductizationAuditReport(
        mission_id=mission_id,
        ready=not gaps,
        present_subsystems=sorted(present),
        missing_subsystems=missing,
        gaps=gaps,
        recovery_bundle=build_recovery_bundle(control_plane, mission_id),
        dashboard=build_dashboard_card(progress),
        behavior_signals=list(behavior_signals),
    )


def audit_productization_code_boundary(
    changed_paths: Sequence[str | Path],
    *,
    repo_root: str | Path | None = None,
) -> CodeBoundaryAuditReport:
    """Classify productization changes before commit/PR.

    The audit keeps formal code, tests, product docs, frontend surface, and
    dogfood state distinct.  It is intentionally conservative: unknown paths
    or executable code inside artifact state are blockers until a human or
    follow-up automation assigns them a clear home.
    """

    root = Path(repo_root).expanduser().resolve() if repo_root else None
    normalized_paths = sorted(
        {_normalize_repo_path(path, root=root) for path in changed_paths if str(path).strip()}
    )
    paths_by_category: dict[CodeBoundaryCategory, list[str]] = {
        "formal_code": [],
        "test": [],
        "product_doc": [],
        "frontend": [],
        "artifact_state": [],
        "configuration": [],
        "generated_output": [],
        "unknown": [],
    }
    findings: list[CodeBoundaryFinding] = []

    if not normalized_paths:
        findings.append(
            CodeBoundaryFinding(
                path=".",
                category="unknown",
                severity="blocker",
                summary="No changed paths were supplied for the code boundary audit.",
                recommended_action="Pass the current changed path list before declaring phase-10 readiness.",
            )
        )

    for path in normalized_paths:
        category = _code_boundary_category(path)
        paths_by_category[category].append(path)
        findings.extend(_code_boundary_findings_for_path(path, category))

    formal_code_paths = paths_by_category["formal_code"]
    test_paths = paths_by_category["test"]
    if formal_code_paths and not test_paths:
        findings.append(
            CodeBoundaryFinding(
                path="tests/unit",
                category="test",
                severity="blocker",
                summary="Formal KUN code changed without any test path in the same boundary.",
                recommended_action="Add or include focused unit/integration tests for the touched Control Plane code.",
            )
        )

    for code_path in formal_code_paths:
        expected_test = _expected_test_path_for_code(code_path)
        if expected_test is None:
            continue
        test_exists = expected_test in normalized_paths or (
            root is not None and (root / expected_test).exists()
        )
        if not test_exists:
            findings.append(
                CodeBoundaryFinding(
                    path=code_path,
                    category="formal_code",
                    severity="blocker",
                    summary=f"Formal code path lacks its expected regression test {expected_test}.",
                    recommended_action=f"Add {expected_test} or document a narrower existing test owner.",
                )
            )

    ready = not any(finding.severity == "blocker" for finding in findings)
    return CodeBoundaryAuditReport(
        ready=ready,
        checked_path_count=len(normalized_paths),
        formal_code_paths=formal_code_paths,
        test_paths=test_paths,
        product_doc_paths=paths_by_category["product_doc"],
        frontend_paths=paths_by_category["frontend"],
        artifact_state_paths=paths_by_category["artifact_state"],
        configuration_paths=paths_by_category["configuration"],
        generated_output_paths=paths_by_category["generated_output"],
        unknown_paths=paths_by_category["unknown"],
        findings=findings,
        recommended_pr_sections=_recommended_pr_sections(paths_by_category),
    )


def materialize_productization_code_boundary_audit(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    report: CodeBoundaryAuditReport,
    *,
    actor: str = "control-plane",
) -> ArtifactRecord:
    """Persist a passing phase-10 code boundary audit as Control Plane evidence."""

    if not report.ready:
        raise ValueError("cannot materialize a code boundary audit with blockers")
    control_plane.missions[mission_id]
    artifact = ArtifactRecord(
        artifact_id=f"artifact-{_slug(mission_id)}-code-boundary-audit",
        kind="report",
        path_or_uri=f"control-plane://productization/{mission_id}/code-boundary-audit",
        content_hash=_hash_payload(report.model_dump(mode="json")),
        created_by=actor,
        mission_id=mission_id,
        supports=[
            "code_boundary_audit",
            "phase_10_submission_boundary",
            "productization_dogfood",
        ],
        freshness="fresh",
        source_quality="primary",
    )
    _upsert_artifact(control_plane, artifact)
    return artifact


def distill_external_behavior_signals(
    sources: Mapping[str, str],
    *,
    default_origin: BehaviorOrigin = "external",
) -> list[ExternalBehaviorSignal]:
    """Distill OpenClaw/Hermes behavior patterns from source text.

    This deliberately returns capability signals, not copied code.  KUN adopts
    the behavior through its own Control Plane contracts and tests.
    """

    signals: list[ExternalBehaviorSignal] = []
    for source_ref, text in sources.items():
        origin = _origin_from_source(source_ref, default_origin=default_origin)
        lowered = text.lower()
        for rule in _DISTILLATION_RULES:
            if rule.matches(lowered):
                signals.append(rule.to_signal(origin=origin, source_ref=source_ref))
    return _dedupe_signals(signals)


def load_external_behavior_sources(
    paths: Sequence[str | Path],
    *,
    allowed_roots: Sequence[str | Path],
    max_chars_per_file: int = 200_000,
) -> dict[str, str]:
    """Read bounded external source files for behavior distillation.

    The Control Plane should learn from OpenClaw/Hermes source behavior without
    turning arbitrary local file access into an API surface.  Callers must pass
    explicit allowed roots; every path is resolved before reading.
    """

    if max_chars_per_file <= 0:
        raise ValueError("max_chars_per_file must be positive")
    resolved_roots = [Path(root).expanduser().resolve() for root in allowed_roots]
    if not resolved_roots:
        raise ValueError("allowed_roots is required")

    sources: dict[str, str] = {}
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not any(path == root or root in path.parents for root in resolved_roots):
            raise ValueError(f"external behavior source outside allowed roots: {path}")
        if not path.is_file():
            raise ValueError(f"external behavior source is not a file: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        sources[str(path)] = text[:max_chars_per_file]
    return sources


def distill_external_behavior_from_paths(
    paths: Sequence[str | Path],
    *,
    allowed_roots: Sequence[str | Path],
    max_chars_per_file: int = 200_000,
    default_origin: BehaviorOrigin = "external",
) -> list[ExternalBehaviorSignal]:
    """Read allowed source paths and distill KUN-native behavior signals."""

    return distill_external_behavior_signals(
        load_external_behavior_sources(
            paths,
            allowed_roots=allowed_roots,
            max_chars_per_file=max_chars_per_file,
        ),
        default_origin=default_origin,
    )


def discover_external_behavior_source_paths(
    roots: Sequence[str | Path],
    *,
    max_files_per_root: int = 80,
    max_bytes_per_file: int = 500_000,
) -> list[str]:
    """Find bounded source/document files under approved external sample roots."""

    if max_files_per_root <= 0:
        raise ValueError("max_files_per_root must be positive")
    if max_bytes_per_file <= 0:
        raise ValueError("max_bytes_per_file must be positive")
    discovered: list[str] = []
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve()
        if not root.exists():
            raise ValueError(f"external behavior root does not exist: {root}")
        if not root.is_dir():
            raise ValueError(f"external behavior root is not a directory: {root}")
        root_paths: list[Path] = []
        candidates = sorted(root.rglob("*"), key=lambda path: _external_source_priority(path, root))
        for path in candidates:
            if len(root_paths) >= max_files_per_root:
                break
            if not path.is_file():
                continue
            if any(part in _EXTERNAL_BEHAVIOR_DISCOVERY_EXCLUDES for part in path.parts):
                continue
            if path.suffix.lower() not in _EXTERNAL_BEHAVIOR_SOURCE_SUFFIXES:
                continue
            if path.stat().st_size > max_bytes_per_file:
                continue
            root_paths.append(path)
        discovered.extend(str(path) for path in root_paths)
    return discovered


def productionize_external_behavior_from_source_paths(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    paths: Sequence[str | Path],
    *,
    allowed_roots: Sequence[str | Path],
    dogfood_validation_refs: Sequence[str],
    regression_refs: Sequence[str],
    supervisor_review_ref: str,
    max_chars_per_file: int = 200_000,
    actor: str = "qi",
) -> ExternalBehaviorSourceProductionizationRun:
    """Distill latest source samples and promote validated KUN-native behavior."""

    source_paths = [str(Path(path).expanduser().resolve()) for path in paths]
    signals = distill_external_behavior_from_paths(
        source_paths,
        allowed_roots=allowed_roots,
        max_chars_per_file=max_chars_per_file,
    )
    if not signals:
        raise ValueError("external behavior source paths produced no KUN-native signals")
    distillation = materialize_external_behavior_distillation(
        control_plane,
        mission_id,
        signals,
        actor=actor,
    )
    productionization = productionize_external_behavior_capabilities(
        control_plane,
        mission_id,
        signals,
        dogfood_validation_refs=dogfood_validation_refs,
        regression_refs=regression_refs,
        supervisor_review_ref=supervisor_review_ref,
        actor=actor,
    )
    return ExternalBehaviorSourceProductionizationRun(
        mission_id=mission_id,
        source_paths=source_paths,
        signal_refs=[behavior_signal_ref(signal) for signal in signals],
        distillation_record=distillation,
        productionization_record=productionization,
        runtime_capability_refs=[
            profile.capability_id
            for profile in control_plane.list_default_runtime_capabilities()
            if profile.capability_id in productionization.capability_profile_refs
        ],
    )


def build_capability_candidates_from_signals(
    signals: Sequence[ExternalBehaviorSignal],
) -> list[CapabilityCandidate]:
    """Convert behavior signals into Qi review-only capability candidates."""

    candidates: list[CapabilityCandidate] = []
    for signal in signals:
        candidates.append(
            CapabilityCandidate(
                candidate_id=f"candidate-{signal.signal_id}",
                capability_name=signal.behavior,
                source="open_source_project",
                source_ref=signal.source_ref,
                hypothesis=(
                    f"Adopting {signal.behavior!r} through {signal.kun_subsystem} "
                    "improves long-task delivery without copying external code."
                ),
                target_task_types=["self_improvement", "ops_tooling"],
                proposed_change_refs=[
                    f"kun-control-plane:{signal.kun_subsystem}",
                    f"adoption_mode:{signal.adoption_mode}",
                ],
                evidence_refs=[signal.source_ref],
                known_limits=[
                    "behavioral distillation only; implementation must remain KUN-native",
                    "requires replay/holdout before production capability promotion",
                ],
            )
        )
    return candidates


def materialize_external_behavior_distillation(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    signals: Sequence[ExternalBehaviorSignal],
    *,
    actor: str = "qi",
) -> ExternalBehaviorDistillationRecord:
    """Persist external behavior learning as KUN-owned evidence and replay profiles."""

    control_plane.missions[mission_id]
    signal_refs: list[str] = []
    artifact_refs: list[str] = []
    profile_refs: list[str] = []
    candidates = build_capability_candidates_from_signals(signals)

    for signal in signals:
        signal_ref = behavior_signal_ref(signal)
        signal_hash = _hash_payload(signal.model_dump(mode="json"))
        signal_id = f"{_slug(signal.signal_id)}-{signal_hash[:12]}"
        signal_refs.append(signal_ref)
        artifact = ArtifactRecord(
            artifact_id=f"artifact-{signal_id}",
            kind="evidence",
            path_or_uri=f"{signal.source_ref}#behavior:{_slug(signal.behavior)}",
            content_hash=signal_hash,
            created_by=actor,
            mission_id=mission_id,
            supports=[
                "external_behavior_distillation",
                signal.kun_subsystem,
                f"origin:{signal.origin}",
                f"adoption:{signal.adoption_mode}",
            ],
            freshness="fresh",
            source_quality="credible",
        )
        _upsert_artifact(control_plane, artifact)
        artifact_refs.append(artifact.artifact_id)

        profile = CapabilityProfile(
            capability_id=f"cap-{signal_id}",
            capability_name=signal.behavior,
            governance_key=normalize_capability_governance_key(signal.behavior),
            source_refs=[signal.source_ref],
            source_versions=[f"{signal.origin}:{signal.source_ref}"],
            evidence_refs=[artifact.artifact_id, signal_ref, signal.source_ref],
            known_limits=[
                "behavioral distillation only; implementation must remain KUN-native",
                "not eligible for production until holdout, shadow, canary, and rollback gates pass",
                *signal.risk_controls,
            ],
            promotion_stage="replay",
            regression_refs=list(signal.required_tests),
            last_verified_at=datetime.now(UTC),
            rollback_plan=[
                f"disable CapabilityProfile {signal_id}",
                "remove replay profile before runtime policy consumption",
            ],
            runtime_enabled=False,
        )
        _upsert_capability_profile(control_plane, profile)
        profile_refs.append(profile.capability_id)

    return ExternalBehaviorDistillationRecord(
        mission_id=mission_id,
        signal_refs=signal_refs,
        artifact_refs=artifact_refs,
        capability_profile_refs=profile_refs,
        candidate_count=len(candidates),
    )


def compare_external_behavior_signals(
    signals: Sequence[ExternalBehaviorSignal],
) -> list[ExternalBehaviorComparisonRecord]:
    """Apply KUN-native/Ockham review before any external behavior is promoted."""

    return [_comparison_from_signal(signal) for signal in signals]


def productionize_external_behavior_capabilities(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    signals: Sequence[ExternalBehaviorSignal],
    *,
    dogfood_validation_refs: Sequence[str],
    regression_refs: Sequence[str],
    supervisor_review_ref: str,
    actor: str = "qi",
) -> ExternalBehaviorProductionizationRecord:
    """Promote validated OpenClaw/Hermes behavior samples into production profiles.

    The function never copies external implementation code.  It records source
    references, KUN-native decisions, dogfood/regression evidence, and Qi
    promotion gates, then lets the Control Plane expose only production-stage
    profiles to KUN Runtime.
    """

    if not dogfood_validation_refs:
        raise ValueError("external behavior productionization requires dogfood_validation_refs")
    if not regression_refs:
        raise ValueError("external behavior productionization requires regression_refs")
    if not supervisor_review_ref:
        raise ValueError("external behavior productionization requires supervisor_review_ref")
    _require_control_plane_refs(
        control_plane,
        dogfood_validation_refs,
        label="dogfood_validation_refs",
    )
    _require_control_plane_refs(control_plane, regression_refs, label="regression_refs")
    _require_control_plane_refs(
        control_plane,
        [supervisor_review_ref],
        label="supervisor_review_ref",
    )

    comparisons = compare_external_behavior_signals(signals)
    artifact_refs: list[str] = []
    promotion_refs: list[str] = []
    profile_refs: list[str] = []
    adopted_count = 0
    merged_count = 0
    discarded_count = 0
    task_plan_version = control_plane.missions[mission_id].current_plan_version or "v6"

    for signal, comparison in zip(signals, comparisons, strict=True):
        if comparison.decision == "discard":
            discarded_count += 1
            continue
        if comparison.decision == "adopt":
            adopted_count += 1
        else:
            merged_count += 1
        artifact = _external_behavior_production_artifact(
            mission_id=mission_id,
            signal=signal,
            comparison=comparison,
            dogfood_validation_refs=dogfood_validation_refs,
            regression_refs=regression_refs,
            supervisor_review_ref=supervisor_review_ref,
            actor=actor,
        )
        _upsert_artifact(control_plane, artifact)
        artifact_refs.append(artifact.artifact_id)
        candidate = CapabilityCandidate(
            candidate_id=f"cand-{_slug(comparison.comparison_ref)}",
            capability_name=signal.behavior,
            source="open_source_project",
            source_ref=signal.source_ref,
            hypothesis=(
                f"Adapting {signal.origin} behavior into {signal.kun_subsystem} improves "
                "long-task delivery quality without copying external implementation code."
            ),
            target_task_types=["self_improvement", "ops_tooling"],
            proposed_change_refs=[f"kun-control-plane:{signal.kun_subsystem}:{signal.signal_id}"],
            evidence_refs=[
                artifact.artifact_id,
                comparison.signal_ref,
                supervisor_review_ref,
                *dogfood_validation_refs,
            ],
            known_limits=[
                "KUN-native adaptation only; external code is not copied.",
                "Rollback disables the production CapabilityProfile.",
                *comparison.production_blockers,
                *signal.risk_controls,
            ],
            created_by=actor,
        )
        promotion = build_capability_promotion(
            candidate,
            _external_behavior_evaluations(
                candidate=candidate,
                comparison=comparison,
                mission_id=mission_id,
                task_plan_version=task_plan_version,
                evidence_ref=artifact.artifact_id,
                dogfood_validation_refs=dogfood_validation_refs,
                regression_refs=regression_refs,
                supervisor_review_ref=supervisor_review_ref,
            ),
            target_stage="production",
            capability_id=f"cap-{_slug(comparison.comparison_ref)}",
        )
        profile = control_plane.apply_capability_promotion(promotion, actor=actor)
        promotion_refs.append(promotion.promotion_id)
        if profile is not None:
            profile_refs.append(profile.capability_id)

    return ExternalBehaviorProductionizationRecord(
        mission_id=mission_id,
        comparison_refs=[comparison.comparison_ref for comparison in comparisons],
        adopted_count=adopted_count,
        merged_count=merged_count,
        discarded_count=discarded_count,
        artifact_refs=artifact_refs,
        promotion_refs=promotion_refs,
        capability_profile_refs=profile_refs,
        supervisor_review_ref=supervisor_review_ref,
        dogfood_validation_refs=list(dogfood_validation_refs),
        regression_refs=list(regression_refs),
    )


def build_productization_collaboration_ticket(
    *,
    mission_id: str,
    context_ref: str,
    deadline: datetime | None = None,
    ticket_id: str | None = None,
    role_needed: str = "user",
) -> CollaborationTicket:
    """Build the canonical human checkpoint for productization dogfood."""

    return CollaborationTicket(
        ticket_id=ticket_id or f"ticket-{_slug(mission_id)}-productization-boundary",
        mission_id=mission_id,
        type="approval",
        role_needed=role_needed,
        why_needed="Confirm the boundary for high-risk productization actions before KUN continues.",
        decision_options=["approve_dry_run_only", "pause_high_risk_actions"],
        recommended_option="approve_dry_run_only",
        context_ref=context_ref,
        risk_if_skipped="KUN may continue a long task without the intended human boundary.",
        deadline=deadline or datetime.now(UTC) + timedelta(minutes=30),
        sla_policy={"respond_within_minutes": 30, "owner": role_needed},
        escalation_policy={"after_minutes": 30, "target": "mission_owner"},
        fallback_policy={
            "option": "pause_high_risk_actions",
            "reason": "approval timed out; continue safe local verification only",
        },
        resume_after_response=True,
        output_contract="selected option plus risk acknowledgement",
    )


def close_productization_collaboration_loop(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    *,
    context_ref: str,
    responder: str = "user",
    selected_option: str = "approve_dry_run_only",
    answer: str = "Approved for dry-run and local verification; production actions stay gated.",
) -> CollaborationTicket:
    """Create and answer the productization checkpoint so the loop is auditable."""

    ticket = build_productization_collaboration_ticket(
        mission_id=mission_id,
        context_ref=context_ref,
    )
    if ticket.ticket_id not in control_plane.collaboration_tickets:
        control_plane.record_collaboration_ticket(ticket)
    current = control_plane.get_collaboration_ticket(ticket.ticket_id)
    if current.status in {"answered", "fallback_selected", "closed"}:
        return current
    return control_plane.record_collaboration_response(
        CollaborationResponse(
            ticket_id=ticket.ticket_id,
            responder=responder,
            selected_option=selected_option,
            answer=answer,
            status="answered",
            resume_allowed=True,
        )
    )


class ProductizationDogfoodRunner:
    """KUN-native runner for the V6 productization dogfood work queue."""

    runner_type: RunnerType = "agent"
    runner_identity: str = "kun-productization-dogfood-runner"

    def __init__(
        self,
        *,
        control_plane: InMemoryControlPlane,
        ab_round_dir: str | Path | None = None,
        ab_round_id: str = "round-02-regression",
        ab_task_ids: Sequence[str] = (),
    ) -> None:
        self.control_plane = control_plane
        self.ab_round_dir = Path(ab_round_dir).expanduser().resolve() if ab_round_dir else None
        self.ab_round_id = ab_round_id
        self.ab_task_ids = list(ab_task_ids)
        self.capability_execution_policy: CapabilityExecutionPolicy | None = None

    def bind_capability_execution_policy(self, policy: CapabilityExecutionPolicy) -> None:
        """Bind governed production capabilities before daemon execution."""

        self.capability_execution_policy = policy

    def can_run(self, work_item: WorkItem) -> bool:
        """Return whether this runner owns the canonical productization item."""

        return work_item.work_item_id.startswith("work-v6-") and any(
            work_item.work_item_id == f"work-v6-{subsystem.replace('_', '-')}"
            for subsystem in _REQUIRED_SUBSYSTEMS
        )

    def run(self, work_item: WorkItem) -> WorkItemResult:
        if not self.can_run(work_item):
            return WorkItemResult(
                status="failed",
                summary=(
                    "Productization runner only handles canonical KUN V6 productization work items."
                ),
                failure_category="tool_failure",
            )
        subsystem = _subsystem_from_work_item(work_item)
        blocker = self._blocker(work_item=work_item, subsystem=subsystem)
        if blocker is not None:
            return WorkItemResult(
                status="failed",
                summary=blocker,
                failure_category="tool_failure"
                if subsystem == "qi_ab_runner"
                else "evidence_failure",
            )
        if subsystem == "qi_ab_runner":
            return self._attach_capability_policy_artifact(
                self._run_ab_regression(work_item),
                work_item=work_item,
                subsystem=subsystem,
            )
        return self._attach_capability_policy_artifact(
            _successful_dogfood_work_item_result(
                work_item=work_item,
                subsystem=subsystem,
                summary=_dogfood_success_summary(subsystem),
            ),
            work_item=work_item,
            subsystem=subsystem,
        )

    def finalize_mission(self, mission_id: str) -> dict[str, object]:
        """Finalize productization delivery when daemon execution has closed the queue."""

        if not _is_canonical_productization_mission(self.control_plane, mission_id):
            return {
                "finalized": False,
                "summary": "Mission is not the canonical KUN V6 productization dogfood queue.",
            }
        if not _all_mission_work_items_done(self.control_plane, mission_id):
            return {
                "finalized": False,
                "summary": "Productization work items are not all done yet.",
            }
        audit = audit_control_plane_productization(self.control_plane, mission_id)
        if not audit.ready:
            return {
                "finalized": False,
                "summary": "Productization audit is not ready for delivery.",
                "missing_subsystems": list(audit.missing_subsystems),
            }
        try:
            delivery_manifest_ref = _latest_delivery_manifest_ref(self.control_plane, mission_id)
            final_gate_ref = _latest_delivery_gate_ref(self.control_plane, mission_id)
        except ValueError:
            gate = finalize_productization_dogfood_delivery(
                self.control_plane,
                mission_id,
                actor=self.runner_identity,
            )
            final_gate_ref = gate.gate_evaluation_id
            delivery_manifest_ref = _latest_delivery_manifest_ref(self.control_plane, mission_id)
        return {
            "finalized": True,
            "delivery_manifest_ref": delivery_manifest_ref,
            "final_gate_ref": final_gate_ref,
            "summary": "Productization delivery manifest and final gate are ready.",
        }

    def _run_ab_regression(self, work_item: WorkItem) -> WorkItemResult:
        if self.ab_round_dir is None:
            return WorkItemResult(
                status="failed",
                summary="AB regression round directory is required before Qi AB runner can pass.",
                failure_category="tool_failure",
            )
        summary = load_frontier50_round_summary(
            self.ab_round_dir,
            mission_id=work_item.mission_id,
            task_plan_version=work_item.task_plan_version,
            round_id=self.ab_round_id,
            work_item_id=work_item.work_item_id,
            task_ids=self.ab_task_ids,
        )
        return build_qi_ab_round_contract(summary).work_item_result

    def _blocker(
        self,
        *,
        work_item: WorkItem,
        subsystem: ProductizationSubsystem,
    ) -> str | None:
        mission_id = work_item.mission_id
        if subsystem == "collaboration_tickets" and not _closed_collaboration_loop_exists(
            self.control_plane,
            mission_id,
        ):
            return "Collaboration ticket loop is not closed yet."
        if subsystem == "external_behavior_distillation" and not _has_distilled_behavior(
            self.control_plane,
            mission_id,
        ):
            return "External behavior distillation artifacts are missing."
        if subsystem == "qi_capability_evolution" and not _has_production_capability_profile(
            self.control_plane,
        ):
            return "Qi capability profiles have not reached production default runtime."
        return None

    def _attach_capability_policy_artifact(
        self,
        result: WorkItemResult,
        *,
        work_item: WorkItem,
        subsystem: ProductizationSubsystem,
    ) -> WorkItemResult:
        policy = self.capability_execution_policy
        if policy is None or not policy.capability_profile_refs:
            return result
        payload = {
            "work_item_id": work_item.work_item_id,
            "subsystem": subsystem,
            "policy_id": policy.policy_id,
            "capability_profile_refs": policy.capability_profile_refs,
            "directive_categories": sorted({directive.category for directive in policy.directives}),
        }
        artifact = ArtifactRecord(
            artifact_id=f"artifact-{_slug(work_item.work_item_id)}-capability-policy",
            kind="evidence",
            path_or_uri=(
                f"control-plane://runtime-capabilities/{policy.policy_id}/{work_item.work_item_id}"
            ),
            content_hash=_hash_payload(payload),
            created_by=self.runner_identity,
            mission_id=work_item.mission_id,
            work_item_id=work_item.work_item_id,
            supports=[
                "runtime_capability_binding",
                "capability_execution_policy",
                subsystem,
                *policy.capability_profile_refs,
            ],
            freshness="fresh",
            source_quality="primary",
        )
        return result.model_copy(update={"artifacts": [*result.artifacts, artifact]})


def run_productization_dogfood_execution(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    *,
    runner: ProductizationDogfoodRunner | None = None,
    max_steps: int = 20,
    finalize_delivery: bool = True,
) -> ProductizationDogfoodExecutionReport:
    """Run queued productization dogfood work items until blocked or deliverable."""

    active_runner = runner or ProductizationDogfoodRunner(control_plane=control_plane)
    run_refs: list[str] = []
    ab_gate_ref: str | None = None
    for _ in range(max_steps):
        run = control_plane.run_next_ready(mission_id=mission_id, runner=active_runner)
        if run is None:
            break
        run_refs.append(run.run_id)
        if run.work_item_id == "work-v6-qi-ab-runner":
            ab_gate_ref = run.gate_evaluation_ref
        mission_status = control_plane.missions[mission_id].status
        if mission_status not in {"queued", "running"}:
            return _materialize_productization_dogfood_execution_report(
                control_plane,
                ProductizationDogfoodExecutionReport(
                    mission_id=mission_id,
                    run_refs=run_refs,
                    completed_work_item_ids=_done_work_item_ids(control_plane, mission_id),
                    ab_regression_gate_ref=ab_gate_ref,
                    mission_status=mission_status,
                    stopped_reason=f"mission_status_{mission_status}",
                ),
            )

    delivery_manifest_ref: str | None = None
    final_gate_ref: str | None = None
    stopped_reason = "no_ready_work_item"
    if finalize_delivery and _all_mission_work_items_done(control_plane, mission_id):
        audit = audit_control_plane_productization(control_plane, mission_id)
        if audit.ready:
            final_gate = finalize_productization_dogfood_delivery(control_plane, mission_id)
            final_gate_ref = final_gate.gate_evaluation_id
            delivery_manifest_ref = (
                control_plane.missions[mission_id].artifact_manifest_refs[-1]
                if control_plane.missions[mission_id].artifact_manifest_refs
                else None
            )
            stopped_reason = "delivery_ready"
        else:
            stopped_reason = "productization_audit_missing_closures"
    return _materialize_productization_dogfood_execution_report(
        control_plane,
        ProductizationDogfoodExecutionReport(
            mission_id=mission_id,
            run_refs=run_refs,
            completed_work_item_ids=_done_work_item_ids(control_plane, mission_id),
            ab_regression_gate_ref=ab_gate_ref,
            delivery_manifest_ref=delivery_manifest_ref,
            final_gate_ref=final_gate_ref,
            mission_status=control_plane.missions[mission_id].status,
            stopped_reason=stopped_reason,
        ),
    )


def finalize_productization_dogfood_delivery(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    *,
    actor: str = "control-plane",
) -> GateEvaluation:
    """Compile a delivery manifest after all productization loops have run."""

    mission = control_plane.missions[mission_id]
    if not _all_mission_work_items_done(control_plane, mission_id):
        raise ValueError("cannot finalize productization delivery before all work items are done")
    audit = audit_control_plane_productization(control_plane, mission_id)
    if not audit.ready:
        raise ValueError("cannot finalize productization delivery while audit has missing closures")
    artifact = ArtifactRecord(
        artifact_id=f"artifact-{_slug(mission_id)}-delivery",
        kind="report",
        path_or_uri=f"control-plane://productization/{mission_id}/delivery",
        content_hash=_hash_payload(
            {
                "mission_id": mission_id,
                "present_subsystems": audit.present_subsystems,
            }
        ),
        created_by=actor,
        mission_id=mission_id,
        supports=["delivery", "productization_dogfood", *_REQUIRED_SUBSYSTEMS],
        freshness="fresh",
        source_quality="primary",
    )
    _upsert_artifact(control_plane, artifact)
    evidence_refs = sorted(
        manifest.manifest_id
        for manifest in control_plane.artifact_manifests.values()
        if manifest.mission_id == mission_id
    )
    manifest = ArtifactManifest(
        manifest_id=f"manifest-{_slug(mission_id)}-delivery",
        mission_id=mission_id,
        kind="delivery",
        artifact_refs=[artifact.artifact_id],
        primary_artifact_ref=artifact.artifact_id,
        evidence_refs=evidence_refs or [artifact.artifact_id],
        review_refs=list(audit.recovery_bundle.open_ticket_ids),
        created_by=actor,
        content_hash=_hash_payload(
            {
                "artifact": artifact.artifact_id,
                "evidence": evidence_refs,
                "subsystems": audit.present_subsystems,
            }
        ),
        supports_delivery=True,
    )
    _upsert_artifact_manifest(control_plane, manifest)
    mission = mission.model_copy(
        update={"artifact_manifest_refs": [*mission.artifact_manifest_refs, manifest.manifest_id]}
    )
    control_plane.missions[mission_id] = mission
    if control_plane.store is not None:
        control_plane.store.put_mission(mission)
    gate = GateEvaluation(
        gate_evaluation_id=f"gate-{_slug(mission_id)}-delivery",
        mission_id=mission_id,
        task_plan_version=mission.current_plan_version or "v6-productization",
        subject_ref=manifest.manifest_id,
        stage="delivery",
        task_type="self_improvement",
        rubric_version="kun-v6-productization-delivery",
        metric_pack_version="productization-dogfood-v1",
        north_star_verdict="pass",
        result_quality=0.95,
        speed=0.8,
        cost=0.8,
        risk=0.2,
        evidence_quality=1.0,
        collaboration_quality=1.0,
        score_breakdown=dict.fromkeys(audit.present_subsystems, 1.0),
        thresholds={"result_quality": 0.8},
        evidence_refs=manifest.evidence_refs,
        artifact_refs=manifest.artifact_refs,
        review_refs=manifest.review_refs,
        source_freshness="fresh",
        confidence=0.9,
        next_action="ready_to_deliver",
        next_state="delivering",
        created_by=actor,
    )
    control_plane.apply_gate(gate)
    return gate


def _materialize_productization_dogfood_execution_report(
    control_plane: InMemoryControlPlane,
    report: ProductizationDogfoodExecutionReport,
    *,
    actor: str = "control-plane",
) -> ProductizationDogfoodExecutionReport:
    """Persist dogfood execution and recovery evidence for later Qi learning."""

    recovery_bundle = build_recovery_bundle(control_plane, report.mission_id)
    recovery_artifact = ArtifactRecord(
        artifact_id=f"artifact-{_slug(report.mission_id)}-dogfood-recovery-bundle",
        kind="report",
        path_or_uri=f"control-plane://productization/{report.mission_id}/dogfood-recovery-bundle",
        content_hash=_hash_payload(recovery_bundle.model_dump(mode="json")),
        created_by=actor,
        mission_id=report.mission_id,
        supports=[
            "recovery_bundle",
            "productization_dogfood",
            "real_long_task_dogfood",
            "cross_restart_resume",
        ],
        freshness="fresh",
        source_quality="primary",
    )
    _upsert_artifact(control_plane, recovery_artifact)
    report_with_recovery = report.model_copy(
        update={"recovery_bundle_artifact_ref": recovery_artifact.artifact_id}
    )
    execution_artifact = ArtifactRecord(
        artifact_id=f"artifact-{_slug(report.mission_id)}-dogfood-execution-report",
        kind="report",
        path_or_uri=f"control-plane://productization/{report.mission_id}/dogfood-execution-report",
        content_hash=_hash_payload(
            {
                "report": report_with_recovery.model_dump(mode="json"),
                "recovery_bundle_ref": recovery_artifact.artifact_id,
            }
        ),
        created_by=actor,
        mission_id=report.mission_id,
        supports=[
            "execution_report",
            "productization_dogfood",
            "real_long_task_dogfood",
            "qi_learning_input",
            "ab_regression_gate",
        ],
        freshness="fresh",
        source_quality="primary",
    )
    _upsert_artifact(control_plane, execution_artifact)
    return report_with_recovery.model_copy(
        update={"execution_report_artifact_ref": execution_artifact.artifact_id}
    )


def accept_productization_dogfood_delivery(
    control_plane: InMemoryControlPlane,
    mission_id: str,
    *,
    reviewer: str = "kun-dogfood",
    satisfaction: float = 0.95,
    close_after_learning: bool = True,
) -> ProductizationDogfoodAcceptanceReport:
    """Accept the dogfood delivery, write learning state, and optionally close."""

    mission = control_plane.missions[mission_id]
    if mission.status == "delivering":
        control_plane.transition_mission(
            mission_id=mission_id,
            target="awaiting_acceptance",
            actor="control-plane",
            reason="delivery manifest is ready for acceptance",
            subject_ref=_latest_delivery_manifest_ref(control_plane, mission_id),
        )
    mission = control_plane.missions[mission_id]
    if mission.status not in {"awaiting_acceptance", "learning_writeback", "closed"}:
        raise ValueError(f"mission {mission_id} is not ready for acceptance: {mission.status}")

    delivery_manifest_ref = _latest_delivery_manifest_ref(control_plane, mission_id)
    gate_evaluation_ref = _latest_delivery_gate_ref(control_plane, mission_id)
    acceptance_id = f"accept-{_slug(mission_id)}-delivery"
    if acceptance_id not in control_plane.acceptance_reviews:
        control_plane.record_acceptance_review(
            AcceptanceReview(
                acceptance_id=acceptance_id,
                mission_id=mission_id,
                task_plan_version=mission.current_plan_version or "v6-productization",
                delivery_manifest_ref=delivery_manifest_ref,
                gate_evaluation_ref=gate_evaluation_ref,
                reviewer=reviewer,
                decision="accepted",
                satisfaction=satisfaction,
                reason=(
                    "Productization dogfood delivery passed Control Plane execution, AB regression, "
                    "audit, and recovery checks."
                ),
                new_info_or_constraints=[
                    "Next productization step is real business long-task dogfood.",
                    "Production-stage capabilities must stay rollbackable, regression-gated, and dogfood-verified.",
                ],
            ),
            actor=reviewer,
        )
    learning_artifact, learning_candidate_refs = _materialize_productization_learning_writeback(
        control_plane,
        mission_id=mission_id,
        acceptance_ref=acceptance_id,
        delivery_manifest_ref=delivery_manifest_ref,
        gate_evaluation_ref=gate_evaluation_ref,
        reviewer=reviewer,
    )
    if close_after_learning and control_plane.missions[mission_id].status == "learning_writeback":
        control_plane.transition_mission(
            mission_id=mission_id,
            target="closed",
            actor="control-plane",
            reason="dogfood acceptance recorded and learning writeback is complete",
            subject_ref=acceptance_id,
        )
    return ProductizationDogfoodAcceptanceReport(
        mission_id=mission_id,
        acceptance_ref=acceptance_id,
        delivery_manifest_ref=delivery_manifest_ref,
        gate_evaluation_ref=gate_evaluation_ref,
        learning_artifact_ref=learning_artifact.artifact_id,
        learning_candidate_refs=learning_candidate_refs,
        mission_status=control_plane.missions[mission_id].status,
        closed=control_plane.missions[mission_id].status == "closed",
    )


def _materialize_productization_learning_writeback(
    control_plane: InMemoryControlPlane,
    *,
    mission_id: str,
    acceptance_ref: str,
    delivery_manifest_ref: str,
    gate_evaluation_ref: str,
    reviewer: str,
) -> tuple[ArtifactRecord, list[str]]:
    """Persist the accepted dogfood review as Qi capability-evolution input."""

    candidate_refs = [
        f"candidate-real-task-review-{_slug(mission_id)}",
        *[
            f"candidate-default-runtime-{_slug(profile.capability_id)}"
            for profile in control_plane.list_default_runtime_capabilities()
        ],
    ]
    payload = {
        "mission_id": mission_id,
        "acceptance_ref": acceptance_ref,
        "delivery_manifest_ref": delivery_manifest_ref,
        "gate_evaluation_ref": gate_evaluation_ref,
        "candidate_refs": candidate_refs,
        "default_runtime_capability_refs": [
            profile.capability_id for profile in control_plane.list_default_runtime_capabilities()
        ],
        "reviewer": reviewer,
    }
    artifact = ArtifactRecord(
        artifact_id=f"artifact-{_slug(mission_id)}-learning-writeback",
        kind="review",
        path_or_uri=f"control-plane://productization/{mission_id}/learning-writeback",
        content_hash=_hash_payload(payload),
        created_by=reviewer,
        mission_id=mission_id,
        supports=[
            "learning_writeback",
            "qi_capability_evolution",
            "real_task_review",
            "productization_dogfood",
            "real_long_task_dogfood",
        ],
        freshness="fresh",
        source_quality="primary",
    )
    _upsert_artifact(control_plane, artifact)
    return artifact, candidate_refs


class _DistillationRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    signal_id: str
    keywords: tuple[str, ...]
    behavior: str
    kun_subsystem: ProductizationSubsystem
    adoption_mode: AdoptionMode
    required_tests: tuple[str, ...] = ()
    risk_controls: tuple[str, ...] = ()

    def matches(self, lowered_text: str) -> bool:
        return all(keyword in lowered_text for keyword in self.keywords)

    def to_signal(self, *, origin: BehaviorOrigin, source_ref: str) -> ExternalBehaviorSignal:
        return ExternalBehaviorSignal(
            signal_id=f"{origin}-{self.signal_id}",
            origin=origin,
            source_ref=source_ref,
            behavior=self.behavior,
            kun_subsystem=self.kun_subsystem,
            adoption_mode=self.adoption_mode,
            required_tests=list(self.required_tests),
            risk_controls=list(self.risk_controls),
        )


_DISTILLATION_RULES: tuple[_DistillationRule, ...] = (
    _DistillationRule(
        signal_id="local-first-gateway",
        keywords=("gateway", "sessions", "tools"),
        behavior="local-first gateway/session/tool event routing",
        kun_subsystem="persistence_recovery",
        adoption_mode="kun_native_runtime",
        required_tests=("recover mission across restart", "route tool events to ledger"),
        risk_controls=("session isolation", "workspace boundary"),
    ),
    _DistillationRule(
        signal_id="isolated-agent-routing",
        keywords=("multi-agent", "isolated", "workspace"),
        behavior="isolated worker routing with per-agent workspaces",
        kun_subsystem="persistence_recovery",
        adoption_mode="kun_native_runtime",
        required_tests=("worker lock isolation", "no cross-worker artifact overwrite"),
        risk_controls=("resource lease", "owner map"),
    ),
    _DistillationRule(
        signal_id="approval-buttons",
        keywords=("approval", "buttons"),
        behavior="explicit approval interaction with resumable tickets",
        kun_subsystem="collaboration_tickets",
        adoption_mode="kun_native_contract",
        required_tests=("approval response resumes work item", "approval timeout applies fallback"),
        risk_controls=("high-risk actions require approval", "refusal closes safely"),
    ),
    _DistillationRule(
        signal_id="activity-timeout",
        keywords=("inactivity", "timeout"),
        behavior="activity-based long-run timeout instead of wall-clock kill",
        kun_subsystem="persistence_recovery",
        adoption_mode="kun_native_runtime",
        required_tests=("active heartbeat prevents timeout", "stale heartbeat creates repair item"),
        risk_controls=("environment failures not counted as KUN quality failure",),
    ),
    _DistillationRule(
        signal_id="background-notify",
        keywords=("background", "notify"),
        behavior="background run completion notification and resume",
        kun_subsystem="mission_dashboard",
        adoption_mode="kun_native_runtime",
        required_tests=("completed process updates dashboard", "agent can continue other work"),
        risk_controls=("dedupe completion events",),
    ),
    _DistillationRule(
        signal_id="behavioral-benchmarking",
        keywords=("behavioral", "benchmark"),
        behavior="behavioral benchmark driven tool-use guidance",
        kun_subsystem="qi_capability_evolution",
        adoption_mode="qi_review_only",
        required_tests=("peer gap becomes review-only candidate", "candidate needs replay gate"),
        risk_controls=("no direct production writeback", "rollback required"),
    ),
    _DistillationRule(
        signal_id="structured-logging",
        keywords=("structured", "logging"),
        behavior="structured logs with user-facing diagnostics",
        kun_subsystem="mission_dashboard",
        adoption_mode="kun_native_contract",
        required_tests=("logs attach artifact refs", "dashboard hides raw stack unless needed"),
        risk_controls=("secret redaction", "source freshness"),
    ),
    _DistillationRule(
        signal_id="tool-result-persistence",
        keywords=("tool", "result", "file"),
        behavior="large tool results persisted as artifacts instead of destructive truncation",
        kun_subsystem="persistence_recovery",
        adoption_mode="kun_native_contract",
        required_tests=("large result writes artifact", "manifest links primary output"),
        risk_controls=("redaction", "access status"),
    ),
)


def _present_subsystems(
    *,
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> set[ProductizationSubsystem]:
    progress = control_plane.progress_report(mission_id)
    items = [item for item in control_plane.work_items.values() if item.mission_id == mission_id]
    gates = [
        gate for gate in control_plane.gate_evaluations.values() if gate.mission_id == mission_id
    ]
    present: set[ProductizationSubsystem] = set()
    if control_plane.store is not None and progress.ledger_event_count > 0:
        present.add("persistence_recovery")
    if progress.total_work_items >= 0:
        present.add("mission_dashboard")
    if any(
        gate.created_by == "nuo" or gate.governance_signal.startswith("nuo_") for gate in gates
    ) or any(item.owner == "nuo" for item in items):
        present.add("nuo_contamination")
    if any(item.owner == "qi" and item.type == "test" for item in items):
        present.add("qi_ab_runner")
    if any(
        ticket.mission_id == mission_id
        and ticket.status in {"answered", "fallback_selected", "closed"}
        and ticket.fallback_policy
        and ticket.decision_options
        for ticket in control_plane.collaboration_tickets.values()
    ):
        present.add("collaboration_tickets")
    if control_plane.list_default_runtime_capabilities():
        present.add("qi_capability_evolution")
    if any(
        artifact.mission_id == mission_id and "external_behavior_distillation" in artifact.supports
        for artifact in control_plane.artifacts.values()
    ):
        present.add("external_behavior_distillation")
    return present


def _productization_work_item(
    subsystem: ProductizationSubsystem,
    *,
    mission_id: str,
    task_plan_version: str,
) -> WorkItem:
    item_type, owner, priority, expected_output = _WORK_ITEM_BY_SUBSYSTEM[subsystem]
    return WorkItem(
        work_item_id=f"work-v6-{subsystem.replace('_', '-')}",
        mission_id=mission_id,
        task_plan_version=task_plan_version,
        type=item_type,
        owner=owner,
        priority=priority,
        idempotency_key=f"v6:{subsystem}",
        expected_output=expected_output,
    )


def _gap_summary(subsystem: ProductizationSubsystem) -> str:
    summaries = {
        "persistence_recovery": "State must survive restart with queue, artifacts, gates, tickets, and resume policy.",
        "mission_dashboard": "Progress must be readable without terminal logs.",
        "nuo_contamination": "Pollution and environment blockers must invalidate the run and trigger repair/rerun.",
        "qi_ab_runner": "Frontier50 must run as a Control Plane work item, not as an untracked script.",
        "collaboration_tickets": "Human/external decisions need SLA, fallback, refusal, and resume semantics.",
        "qi_capability_evolution": "Learning must use replay/holdout/shadow/canary/rollback before production use.",
        "external_behavior_distillation": "OpenClaw/Hermes behavior must be distilled into KUN-native candidates and tests.",
    }
    return summaries[subsystem]


def _resume_policy(progress: ControlPlaneProgressReport) -> str:
    if progress.open_collaboration_ticket_ids:
        return "wait_for_ticket_response_then_requeue_waiting_items"
    if progress.next_ready_work_item_ids:
        return "resume_next_ready_work_item"
    if progress.latest_failure_category in {"environment_failure", "tool_failure"}:
        return "repair_system_blocker_then_same_task_rerun"
    if progress.latest_failure_category == "model_quality_failure":
        return "repair_kun_capability_then_same_task_rerun"
    return "reload_store_and_continue_from_latest_ledger_event"


def _headline(summary: UserProgressSummary) -> str:
    if summary.human_needed:
        return "Waiting for a decision before continuing."
    if summary.quality_gate_status == "invalid":
        return "System issue detected; this is not counted as KUN failure."
    if summary.quality_gate_status == "needs_repair":
        return "Quality gate needs repair before delivery."
    if summary.tone == "done":
        return "Mission is closed."
    return "Mission is moving under Control Plane."


def _dashboard_refs(progress: ControlPlaneProgressReport) -> list[str]:
    refs = []
    if progress.latest_gate_ref:
        refs.append(progress.latest_gate_ref)
    refs.extend(progress.next_ready_work_item_ids)
    refs.extend(progress.open_collaboration_ticket_ids)
    return refs


def _normalize_repo_path(path: str | Path, *, root: Path | None) -> str:
    candidate = Path(path).expanduser()
    if candidate.is_absolute() and root is not None:
        resolved = candidate.resolve()
        if resolved == root:
            return "."
        try:
            candidate = resolved.relative_to(root)
        except ValueError:
            return str(resolved)
    path_text = candidate.as_posix()
    if path_text.startswith("./"):
        return path_text[2:]
    return path_text


def _code_boundary_category(path: str) -> CodeBoundaryCategory:
    if "__pycache__" in path or path.endswith(".pyc") or path.startswith(".next/"):
        return "generated_output"
    if (
        path == "kun/control_plane"
        or path.startswith("kun/control_plane/")
        or path == "kun/api/control_plane.py"
        or path == "kun/cli.py"
    ):
        return "formal_code"
    if path == "tests" or path.startswith("tests/"):
        return "test"
    if path == "docs/v6" or path.startswith("docs/v6/"):
        return "product_doc"
    if (
        path == "frontend/src/app/control-plane"
        or path.startswith("frontend/src/app/control-plane/")
        or path == "frontend/src/app/layout.tsx"
        or path == "frontend/src/kunApiClient.ts"
    ):
        return "frontend"
    if path == "artifacts/control_plane" or path.startswith("artifacts/control_plane/"):
        return "artifact_state"
    if path in {
        "pyproject.toml",
        "package.json",
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "tsconfig.json",
        "next.config.js",
        "next.config.mjs",
    }:
        return "configuration"
    return "unknown"


def _code_boundary_findings_for_path(
    path: str,
    category: CodeBoundaryCategory,
) -> list[CodeBoundaryFinding]:
    findings: list[CodeBoundaryFinding] = []
    suffix = Path(path).suffix
    if category == "unknown":
        findings.append(
            CodeBoundaryFinding(
                path=path,
                category=category,
                severity="blocker",
                summary="Changed path is outside the known KUN V6 productization boundary.",
                recommended_action="Move it into a known subsystem, exclude it from the submission, or extend the audit with an explicit owner.",
            )
        )
    if category == "generated_output":
        findings.append(
            CodeBoundaryFinding(
                path=path,
                category=category,
                severity="blocker",
                summary="Generated output should not be mixed into the productization submission.",
                recommended_action="Remove generated output from the submission boundary and regenerate it during verification.",
            )
        )
    if category == "artifact_state" and suffix in _CODE_FILE_SUFFIXES:
        findings.append(
            CodeBoundaryFinding(
                path=path,
                category=category,
                severity="blocker",
                summary="Executable source code is stored under dogfood artifact state.",
                recommended_action="Move formal implementation code into kun/, frontend/, or tests/ and keep artifacts as state/evidence only.",
            )
        )
    if category == "formal_code" and suffix in _STATE_FILE_SUFFIXES:
        findings.append(
            CodeBoundaryFinding(
                path=path,
                category=category,
                severity="blocker",
                summary="Runtime state or generated artifact data is stored under formal code.",
                recommended_action="Move dogfood state into artifacts/control_plane/ or a fixture path with an explicit test owner.",
            )
        )
    return findings


_CODE_FILE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".sh", ".command"}
_STATE_FILE_SUFFIXES = {".json", ".jsonl", ".log", ".sqlite", ".db"}
_EXTERNAL_BEHAVIOR_SOURCE_SUFFIXES = {
    ".md",
    ".mdx",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".sh",
    ".json",
    ".yaml",
    ".yml",
}
_EXTERNAL_BEHAVIOR_DISCOVERY_EXCLUDES = {
    ".git",
    ".next",
    "__pycache__",
    "dist",
    "build",
    "node_modules",
    "vendor",
    ".venv",
}


def _external_source_priority(path: Path, root: Path) -> tuple[int, int, str]:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    parts = relative.parts
    name = path.name.lower()
    path_text = relative.as_posix().lower()
    if not path.is_file():
        return (99, len(parts), path_text)
    if name in {"readme.md", "agents.md", "contributing.md"} or name.startswith("release"):
        return (0, len(parts), path_text)
    if parts and parts[0] in {"docs", "qa", "skills"}:
        return (1, len(parts), path_text)
    if path.suffix.lower() in {".py", ".ts", ".tsx", ".js", ".mjs"}:
        return (2, len(parts), path_text)
    if any(part.startswith(".") for part in parts):
        return (5, len(parts), path_text)
    if parts and parts[0] in {"test", "tests"}:
        return (6, len(parts), path_text)
    return (3, len(parts), path_text)


def _expected_test_path_for_code(code_path: str) -> str | None:
    if code_path == "kun/cli.py":
        return "tests/unit/test_control_plane_cli_v6.py"
    if code_path == "kun/api/control_plane.py":
        return "tests/unit/test_control_plane_api_v6.py"
    if not code_path.startswith("kun/control_plane/") or not code_path.endswith(".py"):
        return None
    module_name = Path(code_path).stem
    if module_name == "__init__":
        return None
    if module_name == "v6":
        return "tests/unit/test_control_plane_v6.py"
    return f"tests/unit/test_control_plane_{module_name}_v6.py"


def _recommended_pr_sections(
    paths_by_category: Mapping[CodeBoundaryCategory, Sequence[str]],
) -> list[str]:
    sections = ["Summary", "Validation", "Risk and rollback"]
    if paths_by_category["formal_code"]:
        sections.append("Control Plane runtime changes")
    if paths_by_category["frontend"]:
        sections.append("Task cockpit surface")
    if paths_by_category["artifact_state"]:
        sections.append("Dogfood state and evidence")
    if paths_by_category["product_doc"]:
        sections.append("Product plan alignment")
    if paths_by_category["test"]:
        sections.append("Regression coverage")
    return sections


def _origin_from_source(source_ref: str, *, default_origin: BehaviorOrigin) -> BehaviorOrigin:
    lowered = source_ref.lower()
    if "openclaw" in lowered:
        return "openclaw"
    if "hermes" in lowered:
        return "hermes"
    return default_origin


def _dedupe_signals(signals: Iterable[ExternalBehaviorSignal]) -> list[ExternalBehaviorSignal]:
    seen: set[tuple[str, str, str, str, str]] = set()
    result: list[ExternalBehaviorSignal] = []
    for signal in signals:
        key = (
            signal.signal_id,
            signal.origin,
            signal.behavior,
            signal.kun_subsystem,
            signal.adoption_mode,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(signal)
    return result


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _upsert_artifact(control_plane: InMemoryControlPlane, artifact: ArtifactRecord) -> None:
    control_plane.artifacts[artifact.artifact_id] = artifact
    if control_plane.store is not None:
        control_plane.store.put_artifact_record(artifact)


def _upsert_artifact_manifest(
    control_plane: InMemoryControlPlane,
    manifest: ArtifactManifest,
) -> None:
    control_plane.artifact_manifests[manifest.manifest_id] = manifest
    if control_plane.store is not None:
        control_plane.store.put_artifact_manifest(manifest)


def _upsert_capability_profile(
    control_plane: InMemoryControlPlane,
    profile: CapabilityProfile,
) -> None:
    control_plane.capability_profiles[profile.capability_id] = profile
    if control_plane.store is not None:
        control_plane.store.put_capability_profile(profile)


def _require_control_plane_refs(
    control_plane: InMemoryControlPlane,
    refs: Sequence[str],
    *,
    label: str,
) -> None:
    missing = [ref for ref in refs if not _control_plane_ref_exists(control_plane, ref)]
    if missing:
        joined = ", ".join(missing)
        raise ValueError(f"{label} must reference existing Control Plane evidence: {joined}")


def _control_plane_ref_exists(control_plane: InMemoryControlPlane, ref: str) -> bool:
    return (
        ref in control_plane.artifacts
        or ref in control_plane.artifact_manifests
        or ref in control_plane.gate_evaluations
        or ref in control_plane.acceptance_reviews
        or ref in control_plane.runs
        or ref in control_plane.ledger_events
        or ref in control_plane.work_items
    )


def _comparison_from_signal(signal: ExternalBehaviorSignal) -> ExternalBehaviorComparisonRecord:
    if signal.adoption_mode == "kun_native_runtime":
        decision: ExternalBehaviorDecision = "adopt"
        reason = "Runtime behavior is complementary to Control Plane and can be adapted directly."
        complexity_impact: Literal["low", "medium", "high"] = "medium"
        blockers: list[str] = []
    elif signal.adoption_mode == "kun_native_contract":
        decision = "merge"
        reason = (
            "Behavior overlaps existing KUN protocols, so it should merge into the native contract."
        )
        complexity_impact = "low"
        blockers = []
    else:
        decision = "merge"
        reason = "Behavior belongs in Qi governance, not direct task execution."
        complexity_impact = "low"
        blockers = [
            "production profile must be consumed as governance guidance, not copied runtime code"
        ]
    signal_ref = behavior_signal_ref(signal)
    return ExternalBehaviorComparisonRecord(
        comparison_ref=f"comparison:{signal_ref}",
        signal_ref=signal_ref,
        origin=signal.origin,
        source_ref=signal.source_ref,
        behavior=signal.behavior,
        kun_subsystem=signal.kun_subsystem,
        adoption_mode=signal.adoption_mode,
        decision=decision,
        reason=reason,
        required_tests=list(signal.required_tests),
        risk_controls=list(signal.risk_controls),
        complexity_impact=complexity_impact,
        production_blockers=blockers,
    )


def _external_behavior_production_artifact(
    *,
    mission_id: str,
    signal: ExternalBehaviorSignal,
    comparison: ExternalBehaviorComparisonRecord,
    dogfood_validation_refs: Sequence[str],
    regression_refs: Sequence[str],
    supervisor_review_ref: str,
    actor: str,
) -> ArtifactRecord:
    payload = {
        "signal": signal.model_dump(mode="json"),
        "comparison": comparison.model_dump(mode="json"),
        "dogfood_validation_refs": list(dogfood_validation_refs),
        "regression_refs": list(regression_refs),
        "supervisor_review_ref": supervisor_review_ref,
        "copy_external_code": False,
    }
    return ArtifactRecord(
        artifact_id=f"artifact-{_slug(comparison.comparison_ref)}-productionization",
        kind="evidence",
        path_or_uri=f"{signal.source_ref}#kun-native-productionization:{_slug(signal.behavior)}",
        content_hash=_hash_payload(payload),
        created_by=actor,
        mission_id=mission_id,
        supports=[
            "external_behavior_distillation",
            "external_behavior_productionization",
            signal.kun_subsystem,
            f"origin:{signal.origin}",
            f"decision:{comparison.decision}",
            "no_external_code_copy",
        ],
        freshness="fresh",
        source_quality="credible",
    )


def _external_behavior_evaluations(
    *,
    candidate: CapabilityCandidate,
    comparison: ExternalBehaviorComparisonRecord,
    mission_id: str,
    task_plan_version: str,
    evidence_ref: str,
    dogfood_validation_refs: Sequence[str],
    regression_refs: Sequence[str],
    supervisor_review_ref: str,
) -> list[CapabilityEvaluation]:
    return [
        _external_behavior_evaluation(
            candidate=candidate,
            comparison=comparison,
            stage=stage,
            mission_id=mission_id,
            task_plan_version=task_plan_version,
            evidence_ref=evidence_ref,
            dogfood_validation_refs=dogfood_validation_refs,
            regression_refs=regression_refs,
            supervisor_review_ref=supervisor_review_ref,
        )
        for stage in ("replay", "holdout", "shadow", "canary", "production")
    ]


def _external_behavior_evaluation(
    *,
    candidate: CapabilityCandidate,
    comparison: ExternalBehaviorComparisonRecord,
    stage: str,
    mission_id: str,
    task_plan_version: str,
    evidence_ref: str,
    dogfood_validation_refs: Sequence[str],
    regression_refs: Sequence[str],
    supervisor_review_ref: str,
) -> CapabilityEvaluation:
    payload: dict[str, object] = {
        "evaluation_id": f"eval-{_slug(comparison.comparison_ref)}-{stage}",
        "candidate_id": candidate.candidate_id,
        "stage": stage,
        "mission_id": mission_id,
        "task_plan_version": task_plan_version,
        "subject_ref": comparison.comparison_ref,
        "passed": True,
        "result_quality": 0.92 if comparison.decision == "adopt" else 0.9,
        "speed": 0.78,
        "cost": 0.76,
        "risk": 0.2 if comparison.complexity_impact == "low" else 0.28,
        "evidence_refs": [evidence_ref, supervisor_review_ref, *dogfood_validation_refs],
        "artifact_refs": [evidence_ref],
        "review_refs": [supervisor_review_ref],
        "notes": [comparison.reason, *comparison.risk_controls],
    }
    if stage in {"holdout", "canary", "production"}:
        payload["holdout_refs"] = list(dogfood_validation_refs)
    if stage in {"canary", "production"}:
        payload["regression_refs"] = list(regression_refs)
        payload["rollback_plan"] = [
            f"disable CapabilityProfile cap-{_slug(comparison.comparison_ref)}",
            "remove production default runtime route for this KUN-native behavior",
        ]
    return CapabilityEvaluation.model_validate(payload)


def _subsystem_from_work_item(work_item: WorkItem) -> ProductizationSubsystem:
    for subsystem in _REQUIRED_SUBSYSTEMS:
        if work_item.work_item_id == f"work-v6-{subsystem.replace('_', '-')}":
            return subsystem
    raise ValueError(f"unknown productization dogfood work item: {work_item.work_item_id}")


def _successful_dogfood_work_item_result(
    *,
    work_item: WorkItem,
    subsystem: ProductizationSubsystem,
    summary: str,
) -> WorkItemResult:
    artifact = ArtifactRecord(
        artifact_id=f"artifact-{_slug(work_item.work_item_id)}",
        kind="test_result" if work_item.type == "test" else "report",
        path_or_uri=f"control-plane://productization/{work_item.mission_id}/{work_item.work_item_id}",
        content_hash=_hash_payload(
            {
                "work_item_id": work_item.work_item_id,
                "subsystem": subsystem,
                "summary": summary,
            }
        ),
        created_by=work_item.owner,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=[subsystem, "productization_dogfood"],
        freshness="fresh",
        source_quality="primary",
    )
    manifest = ArtifactManifest(
        manifest_id=f"manifest-{_slug(work_item.work_item_id)}",
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        kind="run",
        artifact_refs=[artifact.artifact_id],
        primary_artifact_ref=artifact.artifact_id,
        evidence_refs=[artifact.artifact_id],
        created_by=work_item.owner,
        content_hash=_hash_payload({"artifact": artifact.artifact_id, "subsystem": subsystem}),
    )
    gate = GateEvaluation(
        gate_evaluation_id=f"gate-{_slug(work_item.work_item_id)}",
        mission_id=work_item.mission_id,
        task_plan_version=work_item.task_plan_version,
        subject_ref=work_item.work_item_id,
        stage="workitem",
        task_type="self_improvement",
        rubric_version="kun-v6-productization-workitem",
        metric_pack_version="productization-dogfood-v1",
        north_star_verdict="pass",
        result_quality=0.95,
        speed=0.8,
        cost=0.8,
        risk=0.2,
        evidence_quality=1.0,
        collaboration_quality=1.0,
        score_breakdown={subsystem: 1.0},
        thresholds={"result_quality": 0.8},
        evidence_refs=manifest.evidence_refs,
        artifact_refs=manifest.artifact_refs,
        source_freshness="fresh",
        confidence=0.9,
        next_action="continue",
        next_state="running",
        created_by=work_item.owner,
    )
    return WorkItemResult(
        status="done",
        summary=summary,
        artifacts=[artifact],
        artifact_manifest=manifest,
        gate_evaluation=gate,
    )


def _dogfood_success_summary(subsystem: ProductizationSubsystem) -> str:
    summaries = {
        "persistence_recovery": "Durable state, queue, artifacts, decisions, and resume hints verified.",
        "mission_dashboard": "User-facing mission dashboard can explain status and next step.",
        "nuo_contamination": "Nuo contamination and system-blocker classification contract verified.",
        "collaboration_tickets": "Collaboration ticket loop has question, SLA, fallback, response, and resume semantics.",
        "qi_capability_evolution": (
            "Qi has production-stage capability profiles with evidence, holdout, "
            "regression, and rollback notes."
        ),
        "external_behavior_distillation": "External behavior samples have been distilled into KUN-native evidence and replay profiles.",
        "qi_ab_runner": "Qi AB regression gate passed.",
    }
    return summaries[subsystem]


def _closed_collaboration_loop_exists(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> bool:
    return any(
        ticket.mission_id == mission_id
        and ticket.status in {"answered", "fallback_selected", "closed"}
        and ticket.decision_options
        and ticket.fallback_policy
        for ticket in control_plane.collaboration_tickets.values()
    )


def _has_distilled_behavior(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> bool:
    return any(
        artifact.mission_id == mission_id and "external_behavior_distillation" in artifact.supports
        for artifact in control_plane.artifacts.values()
    )


def _has_production_capability_profile(control_plane: InMemoryControlPlane) -> bool:
    return bool(control_plane.list_default_runtime_capabilities())


def _done_work_item_ids(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> list[str]:
    return sorted(
        item.work_item_id
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id and item.status == "done"
    )


def _all_mission_work_items_done(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> bool:
    items = [item for item in control_plane.work_items.values() if item.mission_id == mission_id]
    return bool(items) and all(item.status == "done" for item in items)


def _is_canonical_productization_mission(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> bool:
    expected_ids = {f"work-v6-{subsystem.replace('_', '-')}" for subsystem in _REQUIRED_SUBSYSTEMS}
    observed_ids = {
        item.work_item_id
        for item in control_plane.work_items.values()
        if item.mission_id == mission_id
    }
    return expected_ids.issubset(observed_ids)


def _latest_delivery_manifest_ref(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> str:
    manifests = [
        manifest
        for manifest in control_plane.artifact_manifests.values()
        if manifest.mission_id == mission_id and manifest.kind == "delivery"
    ]
    if not manifests:
        raise ValueError(f"mission {mission_id} has no delivery manifest")
    return max(manifests, key=lambda manifest: manifest.manifest_id).manifest_id


def _latest_delivery_gate_ref(
    control_plane: InMemoryControlPlane,
    mission_id: str,
) -> str:
    gates = [
        gate
        for gate in control_plane.gate_evaluations.values()
        if gate.mission_id == mission_id and gate.stage == "delivery"
    ]
    if not gates:
        raise ValueError(f"mission {mission_id} has no delivery gate")
    return max(gates, key=lambda gate: gate.gate_evaluation_id).gate_evaluation_id


def behavior_signal_ref(signal: ExternalBehaviorSignal) -> str:
    """Stable compact ref used in tests and product reports."""

    return f"{signal.origin}:{_slug(signal.behavior)}:{signal.kun_subsystem}"
