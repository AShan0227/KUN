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
def test_path_only_constraint_is_read_for_review_tasks() -> None:
    spec = TaskSpec(
        goal_detail="Audit files in an isolated workspace and report findings.",
        required_tools=["file_read"],
        constraints=[Constraint(kind="path_only", detail="/tmp/isolated-workspace")],
    )

    intents = derive_resource_intents(_task(spec=spec, text="审核隔离工作区并输出方案"))
    by_resource = {intent.resource: intent for intent in intents}

    assert by_resource["path:tmp-isolated-workspace"].mode == "read"
    assert by_resource["project:proj-main"].mode == "read"


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


# ===== negation-aware side-effect detection (dogfood-discovered bug 修复) =====


@pytest.mark.unit
def test_chinese_negation_does_not_trigger_delete() -> None:
    """'不得删除' / '未删除' / '一张不删' 都不应触发 resource.delete."""
    for text in (
        "现有 27 张 seeds 不得删除",
        "保留所有 seed, 未删除任何文件",
        "确保 seeds 一张不删",
        "禁止删除现有 dev_logs",
    ):
        spec = TaskSpec(
            goal_detail=text,
            success_metrics=[text],
        )
        actions = pending_actions_for(_task(spec=spec, text=text))
        assert actions == [], f"误判 negation 触发 action for: {text}"


@pytest.mark.unit
def test_english_negation_does_not_trigger_side_effects() -> None:
    """English negations also bypass side-effect detection."""
    for text in (
        "don't deploy this change",
        "do not send any email",
        "never publish to production",
        "without merging the branch",
        "no payment will be made",
    ):
        spec = TaskSpec(goal_detail=text, success_metrics=[text])
        actions = pending_actions_for(_task(spec=spec, text=text))
        assert actions == [], f"误判 English negation: {text}"


@pytest.mark.unit
def test_positive_action_still_triggers_after_negation_fix() -> None:
    """确认 negation fix 没误伤真正的副作用动作."""
    spec = TaskSpec(goal_detail="发送邮件并发布通告", success_metrics=["发送 + 发布"])
    actions = pending_actions_for(_task(spec=spec, text="发送邮件并发布通告"))
    types = {a.action_type for a in actions}
    assert "message.send" in types
    assert "content.publish" in types


@pytest.mark.unit
def test_mixed_negated_and_positive_keeps_positive() -> None:
    """'不得删除现有文件, 但需要发送邮件' — 删除被否定, 发送仍触发."""
    text = "不得删除现有文件, 但需要发送邮件"
    spec = TaskSpec(goal_detail=text, success_metrics=[text])
    actions = pending_actions_for(_task(spec=spec, text=text))
    types = {a.action_type for a in actions}
    assert "resource.delete" not in types
    assert "message.send" in types


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
