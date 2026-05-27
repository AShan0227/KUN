"""LT.A — LongTaskInputRouter 单测 (ADR-022 Layer 3 runtime wiring)."""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.director.anchor import GoalAnchor
from kun.agents.director.long_task_router import (
    LongTaskInputRouter,
    RoutingDecision,
)


def _anchor(
    *,
    goal: str = "implement OAuth2 login for the dashboard",
    success_criteria: list[str] | None = None,
    out_of_scope: list[str] | None = None,
) -> GoalAnchor:
    return GoalAnchor(
        task_id="tk-1",
        goal_statement=goal,
        success_criteria=success_criteria
        or ["login flow works", "session token refreshes"],
        out_of_scope=out_of_scope or [],
        invariants=[],
    )


class _CallRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def make(self, kind: str):
        async def _cb(*args: Any) -> None:
            self.calls.append((kind, args))

        return _cb


# ---- passthrough (no goal anchor) ----


@pytest.mark.asyncio
async def test_passthrough_when_no_goal_anchor() -> None:
    """短任务 / 未挂 anchor → Layer 3 不干预."""
    rec = _CallRecorder()
    router = LongTaskInputRouter(
        off_topic_replier=rec.make("reply"),
        cancel_handler=rec.make("cancel"),
    )
    decision = await router.route(
        task_id="tk-1", new_input="anything", goal_anchor=None
    )
    assert isinstance(decision, RoutingDecision)
    assert decision.bucket == "passthrough"
    assert rec.calls == []  # no side-effects when no anchor


# ---- on_topic ----


@pytest.mark.asyncio
async def test_on_topic_progress_routes_to_executor() -> None:
    rec = _CallRecorder()
    router = LongTaskInputRouter(off_topic_replier=rec.make("reply"))
    decision = await router.route(
        task_id="tk-1",
        new_input="oauth2 login should also persist tokens in session",
        goal_anchor=_anchor(),
    )
    assert decision.bucket == "executor"
    assert decision.classification.category == "on_topic_progress"
    assert rec.calls == []  # 不应该走 off_topic replier


@pytest.mark.asyncio
async def test_on_topic_clarification_routes_to_executor() -> None:
    router = LongTaskInputRouter()
    decision = await router.route(
        task_id="tk-1",
        new_input="actually I meant the oauth2 login should use PKCE",
        goal_anchor=_anchor(),
    )
    assert decision.bucket == "executor"
    assert decision.classification.category == "on_topic_clarification"


# ---- off_topic_noise ----


@pytest.mark.asyncio
async def test_off_topic_noise_replies_polite_and_skips_executor() -> None:
    rec = _CallRecorder()
    router = LongTaskInputRouter(off_topic_replier=rec.make("reply"))
    decision = await router.route(
        task_id="tk-1",
        new_input="今天天气真好",  # 完全无关
        goal_anchor=_anchor(),
    )
    assert decision.bucket == "off_topic_reply"
    assert decision.user_facing_reply is not None
    assert "记下" in decision.user_facing_reply
    # off_topic_replier 被调
    assert len(rec.calls) == 1
    kind, args = rec.calls[0]
    assert kind == "reply"
    assert args[0] == "tk-1"
    assert "记下" in args[1]


@pytest.mark.asyncio
async def test_off_topic_reply_text_truncated_to_50_chars() -> None:
    rec = _CallRecorder()
    router = LongTaskInputRouter(off_topic_replier=rec.make("reply"))
    long_msg = "今天" * 100  # 200 chars
    decision = await router.route(
        task_id="tk-1",
        new_input=long_msg,
        goal_anchor=_anchor(),
    )
    assert decision.user_facing_reply is not None
    # 整体 reply 大约 60 字内 (snippet 50 + 固定模板)
    assert "..." in decision.user_facing_reply


@pytest.mark.asyncio
async def test_off_topic_without_replier_still_returns_decision() -> None:
    """注入 callback 缺失时, decision 仍正确返回."""
    router = LongTaskInputRouter()  # 无 callback
    decision = await router.route(
        task_id="tk-1", new_input="random noise", goal_anchor=_anchor()
    )
    assert decision.bucket == "off_topic_reply"
    assert decision.user_facing_reply is not None


# ---- scope_expansion ----


@pytest.mark.asyncio
async def test_scope_expansion_triggers_rcdh_emitter() -> None:
    rec = _CallRecorder()
    router = LongTaskInputRouter(scope_expansion_emitter=rec.make("rcdh"))
    decision = await router.route(
        task_id="tk-1",
        new_input="also please add password reset flow",
        goal_anchor=_anchor(),
    )
    assert decision.bucket == "scope_expansion_review"
    assert decision.classification.category == "scope_expansion"
    assert len(rec.calls) == 1
    kind, args = rec.calls[0]
    assert kind == "rcdh"
    assert args[0] == "tk-1"
    assert "password reset" in args[1]
    # third arg is integration_hint dict
    assert isinstance(args[2], dict)


# ---- explicit_pivot ----


@pytest.mark.asyncio
async def test_explicit_pivot_pauses_and_asks_confirmation() -> None:
    rec = _CallRecorder()
    router = LongTaskInputRouter(pivot_handler=rec.make("pivot"))
    decision = await router.route(
        task_id="tk-1",
        new_input="我们换成做个购物车的页面吧",  # 不含 interrupt 词
        goal_anchor=_anchor(),
    )
    # pivot 关键词 "换成" + 与 goal (oauth/login) 无重叠 → explicit_pivot
    assert decision.bucket == "pivot_pause"
    assert decision.classification.category == "explicit_pivot"
    assert decision.user_facing_reply is not None
    assert "确认切换" in decision.user_facing_reply
    assert len(rec.calls) == 1
    assert rec.calls[0][0] == "pivot"


# ---- interrupt ----


@pytest.mark.asyncio
async def test_interrupt_invokes_cancel_handler() -> None:
    rec = _CallRecorder()
    router = LongTaskInputRouter(cancel_handler=rec.make("cancel"))
    decision = await router.route(
        task_id="tk-1",
        new_input="stop",
        goal_anchor=_anchor(),
    )
    assert decision.bucket == "cancel"
    assert decision.classification.category == "interrupt"
    assert len(rec.calls) == 1
    assert rec.calls[0][0] == "cancel"
    assert rec.calls[0][1] == ("tk-1",)


@pytest.mark.asyncio
async def test_interrupt_chinese() -> None:
    rec = _CallRecorder()
    router = LongTaskInputRouter(cancel_handler=rec.make("cancel"))
    decision = await router.route(
        task_id="tk-1",
        new_input="停止吧, 别做了",
        goal_anchor=_anchor(),
    )
    assert decision.bucket == "cancel"


# ---- callback errors do not break the route ----


@pytest.mark.asyncio
async def test_callback_exception_does_not_break_route() -> None:
    async def bad_cb(*_: Any) -> None:
        raise RuntimeError("kafka down")

    router = LongTaskInputRouter(off_topic_replier=bad_cb)
    decision = await router.route(
        task_id="tk-1",
        new_input="无关消息",
        goal_anchor=_anchor(),
    )
    # 决策仍返回, 调用方仍能基于 decision 做事
    assert decision.bucket == "off_topic_reply"


# ---- priority: interrupt > pivot > clarification ----


@pytest.mark.asyncio
async def test_interrupt_beats_pivot_when_both_match() -> None:
    """interrupt 是最高优先级, 即使消息也含 pivot 关键词."""
    rec = _CallRecorder()
    router = LongTaskInputRouter(
        pivot_handler=rec.make("pivot"),
        cancel_handler=rec.make("cancel"),
    )
    decision = await router.route(
        task_id="tk-1",
        new_input="cancel this and switch to something else",  # 两个都命中
        goal_anchor=_anchor(),
    )
    assert decision.bucket == "cancel"
    # 只触发 cancel, 不触发 pivot
    kinds = [k for k, _ in rec.calls]
    assert kinds == ["cancel"]


# ---- source param flows through ----


@pytest.mark.asyncio
async def test_source_param_does_not_break() -> None:
    router = LongTaskInputRouter()
    decision = await router.route(
        task_id="tk-1",
        new_input="oauth2 status?",
        goal_anchor=_anchor(),
        source="tool_output",
    )
    # 不管 source 是什么, 该走分类还走分类
    assert isinstance(decision, RoutingDecision)
