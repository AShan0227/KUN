"""Event builder tests."""

import pytest
from kun.datamodel.events import Event


@pytest.mark.unit
def test_event_subject_format():
    ev = Event.build(
        tenant_id="u-sylvan",
        event_type="task.started",
        payload={"task_id": "tk-xxx"},
        task_ref="tk-xxx",
    )
    assert ev.subject == "kun.u-sylvan.task.task.started"
    assert ev.event_id.startswith("ev-")
    assert ev.task_ref == "tk-xxx"
