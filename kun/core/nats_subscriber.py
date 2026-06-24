"""NATS event subscriber — 反向链路 (小尾巴 C).

写端: kun.core.events.outbox_worker → 把 events 表新行发到 NATS.
读端 (本文件): 订阅 NATS, 拿到 event_id → 回 Postgres 拉完整 EventRow →
分发给注册的 handlers (默认 watchtower RuleEngine 评估规则).

部署形态:
  - 嵌入主进程: 在 FastAPI lifespan startup 里 asyncio.create_task(subscriber_worker())
  - 独立进程:   python -m kun.core.nats_subscriber  (--subject 'kun.>' --queue kun-wt)

Queue group ("kun-watchtower") 让多个 subscriber 实例做负载均衡 — 同一条消息
只会派发给一个实例, 不重复处理.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from kun.core.events import close_nats, connect_nats
from kun.core.logging import get_logger
from kun.core.orm import EventRow

if TYPE_CHECKING:
    from nats.aio.client import Client as NATS
    from nats.aio.subscription import Subscription

    from kun.watchtower.engine import RuleEngine

log = get_logger("kun.nats_subscriber")


EventHandler = Callable[[EventRow], Awaitable[None]]
EventFetcher = Callable[[str], Awaitable[EventRow | None]]


async def _default_fetcher(event_id: str) -> EventRow | None:
    """Default fetcher: pull EventRow from Postgres by id."""
    from kun.core.db import session_scope

    async with session_scope(bypass_rls=True) as s:
        return await s.get(EventRow, event_id)


async def dispatch_event_to_handlers(
    event_id: str,
    handlers: list[EventHandler],
    *,
    fetcher: EventFetcher | None = None,
) -> None:
    """Pull event from Postgres + dispatch to all registered handlers.

    一个 handler 失败不影响其它 handler — 我们 catch 每个的异常.
    fetcher 注入是为了单测能 mock 掉数据库.
    """
    fetch = fetcher or _default_fetcher
    row = await fetch(event_id)
    if row is None:
        log.warning("subscriber.event_not_found", event_id=event_id)
        return
    for handler in handlers:
        try:
            await handler(row)
        except Exception as e:
            log.exception(
                "subscriber.handler_failed",
                event_id=event_id,
                handler=getattr(handler, "__name__", repr(handler)),
                error=str(e),
            )


_WATCHTOWER_ENGINE: RuleEngine | None = None


def _watchtower_engine() -> RuleEngine:
    """Cached RuleEngine loaded with the real rule set (audit F029).

    The NATS/cross-process path used to build an empty ``RuleEngine()`` per
    event, so no watchtower rule ever fired off-process — only the in-process
    orchestrator path (which builds ``RuleEngine(load_rules("rules"))``) worked.
    Load the rules once and reuse, matching the in-process engine. Loaded lazily
    so importing this module never touches the filesystem; a load failure falls
    back to an empty engine (logged) rather than crashing the subscriber.
    """
    global _WATCHTOWER_ENGINE
    if _WATCHTOWER_ENGINE is None:
        from kun.watchtower.engine import RuleEngine, load_rules

        try:
            _WATCHTOWER_ENGINE = RuleEngine(load_rules("rules"))
        except Exception as e:
            log.warning("subscriber.rule_load_failed", error=str(e))
            _WATCHTOWER_ENGINE = RuleEngine()
    return _WATCHTOWER_ENGINE


async def watchtower_handler(row: EventRow) -> None:
    """默认 handler: 把事件类型喂给 watchtower RuleEngine（已加载真实规则集）."""
    from kun.watchtower.handlers import handle_tool_skipped

    if row.event_type == "task.tool_skipped":
        await handle_tool_skipped(row)
    engine = _watchtower_engine()
    namespace = {
        "event_type": row.event_type,
        "tenant_id": row.tenant_id,
        "task_ref": row.task_ref,
        "payload": row.payload or {},
    }
    await engine.evaluate(row.event_type, namespace=namespace)


async def subscribe_to_events(
    nc: NATS,
    subject_pattern: str,
    handlers: list[EventHandler],
    *,
    queue: str | None = None,
    fetcher: EventFetcher | None = None,
) -> Subscription:
    """Subscribe to NATS subject + dispatch incoming msgs to handlers.

    NATS 消息 payload 是 event_id (UTF-8); fetcher 拿到完整 EventRow 后
    分发给所有 handler. queue 是 NATS queue group, 让多个 subscriber 实例
    做负载均衡.
    """

    async def _on_msg(msg: object) -> None:
        try:
            data = getattr(msg, "data", b"")
            event_id = data.decode("utf-8") if isinstance(data, bytes | bytearray) else str(data)
            await dispatch_event_to_handlers(event_id, handlers, fetcher=fetcher)
        except Exception as e:
            log.exception("subscriber.on_msg_failed", error=str(e))

    # NATS subscribe queue 不接受 None — 没指定就传空串 (无 queue group)
    return await nc.subscribe(subject_pattern, queue=queue or "", cb=_on_msg)


async def subscriber_worker(
    *,
    subject_pattern: str = "kun.>",
    queue: str = "kun-watchtower",
    handlers: list[EventHandler] | None = None,
) -> None:
    """长跑订阅者. 没 NATS 就直接退出, 不阻塞 startup.

    handlers=None 时用 watchtower_handler 默认.
    """
    nc = await connect_nats()
    if nc is None:
        log.warning("subscriber.no_nats", hint="NATS unavailable, subscriber idle")
        return

    handlers = handlers or [watchtower_handler]
    sub = await subscribe_to_events(nc, subject_pattern, handlers, queue=queue)
    log.info(
        "subscriber.started",
        subject=subject_pattern,
        queue=queue,
        handler_count=len(handlers),
    )
    try:
        # 永远等, 直到 cancel
        await asyncio.Event().wait()
    finally:
        with contextlib.suppress(Exception):
            await sub.unsubscribe()
        await close_nats(nc)


def _main() -> None:
    """python -m kun.core.nats_subscriber 入口."""
    import argparse

    parser = argparse.ArgumentParser(description="KUN NATS event subscriber")
    parser.add_argument("--subject", default="kun.>", help="NATS subject pattern")
    parser.add_argument("--queue", default="kun-watchtower", help="NATS queue group")
    args = parser.parse_args()

    asyncio.run(
        subscriber_worker(
            subject_pattern=args.subject,
            queue=args.queue,
        )
    )


if __name__ == "__main__":
    _main()


__all__ = [
    "EventFetcher",
    "EventHandler",
    "dispatch_event_to_handlers",
    "subscribe_to_events",
    "subscriber_worker",
    "watchtower_handler",
]
