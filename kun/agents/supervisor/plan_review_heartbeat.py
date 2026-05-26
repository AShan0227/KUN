"""Plan Review Heartbeat — ADR-022 Layer 4 长任务定期复审.

每 N 步 / 每 M 秒, Supervisor 注入一次 Plan Review:
  - Executor 自评 (我还在 anchor 上吗? criteria 进展如何?)
  - 工程化规则评判: aligned / drifting / off_track
  - 对应动作: continue / pause_for_anchor_recheck / trigger_rcdh_level_0

工程化优先 (规则 + 阈值), LLM 兜底放 L3 闭环再上.

这是 anti-drift 的"心跳节律"层 — 顶部 anchor pinning (Layer 2) 是空间,
heartbeat (Layer 4) 是时间. 两者协同保证长任务不会"开头记得目标, 中
间忘了".
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kun.core.ids import new_id
from kun.core.logging import get_logger

log = get_logger("kun.agents.supervisor.plan_review_heartbeat")


_DEFAULT_STEP_INTERVAL = 3
_DEFAULT_TIME_INTERVAL_SEC = 300.0


@dataclass
class _TaskCounter:
    """单任务计步 + 计时状态."""

    task_id: str
    anchor_id: str
    steps_since_last_review: int = 0
    last_review_at: datetime | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    total_reviews: int = 0
    total_steps: int = 0

    def record_step(self) -> None:
        self.steps_since_last_review += 1
        self.total_steps += 1

    def mark_reviewed(self, now: datetime) -> None:
        self.steps_since_last_review = 0
        self.last_review_at = now
        self.total_reviews += 1


@dataclass(frozen=True)
class ReviewTrigger:
    """一次 Plan Review 事件 — 由 heartbeat 工程化判定后生成."""

    review_id: str
    task_id: str
    anchor_id: str
    triggered_at_step: int
    triggered_at_time: datetime
    reason: str  # "step_threshold" / "time_threshold" / "step_threshold+time_threshold"
    payload: dict[str, Any]


ReviewRequestEmitter = Callable[[ReviewTrigger], Awaitable[None]]
"""异步回调签名 — 把 ReviewTrigger 写到 plan_reviews 表 / 推 Executor.
测试用 fake; L2.7+ 接 DB writer."""


def evaluate_executor_self_report(
    self_report: dict[str, Any],
    *,
    goal_tokens: set[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """工程化规则评判 self report → verdict + drift_evidence.

    Verdict:
      aligned    — 无 drift 信号
      drifting   — 1 个 drift 信号 (轻度漂移)
      off_track  — ≥2 drift 信号 (明确跑题)

    规则:
      1. self_report.scope_creep_detected == True
      2. self_report.on_anchor == False
      3. self_report.recent_step_summary 与 goal_tokens 零交集
      4. criteria_done_count 比上次少 (回退)
    """
    drift_evidence: list[dict[str, Any]] = []

    if self_report.get("scope_creep_detected"):
        drift_evidence.append({"type": "self_reported_scope_creep"})

    if self_report.get("on_anchor") is False:
        drift_evidence.append({"type": "self_reported_off_anchor"})

    recent_summary = str(self_report.get("recent_step_summary", "")).lower()
    if goal_tokens and recent_summary:
        hits = sum(1 for t in goal_tokens if t.lower() in recent_summary)
        if hits == 0:
            drift_evidence.append(
                {
                    "type": "no_goal_token_in_recent_step",
                    "goal_tokens_checked": sorted(goal_tokens)[:8],
                }
            )

    criteria_now = self_report.get("criteria_done_count")
    criteria_prev = self_report.get("criteria_done_count_prev")
    if (
        criteria_now is not None
        and criteria_prev is not None
        and criteria_now < criteria_prev
    ):
        drift_evidence.append(
            {
                "type": "criteria_regress",
                "from": criteria_prev,
                "to": criteria_now,
            }
        )

    if len(drift_evidence) >= 2:
        verdict = "off_track"
    elif len(drift_evidence) == 1:
        verdict = "drifting"
    else:
        verdict = "aligned"

    return verdict, drift_evidence


def derive_action(verdict: str) -> str:
    """Verdict → 下一步动作 (主线侧).

    aligned   → continue                   (Executor 继续)
    drifting  → pause_for_anchor_recheck   (Director 重新对 anchor)
    off_track → trigger_rcdh_level_0       (Supervisor 走 RCDH 设计层根因)
    """
    if verdict == "aligned":
        return "continue"
    if verdict == "drifting":
        return "pause_for_anchor_recheck"
    return "trigger_rcdh_level_0"


class PlanReviewHeartbeat:
    """长任务 Plan Review Heartbeat (ADR-022 Layer 4).

    入口:
      on_step_completed(...)  — Executor 完成一步, 累计计步 + 检查阈值
      on_idle_tick(...)       — Heartbeat 定时器到点, 只查时间阈值

    阈值任一命中 → 评判 self_report → 生成 ReviewTrigger →
    emitter(trigger) 异步落库 / 推 Executor.

    Per-task in-memory 状态; 真生产 multi-process 用 Redis.
    """

    def __init__(
        self,
        *,
        step_interval: int = _DEFAULT_STEP_INTERVAL,
        time_interval_sec: float = _DEFAULT_TIME_INTERVAL_SEC,
        emitter: ReviewRequestEmitter | None = None,
    ) -> None:
        if step_interval < 1:
            raise ValueError("step_interval must be >= 1")
        if time_interval_sec <= 0:
            raise ValueError("time_interval_sec must be > 0")
        self._step_interval = step_interval
        self._time_interval = timedelta(seconds=time_interval_sec)
        self._emitter = emitter
        self._counters: dict[str, _TaskCounter] = {}
        self._lock = asyncio.Lock()

    def counter_for(self, task_id: str, anchor_id: str) -> _TaskCounter:
        if task_id not in self._counters:
            self._counters[task_id] = _TaskCounter(task_id=task_id, anchor_id=anchor_id)
        return self._counters[task_id]

    def reset(self, task_id: str) -> None:
        """任务结束时调, 清掉计数器."""
        self._counters.pop(task_id, None)

    async def on_step_completed(
        self,
        *,
        task_id: str,
        anchor_id: str,
        executor_self_report: dict[str, Any],
        goal_tokens: set[str] | None = None,
    ) -> ReviewTrigger | None:
        """Executor 完成一步 → 通知 heartbeat. 阈值到 → 返回 trigger."""
        return await self._tick(
            task_id=task_id,
            anchor_id=anchor_id,
            executor_self_report=executor_self_report,
            goal_tokens=goal_tokens,
            increment_step=True,
            check_step=True,
        )

    async def on_idle_tick(
        self,
        *,
        task_id: str,
        anchor_id: str,
        executor_self_report: dict[str, Any],
        goal_tokens: set[str] | None = None,
    ) -> ReviewTrigger | None:
        """定时心跳 (无 step 推进) → 只查时间阈值. 用于 Executor 卡住时."""
        return await self._tick(
            task_id=task_id,
            anchor_id=anchor_id,
            executor_self_report=executor_self_report,
            goal_tokens=goal_tokens,
            increment_step=False,
            check_step=False,
        )

    async def _tick(
        self,
        *,
        task_id: str,
        anchor_id: str,
        executor_self_report: dict[str, Any],
        goal_tokens: set[str] | None,
        increment_step: bool,
        check_step: bool,
    ) -> ReviewTrigger | None:
        async with self._lock:
            counter = self.counter_for(task_id, anchor_id)
            if increment_step:
                counter.record_step()
            now = datetime.now(UTC)

            reasons = self._check_thresholds(counter, now, check_step=check_step)
            if not reasons:
                return None

            verdict, drift_evidence = evaluate_executor_self_report(
                executor_self_report, goal_tokens=goal_tokens
            )
            action = derive_action(verdict)
            counter.mark_reviewed(now)

            trigger = ReviewTrigger(
                review_id=new_id("plan_review"),
                task_id=task_id,
                anchor_id=anchor_id,
                triggered_at_step=counter.total_steps,
                triggered_at_time=now,
                reason="+".join(reasons),
                payload={
                    "executor_self_report": executor_self_report,
                    "supervisor_verdict": verdict,
                    "drift_evidence": drift_evidence,
                    "action_taken": action,
                    "trigger_reasons": reasons,
                    "total_steps": counter.total_steps,
                    "total_reviews": counter.total_reviews,
                    "idle_tick": not increment_step,
                },
            )

        if self._emitter is not None:
            try:
                await self._emitter(trigger)
            except Exception as e:
                log.warning(
                    "plan_review_heartbeat.emit_failed",
                    error=str(e),
                    review_id=trigger.review_id,
                )
        return trigger

    def _check_thresholds(
        self,
        counter: _TaskCounter,
        now: datetime,
        *,
        check_step: bool,
    ) -> list[str]:
        reasons: list[str] = []
        if check_step and counter.steps_since_last_review >= self._step_interval:
            reasons.append("step_threshold")
        last = counter.last_review_at or counter.started_at
        if now - last >= self._time_interval:
            reasons.append("time_threshold")
        return reasons


__all__ = [
    "PlanReviewHeartbeat",
    "ReviewRequestEmitter",
    "ReviewTrigger",
    "derive_action",
    "evaluate_executor_self_report",
]
