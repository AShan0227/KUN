"""NotificationLayer service (ADR-018 §16.3).

Central dispatcher for all outbound pushes — WebSocket side channel,
NUO panels, email, webhook. Walking skeleton just writes to DB + emits event;
full routing (email / webhook) plugs in later.
"""

from __future__ import annotations

from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.logging import get_logger
from kun.core.orm import NotificationRow
from kun.datamodel.events import Event
from kun.datamodel.notification import Notification

log = get_logger("kun.engineering.notifications")


async def push(notification: Notification) -> None:
    """Persist + emit event for delivery via outbox."""
    async with session_scope() as s:
        s.add(
            NotificationRow(
                notification_id=notification.notification_id,
                tenant_id=notification.tenant_id,
                kind=notification.kind,
                severity=notification.severity,
                channel=notification.channel,
                title=notification.title,
                body=notification.body,
                payload=notification.payload,
                render_hint=notification.render_hint,
                task_ref=notification.task_ref,
                causation_event_id=notification.causation_event_id,
                created_at=notification.created_at,
            )
        )
        await emit(
            s,
            Event.build(
                tenant_id=notification.tenant_id,
                event_type="notification.emitted",
                payload={
                    "notification_id": notification.notification_id,
                    "kind": notification.kind,
                    "channel": notification.channel,
                },
                task_ref=notification.task_ref,
                causation_event_id=notification.causation_event_id,
            ),
        )
    log.debug("notification.pushed", id=notification.notification_id, kind=notification.kind)
