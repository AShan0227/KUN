"""Store boundary for KUN V6 control-plane records.

The first implementation is intentionally pure memory.  It defines the durable
API that a later SQL/ORM store should preserve: every V6 record has put/get/list
semantics, mission-scoped lists where the record model carries mission context,
and idempotent upsert for retryable records.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel

from kun.control_plane.v6 import (
    AcceptanceReview,
    ArtifactManifest,
    ArtifactRecord,
    CapabilityProfile,
    CollaborationTicket,
    ExecutionContract,
    GateEvaluation,
    LedgerEvent,
    Mission,
    RunRecord,
    TaskPlan,
    WorkingContext,
    WorkItem,
)


def _copy_record[RecordT: BaseModel](record: RecordT) -> RecordT:
    return record.model_copy(deep=True)


class _RecordBucket[RecordT: BaseModel]:
    def __init__(
        self,
        record_id: Callable[[RecordT], str],
        *,
        mission_id: Callable[[RecordT], str | None] | None = None,
        idempotency_key: Callable[[RecordT], str | None] | None = None,
    ) -> None:
        self._record_id = record_id
        self._mission_id = mission_id
        self._idempotency_key = idempotency_key
        self._records: dict[str, RecordT] = {}
        self._idempotency_index: dict[tuple[str | None, str], str] = {}

    def put(self, record: RecordT) -> RecordT:
        stored = _copy_record(record)
        record_id = self._record_id(stored)
        idempotency_key = self._get_idempotency_key(stored)
        if idempotency_key:
            index_key = (self._get_mission_id(stored), idempotency_key)
            existing_id = self._idempotency_index.get(index_key)
            if existing_id is not None and existing_id != record_id:
                return _copy_record(self._records[existing_id])

        existing = self._records.get(record_id)
        if existing is not None:
            self._drop_idempotency_index(record_id, existing)

        self._records[record_id] = stored
        if idempotency_key:
            self._idempotency_index[(self._get_mission_id(stored), idempotency_key)] = record_id
        return _copy_record(stored)

    def get(self, record_id: str) -> RecordT | None:
        record = self._records.get(record_id)
        if record is None:
            return None
        return _copy_record(record)

    def list(self, *, mission_id: str | None = None) -> list[RecordT]:
        return [
            _copy_record(record)
            for record in self._records.values()
            if mission_id is None or self._get_mission_id(record) == mission_id
        ]

    def _get_mission_id(self, record: RecordT) -> str | None:
        if self._mission_id is None:
            return None
        return self._mission_id(record)

    def _get_idempotency_key(self, record: RecordT) -> str | None:
        if self._idempotency_key is None:
            return None
        return self._idempotency_key(record)

    def _drop_idempotency_index(self, record_id: str, record: RecordT) -> None:
        idempotency_key = self._get_idempotency_key(record)
        if not idempotency_key:
            return
        index_key = (self._get_mission_id(record), idempotency_key)
        if self._idempotency_index.get(index_key) == record_id:
            del self._idempotency_index[index_key]


class ControlPlaneStore(Protocol):
    """Persistence boundary for V6 control-plane state."""

    def put_mission(self, mission: Mission) -> Mission: ...
    def get_mission(self, mission_id: str) -> Mission | None: ...
    def list_missions(self) -> list[Mission]: ...

    def put_task_plan(self, task_plan: TaskPlan) -> TaskPlan: ...
    def get_task_plan(self, plan_id: str) -> TaskPlan | None: ...
    def list_task_plans(self, *, mission_id: str | None = None) -> list[TaskPlan]: ...

    def put_execution_contract(self, contract: ExecutionContract) -> ExecutionContract: ...
    def get_execution_contract(self, contract_id: str) -> ExecutionContract | None: ...
    def list_execution_contracts(
        self, *, mission_id: str | None = None
    ) -> list[ExecutionContract]: ...

    def put_working_context(self, context: WorkingContext) -> WorkingContext: ...
    def get_working_context(self, context_id: str) -> WorkingContext | None: ...
    def list_working_contexts(self, *, mission_id: str | None = None) -> list[WorkingContext]: ...

    def put_work_item(self, work_item: WorkItem) -> WorkItem: ...
    def get_work_item(self, work_item_id: str) -> WorkItem | None: ...
    def list_work_items(self, *, mission_id: str | None = None) -> list[WorkItem]: ...

    def put_run_record(self, run_record: RunRecord) -> RunRecord: ...
    def get_run_record(self, run_id: str) -> RunRecord | None: ...
    def list_run_records(self, *, mission_id: str | None = None) -> list[RunRecord]: ...

    def put_artifact_record(self, artifact: ArtifactRecord) -> ArtifactRecord: ...
    def get_artifact_record(self, artifact_id: str) -> ArtifactRecord | None: ...
    def list_artifact_records(
        self, *, mission_id: str | None = None
    ) -> list[ArtifactRecord]: ...

    def put_artifact_manifest(self, manifest: ArtifactManifest) -> ArtifactManifest: ...
    def get_artifact_manifest(self, manifest_id: str) -> ArtifactManifest | None: ...
    def list_artifact_manifests(
        self, *, mission_id: str | None = None
    ) -> list[ArtifactManifest]: ...

    def put_ledger_event(self, event: LedgerEvent) -> LedgerEvent: ...
    def get_ledger_event(self, event_id: str) -> LedgerEvent | None: ...
    def list_ledger_events(self, *, mission_id: str | None = None) -> list[LedgerEvent]: ...

    def put_gate_evaluation(self, gate: GateEvaluation) -> GateEvaluation: ...
    def get_gate_evaluation(self, gate_evaluation_id: str) -> GateEvaluation | None: ...
    def list_gate_evaluations(
        self, *, mission_id: str | None = None
    ) -> list[GateEvaluation]: ...

    def put_collaboration_ticket(self, ticket: CollaborationTicket) -> CollaborationTicket: ...
    def get_collaboration_ticket(self, ticket_id: str) -> CollaborationTicket | None: ...
    def list_collaboration_tickets(
        self, *, mission_id: str | None = None
    ) -> list[CollaborationTicket]: ...

    def put_acceptance_review(self, review: AcceptanceReview) -> AcceptanceReview: ...
    def get_acceptance_review(self, acceptance_id: str) -> AcceptanceReview | None: ...
    def list_acceptance_reviews(
        self, *, mission_id: str | None = None
    ) -> list[AcceptanceReview]: ...

    def put_capability_profile(self, profile: CapabilityProfile) -> CapabilityProfile: ...
    def get_capability_profile(self, capability_id: str) -> CapabilityProfile | None: ...
    def list_capability_profiles(self) -> list[CapabilityProfile]: ...


class InMemoryControlPlaneStore:
    """Pure-memory implementation of the V6 store boundary."""

    def __init__(self) -> None:
        self._missions = _RecordBucket[Mission](lambda record: record.mission_id)
        self._task_plans = _RecordBucket[TaskPlan](
            lambda record: record.plan_id,
            mission_id=lambda record: record.mission_id,
        )
        self._execution_contracts = _RecordBucket[ExecutionContract](
            lambda record: record.contract_id,
            mission_id=lambda record: record.mission_id,
        )
        self._working_contexts = _RecordBucket[WorkingContext](
            lambda record: record.working_context_id,
            mission_id=lambda record: record.mission_id,
        )
        self._work_items = _RecordBucket[WorkItem](
            lambda record: record.work_item_id,
            mission_id=lambda record: record.mission_id,
            idempotency_key=lambda record: record.idempotency_key,
        )
        self._run_records = _RecordBucket[RunRecord](lambda record: record.run_id)
        self._artifact_records = _RecordBucket[ArtifactRecord](
            lambda record: record.artifact_id,
            mission_id=lambda record: record.mission_id,
        )
        self._artifact_manifests = _RecordBucket[ArtifactManifest](
            lambda record: record.manifest_id,
            mission_id=lambda record: record.mission_id,
        )
        self._ledger_events = _RecordBucket[LedgerEvent](
            lambda record: record.event_id,
            mission_id=lambda record: record.mission_id,
            idempotency_key=lambda record: record.idempotency_key,
        )
        self._gate_evaluations = _RecordBucket[GateEvaluation](
            lambda record: record.gate_evaluation_id,
            mission_id=lambda record: record.mission_id,
        )
        self._collaboration_tickets = _RecordBucket[CollaborationTicket](
            lambda record: record.ticket_id,
            mission_id=lambda record: record.mission_id,
        )
        self._acceptance_reviews = _RecordBucket[AcceptanceReview](
            lambda record: record.acceptance_id,
            mission_id=lambda record: record.mission_id,
        )
        self._capability_profiles = _RecordBucket[CapabilityProfile](
            lambda record: record.capability_id,
        )

    def put_mission(self, mission: Mission) -> Mission:
        return self._missions.put(mission)

    def get_mission(self, mission_id: str) -> Mission | None:
        return self._missions.get(mission_id)

    def list_missions(self) -> list[Mission]:
        return self._missions.list()

    def put_task_plan(self, task_plan: TaskPlan) -> TaskPlan:
        return self._task_plans.put(task_plan)

    def get_task_plan(self, plan_id: str) -> TaskPlan | None:
        return self._task_plans.get(plan_id)

    def list_task_plans(self, *, mission_id: str | None = None) -> list[TaskPlan]:
        return self._task_plans.list(mission_id=mission_id)

    def put_execution_contract(self, contract: ExecutionContract) -> ExecutionContract:
        return self._execution_contracts.put(contract)

    def get_execution_contract(self, contract_id: str) -> ExecutionContract | None:
        return self._execution_contracts.get(contract_id)

    def list_execution_contracts(
        self, *, mission_id: str | None = None
    ) -> list[ExecutionContract]:
        return self._execution_contracts.list(mission_id=mission_id)

    def put_working_context(self, context: WorkingContext) -> WorkingContext:
        return self._working_contexts.put(context)

    def get_working_context(self, context_id: str) -> WorkingContext | None:
        return self._working_contexts.get(context_id)

    def list_working_contexts(self, *, mission_id: str | None = None) -> list[WorkingContext]:
        return self._working_contexts.list(mission_id=mission_id)

    def put_work_item(self, work_item: WorkItem) -> WorkItem:
        return self._work_items.put(work_item)

    def get_work_item(self, work_item_id: str) -> WorkItem | None:
        return self._work_items.get(work_item_id)

    def list_work_items(self, *, mission_id: str | None = None) -> list[WorkItem]:
        return self._work_items.list(mission_id=mission_id)

    def put_run_record(self, run_record: RunRecord) -> RunRecord:
        return self._run_records.put(run_record)

    def get_run_record(self, run_id: str) -> RunRecord | None:
        return self._run_records.get(run_id)

    def list_run_records(self, *, mission_id: str | None = None) -> list[RunRecord]:
        records = self._run_records.list()
        if mission_id is None:
            return records
        work_item_ids = {item.work_item_id for item in self.list_work_items(mission_id=mission_id)}
        return [record for record in records if record.work_item_id in work_item_ids]

    def put_artifact_record(self, artifact: ArtifactRecord) -> ArtifactRecord:
        return self._artifact_records.put(artifact)

    def get_artifact_record(self, artifact_id: str) -> ArtifactRecord | None:
        return self._artifact_records.get(artifact_id)

    def list_artifact_records(
        self, *, mission_id: str | None = None
    ) -> list[ArtifactRecord]:
        return self._artifact_records.list(mission_id=mission_id)

    def put_artifact_manifest(self, manifest: ArtifactManifest) -> ArtifactManifest:
        return self._artifact_manifests.put(manifest)

    def get_artifact_manifest(self, manifest_id: str) -> ArtifactManifest | None:
        return self._artifact_manifests.get(manifest_id)

    def list_artifact_manifests(
        self, *, mission_id: str | None = None
    ) -> list[ArtifactManifest]:
        return self._artifact_manifests.list(mission_id=mission_id)

    def put_ledger_event(self, event: LedgerEvent) -> LedgerEvent:
        return self._ledger_events.put(event)

    def get_ledger_event(self, event_id: str) -> LedgerEvent | None:
        return self._ledger_events.get(event_id)

    def list_ledger_events(self, *, mission_id: str | None = None) -> list[LedgerEvent]:
        return self._ledger_events.list(mission_id=mission_id)

    def put_gate_evaluation(self, gate: GateEvaluation) -> GateEvaluation:
        return self._gate_evaluations.put(gate)

    def get_gate_evaluation(self, gate_evaluation_id: str) -> GateEvaluation | None:
        return self._gate_evaluations.get(gate_evaluation_id)

    def list_gate_evaluations(
        self, *, mission_id: str | None = None
    ) -> list[GateEvaluation]:
        return self._gate_evaluations.list(mission_id=mission_id)

    def put_collaboration_ticket(self, ticket: CollaborationTicket) -> CollaborationTicket:
        return self._collaboration_tickets.put(ticket)

    def get_collaboration_ticket(self, ticket_id: str) -> CollaborationTicket | None:
        return self._collaboration_tickets.get(ticket_id)

    def list_collaboration_tickets(
        self, *, mission_id: str | None = None
    ) -> list[CollaborationTicket]:
        return self._collaboration_tickets.list(mission_id=mission_id)

    def put_acceptance_review(self, review: AcceptanceReview) -> AcceptanceReview:
        return self._acceptance_reviews.put(review)

    def get_acceptance_review(self, acceptance_id: str) -> AcceptanceReview | None:
        return self._acceptance_reviews.get(acceptance_id)

    def list_acceptance_reviews(
        self, *, mission_id: str | None = None
    ) -> list[AcceptanceReview]:
        return self._acceptance_reviews.list(mission_id=mission_id)

    def put_capability_profile(self, profile: CapabilityProfile) -> CapabilityProfile:
        return self._capability_profiles.put(profile)

    def get_capability_profile(self, capability_id: str) -> CapabilityProfile | None:
        return self._capability_profiles.get(capability_id)

    def list_capability_profiles(self) -> list[CapabilityProfile]:
        return self._capability_profiles.list()
