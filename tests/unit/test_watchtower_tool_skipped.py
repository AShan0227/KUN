"""task.tool_skipped → proactive trigger promotion tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from kun.core.orm import EventRow
from kun.watchtower.handlers import (
    _enqueue_external_skill_scout_for_promoted_miss,
    handle_tool_skipped,
)


class _FakeScope:
    async def __aenter__(self) -> object:
        return object()

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _event(tenant_id: str = "tenant-a") -> EventRow:
    return EventRow(
        event_id=f"event-{tenant_id}",
        tenant_id=tenant_id,
        event_type="task.tool_skipped",
        subject=f"kun.{tenant_id}.task.task.tool_skipped",
        payload={
            "missed": [
                {
                    "skill_id": "web-search",
                    "reason": "executor_unregistered",
                    "pattern": "latest|today",
                    "trigger_source": "skill_manifest",
                }
            ]
        },
        task_ref="task-001",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_tool_skipped_handler_promotes_after_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counts: dict[tuple[str, str, str], int] = {}
    emitted: list[dict[str, object]] = []
    scout_requests: list[dict[str, object]] = []

    async def fake_record(
        _session: object,
        *,
        tenant_id: str,
        skill_id: str,
        pattern: str,
        reason: str,
        trigger_source: str,
        threshold: int,
    ) -> int | None:
        key = (tenant_id, skill_id, pattern)
        counts[key] = counts.get(key, 0) + 1
        return counts[key] if counts[key] == threshold else None

    async def fake_emit(_session: object, event: object) -> None:
        emitted.append(
            {
                "tenant_id": getattr(event, "tenant_id"),
                "event_type": getattr(event, "event_type"),
                "payload": getattr(event, "payload"),
            }
        )

    def fake_session_scope(**_kwargs: object) -> AsyncIterator[object]:
        return _FakeScope()

    monkeypatch.setattr("kun.core.db.session_scope", fake_session_scope)
    monkeypatch.setattr("kun.watchtower.handlers._record_proactive_miss", fake_record)
    monkeypatch.setattr("kun.watchtower.handlers.emit", fake_emit)

    async def fake_enqueue_scout(
        *,
        tenant_id: str,
        task_ref: str,
        miss: dict[str, str],
        prompt_excerpt: str = "",
    ) -> int:
        scout_requests.append(
            {
                "tenant_id": tenant_id,
                "task_ref": task_ref,
                "skill_id": miss["skill_id"],
                "pattern": miss["pattern"],
                "prompt_excerpt": prompt_excerpt,
            }
        )
        return 1

    monkeypatch.setattr(
        "kun.watchtower.handlers._enqueue_external_skill_scout_for_promoted_miss",
        fake_enqueue_scout,
    )

    for _ in range(9):
        await handle_tool_skipped(_event("tenant-a"))
        await handle_tool_skipped(_event("tenant-b"))

    assert emitted == []

    await handle_tool_skipped(_event("tenant-a"))

    assert len(emitted) == 1
    assert emitted[0]["tenant_id"] == "tenant-a"
    assert emitted[0]["event_type"] == "proactive.trigger_promoted"
    assert emitted[0]["payload"] == {
        "skill_id": "web-search",
        "pattern": "latest|today",
        "miss_count": 10,
        "threshold": 10,
        "trigger_source": "skill_manifest",
    }
    assert scout_requests == [
        {
            "tenant_id": "tenant-a",
            "task_ref": "task-001",
            "skill_id": "web-search",
            "pattern": "latest|today",
            "prompt_excerpt": "",
        }
    ]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_promoted_tool_miss_queues_review_only_external_skill_scout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kun.qi.problem_queue import get_qi_problem_queue, reset_qi_problem_queue

    reset_qi_problem_queue()
    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")

    persisted = await _enqueue_external_skill_scout_for_promoted_miss(
        tenant_id="tenant-a",
        task_ref="task-001",
        miss={
            "skill_id": "code-review",
            "pattern": "typescript|diff",
            "reason": "executor_unregistered",
            "trigger_source": "skill_manifest",
        },
        prompt_excerpt="Need stronger TypeScript pull request review.",
    )

    queued = get_qi_problem_queue().list("tenant-a", limit=10)
    assert persisted == 1
    assert len(queued) == 1
    signal = queued[0]
    assert signal.source == "external_skill.scout_plan"
    assert signal.evidence["queue_intent"] == "external_skill_scout_review_only"
    assert signal.evidence["review_only"] is True
    assert signal.evidence["auto_install_allowed"] is False
    assert signal.evidence["production_action"] is False
    assert "mattpocock/skills" in signal.evidence["recommended_repo_refs"]
