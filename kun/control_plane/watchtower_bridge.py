"""Bridge V6 Control Plane events into the Watchtower rule engine."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.v6 import (
    ArtifactRecord,
    GateEvaluation,
    LedgerEvent,
    Mission,
    RunRecord,
    WorkItem,
)

if TYPE_CHECKING:
    from kun.watchtower.engine import RuleEngine


class V6WatchtowerBridgeReport(BaseModel):
    """Rules fired while evaluating one V6 Control Plane event."""

    model_config = ConfigDict(extra="forbid")

    event_type: str
    subject_ref: str
    fired_rule_ids: list[str] = Field(default_factory=list)


async def evaluate_v6_watchtower_event(
    rule_engine: RuleEngine,
    *,
    event_type: str,
    mission: Mission,
    work_item: WorkItem | None = None,
    run: RunRecord | None = None,
    gate: GateEvaluation | None = None,
    ledger_event: LedgerEvent | None = None,
    artifacts: list[ArtifactRecord] | None = None,
    tenant_id: str = "control-plane",
) -> V6WatchtowerBridgeReport:
    """Evaluate Watchtower rules against a normalized V6 namespace."""

    namespace = build_v6_watchtower_namespace(
        event_type=event_type,
        mission=mission,
        work_item=work_item,
        run=run,
        gate=gate,
        ledger_event=ledger_event,
        artifacts=artifacts or [],
        tenant_id=tenant_id,
    )
    fired = await rule_engine.evaluate(event_type, namespace=namespace)
    return V6WatchtowerBridgeReport(
        event_type=event_type,
        subject_ref=str(namespace["event"]["task_ref"]),
        fired_rule_ids=fired,
    )


def evaluate_v6_watchtower_event_sync(
    rule_engine: RuleEngine,
    *,
    event_type: str,
    mission: Mission,
    work_item: WorkItem | None = None,
    run: RunRecord | None = None,
    gate: GateEvaluation | None = None,
    ledger_event: LedgerEvent | None = None,
    artifacts: list[ArtifactRecord] | None = None,
    tenant_id: str = "control-plane",
) -> V6WatchtowerBridgeReport:
    """Synchronous adapter for daemon/runtime code paths."""

    return _run_async(
        evaluate_v6_watchtower_event(
            rule_engine,
            event_type=event_type,
            mission=mission,
            work_item=work_item,
            run=run,
            gate=gate,
            ledger_event=ledger_event,
            artifacts=artifacts or [],
            tenant_id=tenant_id,
        )
    )


def build_v6_watchtower_namespace(
    *,
    event_type: str,
    mission: Mission,
    work_item: WorkItem | None = None,
    run: RunRecord | None = None,
    gate: GateEvaluation | None = None,
    ledger_event: LedgerEvent | None = None,
    artifacts: list[ArtifactRecord],
    tenant_id: str,
) -> dict[str, Any]:
    """Build the safe expression namespace used by Watchtower rules."""

    subject_ref = (
        gate.subject_ref
        if gate is not None
        else work_item.work_item_id
        if work_item is not None
        else ledger_event.subject_ref
        if ledger_event is not None and ledger_event.subject_ref is not None
        else mission.mission_id
    )
    payload: dict[str, Any] = {
        "mission_id": mission.mission_id,
        "task_type": mission.task_type,
        "mission_status": mission.status,
        "work_item_status": work_item.status if work_item is not None else None,
        "work_item_type": work_item.type if work_item is not None else None,
        "run_id": run.run_id if run is not None else None,
        "run_exit_status": run.exit_status if run is not None else None,
        "failure_category": run.failure_category if run is not None else None,
        "gate_evaluation_id": gate.gate_evaluation_id if gate is not None else None,
        "north_star_verdict": gate.north_star_verdict if gate is not None else None,
        "result_quality": gate.result_quality if gate is not None else None,
        "next_action": gate.next_action if gate is not None else None,
        "artifact_refs": [artifact.artifact_id for artifact in artifacts],
        "artifact_supports": [support for artifact in artifacts for support in artifact.supports],
    }
    return {
        "tenant_id": tenant_id,
        "task_ref": subject_ref,
        "event": {
            "event_type": event_type,
            "tenant_id": tenant_id,
            "task_ref": subject_ref,
            "payload": payload,
        },
        "task": {
            "task_id": subject_ref,
            "mission_id": mission.mission_id,
            "task_type": mission.task_type,
            "status": mission.status,
            "work_item_type": work_item.type if work_item is not None else None,
            "work_item_status": work_item.status if work_item is not None else None,
        },
        "runtime": {
            "run_id": run.run_id if run is not None else None,
            "runner_type": run.runner_type if run is not None else None,
            "runner_identity": run.runner_identity if run is not None else None,
            "exit_status": run.exit_status if run is not None else None,
            "failure_category": run.failure_category if run is not None else None,
            "gate": gate.model_dump(mode="json") if gate is not None else None,
        },
        "env": {
            "control_plane": "v6",
            "watchtower_bridge": "kun-v6-watchtower-bridge-v1",
        },
    }


def _run_async[T](coro: Any) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(coro)).result()


__all__ = [
    "V6WatchtowerBridgeReport",
    "build_v6_watchtower_namespace",
    "evaluate_v6_watchtower_event",
    "evaluate_v6_watchtower_event_sync",
]
