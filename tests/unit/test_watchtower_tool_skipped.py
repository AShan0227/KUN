"""task.tool_skipped → proactive trigger promotion tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from kun.core.orm import EventRow
from kun.watchtower.handlers import handle_tool_skipped


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
