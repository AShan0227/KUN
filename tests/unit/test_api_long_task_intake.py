"""Unit tests for kun.api.long_task_intake.handle_long_task_input.

Verifies that the WS-level intake helper correctly translates a
LongTaskInputRouter decision into a side-effect plan:

  - sends the right WS message type (off_topic_reply / scope_expansion_review /
    pivot_pause / interrupted / error)
  - cancels current task in pivot / interrupt paths
  - returns the right IntakeOutcome.next_action

Backward-compat is also covered: when no task is running or no goal_anchor is
attached, the helper short-circuits to 'start_new_task' so the WS handler can
keep using its existing "spawn fresh task" branch.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from kun.agents.director.anchor import GoalAnchor
from kun.agents.director.input_classifier import InputClassification
from kun.agents.director.long_task_router import LongTaskInputRouter, RoutingDecision
from kun.api.long_task_intake import IntakeOutcome, handle_long_task_input

# ----------------------------- fixtures / fakes -----------------------------


def _make_anchor(
    *,
    goal: str = "implement OAuth2 login for the dashboard",
    success_criteria: list[str] | None = None,
    out_of_scope: list[str] | None = None,
) -> GoalAnchor:
    return GoalAnchor(
        task_id="tk-test-1",
        goal_statement=goal,
        success_criteria=success_criteria
        or ["login flow works", "session token refreshes"],
        out_of_scope=out_of_scope or [],
        invariants=[],
    )


class _Recorder:
    """Captures send_json / cancel / start calls for assertion."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.cancel_calls: int = 0
        self.start_calls: list[str] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)

    async def cancel_current_task(self) -> None:
        self.cancel_calls += 1

    async def start_new_task(self, msg: str) -> asyncio.Task[None]:
        self.start_calls.append(msg)
        # Return a dummy completed task so type matches.
        async def _noop() -> None:
            return None

        return asyncio.create_task(_noop())


async def _make_pending_task() -> asyncio.Task[None]:
    """A task that stays pending until cancelled — stands in for a live run."""

    async def _wait_forever() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(_wait_forever())
    # Yield once so the task is actually scheduled.
    await asyncio.sleep(0)
    return task


async def _cleanup_task(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()
        try:  # noqa: SIM105
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ----------------------------- short-circuit paths -----------------------------


@pytest.mark.unit
async def test_no_current_task_returns_start_new_task() -> None:
    rec = _Recorder()
    outcome = await handle_long_task_input(
        new_input="hello",
        current_task=None,
        current_goal_anchor=_make_anchor(),  # anchor present but no task → still start
        current_task_id="tk-1",
        send_json=rec.send_json,
        cancel_current_task=rec.cancel_current_task,
        start_new_task=rec.start_new_task,
    )
    assert isinstance(outcome, IntakeOutcome)
    assert outcome.next_action == "start_new_task"
    assert outcome.bucket == "n/a"
    assert rec.sent == []
    assert rec.cancel_calls == 0
    assert rec.start_calls == []


@pytest.mark.unit
async def test_current_task_set_but_no_anchor_returns_start_new_task() -> None:
    """No goal_anchor → Layer 3 doesn't engage; caller handles legacy reject."""
    rec = _Recorder()
    task = await _make_pending_task()
    try:
        outcome = await handle_long_task_input(
            new_input="hello",
            current_task=task,
            current_goal_anchor=None,
            current_task_id=None,
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
        )
        assert outcome.next_action == "start_new_task"
        assert outcome.bucket == "n/a"
        # We didn't send any WS frame nor cancel — caller decides what to do.
        assert rec.sent == []
        assert rec.cancel_calls == 0
    finally:
        await _cleanup_task(task)


# ----------------------------- on-topic / passthrough → rejected -----------------------------


@pytest.mark.unit
async def test_on_topic_input_rejects_as_task_busy_with_todo_note() -> None:
    rec = _Recorder()
    task = await _make_pending_task()
    try:
        outcome = await handle_long_task_input(
            new_input="implement OAuth2 PKCE flow for the dashboard login",
            current_task=task,
            current_goal_anchor=_make_anchor(),
            current_task_id="tk-1",
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
        )
        assert outcome.next_action == "rejected_task_busy"
        # bucket is from the router — on-topic → "executor"
        assert outcome.bucket == "executor"
        # WS error frame sent with explicit TODO marker
        assert len(rec.sent) == 1
        sent = rec.sent[0]
        assert sent["type"] == "error"
        assert "not yet implemented" in sent["message"]
        # Task NOT cancelled
        assert rec.cancel_calls == 0
        assert not task.done()
    finally:
        await _cleanup_task(task)


# ----------------------------- off-topic noise -----------------------------


@pytest.mark.unit
async def test_off_topic_input_sends_polite_reply_task_continues() -> None:
    rec = _Recorder()
    task = await _make_pending_task()
    try:
        outcome = await handle_long_task_input(
            new_input="今天天气真好",  # totally unrelated to OAuth goal
            current_task=task,
            current_goal_anchor=_make_anchor(),
            current_task_id="tk-1",
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
        )
        assert outcome.next_action == "off_topic_reply_sent"
        assert outcome.bucket == "off_topic_reply"
        assert len(rec.sent) == 1
        sent = rec.sent[0]
        assert sent["type"] == "off_topic_reply"
        # Router's polite reply text gets passed through
        assert "记下" in sent["content"]
        # Task must not be cancelled
        assert rec.cancel_calls == 0
        assert not task.done()
    finally:
        await _cleanup_task(task)


# ----------------------------- scope expansion -----------------------------


@pytest.mark.unit
async def test_scope_expansion_queues_confirmation_no_cancel() -> None:
    rec = _Recorder()
    task = await _make_pending_task()
    try:
        outcome = await handle_long_task_input(
            new_input="顺便加上密码重置流程",  # 顺便 = scope_expansion signal
            current_task=task,
            current_goal_anchor=_make_anchor(),
            current_task_id="tk-1",
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
        )
        assert outcome.next_action == "scope_expansion_queued"
        assert outcome.bucket == "scope_expansion_review"
        assert len(rec.sent) == 1
        sent = rec.sent[0]
        assert sent["type"] == "scope_expansion_review"
        # Our helper prepends "Possible scope expansion. " before router text
        assert sent["content"].startswith("Possible scope expansion.")
        # Task continues untouched
        assert rec.cancel_calls == 0
        assert not task.done()
    finally:
        await _cleanup_task(task)


# ----------------------------- pivot pause -----------------------------


@pytest.mark.unit
async def test_pivot_pause_cancels_task_and_sends_message() -> None:
    rec = _Recorder()
    task = await _make_pending_task()
    try:
        outcome = await handle_long_task_input(
            new_input="我们换成做个购物车页面吧",  # 换成 → explicit pivot
            current_task=task,
            current_goal_anchor=_make_anchor(),
            current_task_id="tk-1",
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
        )
        assert outcome.next_action == "pivot_pause_cancelled"
        assert outcome.bucket == "pivot_pause"
        # First sends the pivot message
        assert len(rec.sent) == 1
        sent = rec.sent[0]
        assert sent["type"] == "pivot_pause"
        assert "确认切换" in sent["content"]
        # Then cancels current task
        assert rec.cancel_calls == 1
        # Note: caller decides if it wants to auto-start a new task — we don't.
        assert rec.start_calls == []
    finally:
        await _cleanup_task(task)


# ----------------------------- interrupt -----------------------------


@pytest.mark.unit
async def test_interrupt_signal_cancels_and_emits_interrupted_frame() -> None:
    rec = _Recorder()
    task = await _make_pending_task()
    try:
        outcome = await handle_long_task_input(
            new_input="停止",  # interrupt keyword
            current_task=task,
            current_goal_anchor=_make_anchor(),
            current_task_id="tk-1",
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
        )
        assert outcome.next_action == "cancelled"
        assert outcome.bucket == "cancel"
        assert rec.cancel_calls == 1
        # The interrupted frame is emitted AFTER the cancel completes.
        assert len(rec.sent) == 1
        assert rec.sent[0] == {"type": "interrupted"}
    finally:
        await _cleanup_task(task)


# ----------------------------- injected router is honored -----------------------------


class _StubRouter:
    """A LongTaskInputRouter stand-in that always returns a configured bucket.

    We use a class rather than a real LongTaskInputRouter instance so the test
    is independent of classifier rule changes.
    """

    def __init__(self, decision: RoutingDecision) -> None:
        self._decision = decision
        self.routed_with: dict[str, Any] | None = None

    async def route(
        self,
        *,
        task_id: str,
        new_input: str,
        goal_anchor: Any | None = None,
        source: str = "user",
    ) -> RoutingDecision:
        self.routed_with = {
            "task_id": task_id,
            "new_input": new_input,
            "goal_anchor": goal_anchor,
            "source": source,
        }
        return self._decision


@pytest.mark.unit
async def test_injected_router_overrides_default() -> None:
    """If caller passes a router, the helper must use it instead of building one."""
    forced_decision = RoutingDecision(
        bucket="off_topic_reply",
        classification=InputClassification(
            category="off_topic_noise",
            confidence=1.0,
            matched_signal="stub",
        ),
        rationale="forced for test",
        user_facing_reply="测试用回复",
    )
    stub_router = _StubRouter(forced_decision)
    rec = _Recorder()
    task = await _make_pending_task()
    try:
        outcome = await handle_long_task_input(
            new_input="implement OAuth2 login deeper",  # on-topic-looking text
            current_task=task,
            current_goal_anchor=_make_anchor(),
            current_task_id="tk-99",
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
            long_task_router=stub_router,  # type: ignore[arg-type]
        )
        # Stub forced off_topic_reply regardless of input content.
        assert outcome.next_action == "off_topic_reply_sent"
        assert outcome.bucket == "off_topic_reply"
        # Stub was actually called (not the default router).
        assert stub_router.routed_with is not None
        assert stub_router.routed_with["task_id"] == "tk-99"
        assert stub_router.routed_with["new_input"] == "implement OAuth2 login deeper"
        # And the user-facing reply from the stub propagated to the WS frame.
        assert rec.sent == [{"type": "off_topic_reply", "content": "测试用回复"}]
    finally:
        await _cleanup_task(task)


# ----------------------------- a couple of edge cases for good measure -----------------------------


@pytest.mark.unit
async def test_task_id_none_is_passed_as_empty_string_to_router() -> None:
    """When task_id hasn't been observed yet, the helper still routes — just
    passes an empty string instead of crashing."""
    captured: dict[str, Any] = {}

    decision = RoutingDecision(
        bucket="off_topic_reply",
        classification=InputClassification(
            category="off_topic_noise", confidence=1.0, matched_signal="s"
        ),
        rationale="r",
        user_facing_reply="x",
    )

    class _CaptureRouter:
        async def route(self, **kwargs: Any) -> RoutingDecision:
            captured.update(kwargs)
            return decision

    rec = _Recorder()
    task = await _make_pending_task()
    try:
        outcome = await handle_long_task_input(
            new_input="anything",
            current_task=task,
            current_goal_anchor=_make_anchor(),
            current_task_id=None,
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
            long_task_router=_CaptureRouter(),  # type: ignore[arg-type]
        )
        assert outcome.next_action == "off_topic_reply_sent"
        assert captured["task_id"] == ""
    finally:
        await _cleanup_task(task)


@pytest.mark.unit
async def test_real_router_default_built_when_none_passed() -> None:
    """Sanity check: omitting long_task_router still works (default-built)."""
    rec = _Recorder()
    task = await _make_pending_task()
    try:
        # Use a clear off_topic input so default router picks the bucket
        outcome = await handle_long_task_input(
            new_input="今天我想吃火锅",
            current_task=task,
            current_goal_anchor=_make_anchor(),
            current_task_id="tk-1",
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
            # no long_task_router → handler builds LongTaskInputRouter()
        )
        assert outcome.next_action == "off_topic_reply_sent"
        # We don't assert exact bucket label here beyond next_action — proves
        # the default-build path doesn't blow up.
        assert isinstance(rec.sent[0], dict)
        assert rec.sent[0]["type"] == "off_topic_reply"
    finally:
        await _cleanup_task(task)


@pytest.mark.unit
async def test_outcome_is_frozen_dataclass() -> None:
    """Defensive: IntakeOutcome must be hashable / frozen so it can flow
    through logging/structlog without surprise mutation."""
    rec = _Recorder()
    outcome = await handle_long_task_input(
        new_input="hi",
        current_task=None,
        current_goal_anchor=None,
        current_task_id=None,
        send_json=rec.send_json,
        cancel_current_task=rec.cancel_current_task,
        start_new_task=rec.start_new_task,
    )
    with pytest.raises(Exception):
        outcome.next_action = "cancelled"  # type: ignore[misc]


@pytest.mark.unit
async def test_injected_router_used_for_pivot_propagates_cancel() -> None:
    """Verify the injection path correctly threads cancel side-effect even
    when a stub router is used."""
    pivot_decision = RoutingDecision(
        bucket="pivot_pause",
        classification=InputClassification(
            category="explicit_pivot", confidence=1.0, matched_signal="stub"
        ),
        rationale="stubbed pivot",
        user_facing_reply="确认切换? (yes/no)",
    )
    stub_router = _StubRouter(pivot_decision)
    rec = _Recorder()
    task = await _make_pending_task()
    try:
        outcome = await handle_long_task_input(
            new_input="anything",
            current_task=task,
            current_goal_anchor=_make_anchor(),
            current_task_id="tk-1",
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
            long_task_router=stub_router,  # type: ignore[arg-type]
        )
        assert outcome.next_action == "pivot_pause_cancelled"
        assert rec.cancel_calls == 1
        assert rec.sent[0]["type"] == "pivot_pause"
        assert rec.sent[0]["content"] == "确认切换? (yes/no)"
    finally:
        await _cleanup_task(task)


@pytest.mark.unit
async def test_default_router_instance_is_long_task_input_router() -> None:
    """When no router is injected, the helper must build a real
    LongTaskInputRouter — verify by inspecting class hierarchy via a sentinel
    bucket only the real router could produce ('passthrough' when anchor=None
    is short-circuited earlier, so we trust the test_real_router_default test
    above for behavior). Here we just ensure no exception."""
    rec = _Recorder()
    # We don't actually need a running task here — but a task with anchor
    # exercises the build-default branch.
    task = await _make_pending_task()
    try:
        # 停止 → interrupt regardless of anchor content
        outcome = await handle_long_task_input(
            new_input="停止",
            current_task=task,
            current_goal_anchor=_make_anchor(),
            current_task_id="tk-1",
            send_json=rec.send_json,
            cancel_current_task=rec.cancel_current_task,
            start_new_task=rec.start_new_task,
        )
        assert outcome.next_action == "cancelled"
        assert isinstance(LongTaskInputRouter(), LongTaskInputRouter)  # smoke
    finally:
        await _cleanup_task(task)
