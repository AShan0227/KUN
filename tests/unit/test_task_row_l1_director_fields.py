"""TaskRow persists TaskMeta's L1 Director fields (audit F135/F096).

complexity / priority_profile / estimated_steps existed on TaskMeta but had no
TaskRow columns, so they were silently dropped on persist. These guard against
the column-vs-model drift regressing (no live DB needed).
"""

from __future__ import annotations

import pytest
from kun.core.orm import TaskRow
from kun.datamodel.task import TaskMeta


@pytest.mark.unit
def test_task_row_has_l1_director_columns() -> None:
    cols = set(TaskRow.__table__.columns.keys())
    for field in ("complexity", "priority_profile", "estimated_steps"):
        assert field in cols, f"TaskRow is missing column {field!r}"
        # And TaskMeta still declares it — both layers must agree.
        assert field in TaskMeta.model_fields


@pytest.mark.unit
def test_task_row_round_trips_director_fields() -> None:
    row = TaskRow(
        task_id="task-1",
        tenant_id="u-1",
        fingerprint="fp",
        task_type="coding.python",
        risk_level="low",
        complexity_score=0.7,
        complexity="complex",
        priority_profile="speed_first",
        estimated_cost_usd=0.1,
        estimated_duration_sec=42.0,
        estimated_steps=9,
        success_criteria_short="done",
        version=1,
    )
    assert row.complexity == "complex"
    assert row.priority_profile == "speed_first"
    assert row.estimated_steps == 9
