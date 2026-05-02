from __future__ import annotations

from types import SimpleNamespace

from kun.world.action_reliability import reliability_items_from_rows, summarize_reliability


def test_reliability_marks_external_failure_for_compensation_review() -> None:
    items = reliability_items_from_rows(
        [
            SimpleNamespace(
                action_id="act-1",
                task_ref="task-1",
                action_type="email.send",
                status="failed",
                attempt_count=1,
                handler_id="smtp",
                external_dispatched=True,
                requires_handler=False,
                compensation_strategy="无法自动撤回已送达邮件；只能发送更正邮件",
                retry_policy="不自动重试",
                idempotency_key="act-1",
                last_error="smtp timeout after send",
                updated_at=None,
            )
        ]
    )

    assert items[0].recommended_action == "review_compensation"
    assert items[0].requires_human_confirmation is True
    assert items[0].can_auto_retry is False
    assert items[0].compensation_status == "missing"
    assert summarize_reliability(items)["needs_compensation_review"] == 1


def test_reliability_can_allow_non_external_auto_retry_when_policy_says_so() -> None:
    items = reliability_items_from_rows(
        [
            SimpleNamespace(
                action_id="act-2",
                task_ref="task-2",
                action_type="local_file.write",
                status="failed",
                attempt_count=1,
                handler_id="local",
                external_dispatched=False,
                requires_handler=False,
                compensation_strategy="可通过再次写入旧内容恢复",
                retry_policy="auto retry allowed for local artifact write",
                idempotency_key="artifact-act-2",
                last_error="temporary disk error",
                updated_at=None,
            )
        ]
    )

    assert items[0].recommended_action == "review_retry"
    assert items[0].can_auto_retry is True
    assert items[0].requires_human_confirmation is False
    assert items[0].idempotency_status == "present"
    assert summarize_reliability(items)["auto_retry_allowed"] == 1


def test_reliability_investigates_missing_handler() -> None:
    items = reliability_items_from_rows(
        [
            SimpleNamespace(
                action_id="act-3",
                task_ref="task-3",
                action_type="crm.update",
                status="blocked",
                attempt_count=1,
                handler_id=None,
                external_dispatched=False,
                requires_handler=True,
                compensation_strategy="",
                retry_policy="",
                idempotency_key="act-3",
                last_error="missing handler",
                updated_at=None,
            )
        ]
    )

    assert items[0].recommended_action == "investigate"
    assert "缺少真实执行器" in items[0].reason
    assert summarize_reliability(items)["needs_investigation"] == 1


def test_reliability_surfaces_execution_guard_blocks() -> None:
    items = reliability_items_from_rows(
        [
            SimpleNamespace(
                action_id="act-4",
                task_ref="task-4",
                action_type="email.send",
                status="blocked",
                attempt_count=1,
                handler_id="email.send.smtp.v1",
                external_dispatched=False,
                requires_handler=False,
                compensation_strategy="无法自动撤回已送达邮件；只能发送更正邮件",
                retry_policy="不自动重试",
                idempotency_key="send-user-42-v1",
                last_error="duplicate idempotency key",
                audit_json={
                    "reliability_guard": {
                        "status": "blocked",
                        "reasons": ["duplicate idempotency key already executed"],
                    }
                },
                updated_at=None,
            )
        ]
    )

    assert items[0].recommended_action == "investigate"
    assert items[0].reliability_guard_status == "blocked"
    assert items[0].guard_reasons == ["duplicate idempotency key already executed"]
    assert summarize_reliability(items)["guard_blocked"] == 1
