"""NATS subscriber — dispatch + handler 容错单测 (小尾巴 C)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest
from kun.core.nats_subscriber import dispatch_event_to_handlers
from kun.core.orm import EventRow


def _fake_event(event_id: str = "evt-1") -> EventRow:
    return EventRow(
        event_id=event_id,
        tenant_id="t-test",
        event_type="task.tool_skipped",
        subject="kun.t-test.task.task.tool_skipped",
        payload={"foo": "bar"},
        task_ref="task-001",
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_calls_each_handler_with_fetched_row() -> None:
    """fetcher 返回 row → 每个 handler 都被 await 一次, 拿到同一个 row."""
    seen: list[tuple[str, EventRow]] = []

    async def fetcher(_eid: str) -> EventRow | None:
        return _fake_event()

    async def h1(row: EventRow) -> None:
        seen.append(("h1", row))

    async def h2(row: EventRow) -> None:
        seen.append(("h2", row))

    await dispatch_event_to_handlers("evt-1", [h1, h2], fetcher=fetcher)
    assert [s[0] for s in seen] == ["h1", "h2"]
    assert all(s[1].event_id == "evt-1" for s in seen)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_skips_when_event_not_found() -> None:
    """fetcher 返回 None (event_id 不在 Postgres) → handler 不被调."""
    called: list[str] = []

    async def fetcher(_eid: str) -> EventRow | None:
        return None

    async def h1(_row: EventRow) -> None:
        called.append("h1")

    await dispatch_event_to_handlers("ghost-evt", [h1], fetcher=fetcher)
    assert called == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_one_handler_failure_does_not_block_others() -> None:
    """一个 handler 抛异常不能拖死后续 handler — 我们把每个都包了 try."""
    after_failure_called = False

    async def fetcher(_eid: str) -> EventRow | None:
        return _fake_event()

    async def boom(_row: EventRow) -> None:
        raise RuntimeError("first handler explodes")

    async def survivor(_row: EventRow) -> None:
        nonlocal after_failure_called
        after_failure_called = True

    await dispatch_event_to_handlers("evt-1", [boom, survivor], fetcher=fetcher)
    assert after_failure_called is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dispatch_signature_matches_handler_protocol() -> None:
    """Handler 类型契约: Callable[[EventRow], Awaitable[None]] — 编译时验证."""

    async def h(row: EventRow) -> None:
        assert row.event_id

    callable_obj: Callable[[EventRow], Awaitable[None]] = h
    # 静态契约 + 运行时 smoke
    fake = _fake_event()

    async def fetcher(_eid: str) -> EventRow | None:
        return fake

    await dispatch_event_to_handlers("evt-1", [callable_obj], fetcher=fetcher)
