"""_latest_gate orders by gate_evaluation_id, not dict insertion order (audit F079).

The old code returned ``gates[-1]`` (dict insertion order), so after a store reload
or any non-chronological insert the "latest gate" decision was unreliable.
gate_evaluation_id is a time-sortable ULID, so ordering by it is stable.
"""

from __future__ import annotations

import pytest
from kun.control_plane.mission_director import _latest_gate
from kun.control_plane.runtime import InMemoryControlPlane
from kun.control_plane.v6 import GateEvaluation, Mission


def _gate(gate_id: str, mission_id: str, verdict: str) -> GateEvaluation:
    return GateEvaluation(
        gate_evaluation_id=gate_id,
        mission_id=mission_id,
        task_plan_version="v1",
        subject_ref="w1",
        stage="acceptance",
        task_type="product_development",
        rubric_version="r1",
        metric_pack_version="m1",
        north_star_verdict=verdict,
        result_quality=0.9,
        speed=0.8,
        cost=0.8,
        risk=0.2,
        evidence_quality=0.8,
        collaboration_quality=0.8,
        next_action="continue",
        next_state="running",
        created_by="tester",
    )


@pytest.mark.unit
def test_latest_gate_ignores_insertion_order() -> None:
    cp = InMemoryControlPlane()
    mission = Mission(owner="t", objective="o", task_type="product_development")
    cp.missions[mission.mission_id] = mission
    # Insert out of chronological order: ...003 first, ...002 last.
    cp.gate_evaluations["gate-003"] = _gate("gate-003", mission.mission_id, "pass")
    cp.gate_evaluations["gate-001"] = _gate("gate-001", mission.mission_id, "fail")
    cp.gate_evaluations["gate-002"] = _gate("gate-002", mission.mission_id, "partial")

    latest = _latest_gate(cp, mission)
    assert latest is not None
    # Max id is gate-003 (the real latest), not gate-002 (last inserted).
    assert latest.gate_evaluation_id == "gate-003"


@pytest.mark.unit
def test_latest_gate_scopes_to_mission_and_handles_empty() -> None:
    cp = InMemoryControlPlane()
    m1 = Mission(owner="t", objective="o", task_type="product_development")
    m2 = Mission(owner="t", objective="o2", task_type="ops_tooling")
    cp.missions[m1.mission_id] = m1
    cp.missions[m2.mission_id] = m2
    cp.gate_evaluations["gate-009"] = _gate("gate-009", m2.mission_id, "pass")  # other mission
    assert _latest_gate(cp, m1) is None  # no gate for m1
    cp.gate_evaluations["gate-005"] = _gate("gate-005", m1.mission_id, "partial")
    latest = _latest_gate(cp, m1)
    assert latest is not None and latest.gate_evaluation_id == "gate-005"
