"""Long-task input router (ADR-022 Layer 3 runtime wiring, LT.A).

`classify_input` 是分类器, 这一层把"分类→路由动作"做成可注入服务, 让 KUN
的实际 message intake (WS / REST / event bus) 调一次 route() 就能在
长任务模式下做出正确决策:

  - on_topic_progress / on_topic_clarification → 喂 Executor (working context)
  - off_topic_noise                            → 独立 channel 礼貌回复, 不进 Executor
  - scope_expansion                            → 触发 RCDH L0 review, 等用户确认
  - explicit_pivot                             → pause 当前任务 + new task 流程
  - interrupt                                  → cancel 流程

设计要点 (ADR-024 frozen_dataclass_agent_io_contract 模式):
  - 纯函数主体, 任何 side-effect (回复用户 / 触发 RCDH / pause task) 全注入
  - 返回 frozen RoutingDecision, caller 决定是否真触发 (router 给建议, 不强推)
  - 短任务/无 goal_anchor 时直接 pass-through 给 Executor (router 只在 long-task 模式干预)

为什么不直接在 orchestrator.run 里写 if/elif:
  - 这是个 cross-cutting concern (任何 message intake path 都要), 不属于 orchestrator
  - 测试需要注入 fake handler — 独立 service 易测
  - 未来可能加 LLM 兜底分类 (现在是 rule-based), 接口稳
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from kun.agents.director.input_classifier import (
    InputClassification,
    classify_input,
)
from kun.core.logging import get_logger

log = get_logger("kun.agents.director.long_task_router")


RoutingBucket = Literal[
    "executor",  # 喂 Executor (working context) — on_topic_*
    "off_topic_reply",  # 独立 channel 回复用户, 不进 Executor
    "scope_expansion_review",  # 触发 RCDH L0 / Strategist review
    "pivot_pause",  # pause 当前 task + new task 流程
    "cancel",  # 中断 / 取消
    "passthrough",  # 短任务 / 无 anchor → 不干预, 直接给 Executor
]


@dataclass(frozen=True)
class RoutingDecision:
    """长任务输入路由决策 — pure data, caller 决定怎么消费."""

    bucket: RoutingBucket
    classification: InputClassification
    rationale: str
    user_facing_reply: str | None = None
    """For off_topic_reply / pivot_pause: 建议的礼貌回复文本"""


# ---- Callback types (all optional, all DI-injectable) ----

OffTopicReplier = Callable[[str, str], Awaitable[None]]
"""(task_id, reply_text) → emit polite reply to user via independent channel."""

ScopeExpansionEmitter = Callable[[str, str, dict[str, Any]], Awaitable[None]]
"""(task_id, raw_input, hint) → trigger RCDH L0 review."""

PivotHandler = Callable[[str, str], Awaitable[None]]
"""(task_id, raw_input) → pause current task + queue new task creation."""

CancelHandler = Callable[[str], Awaitable[None]]
"""(task_id) → run cancel flow for current task."""


def _build_off_topic_reply(raw_input: str) -> str:
    """生成礼貌的"我先记下"回复. 截 50 字防超长."""
    snippet = raw_input.strip()
    if len(snippet) > 50:
        snippet = snippet[:47] + "..."
    return f"我先记下「{snippet}」, 当前任务完成后再处理."


def _build_pivot_confirmation_reply(raw_input: str) -> str:
    """pivot 时建议的回复 — 给用户二次确认机会."""
    snippet = raw_input.strip()
    if len(snippet) > 50:
        snippet = snippet[:47] + "..."
    return (
        f"听上去你想切换到「{snippet}」. 当前任务还没完成, 是确认切换吗? "
        f"(yes 切换 / no 继续当前任务)"
    )


class LongTaskInputRouter:
    """长任务模式下输入路由 service. 注入 callback, 调 route() 即可."""

    def __init__(
        self,
        *,
        off_topic_replier: OffTopicReplier | None = None,
        scope_expansion_emitter: ScopeExpansionEmitter | None = None,
        pivot_handler: PivotHandler | None = None,
        cancel_handler: CancelHandler | None = None,
    ) -> None:
        self._off_topic_replier = off_topic_replier
        self._scope_expansion_emitter = scope_expansion_emitter
        self._pivot_handler = pivot_handler
        self._cancel_handler = cancel_handler

    async def route(
        self,
        *,
        task_id: str,
        new_input: str,
        goal_anchor: Any | None = None,
        source: str = "user",
    ) -> RoutingDecision:
        """分类 + 触发对应 side-effect, 返回 RoutingDecision.

        - goal_anchor=None (短任务 / 长任务但未挂 anchor) → bucket='passthrough'
          调用方应直接把消息喂 Executor (Layer 3 不干预)
        - 否则 6 类分流 + 调对应注入 callback (callback 缺失则 skip side-effect
          但仍返回 RoutingDecision, 调用方可自己处理)
        """
        if goal_anchor is None:
            # 短任务 / 未挂 anchor → Layer 3 不干预
            return RoutingDecision(
                bucket="passthrough",
                classification=InputClassification(
                    category="on_topic_progress",
                    confidence=1.0,
                    matched_signal="no_goal_anchor",
                ),
                rationale="goal_anchor not set — Layer 3 routing skipped",
            )

        classification = classify_input(
            new_input, goal_anchor=goal_anchor, source=source
        )
        log.info(
            "long_task_router.classified",
            task_id=task_id,
            category=classification.category,
            confidence=classification.confidence,
            matched=classification.matched_signal,
        )

        # 6 类分流
        if classification.category in ("on_topic_progress", "on_topic_clarification"):
            return RoutingDecision(
                bucket="executor",
                classification=classification,
                rationale=f"on_topic ({classification.category}) → integrate to working context",
            )

        if classification.category == "off_topic_noise":
            reply = _build_off_topic_reply(new_input)
            await self._safe_call(
                self._off_topic_replier,
                task_id,
                reply,
                _kind="off_topic_replier",
            )
            return RoutingDecision(
                bucket="off_topic_reply",
                classification=classification,
                rationale="off_topic — replied politely, not pushed to Executor context",
                user_facing_reply=reply,
            )

        if classification.category == "scope_expansion":
            await self._safe_call(
                self._scope_expansion_emitter,
                task_id,
                new_input,
                dict(classification.integration_hint),
                _kind="scope_expansion_emitter",
            )
            return RoutingDecision(
                bucket="scope_expansion_review",
                classification=classification,
                rationale="scope expansion suspected — emitted to RCDH L0 review",
            )

        if classification.category == "explicit_pivot":
            confirm = _build_pivot_confirmation_reply(new_input)
            await self._safe_call(
                self._pivot_handler,
                task_id,
                new_input,
                _kind="pivot_handler",
            )
            return RoutingDecision(
                bucket="pivot_pause",
                classification=classification,
                rationale="explicit pivot — paused; awaiting user confirmation",
                user_facing_reply=confirm,
            )

        if classification.category == "interrupt":
            await self._safe_call(
                self._cancel_handler, task_id, _kind="cancel_handler"
            )
            return RoutingDecision(
                bucket="cancel",
                classification=classification,
                rationale="interrupt signal — cancel handler invoked",
            )

        # 不应到这 (6 类穷尽); 但兜底 passthrough 防 silent loss
        log.warning(
            "long_task_router.unexpected_category",
            category=classification.category,
        )
        return RoutingDecision(
            bucket="passthrough",
            classification=classification,
            rationale=f"unexpected category {classification.category} — pass through",
        )

    async def _safe_call(
        self,
        cb: Callable[..., Awaitable[None]] | None,
        *args: Any,
        _kind: str,
    ) -> None:
        """调注入 callback. cb 为 None 时跳过 (调用方接管); 异常 log 不上抛.

        side-effect 失败不应打挂 router 主路径 — RoutingDecision 仍要返回让
        caller 知道分类结果.
        """
        if cb is None:
            log.debug("long_task_router.callback_missing", kind=_kind)
            return
        try:
            await cb(*args)
        except Exception as e:
            log.warning(
                "long_task_router.callback_raised",
                kind=_kind,
                error=str(e),
            )


__all__ = [
    "CancelHandler",
    "LongTaskInputRouter",
    "OffTopicReplier",
    "PivotHandler",
    "RoutingBucket",
    "RoutingDecision",
    "ScopeExpansionEmitter",
]
