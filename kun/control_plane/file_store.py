"""File-backed KUN V6 control-plane store.

This implementation is intentionally local and JSON-only.  It preserves the
same idempotent upsert semantics as the in-memory store while making every
write durable with a temp-file-and-replace snapshot.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from threading import RLock
from typing import Any

from pydantic import BaseModel

from kun.control_plane.store import _RecordBucket
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

_SCHEMA_VERSION = 1


class FileControlPlaneStore:
    """JSON file implementation of the V6 store boundary."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = RLock()
        self._init_buckets()
        self._load()

    def put_mission(self, mission: Mission) -> Mission:
        return self._put_and_persist(self._missions, mission)

    def get_mission(self, mission_id: str) -> Mission | None:
        with self._lock:
            return self._missions.get(mission_id)

    def list_missions(self) -> list[Mission]:
        with self._lock:
            return self._missions.list()

    def put_task_plan(self, task_plan: TaskPlan) -> TaskPlan:
        return self._put_and_persist(self._task_plans, task_plan)

    def get_task_plan(self, plan_id: str) -> TaskPlan | None:
        with self._lock:
            return self._task_plans.get(plan_id)

    def list_task_plans(self, *, mission_id: str | None = None) -> list[TaskPlan]:
        with self._lock:
            return self._task_plans.list(mission_id=mission_id)

    def put_execution_contract(self, contract: ExecutionContract) -> ExecutionContract:
        return self._put_and_persist(self._execution_contracts, contract)

    def get_execution_contract(self, contract_id: str) -> ExecutionContract | None:
        with self._lock:
            return self._execution_contracts.get(contract_id)

    def list_execution_contracts(self, *, mission_id: str | None = None) -> list[ExecutionContract]:
        with self._lock:
            return self._execution_contracts.list(mission_id=mission_id)

    def put_working_context(self, context: WorkingContext) -> WorkingContext:
        return self._put_and_persist(self._working_contexts, context)

    def get_working_context(self, context_id: str) -> WorkingContext | None:
        with self._lock:
            return self._working_contexts.get(context_id)

    def list_working_contexts(self, *, mission_id: str | None = None) -> list[WorkingContext]:
        with self._lock:
            return self._working_contexts.list(mission_id=mission_id)

    def put_work_item(self, work_item: WorkItem) -> WorkItem:
        return self._put_and_persist(self._work_items, work_item)

    def get_work_item(self, work_item_id: str) -> WorkItem | None:
        with self._lock:
            return self._work_items.get(work_item_id)

    def list_work_items(self, *, mission_id: str | None = None) -> list[WorkItem]:
        with self._lock:
            return self._work_items.list(mission_id=mission_id)

    def put_run_record(self, run_record: RunRecord) -> RunRecord:
        return self._put_and_persist(self._run_records, run_record)

    def get_run_record(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._run_records.get(run_id)

    def list_run_records(self, *, mission_id: str | None = None) -> list[RunRecord]:
        with self._lock:
            records = self._run_records.list()
            if mission_id is None:
                return records
            work_item_ids = {
                item.work_item_id for item in self._work_items.list(mission_id=mission_id)
            }
            return [record for record in records if record.work_item_id in work_item_ids]

    def put_artifact_record(self, artifact: ArtifactRecord) -> ArtifactRecord:
        return self._put_and_persist(self._artifact_records, artifact)

    def get_artifact_record(self, artifact_id: str) -> ArtifactRecord | None:
        with self._lock:
            return self._artifact_records.get(artifact_id)

    def list_artifact_records(self, *, mission_id: str | None = None) -> list[ArtifactRecord]:
        with self._lock:
            return self._artifact_records.list(mission_id=mission_id)

    def put_artifact_manifest(self, manifest: ArtifactManifest) -> ArtifactManifest:
        return self._put_and_persist(self._artifact_manifests, manifest)

    def get_artifact_manifest(self, manifest_id: str) -> ArtifactManifest | None:
        with self._lock:
            return self._artifact_manifests.get(manifest_id)

    def list_artifact_manifests(self, *, mission_id: str | None = None) -> list[ArtifactManifest]:
        with self._lock:
            return self._artifact_manifests.list(mission_id=mission_id)

    def put_ledger_event(self, event: LedgerEvent) -> LedgerEvent:
        return self._put_and_persist(self._ledger_events, event)

    def get_ledger_event(self, event_id: str) -> LedgerEvent | None:
        with self._lock:
            return self._ledger_events.get(event_id)

    def list_ledger_events(self, *, mission_id: str | None = None) -> list[LedgerEvent]:
        with self._lock:
            return self._ledger_events.list(mission_id=mission_id)

    def put_gate_evaluation(self, gate: GateEvaluation) -> GateEvaluation:
        return self._put_and_persist(self._gate_evaluations, gate)

    def get_gate_evaluation(self, gate_evaluation_id: str) -> GateEvaluation | None:
        with self._lock:
            return self._gate_evaluations.get(gate_evaluation_id)

    def list_gate_evaluations(self, *, mission_id: str | None = None) -> list[GateEvaluation]:
        with self._lock:
            return self._gate_evaluations.list(mission_id=mission_id)

    def put_collaboration_ticket(self, ticket: CollaborationTicket) -> CollaborationTicket:
        return self._put_and_persist(self._collaboration_tickets, ticket)

    def get_collaboration_ticket(self, ticket_id: str) -> CollaborationTicket | None:
        with self._lock:
            return self._collaboration_tickets.get(ticket_id)

    def list_collaboration_tickets(
        self, *, mission_id: str | None = None
    ) -> list[CollaborationTicket]:
        with self._lock:
            return self._collaboration_tickets.list(mission_id=mission_id)

    def put_acceptance_review(self, review: AcceptanceReview) -> AcceptanceReview:
        return self._put_and_persist(self._acceptance_reviews, review)

    def get_acceptance_review(self, acceptance_id: str) -> AcceptanceReview | None:
        with self._lock:
            return self._acceptance_reviews.get(acceptance_id)

    def list_acceptance_reviews(self, *, mission_id: str | None = None) -> list[AcceptanceReview]:
        with self._lock:
            return self._acceptance_reviews.list(mission_id=mission_id)

    def put_capability_profile(self, profile: CapabilityProfile) -> CapabilityProfile:
        return self._put_and_persist(self._capability_profiles, profile)

    def get_capability_profile(self, capability_id: str) -> CapabilityProfile | None:
        with self._lock:
            return self._capability_profiles.get(capability_id)

    def list_capability_profiles(self) -> list[CapabilityProfile]:
        with self._lock:
            return self._capability_profiles.list()

    def _init_buckets(self) -> None:
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

    def _load(self) -> None:
        if not self._path.exists():
            return

        payload = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("control-plane file store must contain a JSON object")
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError(
                f"unsupported control-plane file store schema: {payload.get('schema_version')!r}"
            )

        self._load_bucket("missions", Mission, self._missions, payload)
        self._load_bucket("task_plans", TaskPlan, self._task_plans, payload)
        self._load_bucket(
            "execution_contracts",
            ExecutionContract,
            self._execution_contracts,
            payload,
        )
        self._load_bucket("working_contexts", WorkingContext, self._working_contexts, payload)
        self._load_bucket("work_items", WorkItem, self._work_items, payload)
        self._load_bucket("run_records", RunRecord, self._run_records, payload)
        self._load_bucket("artifact_records", ArtifactRecord, self._artifact_records, payload)
        self._load_bucket(
            "artifact_manifests",
            ArtifactManifest,
            self._artifact_manifests,
            payload,
        )
        self._load_bucket("ledger_events", LedgerEvent, self._ledger_events, payload)
        self._load_bucket("gate_evaluations", GateEvaluation, self._gate_evaluations, payload)
        self._load_bucket(
            "collaboration_tickets",
            CollaborationTicket,
            self._collaboration_tickets,
            payload,
        )
        self._load_bucket(
            "acceptance_reviews",
            AcceptanceReview,
            self._acceptance_reviews,
            payload,
        )
        self._load_bucket(
            "capability_profiles",
            CapabilityProfile,
            self._capability_profiles,
            payload,
        )

    def _load_bucket[RecordT: BaseModel](
        self,
        key: str,
        model: type[RecordT],
        bucket: _RecordBucket[RecordT],
        payload: dict[str, Any],
    ) -> None:
        raw_records = payload.get(key, [])
        if not isinstance(raw_records, list):
            raise ValueError(f"control-plane file store field {key!r} must be a list")
        for raw_record in raw_records:
            bucket.put(model.model_validate(raw_record))

    def _put_and_persist[RecordT: BaseModel](
        self,
        bucket: _RecordBucket[RecordT],
        record: RecordT,
    ) -> RecordT:
        with self._lock:
            stored = bucket.put(record)
            self._persist_locked()
            return stored

    def _persist_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._snapshot_locked()
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{self._path.name}.",
            suffix=".tmp",
            dir=self._path.parent,
            text=True,
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
                json.dump(payload, temp_file, ensure_ascii=False, indent=2, sort_keys=True)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self._path)
            self._fsync_parent_dir()
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "missions": self._dump_records(self._missions.list()),
            "task_plans": self._dump_records(self._task_plans.list()),
            "execution_contracts": self._dump_records(self._execution_contracts.list()),
            "working_contexts": self._dump_records(self._working_contexts.list()),
            "work_items": self._dump_records(self._work_items.list()),
            "run_records": self._dump_records(self._run_records.list()),
            "artifact_records": self._dump_records(self._artifact_records.list()),
            "artifact_manifests": self._dump_records(self._artifact_manifests.list()),
            "ledger_events": self._dump_records(self._ledger_events.list()),
            "gate_evaluations": self._dump_records(self._gate_evaluations.list()),
            "collaboration_tickets": self._dump_records(self._collaboration_tickets.list()),
            "acceptance_reviews": self._dump_records(self._acceptance_reviews.list()),
            "capability_profiles": self._dump_records(self._capability_profiles.list()),
        }

    def _dump_records(self, records: Sequence[BaseModel]) -> list[dict[str, Any]]:
        return [record.model_dump(mode="json") for record in records]

    def _fsync_parent_dir(self) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            return
        parent_fd = os.open(self._path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
