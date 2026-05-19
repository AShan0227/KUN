"""Minimal executable V6 control-plane runtime.

This module turns the V6 product contract into a deterministic runtime surface:
missions can be submitted, queued work can run through a supervisor loop, every
state movement is ledgered, and gates decide the next mission state.  Durable DB,
process isolation, and external AB wiring can sit behind this interface later.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.collaboration import CollaborationResponse
from kun.control_plane.store import ControlPlaneStore
from kun.control_plane.v6 import (
    AcceptanceReview,
    ArtifactManifest,
    ArtifactRecord,
    CapabilityProfile,
    CollaborationTicket,
    ExecutionContract,
    FailureCategory,
    GateEvaluation,
    LedgerEvent,
    LedgerEventType,
    Mission,
    MissionStatus,
    RunRecord,
    TaskPlan,
    WorkingContext,
    WorkItem,
    WorkItemStatus,
    assert_transition_allowed,
    default_recovery_for_failure,
    validate_workitem_dag,
)

if TYPE_CHECKING:
    from kun.control_plane.capability_evolution import (
        CapabilityPromotion,
        CapabilityPromotionStage,
        CapabilityRollback,
    )

RunnerType = Literal["model", "tool", "agent", "command", "human", "external_worker"]
RunExitStatus = Literal["running", "succeeded", "failed", "cancelled"]


def _now() -> datetime:
    return datetime.now(UTC)


def _failure_still_blocks_progress(
    status: MissionStatus,
    latest_gate: GateEvaluation | None,
) -> bool:
    if latest_gate and latest_gate.north_star_verdict == "pass":
        return False
    return status in {
        "blocked",
        "retrying",
        "repairing",
        "rolling_back",
        "changing_plan",
        "failed",
    }


class WorkItemResult(BaseModel):
    """Normalized runner output accepted by the V6 control plane."""

    model_config = ConfigDict(extra="forbid")

    status: WorkItemStatus
    summary: str = ""
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    artifact_manifest: ArtifactManifest | None = None
    gate_evaluation: GateEvaluation | None = None
    failure_category: FailureCategory | None = None
    collaboration_tickets: list[CollaborationTicket] = Field(default_factory=list)
    followup_work_items: list[WorkItem] = Field(default_factory=list)


class ControlPlaneRunner(Protocol):
    """Runner boundary for model/tool/agent/process workers."""

    @property
    def runner_type(self) -> RunnerType:
        """Kind of runner behind this boundary."""
        ...

    @property
    def runner_identity(self) -> str:
        """Stable identity recorded into RunRecord."""
        ...

    def run(self, work_item: WorkItem) -> WorkItemResult:
        """Execute one work item and return normalized output."""


class ControlPlaneProgressReport(BaseModel):
    """Operator-facing summary for long-running mission progress."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    status: MissionStatus
    current_plan_version: str | None
    total_work_items: int
    work_item_counts: dict[str, int]
    open_collaboration_ticket_ids: list[str] = Field(default_factory=list)
    latest_gate_ref: str | None = None
    latest_gate_action: str | None = None
    latest_gate_verdict: str | None = None
    latest_failure_category: FailureCategory | None = None
    next_ready_work_item_ids: list[str] = Field(default_factory=list)
    ledger_event_count: int = 0
    artifact_manifest_count: int = 0


class InMemoryControlPlane:
    """Strict V6 runtime protocol with optional store-backed recovery."""

    def __init__(self, *, store: ControlPlaneStore | None = None) -> None:
        self.store = store
        self.missions: dict[str, Mission] = {}
        self.task_plans: dict[str, TaskPlan] = {}
        self.contracts: dict[str, ExecutionContract] = {}
        self.working_contexts: dict[str, WorkingContext] = {}
        self.work_items: dict[str, WorkItem] = {}
        self.runs: dict[str, RunRecord] = {}
        self.artifacts: dict[str, ArtifactRecord] = {}
        self.artifact_manifests: dict[str, ArtifactManifest] = {}
        self.ledger_events: dict[str, LedgerEvent] = {}
        self.collaboration_tickets: dict[str, CollaborationTicket] = {}
        self.gate_evaluations: dict[str, GateEvaluation] = {}
        self.acceptance_reviews: dict[str, AcceptanceReview] = {}
        self.capability_profiles: dict[str, CapabilityProfile] = {}
        self._ledger_sequences: Counter[str] = Counter()
        if self.store is not None:
            self._hydrate_from_store(self.store)

    def submit_mission(
        self,
        *,
        mission: Mission,
        task_plan: TaskPlan,
        execution_contract: ExecutionContract,
        working_context: WorkingContext,
        work_items: list[WorkItem],
        actor: str = "kun",
    ) -> Mission:
        """Register a fully contracted mission and queue its work items."""

        if mission.mission_id != task_plan.mission_id:
            raise ValueError("mission and task_plan mission_id mismatch")
        if mission.mission_id != execution_contract.mission_id:
            raise ValueError("mission and execution_contract mission_id mismatch")
        if mission.mission_id != working_context.mission_id:
            raise ValueError("mission and working_context mission_id mismatch")
        if execution_contract.task_plan_version != task_plan.version:
            raise ValueError("execution_contract task_plan_version mismatch")
        if working_context.task_plan_version != task_plan.version:
            raise ValueError("working_context task_plan_version mismatch")
        if mission.status != "contracted":
            raise ValueError("submit_mission requires a contracted mission")
        assert_transition_allowed(mission.status, "queued")
        if not task_plan.can_contract:
            raise ValueError("task_plan must be approved and have no info_gaps before execution")
        validate_workitem_dag(work_items)
        for item in work_items:
            if item.mission_id != mission.mission_id:
                raise ValueError(f"work item {item.work_item_id} mission_id mismatch")
            if item.task_plan_version != task_plan.version:
                raise ValueError(f"work item {item.work_item_id} task_plan_version mismatch")

        queued_mission = mission.model_copy(
            update={
                "status": "queued",
                "current_plan_version": task_plan.version,
                "execution_contract_ref": execution_contract.contract_id,
                "working_context_ref": working_context.working_context_id,
            }
        )
        self.missions[queued_mission.mission_id] = queued_mission
        self.task_plans[task_plan.plan_id] = task_plan
        self.contracts[execution_contract.contract_id] = execution_contract
        self.working_contexts[working_context.working_context_id] = working_context
        self.work_items.update({item.work_item_id: item for item in work_items})
        self._persist_task_plan(task_plan)
        self._persist_contract(execution_contract)
        self._persist_working_context(working_context)
        for item in work_items:
            self._persist_work_item(item)
        self._record_ledger_event(
            mission_id=queued_mission.mission_id,
            event_type="state_change",
            actor=actor,
            subject_ref=queued_mission.mission_id,
            before={"status": mission.status},
            after={"status": queued_mission.status},
            payload={"reason": "mission submitted with approved task plan"},
        )
        return self.missions[queued_mission.mission_id]

    def transition_mission(
        self,
        *,
        mission_id: str,
        target: MissionStatus,
        actor: str,
        reason: str,
        subject_ref: str | None = None,
    ) -> Mission:
        """Move a mission through the V6 state machine and record the move."""

        mission = self._mission(mission_id)
        assert_transition_allowed(mission.status, target)
        updated = mission.model_copy(update={"status": target})
        self.missions[mission_id] = updated
        self._persist_mission(updated)
        self._record_ledger_event(
            mission_id=mission_id,
            event_type="state_change",
            actor=actor,
            subject_ref=subject_ref or mission_id,
            before={"status": mission.status},
            after={"status": target},
            payload={"reason": reason},
        )
        return updated

    def next_ready_work_item(self, mission_id: str) -> WorkItem | None:
        """Return the highest-priority queued item whose dependencies are done."""

        self._mission(mission_id)
        done_ids = {
            item.work_item_id
            for item in self._mission_work_items(mission_id)
            if item.status == "done"
        }
        ready = [
            item
            for item in self._mission_work_items(mission_id)
            if item.status == "queued" and set(item.dependencies).issubset(done_ids)
        ]
        return max(ready, key=lambda item: (item.priority, item.work_item_id), default=None)

    def run_next_ready(self, *, mission_id: str, runner: ControlPlaneRunner) -> RunRecord | None:
        """Run one ready work item and apply its normalized result."""

        work_item = self.next_ready_work_item(mission_id)
        if work_item is None:
            return None
        mission = self._mission(mission_id)
        if mission.status == "queued":
            self.transition_mission(
                mission_id=mission_id,
                target="running",
                actor="control-plane",
                reason="ready work item acquired",
                subject_ref=work_item.work_item_id,
            )
        elif mission.status != "running":
            assert_transition_allowed(mission.status, "running")
            self.transition_mission(
                mission_id=mission_id,
                target="running",
                actor="control-plane",
                reason="resume ready work item",
                subject_ref=work_item.work_item_id,
            )

        started = _now()
        running_item = work_item.model_copy(update={"status": "running", "heartbeat": started})
        self.work_items[work_item.work_item_id] = running_item
        self._persist_work_item(running_item)
        run = RunRecord(
            work_item_id=running_item.work_item_id,
            runner_type=runner.runner_type,
            runner_identity=runner.runner_identity,
            started_at=started,
        )
        self.runs[run.run_id] = run
        self._persist_run(run)
        self._record_ledger_event(
            mission_id=mission_id,
            event_type="state_change",
            actor="control-plane",
            subject_ref=running_item.work_item_id,
            before={"status": work_item.status},
            after={"status": "running"},
            payload={"run_id": run.run_id},
        )

        try:
            result = runner.run(running_item)
        except Exception:  # pragma: no cover - deterministic tests cover normalized failure.
            result = WorkItemResult(status="failed", failure_category="tool_failure")
        return self.apply_work_item_result(run_id=run.run_id, result=result)

    def apply_work_item_result(self, *, run_id: str, result: WorkItemResult) -> RunRecord:
        """Persist runner output, apply gate decisions, and update mission state."""

        run = self.runs[run_id]
        work_item = self.work_items[run.work_item_id]
        mission = self._mission(work_item.mission_id)
        for artifact in result.artifacts:
            self.artifacts[artifact.artifact_id] = artifact
            self._persist_artifact(artifact)
        manifest_ref = None
        if result.artifact_manifest is not None:
            manifest_ref = result.artifact_manifest.manifest_id
            self.artifact_manifests[manifest_ref] = result.artifact_manifest
            self._persist_artifact_manifest(result.artifact_manifest)
            mission = mission.model_copy(
                update={
                    "artifact_manifest_refs": [
                        *mission.artifact_manifest_refs,
                        result.artifact_manifest.manifest_id,
                    ]
                }
            )
            self.missions[mission.mission_id] = mission
            self._persist_mission(mission)
        for ticket in result.collaboration_tickets:
            self.collaboration_tickets[ticket.ticket_id] = ticket
            self._persist_collaboration_ticket(ticket)
        for followup in result.followup_work_items:
            if followup.mission_id != work_item.mission_id:
                raise ValueError(f"followup work item {followup.work_item_id} mission_id mismatch")
            self.work_items[followup.work_item_id] = followup
            self._persist_work_item(followup)

        gate_ref = None
        if result.gate_evaluation is not None:
            gate_ref = result.gate_evaluation.gate_evaluation_id
            self.gate_evaluations[gate_ref] = result.gate_evaluation
            self._persist_gate(result.gate_evaluation)

        exit_status: RunExitStatus = (
            "succeeded" if result.status in {"done", "partial"} else "failed"
        )
        if result.status in {"waiting_human", "waiting_external", "blocked"}:
            exit_status = "cancelled"
        updated_run = RunRecord.model_validate(
            {
                **run.model_dump(),
                "ended_at": _now(),
                "exit_status": exit_status,
                "failure_category": result.failure_category,
                "artifact_manifest_ref": manifest_ref,
                "gate_evaluation_ref": gate_ref,
            }
        )
        self.runs[run_id] = updated_run
        self._persist_run(updated_run)
        updated_item = work_item.model_copy(
            update={
                "status": result.status,
                "artifact_manifest_ref": manifest_ref or work_item.artifact_manifest_ref,
                "heartbeat": _now(),
            }
        )
        self.work_items[work_item.work_item_id] = updated_item
        self._persist_work_item(updated_item)
        self._record_ledger_event(
            mission_id=work_item.mission_id,
            event_type="state_change",
            actor="control-plane",
            subject_ref=work_item.work_item_id,
            before={"status": work_item.status},
            after={"status": result.status},
            payload={"run_id": run_id, "summary": result.summary},
            artifact_refs=[artifact.artifact_id for artifact in result.artifacts],
        )

        if result.gate_evaluation is not None:
            self.apply_gate(result.gate_evaluation)
        elif result.failure_category is not None:
            recovery = default_recovery_for_failure(result.failure_category)
            self.transition_mission(
                mission_id=work_item.mission_id,
                target=recovery.next_state,
                actor="control-plane",
                reason=f"recovered from {result.failure_category}",
                subject_ref=work_item.work_item_id,
            )
        elif result.status == "waiting_human":
            self.transition_mission(
                mission_id=work_item.mission_id,
                target="waiting_human",
                actor="control-plane",
                reason="work item requires human collaboration",
                subject_ref=work_item.work_item_id,
            )
        elif result.status == "waiting_external":
            self.transition_mission(
                mission_id=work_item.mission_id,
                target="waiting_external",
                actor="control-plane",
                reason="work item requires external dependency",
                subject_ref=work_item.work_item_id,
            )
        elif result.status == "blocked":
            self.transition_mission(
                mission_id=work_item.mission_id,
                target="blocked",
                actor="control-plane",
                reason="work item blocked without runnable fallback",
                subject_ref=work_item.work_item_id,
            )
        return updated_run

    def record_collaboration_ticket(
        self,
        ticket: CollaborationTicket,
        *,
        actor: str = "control-plane",
    ) -> CollaborationTicket:
        """Persist one human/external collaboration request and ledger it."""

        self._mission(ticket.mission_id)
        if ticket.ticket_id in self.collaboration_tickets:
            raise ValueError(f"collaboration ticket already exists: {ticket.ticket_id}")
        self.collaboration_tickets[ticket.ticket_id] = ticket
        self._persist_collaboration_ticket(ticket)
        self._record_ledger_event(
            mission_id=ticket.mission_id,
            event_type="message",
            actor=actor,
            subject_ref=ticket.ticket_id,
            payload={
                "sender": actor,
                "receiver": ticket.role_needed,
                "intent": ticket.type,
                "requires_response": ticket.resume_after_response,
                "deadline": ticket.deadline.isoformat(),
                "resume_rule": "resume_after_response"
                if ticket.resume_after_response
                else "record_only",
                "why_needed": ticket.why_needed,
                "recommended_option": ticket.recommended_option or "",
            },
        )
        return ticket

    def list_collaboration_tickets(self, mission_id: str) -> list[CollaborationTicket]:
        """List mission-scoped collaboration tickets for UI and operators."""

        self._mission(mission_id)
        return sorted(
            (
                ticket
                for ticket in self.collaboration_tickets.values()
                if ticket.mission_id == mission_id
            ),
            key=lambda ticket: ticket.ticket_id,
        )

    def get_collaboration_ticket(self, ticket_id: str) -> CollaborationTicket:
        """Return one collaboration ticket or raise a stable runtime error."""

        try:
            return self.collaboration_tickets[ticket_id]
        except KeyError as exc:
            raise ValueError(f"unknown collaboration ticket {ticket_id}") from exc

    def record_collaboration_response(
        self,
        response: CollaborationResponse,
        *,
        actor: str = "control-plane",
    ) -> CollaborationTicket:
        """Record a human/external answer and resume the mission when possible."""

        ticket = self.get_collaboration_ticket(response.ticket_id)
        if ticket.status in {"cancelled", "closed"}:
            raise ValueError(f"ticket {ticket.ticket_id} is already {ticket.status}")
        if (
            response.selected_option
            and ticket.decision_options
            and response.selected_option not in ticket.decision_options
        ):
            raise ValueError("selected_option is not in decision_options")
        if response.status == "answered" and not (response.answer or response.selected_option):
            raise ValueError("answered collaboration response needs answer or selected_option")

        updated = ticket.model_copy(update={"status": response.status})
        self.collaboration_tickets[ticket.ticket_id] = updated
        self._persist_collaboration_ticket(updated)
        self._record_ledger_event(
            mission_id=ticket.mission_id,
            event_type="message",
            actor=actor,
            subject_ref=ticket.ticket_id,
            payload={
                "sender": response.responder,
                "receiver": "kun",
                "intent": f"collaboration_{response.status}",
                "requires_response": False,
                "deadline": response.received_at.isoformat(),
                "resume_rule": "resume" if response.resume_allowed else "do_not_resume",
                "selected_option": response.selected_option or "",
                "answer": response.answer,
            },
        )
        if response.resume_allowed and ticket.resume_after_response:
            self._resume_after_collaboration(ticket.mission_id)
        return updated

    def apply_gate(self, gate: GateEvaluation) -> Mission:
        """Apply a unified V6 gate to mission state."""

        mission = self._mission(gate.mission_id)
        self.gate_evaluations[gate.gate_evaluation_id] = gate
        self._persist_gate(gate)
        if mission.status != gate.next_state:
            mission = self.transition_mission(
                mission_id=gate.mission_id,
                target=gate.next_state,
                actor=gate.created_by,
                reason=f"gate {gate.stage}:{gate.next_action}",
                subject_ref=gate.subject_ref,
            )
        self._record_ledger_event(
            mission_id=gate.mission_id,
            event_type="gate_evaluation",
            actor=gate.created_by,
            subject_ref=gate.subject_ref,
            payload={
                "gate_evaluation_id": gate.gate_evaluation_id,
                "stage": gate.stage,
                "next_action": gate.next_action,
                "north_star_verdict": gate.north_star_verdict,
                "result_quality": gate.result_quality,
            },
            artifact_refs=gate.artifact_refs,
        )
        return mission

    def record_acceptance_review(self, review: AcceptanceReview, *, actor: str = "kun") -> None:
        """Persist user/operator acceptance and advance to the appropriate state."""

        mission = self._mission(review.mission_id)
        self.acceptance_reviews[review.acceptance_id] = review
        self._persist_acceptance_review(review)
        target: MissionStatus
        if review.decision == "accepted":
            target = "learning_writeback"
        elif review.decision == "partial_accepted":
            target = "partial_closed"
        else:
            target = "repairing"
        if mission.status != target:
            self.transition_mission(
                mission_id=review.mission_id,
                target=target,
                actor=actor,
                reason=f"acceptance review {review.decision}",
                subject_ref=review.acceptance_id,
            )
        mission = self._mission(review.mission_id)
        mission = mission.model_copy(update={"acceptance_ref": review.acceptance_id})
        self.missions[review.mission_id] = mission
        self._persist_mission(mission)
        self._record_ledger_event(
            mission_id=review.mission_id,
            event_type="acceptance",
            actor=actor,
            subject_ref=review.acceptance_id,
            payload={"decision": review.decision, "satisfaction": review.satisfaction},
        )

    def apply_capability_promotion(
        self,
        promotion: CapabilityPromotion,
        *,
        actor: str = "qi",
    ) -> CapabilityProfile | None:
        """Persist a Qi promotion decision without auto-loading non-production abilities.

        Approved replay/holdout/shadow/canary profiles are stored as evidence and
        rollback material, but ``list_default_runtime_capabilities`` is the only
        default KUN Runtime consumption path and exposes production profiles only.
        """

        gate = promotion.gate_evaluation
        self.gate_evaluations[gate.gate_evaluation_id] = gate
        self._persist_gate(gate)
        profile = promotion.capability_profile
        if promotion.decision == "approved" and profile is not None:
            self.capability_profiles[profile.capability_id] = profile
            self._persist_capability_profile(profile)
        self._record_ledger_event(
            mission_id=gate.mission_id,
            event_type="promotion",
            actor=actor,
            subject_ref=promotion.candidate_id,
            payload={
                "promotion_id": promotion.promotion_id,
                "candidate_id": promotion.candidate_id,
                "target_stage": promotion.target_stage,
                "decision": promotion.decision,
                "reason": promotion.reason,
                "capability_profile_ref": profile.capability_id if profile else "",
                "default_runtime_enabled": bool(
                    profile is not None and profile.promotion_stage == "production"
                ),
            },
            artifact_refs=promotion.evidence_refs,
        )
        return profile

    def list_default_runtime_capabilities(self) -> list[CapabilityProfile]:
        """Return only production-stage capabilities for KUN Runtime default use."""

        return [
            profile
            for profile in self.list_capability_profiles(stage="production")
            if profile.runtime_enabled
        ]

    def apply_capability_rollback(
        self,
        rollback: CapabilityRollback,
        *,
        actor: str = "qi",
    ) -> CapabilityProfile:
        """Disable a production/canary runtime ability and ledger the rollback."""

        profile = self.capability_profiles.get(rollback.capability_id)
        if profile is None:
            raise ValueError(f"capability profile not found: {rollback.capability_id}")
        gate = rollback.gate_evaluation
        self.gate_evaluations[gate.gate_evaluation_id] = gate
        self._persist_gate(gate)
        rolled_back = profile.model_copy(
            update={
                "runtime_enabled": False,
                "rolled_back_at": _now(),
                "rollback_reason": rollback.reason,
                "rollback_refs": [
                    rollback.rollback_id,
                    rollback.failed_evaluation_ref,
                    gate.gate_evaluation_id,
                ],
            }
        )
        self.capability_profiles[rolled_back.capability_id] = rolled_back
        self._persist_capability_profile(rolled_back)
        self._record_ledger_event(
            mission_id=gate.mission_id,
            event_type="rollback",
            actor=actor,
            subject_ref=rollback.capability_id,
            before={
                "runtime_enabled": profile.runtime_enabled,
                "promotion_stage": profile.promotion_stage,
            },
            after={
                "runtime_enabled": rolled_back.runtime_enabled,
                "promotion_stage": rolled_back.promotion_stage,
            },
            payload={
                "rollback_id": rollback.rollback_id,
                "capability_id": rollback.capability_id,
                "reason": rollback.reason,
                "failed_evaluation_ref": rollback.failed_evaluation_ref,
                "default_runtime_enabled": False,
            },
            artifact_refs=rollback.evidence_refs,
        )
        return rolled_back

    def list_capability_profiles(
        self,
        *,
        stage: CapabilityPromotionStage | None = None,
    ) -> list[CapabilityProfile]:
        """List stored capability profiles, optionally filtered by promotion stage."""

        profiles = list(self.capability_profiles.values())
        if stage is not None:
            profiles = [profile for profile in profiles if profile.promotion_stage == stage]
        return sorted(profiles, key=lambda profile: profile.capability_id)

    def progress_report(self, mission_id: str) -> ControlPlaneProgressReport:
        """Build a compact mission progress report from runtime state."""

        mission = self._mission(mission_id)
        items = self._mission_work_items(mission_id)
        counts = Counter(item.status for item in items)
        latest_gate = self._latest_gate(mission_id)
        latest_failed_run = self._latest_failed_run(mission_id)
        latest_failure_category = (
            latest_gate.failure_category
            if latest_gate and latest_gate.failure_category
            else latest_failed_run.failure_category
            if latest_failed_run and _failure_still_blocks_progress(mission.status, latest_gate)
            else None
        )
        return ControlPlaneProgressReport(
            mission_id=mission_id,
            status=mission.status,
            current_plan_version=mission.current_plan_version,
            total_work_items=len(items),
            work_item_counts={key: counts[key] for key in sorted(counts)},
            open_collaboration_ticket_ids=[
                ticket.ticket_id
                for ticket in self.collaboration_tickets.values()
                if ticket.mission_id == mission_id
                and ticket.status in {"open", "waiting", "escalated"}
            ],
            latest_gate_ref=latest_gate.gate_evaluation_id if latest_gate else None,
            latest_gate_action=latest_gate.next_action if latest_gate else None,
            latest_gate_verdict=latest_gate.north_star_verdict if latest_gate else None,
            latest_failure_category=latest_failure_category,
            next_ready_work_item_ids=[
                item.work_item_id for item in self._ready_work_items(mission_id)
            ],
            ledger_event_count=sum(
                1 for event in self.ledger_events.values() if event.mission_id == mission_id
            ),
            artifact_manifest_count=sum(
                1
                for manifest in self.artifact_manifests.values()
                if manifest.mission_id == mission_id
            ),
        )

    def _resume_after_collaboration(self, mission_id: str) -> None:
        open_tickets = [
            ticket
            for ticket in self.collaboration_tickets.values()
            if ticket.mission_id == mission_id and ticket.status in {"open", "waiting", "escalated"}
        ]
        if open_tickets:
            return
        for item in self._mission_work_items(mission_id):
            if item.status in {"waiting_human", "waiting_external"}:
                resumed = item.model_copy(update={"status": "queued"})
                self.work_items[item.work_item_id] = resumed
                self._persist_work_item(resumed)
        mission = self._mission(mission_id)
        if mission.status in {"waiting_human", "waiting_external", "escalated"}:
            self.transition_mission(
                mission_id=mission_id,
                target="queued",
                actor="control-plane",
                reason="collaboration response recorded; mission can resume",
            )

    def _mission(self, mission_id: str) -> Mission:
        try:
            return self.missions[mission_id]
        except KeyError as exc:
            raise ValueError(f"unknown mission {mission_id}") from exc

    def _mission_work_items(self, mission_id: str) -> list[WorkItem]:
        return [item for item in self.work_items.values() if item.mission_id == mission_id]

    def _ready_work_items(self, mission_id: str) -> list[WorkItem]:
        done_ids = {
            item.work_item_id
            for item in self._mission_work_items(mission_id)
            if item.status == "done"
        }
        return sorted(
            (
                item
                for item in self._mission_work_items(mission_id)
                if item.status == "queued" and set(item.dependencies).issubset(done_ids)
            ),
            key=lambda item: (-item.priority, item.work_item_id),
        )

    def _latest_gate(self, mission_id: str) -> GateEvaluation | None:
        gates = {
            gate.gate_evaluation_id: gate
            for gate in self.gate_evaluations.values()
            if gate.mission_id == mission_id
        }
        gate_events = [
            event
            for event in self.ledger_events.values()
            if event.mission_id == mission_id
            and event.event_type == "gate_evaluation"
            and event.payload.get("gate_evaluation_id") in gates
        ]
        latest_event = max(gate_events, key=lambda event: event.sequence, default=None)
        if latest_event is not None:
            return gates[str(latest_event.payload["gate_evaluation_id"])]
        return max(gates.values(), key=lambda gate: gate.gate_evaluation_id, default=None)

    def _latest_failed_run(self, mission_id: str) -> RunRecord | None:
        work_item_ids = {item.work_item_id for item in self._mission_work_items(mission_id)}
        runs = [
            run
            for run in self.runs.values()
            if run.work_item_id in work_item_ids and run.exit_status == "failed"
        ]
        return max(runs, key=lambda run: run.run_id, default=None)

    def _record_ledger_event(
        self,
        *,
        mission_id: str,
        event_type: LedgerEventType,
        actor: str,
        subject_ref: str,
        before: dict[str, object] | None = None,
        after: dict[str, object] | None = None,
        payload: dict[str, object] | None = None,
        artifact_refs: Iterable[str] = (),
    ) -> LedgerEvent:
        self._ledger_sequences[mission_id] += 1
        event = LedgerEvent(
            mission_id=mission_id,
            sequence=self._ledger_sequences[mission_id],
            event_type=event_type,
            actor=actor,
            correlation_id=mission_id,
            subject_ref=subject_ref,
            before=before or {},
            after=after or {},
            payload=payload or {},
            artifact_refs=list(artifact_refs),
            idempotency_key=f"{mission_id}:{self._ledger_sequences[mission_id]}:{event_type}",
        )
        self.ledger_events[event.event_id] = event
        self._persist_ledger_event(event)
        mission = self.missions.get(mission_id)
        if mission is not None:
            self.missions[mission_id] = mission.model_copy(
                update={"ledger_refs": [*mission.ledger_refs, event.event_id]}
            )
            self._persist_mission(self.missions[mission_id])
        return event

    def _hydrate_from_store(self, store: ControlPlaneStore) -> None:
        self.missions = {item.mission_id: item for item in store.list_missions()}
        self.task_plans = {item.plan_id: item for item in store.list_task_plans()}
        self.contracts = {item.contract_id: item for item in store.list_execution_contracts()}
        self.working_contexts = {
            item.working_context_id: item for item in store.list_working_contexts()
        }
        self.work_items = {item.work_item_id: item for item in store.list_work_items()}
        self.runs = {item.run_id: item for item in store.list_run_records()}
        self.artifacts = {item.artifact_id: item for item in store.list_artifact_records()}
        self.artifact_manifests = {
            item.manifest_id: item for item in store.list_artifact_manifests()
        }
        self.ledger_events = {item.event_id: item for item in store.list_ledger_events()}
        self.collaboration_tickets = {
            item.ticket_id: item for item in store.list_collaboration_tickets()
        }
        self.gate_evaluations = {
            item.gate_evaluation_id: item for item in store.list_gate_evaluations()
        }
        self.acceptance_reviews = {
            item.acceptance_id: item for item in store.list_acceptance_reviews()
        }
        self.capability_profiles = {
            item.capability_id: item for item in store.list_capability_profiles()
        }
        self._ledger_sequences = Counter(
            {
                mission_id: max(
                    (
                        event.sequence
                        for event in self.ledger_events.values()
                        if event.mission_id == mission_id
                    ),
                    default=0,
                )
                for mission_id in self.missions
            }
        )

    def _persist_mission(self, mission: Mission) -> None:
        if self.store is not None:
            self.store.put_mission(mission)

    def _persist_task_plan(self, task_plan: TaskPlan) -> None:
        if self.store is not None:
            self.store.put_task_plan(task_plan)

    def _persist_contract(self, contract: ExecutionContract) -> None:
        if self.store is not None:
            self.store.put_execution_contract(contract)

    def _persist_working_context(self, context: WorkingContext) -> None:
        if self.store is not None:
            self.store.put_working_context(context)

    def _persist_work_item(self, work_item: WorkItem) -> None:
        if self.store is not None:
            self.store.put_work_item(work_item)

    def _persist_run(self, run: RunRecord) -> None:
        if self.store is not None:
            self.store.put_run_record(run)

    def _persist_artifact(self, artifact: ArtifactRecord) -> None:
        if self.store is not None:
            self.store.put_artifact_record(artifact)

    def _persist_artifact_manifest(self, manifest: ArtifactManifest) -> None:
        if self.store is not None:
            self.store.put_artifact_manifest(manifest)

    def _persist_ledger_event(self, event: LedgerEvent) -> None:
        if self.store is not None:
            self.store.put_ledger_event(event)

    def _persist_collaboration_ticket(self, ticket: CollaborationTicket) -> None:
        if self.store is not None:
            self.store.put_collaboration_ticket(ticket)

    def _persist_gate(self, gate: GateEvaluation) -> None:
        if self.store is not None:
            self.store.put_gate_evaluation(gate)

    def _persist_acceptance_review(self, review: AcceptanceReview) -> None:
        if self.store is not None:
            self.store.put_acceptance_review(review)

    def _persist_capability_profile(self, profile: CapabilityProfile) -> None:
        if self.store is not None:
            self.store.put_capability_profile(profile)
