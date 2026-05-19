"""Strict V6 control-plane objects and runtime protocol helpers.

The goal of this module is not to replace all existing Mission code in one
step.  It defines the small product-contract layer that old and new code can
align to: state machine, execution contract, work queue objects, artifact
manifests, working context, collaboration tickets, and the unified
GateEvaluation judge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from ulid import ULID

TaskType = Literal[
    "product_development",
    "research_evidence",
    "ops_tooling",
    "collaboration",
    "external_action",
    "self_improvement",
]

MissionStatus = Literal[
    "intake",
    "planning",
    "info_gap",
    "awaiting_approval",
    "contracted",
    "queued",
    "running",
    "waiting_human",
    "waiting_external",
    "blocked",
    "retrying",
    "repairing",
    "rolling_back",
    "changing_plan",
    "merging",
    "delivering",
    "awaiting_acceptance",
    "learning_writeback",
    "closed",
    "partial_closed",
    "failed",
    "cancelled",
    "paused",
    "escalated",
]

WorkItemType = Literal[
    "execution",
    "research",
    "review",
    "test",
    "collaboration",
    "external_worker",
    "repair",
    "rollback",
    "retest",
    "plan_change",
    "merge",
    "governance",
]

WorkItemStatus = Literal[
    "queued",
    "running",
    "waiting_human",
    "waiting_external",
    "blocked",
    "retrying",
    "repairing",
    "rolling_back",
    "changing_plan",
    "merging",
    "done",
    "partial",
    "failed",
    "cancelled",
]

ArtifactKind = Literal[
    "answer",
    "evidence",
    "source",
    "context",
    "log",
    "diff",
    "test_result",
    "review",
    "report",
    "screenshot",
    "decision",
]

ArtifactManifestKind = Literal["run", "merge", "delivery", "rollback", "retest"]

LedgerEventType = Literal[
    "message",
    "decision",
    "approval",
    "state_change",
    "plan_change",
    "context_refresh",
    "artifact_recorded",
    "gate_evaluation",
    "acceptance",
    "promotion",
    "rollback",
    "governance",
]

CollaborationTicketType = Literal[
    "user_decision",
    "operator_action",
    "review",
    "expert_input",
    "external_worker",
    "approval",
    "external_action",
]

CollaborationTicketStatus = Literal[
    "open",
    "waiting",
    "answered",
    "escalated",
    "fallback_selected",
    "cancelled",
    "closed",
]

GateStage = Literal[
    "plan",
    "decision",
    "workitem",
    "merge",
    "delivery",
    "acceptance",
    "learning",
    "governance",
]

NextAction = Literal[
    "continue",
    "needs_info",
    "needs_human",
    "needs_external",
    "needs_repair",
    "needs_rollback",
    "needs_plan_change",
    "ready_to_deliver",
    "accepted",
    "partial",
    "rejected",
    "promote_candidate",
    "rollback_capability",
]

FailureCategory = Literal[
    "environment_failure",
    "permission_failure",
    "tool_failure",
    "model_quality_failure",
    "evidence_failure",
    "plan_failure",
    "external_dependency_failure",
    "user_input_missing",
    "delivery_failure",
    "cost_overrun",
]

AcceptanceDecision = Literal["accepted", "partial_accepted", "rework_required", "rejected"]

TERMINAL_STATUSES: frozenset[MissionStatus] = frozenset(
    {"closed", "partial_closed", "failed", "cancelled"}
)

_ALLOWED_TRANSITIONS: dict[MissionStatus, frozenset[MissionStatus]] = {
    "intake": frozenset({"planning", "cancelled"}),
    "planning": frozenset({"info_gap", "awaiting_approval", "cancelled", "paused"}),
    "info_gap": frozenset({"planning", "awaiting_approval", "waiting_human", "cancelled"}),
    "awaiting_approval": frozenset({"planning", "contracted", "cancelled", "paused"}),
    "contracted": frozenset({"queued", "changing_plan", "cancelled"}),
    "queued": frozenset({"running", "blocked", "cancelled", "paused"}),
    "running": frozenset(
        {
            "waiting_human",
            "waiting_external",
            "info_gap",
            "blocked",
            "retrying",
            "repairing",
            "rolling_back",
            "changing_plan",
            "merging",
            "delivering",
            "failed",
            "paused",
        }
    ),
    "waiting_human": frozenset(
        {"queued", "running", "escalated", "changing_plan", "partial_closed", "cancelled"}
    ),
    "waiting_external": frozenset(
        {"queued", "running", "escalated", "changing_plan", "partial_closed", "cancelled"}
    ),
    "blocked": frozenset(
        {"retrying", "repairing", "changing_plan", "waiting_human", "escalated", "failed"}
    ),
    "retrying": frozenset({"running", "repairing", "changing_plan", "failed"}),
    "repairing": frozenset({"queued", "retrying", "changing_plan", "failed"}),
    "rolling_back": frozenset({"queued", "partial_closed", "failed"}),
    "changing_plan": frozenset({"awaiting_approval", "queued", "partial_closed", "cancelled"}),
    "merging": frozenset({"delivering", "repairing", "changing_plan"}),
    "delivering": frozenset({"awaiting_acceptance", "repairing", "changing_plan"}),
    "awaiting_acceptance": frozenset(
        {"learning_writeback", "repairing", "changing_plan", "partial_closed", "failed"}
    ),
    "learning_writeback": frozenset({"closed", "partial_closed", "failed"}),
    "paused": frozenset({"queued", "running", "changing_plan", "cancelled"}),
    "escalated": frozenset({"waiting_human", "changing_plan", "partial_closed", "failed"}),
    "closed": frozenset(),
    "partial_closed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}

_ACTION_TO_STATUS: dict[NextAction, MissionStatus] = {
    "continue": "running",
    "needs_info": "info_gap",
    "needs_human": "waiting_human",
    "needs_external": "waiting_external",
    "needs_repair": "repairing",
    "needs_rollback": "rolling_back",
    "needs_plan_change": "changing_plan",
    "ready_to_deliver": "delivering",
    "accepted": "learning_writeback",
    "partial": "partial_closed",
    "rejected": "repairing",
    "promote_candidate": "learning_writeback",
    "rollback_capability": "rolling_back",
}

_FAILURE_RECOVERY: dict[FailureCategory, tuple[NextAction, MissionStatus]] = {
    "environment_failure": ("needs_repair", "repairing"),
    "permission_failure": ("needs_human", "waiting_human"),
    "tool_failure": ("needs_repair", "repairing"),
    "model_quality_failure": ("needs_plan_change", "changing_plan"),
    "evidence_failure": ("needs_info", "info_gap"),
    "plan_failure": ("needs_plan_change", "changing_plan"),
    "external_dependency_failure": ("needs_external", "waiting_external"),
    "user_input_missing": ("needs_human", "waiting_human"),
    "delivery_failure": ("needs_repair", "repairing"),
    "cost_overrun": ("needs_plan_change", "changing_plan"),
}


def _now() -> datetime:
    return datetime.now(UTC)


def _v6_id(prefix: str) -> str:
    return f"{prefix}-{ULID()}"


class Mission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str = Field(default_factory=lambda: _v6_id("msn"))
    owner: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    task_type: TaskType
    priority: int = Field(default=50, ge=0, le=100)
    risk_level: Literal["low", "medium", "high", "critical"] = "medium"
    status: MissionStatus = "intake"
    current_plan_version: str | None = None
    execution_contract_ref: str | None = None
    working_context_ref: str | None = None
    ledger_refs: list[str] = Field(default_factory=list)
    artifact_manifest_refs: list[str] = Field(default_factory=list)
    acceptance_ref: str | None = None


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(default_factory=lambda: _v6_id("plan"))
    mission_id: str
    version: str = "v0"
    objective: str = Field(min_length=1)
    known_facts: list[str] = Field(default_factory=list)
    unknowns: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    info_gaps: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    risk_register: list[str] = Field(default_factory=list)
    evidence_plan: list[str] = Field(default_factory=list)
    decomposition: list[str] = Field(default_factory=list)
    worker_plan: list[str] = Field(default_factory=list)
    merge_plan: list[str] = Field(default_factory=list)
    test_plan: list[str] = Field(default_factory=list)
    rollback_plan: list[str] = Field(default_factory=list)
    human_confirmation_points: list[str] = Field(default_factory=list)
    change_log: list[str] = Field(default_factory=list)
    approval_status: Literal["draft", "approved", "approved_with_limits", "blocked"] = "draft"

    @property
    def can_contract(self) -> bool:
        return self.approval_status in {"approved", "approved_with_limits"} and not self.info_gaps


class ExecutionContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contract_id: str = Field(default_factory=lambda: _v6_id("contract"))
    mission_id: str
    task_plan_version: str
    allowed_actions: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)
    permissions: list[str] = Field(default_factory=list)
    budget: dict[str, float] = Field(default_factory=dict)
    deadline: datetime | None = None
    evidence_policy: dict[str, Any] = Field(default_factory=dict)
    delivery_contract: dict[str, Any] = Field(default_factory=dict)
    risk_policy: dict[str, Any] = Field(default_factory=dict)
    rollback_policy: dict[str, Any] = Field(default_factory=dict)
    external_worker_policy: dict[str, Any] = Field(default_factory=dict)
    approval_policy: dict[str, Any] = Field(default_factory=dict)


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_item_id: str = Field(default_factory=lambda: _v6_id("work"))
    mission_id: str
    task_plan_version: str
    type: WorkItemType
    owner: str
    dependencies: list[str] = Field(default_factory=list)
    priority: int = Field(default=50, ge=0, le=100)
    resource_locks: list[str] = Field(default_factory=list)
    lease: str | None = None
    heartbeat: datetime | None = None
    timeout: datetime | None = None
    retry_budget: int = Field(default=0, ge=0)
    idempotency_key: str | None = None
    expected_output: str = ""
    artifact_manifest_ref: str | None = None
    status: WorkItemStatus = "queued"

    @model_validator(mode="after")
    def _require_idempotency_for_risky_items(self) -> WorkItem:
        if self.type in {"rollback", "external_worker"} and not self.idempotency_key:
            raise ValueError(f"{self.type} work item requires an idempotency_key")
        return self


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str = Field(default_factory=lambda: _v6_id("run"))
    work_item_id: str
    runner_type: Literal["model", "tool", "agent", "command", "human", "external_worker"]
    runner_identity: str
    started_at: datetime = Field(default_factory=_now)
    ended_at: datetime | None = None
    exit_status: Literal["running", "succeeded", "failed", "cancelled"] = "running"
    stdout_ref: str | None = None
    stderr_ref: str | None = None
    cost: float = Field(default=0.0, ge=0.0)
    failure_category: FailureCategory | None = None
    artifact_manifest_ref: str | None = None
    gate_evaluation_ref: str | None = None

    @model_validator(mode="after")
    def _failed_runs_need_failure_category(self) -> RunRecord:
        if self.exit_status == "failed" and self.failure_category is None:
            raise ValueError("failed runs require failure_category")
        return self


class ArtifactRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str = Field(default_factory=lambda: _v6_id("artifact"))
    kind: ArtifactKind
    path_or_uri: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)
    created_by: str = Field(min_length=1)
    mission_id: str
    work_item_id: str | None = None
    access_status: Literal["available", "missing", "expired", "permission_denied"] = "available"
    supports: list[str] = Field(default_factory=list)
    freshness: Literal["fresh", "stale", "unknown"] = "unknown"
    source_quality: Literal["primary", "credible", "weak", "unknown", "not_applicable"] = (
        "not_applicable"
    )
    expires_at: datetime | None = None


class ArtifactManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest_id: str = Field(default_factory=lambda: _v6_id("manifest"))
    mission_id: str
    work_item_id: str | None = None
    kind: ArtifactManifestKind
    artifact_refs: list[str] = Field(default_factory=list)
    primary_artifact_ref: str | None = None
    test_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    review_refs: list[str] = Field(default_factory=list)
    created_by: str
    content_hash: str = Field(min_length=1)
    supports_delivery: bool = False
    rollback_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _delivery_manifest_requires_traceable_artifacts(self) -> ArtifactManifest:
        if self.kind == "delivery":
            if not self.supports_delivery:
                raise ValueError("delivery manifest must support delivery")
            if not self.primary_artifact_ref:
                raise ValueError("delivery manifest requires primary_artifact_ref")
            if not (self.evidence_refs or self.test_refs or self.review_refs):
                raise ValueError("delivery manifest requires evidence, test, or review refs")
        return self


class LedgerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: _v6_id("ledger"))
    mission_id: str
    sequence: int = Field(ge=0)
    event_type: LedgerEventType
    actor: str
    time: datetime = Field(default_factory=_now)
    correlation_id: str
    causation_id: str | None = None
    subject_ref: str
    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)
    artifact_refs: list[str] = Field(default_factory=list)
    idempotency_key: str
    replay_hint: str = ""

    @model_validator(mode="after")
    def _strong_event_schema(self) -> LedgerEvent:
        required: dict[LedgerEventType, set[str]] = {
            "message": {
                "sender",
                "receiver",
                "intent",
                "requires_response",
                "deadline",
                "resume_rule",
            },
            "decision": {
                "options",
                "selected_option",
                "reason",
                "risk_impact",
                "quality_impact",
                "speed_impact",
                "cost_impact",
                "approver",
            },
            "approval": {"requested_action", "approval_scope", "expires_at"},
        }
        missing = required.get(self.event_type, set()) - set(self.payload)
        if missing:
            missing_list = ", ".join(sorted(missing))
            raise ValueError(
                f"{self.event_type} ledger event missing payload fields: {missing_list}"
            )
        return self


class WorkingContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    working_context_id: str = Field(default_factory=lambda: _v6_id("ctx"))
    mission_id: str
    task_plan_version: str
    audience: str
    scope: str
    summary: str = Field(min_length=1)
    critical_facts: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    decision_refs: list[str] = Field(default_factory=list)
    source_hashes: list[str] = Field(default_factory=list)
    freshness: Literal["fresh", "stale", "invalidated"] = "fresh"
    invalidated_by: str | None = None
    omitted_reason: str = ""

    @model_validator(mode="after")
    def _requires_non_lossy_contract_context(self) -> WorkingContext:
        if not self.acceptance_criteria:
            raise ValueError("working context must include acceptance criteria")
        if not self.constraints:
            raise ValueError("working context must include constraints")
        if self.freshness == "invalidated" and not self.invalidated_by:
            raise ValueError("invalidated working context requires invalidated_by")
        return self


class CollaborationTicket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(default_factory=lambda: _v6_id("collab"))
    mission_id: str
    type: CollaborationTicketType
    role_needed: str
    why_needed: str = Field(min_length=1)
    decision_options: list[str] = Field(default_factory=list)
    recommended_option: str | None = None
    context_ref: str
    risk_if_skipped: str = Field(min_length=1)
    deadline: datetime
    sla_policy: dict[str, Any] = Field(default_factory=dict)
    escalation_policy: dict[str, Any] = Field(default_factory=dict)
    fallback_policy: dict[str, Any] = Field(default_factory=dict)
    resume_after_response: bool = True
    output_contract: str = Field(min_length=1)
    status: CollaborationTicketStatus = "open"

    @model_validator(mode="after")
    def _decision_tickets_need_options(self) -> CollaborationTicket:
        if self.type in {"user_decision", "approval", "external_action"} and (
            not self.decision_options or not self.recommended_option
        ):
            raise ValueError(f"{self.type} ticket requires decision options and recommendation")
        return self


class GateEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate_evaluation_id: str = Field(default_factory=lambda: _v6_id("gate"))
    mission_id: str
    task_plan_version: str
    subject_ref: str
    stage: GateStage
    task_type: TaskType
    rubric_version: str
    metric_pack_version: str
    north_star_verdict: Literal["pass", "partial", "fail"]
    result_quality: float = Field(ge=0.0, le=1.0)
    speed: float = Field(ge=0.0, le=1.0)
    cost: float = Field(ge=0.0, le=1.0)
    risk: float = Field(ge=0.0, le=1.0)
    evidence_quality: float = Field(ge=0.0, le=1.0)
    collaboration_quality: float = Field(ge=0.0, le=1.0)
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    thresholds: dict[str, float] = Field(default_factory=lambda: {"result_quality": 0.8})
    hard_gate_failures: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    artifact_refs: list[str] = Field(default_factory=list)
    test_refs: list[str] = Field(default_factory=list)
    review_refs: list[str] = Field(default_factory=list)
    source_freshness: Literal["fresh", "mixed", "stale", "unknown"] = "unknown"
    evidence_conflicts: list[str] = Field(default_factory=list)
    failure_category: FailureCategory | None = None
    root_cause: str = ""
    responsibility_scope: Literal[
        "kun_auto",
        "human_collaboration",
        "external_worker",
        "environment",
        "mixed",
        "unknown",
    ] = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    next_action: NextAction
    next_state: MissionStatus
    next_ticket_refs: list[str] = Field(default_factory=list)
    learning_eligibility: Literal["none", "candidate", "blocked", "ready_for_shadow"] = "none"
    governance_signal: str = ""
    created_by: str
    ledger_event_ref: str | None = None

    @model_validator(mode="after")
    def _enforce_north_star_and_traceability(self) -> GateEvaluation:
        result_threshold = self.thresholds.get("result_quality", 0.8)
        if self.result_quality < result_threshold:
            if self.next_action in {"ready_to_deliver", "accepted", "promote_candidate"}:
                raise ValueError("result_quality below threshold cannot be offset by speed or cost")
            if self.north_star_verdict == "pass":
                raise ValueError(
                    "north_star_verdict cannot pass when result_quality is below threshold"
                )
        if self.next_action in {"ready_to_deliver", "accepted"} and not (
            self.artifact_refs and (self.evidence_refs or self.test_refs or self.review_refs)
        ):
            raise ValueError(
                "delivery/acceptance decisions require artifact and evidence/test/review refs"
            )
        expected_next_state = _ACTION_TO_STATUS[self.next_action]
        if self.next_state != expected_next_state:
            raise ValueError(
                f"next_state {self.next_state!r} does not match next_action {self.next_action!r}"
            )
        if (
            self.learning_eligibility != "none"
            and not self.failure_category
            and self.stage != "learning"
        ):
            raise ValueError("learning candidates outside learning stage require failure_category")
        return self


class AcceptanceReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    acceptance_id: str = Field(default_factory=lambda: _v6_id("accept"))
    mission_id: str
    task_plan_version: str
    delivery_manifest_ref: str
    gate_evaluation_ref: str
    reviewer: str
    decision: AcceptanceDecision
    satisfaction: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    requested_changes: list[str] = Field(default_factory=list)
    new_info_or_constraints: list[str] = Field(default_factory=list)
    followup_work_item_refs: list[str] = Field(default_factory=list)
    ledger_event_ref: str | None = None

    @model_validator(mode="after")
    def _rework_and_rejection_need_followup(self) -> AcceptanceReview:
        if self.decision in {"rework_required", "rejected"} and not (
            self.requested_changes or self.followup_work_item_refs
        ):
            raise ValueError(f"{self.decision} acceptance requires changes or followup work items")
        if self.decision == "accepted" and self.satisfaction < 0.5:
            raise ValueError("accepted review cannot have low satisfaction")
        return self


class CapabilityProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability_id: str = Field(default_factory=lambda: _v6_id("cap"))
    capability_name: str
    evidence_refs: list[str] = Field(default_factory=list)
    known_limits: list[str] = Field(default_factory=list)
    promotion_stage: Literal["review_only", "replay", "holdout", "shadow", "canary", "production"]
    holdout_refs: list[str] = Field(default_factory=list)
    regression_refs: list[str] = Field(default_factory=list)
    last_verified_at: datetime | None = None
    rollback_plan: list[str] = Field(default_factory=list)
    runtime_enabled: bool = True
    rolled_back_at: datetime | None = None
    rollback_reason: str = ""
    rollback_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _production_capability_requires_proof_and_rollback(self) -> CapabilityProfile:
        if self.promotion_stage in {"canary", "production"}:
            if not (self.evidence_refs and self.holdout_refs and self.regression_refs):
                raise ValueError(
                    "canary/production capabilities require evidence, holdout, regression refs"
                )
            if not self.rollback_plan:
                raise ValueError("canary/production capabilities require rollback_plan")
        if not self.runtime_enabled:
            if not self.rolled_back_at:
                raise ValueError("disabled capability profiles require rolled_back_at")
            if not (self.rollback_reason and self.rollback_refs):
                raise ValueError("disabled capability profiles require rollback reason and refs")
        return self


class FailureRecovery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failure_category: FailureCategory
    next_action: NextAction
    next_state: MissionStatus


def assert_transition_allowed(current: MissionStatus, target: MissionStatus) -> None:
    """Raise when the V6 mission state machine forbids a transition."""

    if current in TERMINAL_STATUSES:
        raise ValueError(f"terminal mission status {current!r} cannot transition to {target!r}")
    if target == "paused":
        return
    allowed = _ALLOWED_TRANSITIONS[current]
    if target not in allowed:
        raise ValueError(f"transition {current!r} -> {target!r} is not allowed")


def default_recovery_for_failure(failure_category: FailureCategory) -> FailureRecovery:
    """Return the V6 default action/state for a failure category."""

    next_action, next_state = _FAILURE_RECOVERY[failure_category]
    return FailureRecovery(
        failure_category=failure_category,
        next_action=next_action,
        next_state=next_state,
    )


def validate_workitem_dag(work_items: list[WorkItem]) -> None:
    """Validate WorkItem dependencies before scheduling.

    This is intentionally small and deterministic; the production scheduler can
    add persistence and locks, but it should keep these semantics.
    """

    ids = {item.work_item_id for item in work_items}
    dependency_map = {item.work_item_id: list(item.dependencies) for item in work_items}
    for item_id, dependencies in dependency_map.items():
        if item_id in dependencies:
            raise ValueError(f"work item {item_id} depends on itself")
        missing = sorted(set(dependencies) - ids)
        if missing:
            raise ValueError(f"work item {item_id} has missing dependencies: {missing}")

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(item_id: str) -> None:
        if item_id in visited:
            return
        if item_id in visiting:
            raise ValueError(f"cycle detected at work item {item_id}")
        visiting.add(item_id)
        for dependency in dependency_map[item_id]:
            visit(dependency)
        visiting.remove(item_id)
        visited.add(item_id)

    for item_id in ids:
        visit(item_id)
