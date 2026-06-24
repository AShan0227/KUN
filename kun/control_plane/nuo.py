"""Pure Nuo contamination and health diagnostics for the V6 control plane."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.v6 import (
    FailureCategory,
    GateEvaluation,
    MissionStatus,
    NextAction,
    TaskType,
    WorkItem,
    WorkItemType,
)

NuoFindingCode = Literal[
    "stub_echo",
    "fallback",
    "family_routing_mismatch",
    "timeout",
    "network_eof",
    "network_blocked",
    "wrapper_missing",
    "wrapper_contract_mismatch",
    "auth_failure",
    "permission_denied",
    "sandbox_permission_blocked",
    "report_missing",
    "rollback_refs_missing",
    "review_count_missing",
    "review_count_insufficient",
    "comparator_unhealthy",
    "premature_delivery_claim",
    "subjective_playtest_missing",
]
NuoFindingKind = Literal[
    "contamination",
    "environment_blocker",
    "artifact_gap",
    "governance_blocker",
    "product_gap",
]
NuoSeverity = Literal["info", "warning", "blocker"]
NuoHealthStatus = Literal["healthy", "warning", "blocked"]
NuoRecoveryAction = Literal[
    "rerun",
    "repair",
    "reconfigure_router",
    "fix_wrapper",
    "fix_auth",
    "collect_report",
    "collect_reviews",
    "repair_comparator",
    "continue_iteration",
    "request_human_playtest",
    "pause",
]

_ENVIRONMENT_FINDINGS: frozenset[NuoFindingCode] = frozenset(
    {
        "timeout",
        "network_eof",
        "network_blocked",
        "wrapper_missing",
        "wrapper_contract_mismatch",
        "auth_failure",
        "permission_denied",
        "sandbox_permission_blocked",
    }
)
_CONTAMINATION_FINDINGS: frozenset[NuoFindingCode] = frozenset(
    {"stub_echo", "fallback", "family_routing_mismatch"}
)


class NuoObservation(BaseModel):
    """Immutable diagnostic input captured by the caller.

    The model intentionally keeps runtime observation generic so Qi, supervisor,
    and product rounds can all ask Nuo for the same pure health decision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mission_id: str
    task_plan_version: str
    subject_ref: str
    task_type: TaskType
    output_text: str = ""
    error_text: str = ""
    requested_model_family: str | None = None
    actual_model_family: str | None = None
    requested_model_tier: str | None = None
    actual_model_tier: str | None = None
    fallback_engaged: bool = False
    fallback_authorized: bool = False
    fallback_reason: str = ""
    timed_out: bool = False
    network_eof: bool = False
    wrapper_missing: bool = False
    auth_failure: bool = False
    report_required: bool = True
    report_ref: str | None = None
    review_count: int | None = None
    expected_review_count: int = Field(default=0, ge=0)
    comparator_healthy: bool = True
    comparator_health_reason: str = ""
    product_acceptance_claimed: bool = False
    product_surface_gap_codes: list[str] = Field(default_factory=list)
    human_playtest_required: bool = False
    human_playtest_ref: str | None = None
    artifact_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    test_refs: list[str] = Field(default_factory=list)
    review_refs: list[str] = Field(default_factory=list)
    rollback_refs: list[str] = Field(default_factory=list)


class NuoPollutionSample(BaseModel):
    """Regression fixture for a real pollution or environment-blocker pattern."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    sample_id: str
    description: str
    observation: NuoObservation
    expected_codes: list[NuoFindingCode]
    expected_recovery_action: NuoRecoveryAction
    counts_as_kun_failure: bool = False


class NuoHealthFinding(BaseModel):
    """One actionable Nuo health finding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    finding_id: str
    code: NuoFindingCode
    kind: NuoFindingKind
    severity: NuoSeverity = "blocker"
    summary: str
    evidence: list[str] = Field(default_factory=list)
    failure_category: FailureCategory
    responsibility_scope: Literal[
        "kun_auto",
        "human_collaboration",
        "external_worker",
        "environment",
        "mixed",
        "unknown",
    ] = "environment"
    counts_as_kun_failure: bool = False
    invalidates_round: bool = True
    recommended_action: NuoRecoveryAction


class NuoRecoveryRecommendation(BaseModel):
    """Recovery action derived from one or more findings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: NuoRecoveryAction
    next_action: NextAction
    next_state: MissionStatus
    failure_category: FailureCategory
    owner: str
    reason: str
    finding_refs: list[str] = Field(default_factory=list)
    counts_as_kun_failure: bool = False


class NuoHealthReport(BaseModel):
    """Nuo health output that can feed gates, ranking validity, and recovery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    mission_id: str
    task_plan_version: str
    subject_ref: str
    task_type: TaskType
    status: NuoHealthStatus
    findings: list[NuoHealthFinding] = Field(default_factory=list)
    contamination_detected: bool = False
    environment_blocked: bool = False
    valid_for_ranking: bool = True
    valid_for_delivery: bool = True
    created_by: str = "nuo"

    @property
    def counts_as_kun_failure(self) -> bool:
        """Whether this health report should be counted as KUN capability failure."""

        return any(finding.counts_as_kun_failure for finding in self.findings)

    def recovery_recommendation(self) -> NuoRecoveryRecommendation | None:
        """Return the first recovery action Nuo recommends, if any."""

        if not self.findings:
            return None

        priority = {
            "repair": 0,
            "reconfigure_router": 1,
            "repair_comparator": 2,
            "fix_wrapper": 3,
            "fix_auth": 4,
            "continue_iteration": 5,
            "request_human_playtest": 6,
            "collect_report": 7,
            "collect_reviews": 8,
            "rerun": 9,
            "pause": 10,
        }
        finding = min(self.findings, key=lambda item: priority[item.recommended_action])
        next_action, next_state, owner = _recovery_route(finding)
        return NuoRecoveryRecommendation(
            action=finding.recommended_action,
            next_action=next_action,
            next_state=next_state,
            failure_category=finding.failure_category,
            owner=owner,
            reason=finding.summary,
            finding_refs=[item.finding_id for item in self.findings],
            counts_as_kun_failure=self.counts_as_kun_failure,
        )

    def to_gate_evaluation(
        self,
        *,
        rubric_version: str = "nuo-health-v1",
        metric_pack_version: str = "north-star-v6",
    ) -> GateEvaluation:
        """Convert the Nuo report into the unified V6 gate protocol."""

        recommendation = self.recovery_recommendation()
        if recommendation is None:
            return GateEvaluation(
                mission_id=self.mission_id,
                task_plan_version=self.task_plan_version,
                subject_ref=self.subject_ref,
                stage="governance",
                task_type=self.task_type,
                rubric_version=rubric_version,
                metric_pack_version=metric_pack_version,
                north_star_verdict="pass",
                result_quality=1.0,
                speed=1.0,
                cost=1.0,
                risk=0.0,
                evidence_quality=1.0,
                collaboration_quality=1.0,
                confidence=0.95,
                next_action="continue",
                next_state="running",
                created_by=self.created_by,
                governance_signal="nuo_health_clear",
            )

        responsibility_scope = "environment" if not self.counts_as_kun_failure else "kun_auto"
        return GateEvaluation(
            mission_id=self.mission_id,
            task_plan_version=self.task_plan_version,
            subject_ref=self.subject_ref,
            stage="governance",
            task_type=self.task_type,
            rubric_version=rubric_version,
            metric_pack_version=metric_pack_version,
            north_star_verdict="fail",
            result_quality=0.0,
            speed=0.0,
            cost=0.0,
            risk=1.0,
            evidence_quality=0.0,
            collaboration_quality=1.0,
            hard_gate_failures=[finding.code for finding in self.findings],
            failure_category=recommendation.failure_category,
            root_cause=recommendation.reason,
            responsibility_scope=responsibility_scope,
            confidence=0.9,
            next_action=recommendation.next_action,
            next_state=recommendation.next_state,
            learning_eligibility="blocked",
            governance_signal="nuo_health_blocked",
            created_by=self.created_by,
        )


class NuoRecoveryPlan(BaseModel):
    """Gate plus follow-up work item generated by Nuo health governance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_evaluation: GateEvaluation
    recovery_work_item: WorkItem | None = None
    recommendation: NuoRecoveryRecommendation | None = None


Finding = NuoHealthFinding
HealthReport = NuoHealthReport


def diagnose_nuo_health(observation: NuoObservation) -> NuoHealthReport:
    """Detect contamination, environment blockers, and invalid round health."""

    findings = [
        *_detect_output_contamination(observation),
        *_detect_environment_blockers(observation),
        *_detect_artifact_gaps(observation),
        *_detect_governance_blockers(observation),
        *_detect_product_delivery_gaps(observation),
    ]
    status: NuoHealthStatus
    if any(finding.severity == "blocker" for finding in findings):
        status = "blocked"
    elif findings:
        status = "warning"
    else:
        status = "healthy"

    return NuoHealthReport(
        mission_id=observation.mission_id,
        task_plan_version=observation.task_plan_version,
        subject_ref=observation.subject_ref,
        task_type=observation.task_type,
        status=status,
        findings=findings,
        contamination_detected=any(finding.code in _CONTAMINATION_FINDINGS for finding in findings),
        environment_blocked=any(finding.code in _ENVIRONMENT_FINDINGS for finding in findings),
        valid_for_ranking=not any(finding.invalidates_round for finding in findings),
        valid_for_delivery=not findings,
    )


def build_nuo_recovery_plan(
    report: NuoHealthReport,
    *,
    depends_on_subject: bool = True,
    priority: int = 95,
) -> NuoRecoveryPlan:
    """Convert a Nuo report into an auditable gate and recovery work item."""

    gate = report.to_gate_evaluation()
    recommendation = report.recovery_recommendation()
    if recommendation is None:
        return NuoRecoveryPlan(gate_evaluation=gate)
    codes = [finding.code for finding in report.findings]
    work_type = _recovery_work_item_type(recommendation.action)
    work_item = WorkItem(
        work_item_id=f"work-nuo-{_slug(report.subject_ref)}-{recommendation.action}",
        mission_id=report.mission_id,
        task_plan_version=report.task_plan_version,
        type=work_type,
        owner=recommendation.owner,
        dependencies=[report.subject_ref] if depends_on_subject else [],
        priority=priority,
        idempotency_key=(
            f"nuo-recovery:{report.subject_ref}:{recommendation.action}:{','.join(codes)}"
        ),
        expected_output=_recovery_expected_output(
            recommendation=recommendation,
            codes=codes,
        ),
    )
    return NuoRecoveryPlan(
        gate_evaluation=gate,
        recovery_work_item=work_item,
        recommendation=recommendation,
    )


def build_nuo_pollution_sample_library() -> list[NuoPollutionSample]:
    """Return first-class Nuo regression samples for known invalid runs."""

    return [
        _sample(
            sample_id="stub-echo-output",
            description="Agent wrapper returned a stub echo instead of model output.",
            output_text="[stub echo] benchmark prompt",
            expected_codes=["stub_echo"],
            expected_recovery_action="repair",
        ),
        _sample(
            sample_id="unauthorized-fallback",
            description="Router silently used a fallback family or tier.",
            fallback_engaged=True,
            fallback_reason="primary route quota exceeded",
            expected_codes=["fallback"],
            expected_recovery_action="reconfigure_router",
        ),
        _sample(
            sample_id="family-routing-mismatch",
            description="Frontier50 family routing sent the task to the wrong model family.",
            requested_model_family="evidence_research",
            actual_model_family="code_capability",
            expected_codes=["family_routing_mismatch"],
            expected_recovery_action="reconfigure_router",
        ),
        _sample(
            sample_id="runner-timeout",
            description="Runner timed out before producing a trustworthy result.",
            error_text="operation timed out after 900 seconds",
            expected_codes=["timeout"],
            expected_recovery_action="rerun",
        ),
        _sample(
            sample_id="network-eof",
            description="Network EOF interrupted a comparator or model call.",
            error_text="unexpected EOF while reading response body",
            expected_codes=["network_eof"],
            expected_recovery_action="rerun",
        ),
        _sample(
            sample_id="transport-stream-disconnected",
            description="Model/comparator transport stream disconnected after retry.",
            error_text="tls handshake eof; stream disconnected during http/request failed retry",
            expected_codes=["network_eof", "network_blocked"],
            expected_recovery_action="rerun",
        ),
        _sample(
            sample_id="network-blocked",
            description="Network transport was blocked before the run could be trusted.",
            error_text="connection reset by peer during comparator call",
            expected_codes=["network_blocked"],
            expected_recovery_action="rerun",
        ),
        _sample(
            sample_id="auth-failure",
            description="Authentication failed before execution could be trusted.",
            error_text="401 unauthorized: invalid api key",
            expected_codes=["auth_failure"],
            expected_recovery_action="fix_auth",
        ),
        _sample(
            sample_id="permission-denied",
            description="External system rejected the action for permission reasons.",
            error_text="403 forbidden: permission denied",
            expected_codes=["permission_denied"],
            expected_recovery_action="fix_auth",
        ),
        _sample(
            sample_id="sandbox-process-pool-permission",
            description="Sandbox denied multiprocessing semaphore access.",
            error_text=(
                "ProcessPoolExecutor failed: PermissionError: [Errno 1] Operation not permitted "
                "while reading os.sysconf('SC_SEM_NSEMS_MAX')"
            ),
            expected_codes=["sandbox_permission_blocked"],
            expected_recovery_action="fix_wrapper",
        ),
        _sample(
            sample_id="wrapper-missing",
            description="Expected wrapper executable or adapter was missing.",
            error_text="wrapper not found: frontier50-runner",
            expected_codes=["wrapper_missing"],
            expected_recovery_action="fix_wrapper",
        ),
        _sample(
            sample_id="wrapper-contract-change",
            description="Wrapper interface or schema changed and invalidated the run.",
            error_text="tool schema mismatch: unexpected argument --run-tag",
            expected_codes=["wrapper_contract_mismatch"],
            expected_recovery_action="fix_wrapper",
        ),
        _sample(
            sample_id="report-missing",
            description="Required report artifact was not produced.",
            report_ref=None,
            expected_codes=["report_missing"],
            expected_recovery_action="collect_report",
        ),
        _sample(
            sample_id="reviews-missing",
            description="Expected peer reviews were absent or not countable.",
            review_count=None,
            expected_codes=["review_count_missing"],
            expected_recovery_action="collect_reviews",
        ),
        _sample(
            sample_id="reviews-insufficient",
            description="Peer reviews were fewer than the contract requires.",
            review_count=39,
            expected_codes=["review_count_insufficient"],
            expected_recovery_action="collect_reviews",
        ),
        _sample(
            sample_id="comparator-unhealthy",
            description="Comparator health failed, so ranking conclusions are invalid.",
            comparator_healthy=False,
            comparator_health_reason="judge quorum failed",
            expected_codes=["comparator_unhealthy"],
            expected_recovery_action="repair_comparator",
        ),
        _sample(
            sample_id="premature-product-delivery",
            description="A product task claimed delivery even though product-surface gaps remained.",
            output_text="ready_to_deliver, but only mechanics passed and visual polish is missing",
            product_acceptance_claimed=True,
            product_surface_gap_codes=["mechanics_only", "visual_gap"],
            expected_codes=["premature_delivery_claim"],
            expected_recovery_action="continue_iteration",
            counts_as_kun_failure=True,
        ),
        _sample(
            sample_id="subjective-playtest-missing",
            description="A user-facing product delivery needs human or target-user acceptance evidence.",
            product_acceptance_claimed=True,
            human_playtest_required=True,
            human_playtest_ref=None,
            expected_codes=["subjective_playtest_missing"],
            expected_recovery_action="request_human_playtest",
            counts_as_kun_failure=True,
        ),
    ]


def _detect_output_contamination(observation: NuoObservation) -> list[NuoHealthFinding]:
    findings: list[NuoHealthFinding] = []
    output = observation.output_text.strip().lower()
    if output.startswith("[stub echo]") or "stub echo" in output:
        findings.append(
            _finding(
                observation,
                code="stub_echo",
                kind="contamination",
                summary="Stub echo output detected; this run is not valid model output.",
                evidence=[_clip(observation.output_text)],
                failure_category="tool_failure",
                recommended_action="repair",
            )
        )
    if _fallback_detected(observation, text=output):
        findings.append(
            _finding(
                observation,
                code="fallback",
                kind="contamination",
                summary="Fallback model path was used without authorization for this contract.",
                evidence=[observation.fallback_reason or "fallback path engaged"],
                failure_category="tool_failure",
                recommended_action="reconfigure_router",
            )
        )
    if _family_mismatch(observation):
        findings.append(
            _finding(
                observation,
                code="family_routing_mismatch",
                kind="contamination",
                summary="Requested model family does not match the family that actually ran.",
                evidence=[
                    f"requested={observation.requested_model_family}",
                    f"actual={observation.actual_model_family}",
                ],
                failure_category="tool_failure",
                recommended_action="reconfigure_router",
            )
        )
    return findings


def _detect_environment_blockers(observation: NuoObservation) -> list[NuoHealthFinding]:
    error_text = observation.error_text.lower()
    text = error_text
    isolated_workspace_fallback_succeeded = _isolated_workspace_output_fallback_succeeded(
        observation,
        text=f"{observation.error_text}\n{observation.output_text}".lower(),
    )
    findings: list[NuoHealthFinding] = []
    if (
        observation.timed_out
        or "timed out" in error_text
        or "timeout" in error_text
        or "deadline exceeded" in error_text
    ):
        findings.append(
            _finding(
                observation,
                code="timeout",
                kind="environment_blocker",
                summary="Runner timed out before producing a trustworthy result.",
                evidence=[_clip(observation.error_text or observation.output_text)],
                failure_category="environment_failure",
                recommended_action="rerun",
            )
        )
    if observation.network_eof or any(
        pattern in text
        for pattern in (
            "network eof",
            "unexpected eof",
            "tls handshake eof",
            "stream disconnected",
            "remote disconnected",
        )
    ):
        findings.append(
            _finding(
                observation,
                code="network_eof",
                kind="environment_blocker",
                summary="Network EOF interrupted the run; this is an environment blocker.",
                evidence=[_clip(observation.error_text or observation.output_text)],
                failure_category="environment_failure",
                recommended_action="rerun",
            )
        )
    if any(
        pattern in text
        for pattern in (
            "connection reset",
            "connection refused",
            "connect call failed",
            "network unreachable",
            "tls handshake timeout",
            "failed to connect to websocket",
            "http/request failed",
            "dns error",
            "transport error",
            "could not connect to server",
            "database is unavailable",
            "temporary failure in name resolution",
        )
    ):
        findings.append(
            _finding(
                observation,
                code="network_blocked",
                kind="environment_blocker",
                summary="Network transport failed before execution could be trusted.",
                evidence=[_clip(observation.error_text or observation.output_text)],
                failure_category="environment_failure",
                recommended_action="rerun",
            )
        )
    if observation.wrapper_missing or "wrapper missing" in text or "wrapper not found" in text:
        findings.append(
            _finding(
                observation,
                code="wrapper_missing",
                kind="environment_blocker",
                summary="Required wrapper is missing, so the run cannot prove KUN capability.",
                evidence=[_clip(observation.error_text or observation.output_text)],
                failure_category="environment_failure",
                recommended_action="fix_wrapper",
            )
        )
    if any(
        pattern in text
        for pattern in (
            "tool schema mismatch",
            "schema mismatch",
            "wrapper version",
            "contract mismatch",
            "unexpected argument",
            "unknown option",
            "interface changed",
        )
    ):
        findings.append(
            _finding(
                observation,
                code="wrapper_contract_mismatch",
                kind="environment_blocker",
                summary="Wrapper interface or schema changed, so the run is invalid.",
                evidence=[_clip(observation.error_text or observation.output_text)],
                failure_category="tool_failure",
                recommended_action="fix_wrapper",
            )
        )
    if (
        observation.auth_failure
        or "auth failure" in text
        or "authentication failed" in text
        or "unauthorized" in text
        or "invalid api key" in text
    ):
        findings.append(
            _finding(
                observation,
                code="auth_failure",
                kind="environment_blocker",
                summary="Authentication failed before execution could be trusted.",
                evidence=[_clip(observation.error_text or observation.output_text)],
                failure_category="permission_failure",
                recommended_action="fix_auth",
            )
        )
    if not isolated_workspace_fallback_succeeded and any(
        pattern in text
        for pattern in (
            "operation not permitted",
            "errno 1",
            "sc_sem_nsems_max",
            "processpoolexecutor",
            "multiprocessing.synchronize",
            "sem_open",
            "semaphore",
        )
    ):
        findings.append(
            _finding(
                observation,
                code="sandbox_permission_blocked",
                kind="environment_blocker",
                summary=(
                    "Sandbox or process-pool permissions blocked execution before the run "
                    "could be trusted."
                ),
                evidence=[_clip(observation.error_text or observation.output_text)],
                failure_category="environment_failure",
                recommended_action="fix_wrapper",
            )
        )
    if not isolated_workspace_fallback_succeeded and any(
        pattern in text for pattern in ("permission denied", "access denied", "forbidden")
    ):
        findings.append(
            _finding(
                observation,
                code="permission_denied",
                kind="environment_blocker",
                summary="Permission denied before execution could be trusted.",
                evidence=[_clip(observation.error_text or observation.output_text)],
                failure_category="permission_failure",
                recommended_action="fix_auth",
            )
        )
    return findings


def _isolated_workspace_output_fallback_succeeded(
    observation: NuoObservation,
    *,
    text: str,
) -> bool:
    """Do not block a successful isolated-output fallback as sandbox failure."""

    if observation.error_text.strip():
        return False
    refs = [
        *observation.artifact_refs,
        *observation.evidence_refs,
        *observation.test_refs,
        *observation.review_refs,
    ]
    if not any("local-evidence" in ref or "local_runtime_evidence" in ref for ref in refs):
        return False
    if not any(
        phrase in text
        for phrase in (
            "isolated workspace",
            "workspace output path",
            "workspace outputs",
            "inside the workspace",
            "under the workspace",
        )
    ):
        return False
    if not any(
        phrase in text
        for phrase in (
            "output directory used",
            "fresh unique output directory",
            "new unique output directory",
            "created a fresh unique output directory",
            "used a new unique directory",
        )
    ):
        return False
    return any(
        phrase in text
        for phrase in (
            "requested sibling output root",
            "outside requested output root",
            "sandbox-blocked",
            "sandbox blocked",
            "permission",
            "operation not permitted",
        )
    )


def _detect_artifact_gaps(observation: NuoObservation) -> list[NuoHealthFinding]:
    findings: list[NuoHealthFinding] = []
    if observation.report_required and not observation.report_ref:
        findings.append(
            _finding(
                observation,
                code="report_missing",
                kind="artifact_gap",
                summary="Required report artifact is missing.",
                failure_category="evidence_failure",
                recommended_action="collect_report",
            )
        )
    stronger_product_gap_exists = bool(observation.product_surface_gap_codes) or (
        observation.human_playtest_required and not observation.human_playtest_ref
    )
    if (
        observation.product_acceptance_claimed
        and not observation.rollback_refs
        and not stronger_product_gap_exists
    ):
        findings.append(
            _finding(
                observation,
                code="rollback_refs_missing",
                kind="artifact_gap",
                summary="Delivery claim is missing rollback references.",
                failure_category="evidence_failure",
                recommended_action="collect_report",
            )
        )
    if observation.expected_review_count > 0 and observation.review_count is None:
        findings.append(
            _finding(
                observation,
                code="review_count_missing",
                kind="artifact_gap",
                summary="Review count is missing, so the round cannot be validated.",
                evidence=[f"expected_review_count={observation.expected_review_count}"],
                failure_category="evidence_failure",
                recommended_action="collect_reviews",
            )
        )
    elif (
        observation.review_count is not None
        and observation.review_count < observation.expected_review_count
    ):
        findings.append(
            _finding(
                observation,
                code="review_count_insufficient",
                kind="artifact_gap",
                summary="Review count is below the contract minimum.",
                evidence=[
                    f"review_count={observation.review_count}",
                    f"expected_review_count={observation.expected_review_count}",
                ],
                failure_category="evidence_failure",
                recommended_action="collect_reviews",
            )
        )
    return findings


def _detect_governance_blockers(observation: NuoObservation) -> list[NuoHealthFinding]:
    if observation.comparator_healthy:
        return []
    return [
        _finding(
            observation,
            code="comparator_unhealthy",
            kind="governance_blocker",
            summary="Comparator is unhealthy; rankings and capability conclusions are invalid.",
            evidence=[observation.comparator_health_reason]
            if observation.comparator_health_reason
            else [],
            failure_category="tool_failure",
            recommended_action="repair_comparator",
        )
    ]


def _detect_product_delivery_gaps(observation: NuoObservation) -> list[NuoHealthFinding]:
    findings: list[NuoHealthFinding] = []
    if observation.product_acceptance_claimed and observation.product_surface_gap_codes:
        findings.append(
            _finding(
                observation,
                code="premature_delivery_claim",
                kind="product_gap",
                summary=(
                    "Product delivery was claimed while product-surface gaps remain; "
                    "mechanism-level gates cannot be treated as final user-value acceptance."
                ),
                evidence=[", ".join(observation.product_surface_gap_codes)],
                failure_category="delivery_failure",
                recommended_action="continue_iteration",
                counts_as_kun_failure=True,
            )
        )
    if observation.human_playtest_required and not observation.human_playtest_ref:
        findings.append(
            _finding(
                observation,
                code="subjective_playtest_missing",
                kind="product_gap",
                summary=(
                    "A subjective product-experience gate requires human or target-user "
                    "playtest evidence before final closure."
                ),
                failure_category="evidence_failure",
                recommended_action="request_human_playtest",
                counts_as_kun_failure=True,
            )
        )
    return findings


def _finding(
    observation: NuoObservation,
    *,
    code: NuoFindingCode,
    kind: NuoFindingKind,
    summary: str,
    failure_category: FailureCategory,
    recommended_action: NuoRecoveryAction,
    evidence: list[str] | None = None,
    counts_as_kun_failure: bool = False,
) -> NuoHealthFinding:
    return NuoHealthFinding(
        finding_id=f"nuo-{observation.subject_ref}-{code}",
        code=code,
        kind=kind,
        summary=summary,
        evidence=[item for item in evidence or [] if item],
        failure_category=failure_category,
        counts_as_kun_failure=counts_as_kun_failure,
        invalidates_round=True,
        recommended_action=recommended_action,
    )


def _fallback_detected(observation: NuoObservation, *, text: str) -> bool:
    if observation.fallback_authorized:
        return False
    return (
        observation.fallback_engaged
        or (
            observation.actual_model_tier == "fallback"
            and observation.requested_model_tier not in {None, "fallback"}
        )
        or "using fallback model" in text
        or "fallback model path" in text
    )


def _family_mismatch(observation: NuoObservation) -> bool:
    if not observation.requested_model_family or not observation.actual_model_family:
        return False
    return observation.requested_model_family != observation.actual_model_family


def _recovery_route(finding: NuoHealthFinding) -> tuple[NextAction, MissionStatus, str]:
    if finding.recommended_action in {"collect_report", "collect_reviews"}:
        return "needs_info", "info_gap", "qi"
    if finding.code in {"auth_failure", "permission_denied", "subjective_playtest_missing"}:
        return "needs_human", "waiting_human", "operator"
    if finding.code == "premature_delivery_claim":
        return "needs_plan_change", "changing_plan", "qi"
    return "needs_repair", "repairing", "control-plane"


def _recovery_work_item_type(action: NuoRecoveryAction) -> WorkItemType:
    if action in {"fix_auth", "request_human_playtest"}:
        return "collaboration"
    if action in {"collect_report", "collect_reviews"}:
        return "research"
    if action in {"pause", "continue_iteration"}:
        return "governance"
    return "repair"


def _recovery_expected_output(
    *,
    recommendation: NuoRecoveryRecommendation,
    codes: list[NuoFindingCode],
) -> str:
    joined_codes = ", ".join(codes)
    if recommendation.action == "rerun":
        return (
            "Rerun the same subject after the environment blocker is cleared; do not count "
            f"this as KUN capability failure. Nuo findings: {joined_codes}."
        )
    if recommendation.action == "reconfigure_router":
        return (
            "Fix routing/fallback configuration, then rerun the same subject before ranking. "
            f"Nuo findings: {joined_codes}."
        )
    if recommendation.action == "fix_wrapper":
        return (
            "Repair wrapper availability or contract compatibility, then rerun the same subject. "
            f"Nuo findings: {joined_codes}."
        )
    if recommendation.action == "fix_auth":
        return (
            "Request operator credential or permission repair through a collaboration ticket; "
            f"resume after approval. Nuo findings: {joined_codes}."
        )
    if recommendation.action == "collect_report":
        return (
            "Collect or regenerate the missing report artifact before any delivery or ranking. "
            f"Nuo findings: {joined_codes}."
        )
    if recommendation.action == "collect_reviews":
        return (
            "Collect the missing peer reviews before any delivery, ranking, or capability conclusion. "
            f"Nuo findings: {joined_codes}."
        )
    if recommendation.action == "repair_comparator":
        return (
            "Repair comparator health and rerun the same subject without counting agent failure. "
            f"Nuo findings: {joined_codes}."
        )
    if recommendation.action == "continue_iteration":
        return (
            "Open a stricter continuation branch before final closure: translate the product gaps "
            "into new acceptance criteria, seek a better path, and rerun delivery gates. "
            f"Nuo findings: {joined_codes}."
        )
    if recommendation.action == "request_human_playtest":
        return (
            "Open a human or target-user playtest/acceptance ticket with clear acceptance questions, "
            "deadline, fallback policy, and automatic resume path. "
            f"Nuo findings: {joined_codes}."
        )
    return f"Pause the subject until governance resolves Nuo findings: {joined_codes}."


def _slug(value: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")


def _clip(text: str, limit: int = 160) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[: limit - 3]}..."


def _sample(
    *,
    sample_id: str,
    description: str,
    expected_codes: list[NuoFindingCode],
    expected_recovery_action: NuoRecoveryAction,
    output_text: str = "valid answer",
    error_text: str = "",
    requested_model_family: str | None = "codex",
    actual_model_family: str | None = "codex",
    requested_model_tier: str | None = "top",
    actual_model_tier: str | None = "top",
    fallback_engaged: bool = False,
    fallback_reason: str = "",
    timed_out: bool = False,
    network_eof: bool = False,
    wrapper_missing: bool = False,
    auth_failure: bool = False,
    report_ref: str | None = "report-sample",
    review_count: int | None = 45,
    expected_review_count: int = 45,
    rollback_refs: list[str] | None = None,
    comparator_healthy: bool = True,
    comparator_health_reason: str = "",
    product_acceptance_claimed: bool = False,
    product_surface_gap_codes: list[str] | None = None,
    human_playtest_required: bool = False,
    human_playtest_ref: str | None = "human-playtest-sample",
    counts_as_kun_failure: bool = False,
) -> NuoPollutionSample:
    return NuoPollutionSample(
        sample_id=sample_id,
        description=description,
        observation=NuoObservation(
            mission_id="msn-nuo-sample-library",
            task_plan_version="v6",
            subject_ref=sample_id,
            task_type="self_improvement",
            output_text=output_text,
            error_text=error_text,
            requested_model_family=requested_model_family,
            actual_model_family=actual_model_family,
            requested_model_tier=requested_model_tier,
            actual_model_tier=actual_model_tier,
            fallback_engaged=fallback_engaged,
            fallback_reason=fallback_reason,
            timed_out=timed_out,
            network_eof=network_eof,
            wrapper_missing=wrapper_missing,
            auth_failure=auth_failure,
            report_ref=report_ref,
            review_count=review_count,
            expected_review_count=expected_review_count,
            rollback_refs=rollback_refs or ["rollback-sample"],
            comparator_healthy=comparator_healthy,
            comparator_health_reason=comparator_health_reason,
            product_acceptance_claimed=product_acceptance_claimed,
            product_surface_gap_codes=product_surface_gap_codes or [],
            human_playtest_required=human_playtest_required,
            human_playtest_ref=human_playtest_ref,
        ),
        expected_codes=expected_codes,
        expected_recovery_action=expected_recovery_action,
        counts_as_kun_failure=counts_as_kun_failure,
    )


__all__ = [
    "Finding",
    "HealthReport",
    "NuoFindingCode",
    "NuoFindingKind",
    "NuoHealthFinding",
    "NuoHealthReport",
    "NuoHealthStatus",
    "NuoObservation",
    "NuoPollutionSample",
    "NuoRecoveryAction",
    "NuoRecoveryPlan",
    "NuoRecoveryRecommendation",
    "NuoSeverity",
    "build_nuo_pollution_sample_library",
    "build_nuo_recovery_plan",
    "diagnose_nuo_health",
]
