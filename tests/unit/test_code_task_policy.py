from __future__ import annotations

import pytest
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.code_task_policy import (
    CODE_PROPOSE_CHANGE_APPLY_ENV,
    CODE_PROPOSE_CHANGE_SKILL_ID,
    CODE_REVIEW_SKILL_ID,
    code_task_directive,
    is_code_task,
    merge_code_capability_skill_summaries,
)


def _task(
    message: str,
    *,
    task_type: str = "writing.general",
    spec: TaskSpec | None = None,
) -> TaskRef:
    owner = Owner(tenant_id="tenant-test", user_id="user-test")
    return TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint(message, owner),
            task_type=task_type,
            owner=owner,
            success_criteria_short=message,
        ),
        spec=spec,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("task_type", "message"),
    [
        ("coding.python", "fix a bug in app.py"),
        ("debug.api", "find why the FastAPI route fails"),
        ("refactor.service", "split this module safely"),
        ("testing.python", "add pytest coverage"),
        ("writing.general", "修 bug 并补单测"),
    ],
)
def test_is_code_task_recognizes_coding_debug_refactor_test_tasks(
    task_type: str,
    message: str,
) -> None:
    assert is_code_task(_task(message, task_type=task_type)) is True


@pytest.mark.unit
def test_is_code_task_ignores_non_coding_tasks() -> None:
    assert is_code_task(_task("write a greeting email", task_type="writing.email")) is False


@pytest.mark.unit
def test_code_task_directive_names_safe_code_capability_path() -> None:
    directive = code_task_directive(_task("fix a bug", task_type="coding.python"))

    assert CODE_REVIEW_SKILL_ID in directive
    assert CODE_PROPOSE_CHANGE_SKILL_ID in directive
    assert CODE_PROPOSE_CHANGE_APPLY_ENV in directive
    assert "dry-run" in directive
    assert "不会写真实工作区" in directive
    assert "全自动 coder" in directive


@pytest.mark.unit
def test_code_task_directive_empty_for_non_coding_task() -> None:
    assert code_task_directive(_task("write a greeting email", task_type="writing.email")) == ""


@pytest.mark.unit
def test_merge_code_capability_skill_summaries_adds_missing_skills_once() -> None:
    merged = merge_code_capability_skill_summaries([(CODE_REVIEW_SKILL_ID, "existing review", {})])

    ids = [skill_id for skill_id, _, _ in merged]
    assert ids.count(CODE_REVIEW_SKILL_ID) == 1
    assert ids.count(CODE_PROPOSE_CHANGE_SKILL_ID) == 1
