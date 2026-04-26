"""IncidentResponse — 安全异常 4 档应急响应 (V2.1 §12.11 / 漏洞 11).

V2 §12.7 异常检测列了 4 类 (成本/质量/行为/安全), 但响应流程没明确.
V2.1 加 4 档响应矩阵 + 异步执行不阻塞主路径.

| 档 | 触发场景 | SLA |
|----|---------|-----|
| L1 留痕 | 单次小成本异常 / 单次质量略降 | 30 分钟 |
| L2 告警 | 同模式异常累积 N 次 / 跨租户访问尝试 1 次 | 5 分钟 |
| L3 隔离 | prompt injection / 灵魂档案异常修改 / agent 拒停 | 30 秒 |
| L4 熔断 | 安全红线 / 大规模 cross-tenant 泄漏 / 系统级 prompt 操控 | 5 秒 |
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from kun.core.anchor_expand import AnchorExpandIterator

logger = logging.getLogger(__name__)


IncidentSeverity = Literal["L1", "L2", "L3", "L4"]
IncidentCategory = Literal["cost", "quality", "behavior", "security"]


# 各档 SLA (秒)
SLA_BY_SEVERITY: dict[IncidentSeverity, int] = {
    "L1": 30 * 60,  # 30 分钟
    "L2": 5 * 60,  # 5 分钟
    "L3": 30,  # 30 秒
    "L4": 5,  # 5 秒
}


@dataclass
class IncidentEvent:
    """异常事件."""

    incident_id: str
    severity: IncidentSeverity
    category: IncidentCategory
    title: str
    detected_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    affected_user_id: str | None = None
    affected_tenant_id: str | None = None
    affected_task_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class IncidentResponseAction:
    """响应动作."""

    action_kind: Literal[
        "log_only",
        "notify_user",
        "notify_admin",
        "pause_task",
        "isolate_user",
        "freeze_tenant",
        "global_readonly",
        "escalate_human",
        "revert_soul_file",
        "block_writes",
    ]
    target: str  # task_id / user_id / tenant_id / "global"
    reason: str
    executed_at: datetime | None = None
    success: bool = False


# 严重度 → 响应动作映射 (按场景)
RESPONSE_MATRIX: dict[IncidentSeverity, list[str]] = {
    "L1": ["log_only"],
    "L2": ["log_only", "notify_user"],
    "L3": ["log_only", "notify_user", "pause_task", "isolate_user"],
    "L4": [
        "log_only",
        "notify_user",
        "notify_admin",
        "pause_task",
        "freeze_tenant",
        "global_readonly",
        "escalate_human",
        "block_writes",
    ],
}


class IncidentResponseEngine:
    """4 档应急响应引擎.

    用法:
        eng = IncidentResponseEngine()
        eng.register_action_handler("pause_task", my_pause_handler)
        eng.register_action_handler("isolate_user", my_isolate_handler)
        await eng.handle(IncidentEvent(...))
    """

    def __init__(self) -> None:
        self._handlers: dict[
            str, Callable[[IncidentResponseAction, IncidentEvent], Awaitable[bool]]
        ] = {}
        self._history: list[tuple[IncidentEvent, list[IncidentResponseAction]]] = []
        # 累积模式检测 (key: (category, user/tenant) → 次数)
        self._pattern_counts: dict[tuple[str, str], int] = {}

    def register_action_handler(
        self,
        action_kind: str,
        handler: Callable[[IncidentResponseAction, IncidentEvent], Awaitable[bool]],
    ) -> None:
        self._handlers[action_kind] = handler

    def upgrade_severity(self, event: IncidentEvent) -> IncidentSeverity:
        """累积模式: 同 category + user 多次小异常 → 升档.

        L1 异常累积 3 次 → 升 L2 告警.
        L2 异常累积 5 次 → 升 L3 隔离.
        """
        key = (event.category, event.affected_user_id or event.affected_tenant_id or "global")
        self._pattern_counts[key] = self._pattern_counts.get(key, 0) + 1
        count = self._pattern_counts[key]

        sev = event.severity
        if sev == "L1" and count >= 3:
            return "L2"
        if sev == "L2" and count >= 5:
            return "L3"
        return sev

    async def handle(self, event: IncidentEvent) -> list[IncidentResponseAction]:
        """处理异常事件.

        - 异步执行所有动作 (不阻塞主路径)
        - SLA 守护: 各档动作总耗时 ≤ 对应 SLA
        - 失败 1 个不阻塞其他
        """
        # 累积升档
        actual_severity = self.upgrade_severity(event)
        event.severity = actual_severity

        actions = self._build_actions_for(event, actual_severity)

        sla_sec = SLA_BY_SEVERITY[actual_severity]

        # 异步并发跑动作 (不阻塞主路径)
        async def _exec(action: IncidentResponseAction) -> None:
            handler = self._handlers.get(action.action_kind)
            if handler is None:
                # 默认 log_only
                if action.action_kind == "log_only":
                    logger.warning(
                        "INCIDENT %s/%s: %s (target=%s)",
                        event.category,
                        actual_severity,
                        event.title,
                        action.target,
                    )
                    action.success = True
                else:
                    logger.warning(
                        "no handler for %s, skipping (incident=%s)",
                        action.action_kind,
                        event.incident_id,
                    )
                action.executed_at = datetime.now(UTC)
                return
            try:
                action.success = await asyncio.wait_for(
                    handler(action, event),
                    timeout=sla_sec,
                )
            except TimeoutError:
                logger.error(
                    "action %s exceeded SLA %ds for incident %s",
                    action.action_kind,
                    sla_sec,
                    event.incident_id,
                )
                action.success = False
            except Exception:
                logger.exception("action %s failed (non-fatal)", action.action_kind)
                action.success = False
            action.executed_at = datetime.now(UTC)

        await asyncio.gather(*(_exec(a) for a in actions))
        self._history.append((event, actions))
        return actions

    async def iter_response_actions_anchor_then_expand(
        self,
        event: IncidentEvent,
        *,
        max_rounds: int = 3,
        apply_upgrade: bool = False,
    ) -> AsyncIterator[IncidentResponseAction]:
        """按需返回应急响应动作.

        默认不做累积升档, 避免调用方只是预览动作时污染 pattern_counts.
        真正执行仍走 ``handle``.

        # TODO: wire by Claude in V2.2
        """
        severity = self.upgrade_severity(event) if apply_upgrade else event.severity
        actions = self._build_actions_for(event, severity)
        if not actions:
            return

        async def anchor_fn() -> IncidentResponseAction:
            return actions[0]

        async def expand_fn(
            _anchor: IncidentResponseAction,
            prior: list[IncidentResponseAction],
        ) -> IncidentResponseAction | None:
            idx = len(prior)
            if idx >= len(actions):
                return None
            return actions[idx]

        async for action in AnchorExpandIterator(
            anchor_fn,
            expand_fn,
            max_rounds=max_rounds,
        ):
            yield action

    def _build_actions_for(
        self,
        event: IncidentEvent,
        severity: IncidentSeverity,
    ) -> list[IncidentResponseAction]:
        action_kinds = RESPONSE_MATRIX[severity]
        return [
            IncidentResponseAction(
                action_kind=ak,  # type: ignore[arg-type]
                target=(
                    event.affected_task_id
                    or event.affected_user_id
                    or event.affected_tenant_id
                    or "global"
                ),
                reason=f"{event.category}/{severity}: {event.title}",
            )
            for ak in action_kinds
        ]

    def get_history(
        self,
        severity: IncidentSeverity | None = None,
        category: IncidentCategory | None = None,
    ) -> list[tuple[IncidentEvent, list[IncidentResponseAction]]]:
        out = self._history
        if severity:
            out = [(e, a) for e, a in out if e.severity == severity]
        if category:
            out = [(e, a) for e, a in out if e.category == category]
        return list(out)

    def get_pattern_counts(self) -> dict[tuple[str, str], int]:
        return dict(self._pattern_counts)


__all__ = [
    "RESPONSE_MATRIX",
    "SLA_BY_SEVERITY",
    "IncidentCategory",
    "IncidentEvent",
    "IncidentResponseAction",
    "IncidentResponseEngine",
    "IncidentSeverity",
]
