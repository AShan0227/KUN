"""Concurrency safety tests."""

from __future__ import annotations

import pytest
from kun.datamodel.task import Constraint, Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.concurrency import (
    PendingActionSpec,
    derive_resource_intents,
    enqueue_pending_actions,
    pending_actions_for,
)


def _task(*, spec: TaskSpec | None = None, text: str = "整理报告") -> TaskRef:
    owner = Owner(tenant_id="u-sylvan", project_id="proj-main")
    meta = TaskMeta(
        fingerprint=TaskMeta.compute_fingerprint(text, owner),
        task_type="ops.workflow",
        risk_level="low",
        owner=owner,
        success_criteria_short=text,
    )
    return TaskRef(meta=meta, spec=spec)


@pytest.mark.unit
def test_derive_resource_intents_marks_side_effect_tools_as_write() -> None:
    spec = TaskSpec(
        goal_detail="给客户发送邮件",
        required_tools=["email_sender", "csv_reader"],
        external_resources=["crm export"],
        constraints=[Constraint(kind="path_only", detail="/tmp/report.csv")],
    )

    intents = derive_resource_intents(_task(spec=spec, text="给客户发送邮件"))
    by_resource = {intent.resource: intent for intent in intents}

    assert by_resource["tool:email_sender"].mode == "write"
    assert by_resource["tool:csv_reader"].mode == "read"
    assert by_resource["external:crm-export"].mode == "read"
    assert by_resource["path:tmp-report.csv"].mode == "write"
    assert by_resource["project:proj-main"].mode == "write"


@pytest.mark.unit
def test_pending_actions_for_requires_approval_for_external_side_effects() -> None:
    spec = TaskSpec(
        goal_detail="发布公告并发送邮件",
        required_tools=["email_sender"],
        external_resources=["customer-list"],
    )

    actions = pending_actions_for(_task(spec=spec, text="发布公告并发送邮件"))

    assert {action.action_type for action in actions} == {
        "content.publish",
        "message.send",
    }
    assert all(action.risk_level == "medium" for action in actions)


@pytest.mark.unit
def test_side_effect_detection_does_not_match_inside_words() -> None:
    spec = TaskSpec(
        goal_detail="修复 postgres 连接池",
        required_tools=["postgres_client"],
        external_resources=["postgres database"],
    )

    actions = pending_actions_for(_task(spec=spec, text="修复 postgres 连接池"))

    assert actions == []


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_enqueue_pending_actions_adds_rows() -> None:
    session = _FakeSession()
    task = _task()
    actions = [
        PendingActionSpec(
            action_type="message.send",
            target_ref="customer-list",
            risk_level="high",
        )
    ]

    await enqueue_pending_actions(
        session,  # type: ignore[arg-type]
        tenant_id="u-sylvan",
        task_ref=task,
        actions=actions,
    )

    assert len(session.added) == 1
    row = session.added[0]
    assert getattr(row, "action_type") == "message.send"
    assert getattr(row, "status") == "pending_approval"
    assert getattr(row, "task_ref") == task.meta.task_id
