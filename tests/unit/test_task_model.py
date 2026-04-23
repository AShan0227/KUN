"""TASK.md Pydantic model tests."""

import pytest
from kun.datamodel.task import Owner, TaskMeta
from pydantic import ValidationError


@pytest.mark.unit
def test_fingerprint_format():
    owner = Owner(tenant_id="u-sylvan")
    fp = TaskMeta.compute_fingerprint("hello world", owner)
    assert fp.startswith("sha256:")
    assert len(fp) == len("sha256:") + 64


@pytest.mark.unit
def test_fingerprint_stable_inside_window():
    owner = Owner(tenant_id="u-sylvan")
    fp1 = TaskMeta.compute_fingerprint("hello", owner, time_window_min=60)
    fp2 = TaskMeta.compute_fingerprint("hello", owner, time_window_min=60)
    assert fp1 == fp2


@pytest.mark.unit
def test_task_type_validation():
    owner = Owner(tenant_id="u-sylvan")
    m = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint("x", owner),
        task_type="Coding.Python.FastAPI",
        owner=owner,
        success_criteria_short="test",
    )
    assert m.task_type == "coding.python.fastapi"


@pytest.mark.unit
def test_task_type_rejects_invalid():
    owner = Owner(tenant_id="u-sylvan")
    with pytest.raises(ValidationError):
        TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("x", owner),
            task_type="",
            owner=owner,
            success_criteria_short="test",
        )
