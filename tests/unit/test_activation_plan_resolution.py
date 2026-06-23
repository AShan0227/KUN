"""Current-plan resolution for feature activation (audit F083).

control_plane.task_plans is keyed by plan_id, but mission.current_plan_version holds
a version string. The old `task_plans.get(current_plan_version)` lookup therefore
returned None almost always, silently degrading skill/external-ref matching.
"""

from __future__ import annotations

import pytest
from kun.control_plane.activation import _resolve_current_plan
from kun.control_plane.runtime import InMemoryControlPlane
from kun.control_plane.v6 import Mission, TaskPlan


def _cp_with_plan(version: str) -> tuple[InMemoryControlPlane, Mission, TaskPlan]:
    cp = InMemoryControlPlane()
    mission = Mission(
        owner="tester",
        objective="ship the thing",
        task_type="product_development",
        current_plan_version=version,
    )
    plan = TaskPlan(mission_id=mission.mission_id, version=version, objective="ship the thing")
    cp.missions[mission.mission_id] = mission
    cp.task_plans[plan.plan_id] = plan  # keyed by plan_id, NOT version
    return cp, mission, plan


@pytest.mark.unit
def test_resolves_plan_by_version_not_plan_id() -> None:
    cp, mission, plan = _cp_with_plan("v3")
    resolved = _resolve_current_plan(cp, mission.mission_id)
    assert resolved is plan
    # The pre-fix lookup (by version against a plan_id-keyed dict) would miss it.
    assert cp.task_plans.get(mission.current_plan_version or "") is None


@pytest.mark.unit
def test_no_plan_version_returns_none() -> None:
    cp = InMemoryControlPlane()
    mission = Mission(owner="t", objective="o", task_type="ops_tooling")
    cp.missions[mission.mission_id] = mission
    assert _resolve_current_plan(cp, mission.mission_id) is None


@pytest.mark.unit
def test_does_not_cross_missions() -> None:
    cp, mission, plan = _cp_with_plan("v1")
    # A different mission's plan that happens to share the version must not match.
    other = TaskPlan(mission_id="msn-other", version="v1", objective="other")
    cp.task_plans[other.plan_id] = other
    resolved = _resolve_current_plan(cp, mission.mission_id)
    assert resolved is plan
    assert resolved.mission_id == mission.mission_id
