"""Safety Guards — 致命差评第一批的 5 个硬约束 (V2.1 §5.2 + §11.4-11.5).

5 组件:
- KillSwitch (§5.2.3 / T55): 任何 step ≤500ms 收到 SIGSTOP
- TokenMeter (§5.2.1 / T46+T47): token 实时仪表盘 + 单步上限
- PlanOnlyGate (§5.2.4 / T51): 删除/部署/支付/跨租户强制 plan-only + human-gate
- TaskTimeoutGuard (§5.2.2 / T52): 任务整体超时
- ZeroTelemetryEnforcer (§11.5 / T56): 默认零回传, 用户审计权

每个组件独立可测, 接 orchestrator hook.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

logger = logging.getLogger(__name__)


# ============================================================================
# T55 Kill Switch (§5.2.3): 任何 step ≤500ms 收到 SIGSTOP
# ============================================================================


@dataclass
class KillSignal:
    """Kill 信号."""

    task_id: str
    requested_at: datetime
    reason: str = "user_interrupt"
    confirm_token_required: bool = False


class KillSwitch:
    """Kill Switch — 紧急中断 (V2.1 §5.2.3 / T55).

    SLA: 任何 task 必须 ≤500ms 收到 SIGSTOP.
    实现: per-task asyncio.Event 信号.

    用法:
        ks = KillSwitch()
        ks.register_task("tk-123")
        # 用户点停 → ks.kill("tk-123", "user clicked stop")
        # orchestrator 在每步开头查 ks.is_killed("tk-123") → cancel
    """

    def __init__(self, sla_ms: int = 500) -> None:
        self._signals: dict[str, KillSignal] = {}
        self._events: dict[str, asyncio.Event] = {}
        self.sla_ms = sla_ms

    def register_task(self, task_id: str) -> None:
        """任务开始时注册."""
        if task_id not in self._events:
            self._events[task_id] = asyncio.Event()

    def kill(self, task_id: str, reason: str = "user_interrupt") -> bool:
        """发出 kill 信号. 返 True 如果 task 在跑."""
        if task_id not in self._events:
            return False
        self._signals[task_id] = KillSignal(
            task_id=task_id,
            requested_at=datetime.now(UTC),
            reason=reason,
        )
        self._events[task_id].set()
        return True

    def is_killed(self, task_id: str) -> bool:
        """检查任务是否被 kill (orchestrator 每步开头调)."""
        ev = self._events.get(task_id)
        return ev is not None and ev.is_set()

    def get_kill_signal(self, task_id: str) -> KillSignal | None:
        return self._signals.get(task_id)

    def cleanup(self, task_id: str) -> None:
        """任务结束清理."""
        self._events.pop(task_id, None)
        self._signals.pop(task_id, None)

    async def wait_or_proceed(
        self,
        task_id: str,
        coro: Any,
        timeout_sec: float | None = None,
    ) -> Any:
        """跑 coro, 但响应 kill (≤500ms 内).

        实际是 asyncio.wait + race 模式: kill_event 触发立即 cancel coro.
        """
        if task_id not in self._events:
            self.register_task(task_id)
        kill_ev = self._events[task_id]
        kill_task = asyncio.create_task(kill_ev.wait())
        work_task = asyncio.create_task(coro)

        try:
            done, pending = await asyncio.wait(
                {kill_task, work_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=timeout_sec,
            )
            if kill_task in done:
                # Kill 触发, cancel 工作
                work_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await work_task
                raise asyncio.CancelledError(
                    f"task {task_id} killed: {self._signals.get(task_id, '?')}"
                )
            for p in pending:
                p.cancel()
            return work_task.result()
        finally:
            if not kill_task.done():
                kill_task.cancel()


import contextlib  # noqa: E402

# ============================================================================
# T46+T47 TokenMeter (§5.2.1 / §11.3): token 实时仪表盘 + 单步上限
# ============================================================================


@dataclass
class TokenUsageWindow:
    """5h 滚动窗口 token 用量."""

    used: int = 0
    limit: int = 0
    window_start: datetime = field(default_factory=lambda: datetime.now(UTC))


class TokenMeter:
    """Token 实时仪表盘 + 单步上限 (V2.1 T46 + T47).

    职责:
    - 累计每个 task / user 的 token 用量
    - 单步 token 上限检查 (默认 50K, 防 "hello 烧 2%" 这种事)
    - 5h / day / month 滚动窗口
    - 接近上限主动告警 (80% / 95%)
    """

    def __init__(
        self,
        *,
        single_step_limit: int = 50_000,
        five_hour_limit: int = 500_000,
        daily_limit: int = 2_000_000,
        warn_threshold: float = 0.80,
        alert_threshold: float = 0.95,
    ) -> None:
        self.single_step_limit = single_step_limit
        self.five_hour_limit = five_hour_limit
        self.daily_limit = daily_limit
        self.warn_threshold = warn_threshold
        self.alert_threshold = alert_threshold
        # per-user 5h / day 窗口
        self._user_5h: dict[str, TokenUsageWindow] = {}
        self._user_daily: dict[str, TokenUsageWindow] = {}
        self._task_total: dict[str, int] = {}
        self._listeners: list[Callable[[str, dict[str, Any]], None]] = []

    def register_listener(self, fn: Callable[[str, dict[str, Any]], None]) -> None:
        """注册告警监听 (warn / alert / step_limit_exceeded)."""
        self._listeners.append(fn)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        for fn in self._listeners:
            try:
                fn(kind, payload)
            except Exception:
                logger.exception("token meter listener failed (non-fatal)")

    def check_step_limit(self, requested_tokens: int) -> bool:
        """单步上限检查. 返 True 如果允许. False 触发 fast_split."""
        if requested_tokens > self.single_step_limit:
            self._emit(
                "step_limit_exceeded",
                {
                    "requested": requested_tokens,
                    "limit": self.single_step_limit,
                },
            )
            return False
        return True

    def record_usage(
        self,
        user_id: str,
        task_id: str,
        tokens_used: int,
    ) -> None:
        """记录 token 用量 (LLM 调用后)."""
        # task 累计
        self._task_total[task_id] = self._task_total.get(task_id, 0) + tokens_used

        # user 5h 窗口
        now = datetime.now(UTC)
        win5 = self._user_5h.setdefault(
            user_id,
            TokenUsageWindow(
                limit=self.five_hour_limit,
                window_start=now,
            ),
        )
        if (now - win5.window_start).total_seconds() > 5 * 3600:
            win5.used = 0
            win5.window_start = now
        win5.used += tokens_used

        # user 日窗口
        wind = self._user_daily.setdefault(
            user_id,
            TokenUsageWindow(
                limit=self.daily_limit,
                window_start=now,
            ),
        )
        if (now - wind.window_start).total_seconds() > 24 * 3600:
            wind.used = 0
            wind.window_start = now
        wind.used += tokens_used

        # 阈值告警
        ratio_5h = win5.used / win5.limit if win5.limit > 0 else 0
        if ratio_5h >= self.alert_threshold:
            self._emit(
                "alert_5h",
                {
                    "user_id": user_id,
                    "ratio": ratio_5h,
                    "used": win5.used,
                    "limit": win5.limit,
                },
            )
        elif ratio_5h >= self.warn_threshold:
            self._emit(
                "warn_5h",
                {
                    "user_id": user_id,
                    "ratio": ratio_5h,
                    "used": win5.used,
                    "limit": win5.limit,
                },
            )

    def get_dashboard(self, user_id: str) -> dict[str, Any]:
        """T46 token 实时仪表盘 (NUO 第 1 层置顶用)."""
        win5 = self._user_5h.get(user_id, TokenUsageWindow(limit=self.five_hour_limit))
        wind = self._user_daily.get(user_id, TokenUsageWindow(limit=self.daily_limit))
        return {
            "user_id": user_id,
            "five_hour": {
                "used": win5.used,
                "limit": win5.limit,
                "remaining": max(0, win5.limit - win5.used),
                "ratio": win5.used / win5.limit if win5.limit > 0 else 0,
                "window_start": win5.window_start.isoformat(),
            },
            "daily": {
                "used": wind.used,
                "limit": wind.limit,
                "remaining": max(0, wind.limit - wind.used),
                "ratio": wind.used / wind.limit if wind.limit > 0 else 0,
                "window_start": wind.window_start.isoformat(),
            },
        }

    def get_task_total(self, task_id: str) -> int:
        return self._task_total.get(task_id, 0)


# ============================================================================
# T51 PlanOnlyGate (§5.2.4 / §12.9): destructive 操作强制 plan-only
# ============================================================================


# 强制 plan-only 关键词 (绝对不能跳过的)
PLAN_ONLY_HARD_LIST = [
    re.compile(r"\b(drop|delete|truncate)\s+(database|table|schema)", re.I),
    re.compile(r"\bdeploy\b.*prod", re.I),
    re.compile(r"\b(transfer|withdraw|payment|refund)", re.I),
    re.compile(r"\bsend\s+email", re.I),
    re.compile(r"\bbroadcast", re.I),
    re.compile(r"删除.*生产", re.S),
    re.compile(r"删库", re.S),
    re.compile(r"上线", re.S),
    re.compile(r"扣款|支付|转账", re.S),
    re.compile(r"跨租户|cross.?tenant", re.I),
    re.compile(r"rm\s+-rf", re.I),
]


@dataclass
class PlanOnlyDecision:
    """Plan-only 触发决策."""

    triggered: bool
    reason: str = ""
    matched_pattern: str = ""
    confirm_token: str | None = None
    plan_text: str = ""


class PlanOnlyGate:
    """Plan-only + human-gate (V2.1 §5.2.4 / T51).

    核心: destructive 操作前先输出 plan, 等用户 confirm token.
    硬清单永远拦; 软清单可由用户 pre-approve 跳过.
    """

    def __init__(
        self,
        *,
        confirm_timeout_sec: int = 300,
        soft_actions_pre_approved: tuple[str, ...] = (),
    ) -> None:
        self.confirm_timeout_sec = confirm_timeout_sec
        self.soft_actions_pre_approved = set(soft_actions_pre_approved)
        self._pending_confirms: dict[str, PlanOnlyDecision] = {}

    def check(
        self,
        action_text: str,
        action_kind: str = "",
        env: str = "dev",
    ) -> PlanOnlyDecision:
        """检查动作是否触发 plan-only."""
        # 硬清单永远拦
        for pat in PLAN_ONLY_HARD_LIST:
            m = pat.search(action_text)
            if m:
                token = self._gen_token()
                decision = PlanOnlyDecision(
                    triggered=True,
                    reason="hard_destructive_action",
                    matched_pattern=pat.pattern,
                    confirm_token=token,
                    plan_text=f"我打算: {action_text[:200]}",
                )
                self._pending_confirms[token] = decision
                return decision

        # prod env 升档
        if env == "prod" and action_kind in ("write", "delete", "deploy"):
            token = self._gen_token()
            decision = PlanOnlyDecision(
                triggered=True,
                reason=f"prod_{action_kind}",
                matched_pattern="env=prod + write/delete/deploy",
                confirm_token=token,
                plan_text=f"准备在 prod 环境 {action_kind}: {action_text[:200]}",
            )
            self._pending_confirms[token] = decision
            return decision

        # 软清单 pre-approved 跳过
        if action_kind in self.soft_actions_pre_approved:
            return PlanOnlyDecision(triggered=False, reason="pre_approved_soft_action")

        return PlanOnlyDecision(triggered=False)

    def confirm(self, token: str, accept: bool = True) -> bool:
        """用户回复 confirm token."""
        if token not in self._pending_confirms:
            return False
        decision = self._pending_confirms.pop(token)
        return accept and decision.triggered

    def _gen_token(self) -> str:
        """生成 4 字符确认 token."""
        import secrets

        return secrets.token_urlsafe(3).upper()[:4]


# ============================================================================
# T52 TaskTimeoutGuard (§5.2.2): 任务整体超时
# ============================================================================


@dataclass
class TaskRuntime:
    """任务运行时跟踪."""

    task_id: str
    started_at: datetime
    max_duration_sec: int
    max_steps: int
    steps_done: int = 0
    timeout_action: Literal["pause_ask_user", "cancel", "downgrade_model"] = "pause_ask_user"


class TaskTimeoutGuard:
    """任务整体超时守护 (V2.1 §5.2.2 / T52).

    V1 只有单步 LLM 180s 超时. V2 加任务级超时 (默认 30 分钟 / 50 步).
    超限走 timeout_action: pause_ask_user / cancel / downgrade_model.
    """

    def __init__(
        self,
        *,
        default_max_duration_sec: int = 1800,  # 30 分钟
        default_max_steps: int = 50,
    ) -> None:
        self.default_max_duration_sec = default_max_duration_sec
        self.default_max_steps = default_max_steps
        self._runtimes: dict[str, TaskRuntime] = {}

    def start(
        self,
        task_id: str,
        *,
        max_duration_sec: int | None = None,
        max_steps: int | None = None,
        timeout_action: Literal["pause_ask_user", "cancel", "downgrade_model"] = "pause_ask_user",
    ) -> TaskRuntime:
        rt = TaskRuntime(
            task_id=task_id,
            started_at=datetime.now(UTC),
            max_duration_sec=max_duration_sec or self.default_max_duration_sec,
            max_steps=max_steps or self.default_max_steps,
            timeout_action=timeout_action,
        )
        self._runtimes[task_id] = rt
        return rt

    def step_completed(self, task_id: str) -> None:
        rt = self._runtimes.get(task_id)
        if rt is not None:
            rt.steps_done += 1

    def check(self, task_id: str) -> tuple[bool, str]:
        """检查任务是否超时. 返 (是否超时, 原因)."""
        rt = self._runtimes.get(task_id)
        if rt is None:
            return (False, "")
        elapsed = (datetime.now(UTC) - rt.started_at).total_seconds()
        if elapsed > rt.max_duration_sec:
            return (True, f"duration {elapsed:.1f}s > {rt.max_duration_sec}s")
        if rt.steps_done >= rt.max_steps:
            return (True, f"steps {rt.steps_done} >= {rt.max_steps}")
        return (False, "")

    def get_action(self, task_id: str) -> str:
        rt = self._runtimes.get(task_id)
        return rt.timeout_action if rt else "cancel"

    def cleanup(self, task_id: str) -> None:
        self._runtimes.pop(task_id, None)


# ============================================================================
# T56 ZeroTelemetryEnforcer (§11.5): 默认零回传 + 用户审计权
# ============================================================================


class ZeroTelemetryEnforcer:
    """零回传 + 用户审计权 (V2.1 §11.5 / T56).

    KUN 默认不收任何用户对话内容到中心服务器.
    用户接入自己的 SIEM, 日志/审计/告警走用户配置的端点.
    NEVER analyze user 挫败感, NEVER 改回应策略基于"用户骂街检测".
    """

    def __init__(
        self,
        *,
        telemetry_enabled: bool = False,
        user_siem_endpoint: str | None = None,
        opt_in_categories: tuple[str, ...] = (),
    ) -> None:
        self.telemetry_enabled = telemetry_enabled
        self.user_siem_endpoint = user_siem_endpoint
        self.opt_in_categories = set(opt_in_categories)
        # 永远禁用的回传类别 (即使用户开 telemetry 也不收)
        self.permanently_blocked = {
            "user_message_content",
            "user_emotion_analysis",
            "user_complaint_text",
            "frustration_regex_match",
        }

    def can_send(self, category: str) -> bool:
        """检查这个类别是否可以回传."""
        if category in self.permanently_blocked:
            logger.warning("attempt to send permanently_blocked category=%s", category)
            return False
        if not self.telemetry_enabled:
            return False
        return category in self.opt_in_categories

    def get_audit_endpoint(self) -> str | None:
        """用户自己的 SIEM 端点 (审计日志走这里, 不是 KUN 中心)."""
        return self.user_siem_endpoint

    def get_landing_page_promise(self) -> dict[str, Any]:
        """Landing Page 第一屏明确标的承诺."""
        return {
            "telemetry_default": "off" if not self.telemetry_enabled else "on",
            "permanently_blocked_categories": sorted(self.permanently_blocked),
            "user_audit_endpoint": self.user_siem_endpoint or "(用户未配置)",
            "policy_url": "/policy/zero-retention",
            "open_source_proof": "https://github.com/AShan0227/KUN",
        }


__all__ = [
    "PLAN_ONLY_HARD_LIST",
    "KillSignal",
    "KillSwitch",
    "PlanOnlyDecision",
    "PlanOnlyGate",
    "TaskRuntime",
    "TaskTimeoutGuard",
    "TokenMeter",
    "TokenUsageWindow",
    "ZeroTelemetryEnforcer",
]
