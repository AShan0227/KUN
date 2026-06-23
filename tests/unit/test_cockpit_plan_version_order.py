"""_current_plan picks the newest plan numerically, not lexically (audit F145).

Plan versions are v<N>; the old max(key=version) compared strings, so v10 ranked
below v9 and the cockpit showed a stale plan after >9 revisions.
"""

from __future__ import annotations

import pytest
from kun.control_plane.cockpit import _current_plan, _plan_version_sort_key
from kun.control_plane.runtime import InMemoryControlPlane
from kun.control_plane.v6 import Mission, TaskPlan


@pytest.mark.unit
def test_version_key_orders_numerically() -> None:
    versions = ["v0", "v1", "v2", "v9", "v10", "v11"]
    assert sorted(versions, key=_plan_version_sort_key) == ["v0", "v1", "v2", "v9", "v10", "v11"]
    # The exact bug: v10 must outrank v9.
    assert _plan_version_sort_key("v10") > _plan_version_sort_key("v9")


@pytest.mark.unit
def test_current_plan_returns_highest_numeric_version() -> None:
    cp = InMemoryControlPlane()
    mission = Mission(owner="t", objective="o", task_type="product_development")
    cp.missions[mission.mission_id] = mission
    for n in (0, 1, 9, 10, 2):  # inserted out of order, includes the v9/v10 trap
        plan = TaskPlan(mission_id=mission.mission_id, version=f"v{n}", objective="o")
        cp.task_plans[plan.plan_id] = plan

    latest = _current_plan(cp, mission_id=mission.mission_id, version=None)
    assert latest is not None
    assert latest.version == "v10"  # not v9


@pytest.mark.unit
def test_current_plan_exact_version_still_wins() -> None:
    cp = InMemoryControlPlane()
    mission = Mission(owner="t", objective="o", task_type="ops_tooling")
    cp.missions[mission.mission_id] = mission
    for n in (1, 2, 3):
        plan = TaskPlan(mission_id=mission.mission_id, version=f"v{n}", objective="o")
        cp.task_plans[plan.plan_id] = plan
    got = _current_plan(cp, mission_id=mission.mission_id, version="v2")
    assert got is not None and got.version == "v2"
