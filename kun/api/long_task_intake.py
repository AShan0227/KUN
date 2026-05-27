"""WebSocket-level glue for routing follow-up user messages while a long task runs.

ADR-022 Layer 3 entry point. `LongTaskInputRouter` decides *what kind* of input
the message is; this module decides what to actually do with the WS connection
state (send response, cancel task, etc.).

Why a separate helper instead of inlining into ws.py:
  - ws.py has lots of intertwined state (auth, lock, current_task, etc.) and
    is hard to unit-test against a fake socket. This module is pure async
    plumbing — fakes for send_json / cancel / start are trivial.
  - The decision tree (6 buckets → actions) is non-trivial and deserves its
    own tests independent of WS framing.

Backward-compat guarantee:
  - If `current_task is None` OR `current_goal_anchor is None`, we return
    `next_action="start_new_task"` so the caller falls through to its existing
    "create a fresh task" branch. The new routing ONLY engages when a long
    task with a GoalAnchor is already running.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from kun.agents.director.long_task_router import LongTaskInputRouter
from kun.core.logging import get_logger

log = get_logger("kun.api.long_task_intake")


IntakeAction = Literal[
    "start_new_task",
    "rejected_task_busy",
    "off_topic_reply_sent",
    "scope_expansion_queued",
    "pivot_pause_cancelled",
    "cancelled",
]


@dataclass(frozen=True)
class IntakeOutcome:
    """What the WS handler should do next based on intake routing."""

    next_action: IntakeAction
    bucket: str  # RoutingBucket string, or "n/a" when routing was skipped
    detail: str = ""


SendJson = Callable[[dict[str, Any]], Awaitable[None]]
CancelCurrent = Callable[[], Awaitable[None]]
StartNewTask = Callable[[str], Awaitable[asyncio.Task[None]]]


_REJECT_BUSY_MSG = (
    "task already running (LongTaskInputRouter feed-in not yet implemented)"
)


async def handle_long_task_input(
    *,
    new_input: str,
    current_task: asyncio.Task[None] | None,
    current_goal_anchor: Any | None,
    current_task_id: str | None,
    send_json: SendJson,
    cancel_current_task: CancelCurrent,
    start_new_task: StartNewTask,
    long_task_router: LongTaskInputRouter | None = None,
) -> IntakeOutcome:
    """Decide what to do with a follow-up message arriving on the WS.

    Returns IntakeOutcome telling the caller:
      - 'start_new_task'         no task / no anchor — caller should run existing
                                 "spawn a fresh task" branch (preserves today's
                                 short-task behavior).
      - 'rejected_task_busy'     long task running and the input is on-topic /
                                 passthrough — feed-in to the live executor
                                 isn't wired yet, so we reject with an error.
      - 'off_topic_reply_sent'   polite "noted, will handle later" message
                                 dispatched; task untouched.
      - 'scope_expansion_queued' confirmation question dispatched; task
                                 untouched (user must confirm before scope grows).
      - 'pivot_pause_cancelled'  current task cancelled, pivot message
                                 dispatched. Caller decides whether to spawn a
                                 new task with the pivot content (TODO: explicit
                                 confirm-then-start flow).
      - 'cancelled'              explicit interrupt — task cancelled.
    """
    # Fast path: nothing to route around. Caller handles as a fresh task.
    if current_task is None or current_goal_anchor is None:
        return IntakeOutcome(
            next_action="start_new_task",
            bucket="n/a",
            detail="no current task or no goal_anchor — fall through to normal start",
        )

    router = long_task_router or LongTaskInputRouter()
    decision = await router.route(
        task_id=current_task_id or "",
        new_input=new_input,
        goal_anchor=current_goal_anchor,
        source="user",
    )

    bucket = decision.bucket
    log.info(
        "long_task_intake.routed",
        task_id=current_task_id,
        bucket=bucket,
        category=decision.classification.category,
    )

    # Executor / passthrough / on_topic_*: feed-in is the next phase; for now
    # we keep today's "reject with task already running" but tag the reason so
    # ops can tell the difference between the legacy short-task reject and the
    # known long-task TODO.
    if bucket in ("executor", "passthrough"):
        await send_json({"type": "error", "message": _REJECT_BUSY_MSG})
        return IntakeOutcome(
            next_action="rejected_task_busy",
            bucket=bucket,
            detail="on-topic / passthrough feed-in not yet implemented",
        )

    if bucket == "off_topic_reply":
        await send_json(
            {
                "type": "off_topic_reply",
                "content": decision.user_facing_reply or "",
            }
        )
        return IntakeOutcome(
            next_action="off_topic_reply_sent",
            bucket=bucket,
            detail="polite acknowledge sent; task continues",
        )

    if bucket == "scope_expansion_review":
        prefix = "Possible scope expansion. "
        body = decision.user_facing_reply or "Confirm?"
        await send_json(
            {
                "type": "scope_expansion_review",
                "content": prefix + body,
            }
        )
        return IntakeOutcome(
            next_action="scope_expansion_queued",
            bucket=bucket,
            detail="confirmation requested; task continues uninterrupted",
        )

    if bucket == "pivot_pause":
        await send_json(
            {
                "type": "pivot_pause",
                "content": decision.user_facing_reply or "",
            }
        )
        await cancel_current_task()
        # TODO: when an explicit user-confirm protocol exists, start a new
        # task with the pivot content here. For now we just pause and let the
        # caller decide.
        return IntakeOutcome(
            next_action="pivot_pause_cancelled",
            bucket=bucket,
            detail="current task cancelled; new task not auto-started",
        )

    if bucket == "cancel":
        await cancel_current_task()
        await send_json({"type": "interrupted"})
        return IntakeOutcome(
            next_action="cancelled",
            bucket=bucket,
            detail="interrupt signal — task cancelled",
        )

    # Belt + suspenders: unknown bucket shouldn't happen, but if it does we
    # don't want to silently hang. Reject like the executor branch.
    log.warning("long_task_intake.unexpected_bucket", bucket=bucket)
    await send_json({"type": "error", "message": _REJECT_BUSY_MSG})
    return IntakeOutcome(
        next_action="rejected_task_busy",
        bucket=bucket,
        detail=f"unexpected bucket {bucket} — defaulted to reject",
    )


__all__ = [
    "IntakeAction",
    "IntakeOutcome",
    "handle_long_task_input",
]
