"""NotificationLayer (ADR-018 §16.3) — 统一所有对外推送.

合并前: 三层透明化报告 / 惊喜反馈 / 错误告警 / idle-batch 报告 / 守望干预通知 五种分散.
合并后: 单一 Notification 载体 + 分 kind + 分 channel + 分 severity.

对应 WebSocket side channel 消息块 (ADR-010).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.core.ids import new_id

NotificationKind = Literal[
    # WebSocket side channel
    "cost_tick",
    "evolution_note",
    "insight",
    "surprise",
    "alert",
    "idle_batch_report",
    "guard_intervention",
    "correction_ack",
    # Periodic reports
    "weekly_digest",
    "monthly_report",
    "calibration_summary",
]

NotificationChannel = Literal[
    "main",  # WebSocket 主通道
    "side",  # WebSocket side channel
    "email",
    "webhook",
    "push",  # 移动推送
    "silent",  # 只记录, 不推
]

Severity = Literal["info", "insight", "warn", "error"]


class Notification(BaseModel):
    """Unified notification payload."""

    model_config = ConfigDict(extra="forbid")

    notification_id: str = Field(default_factory=lambda: new_id("notification"))
    tenant_id: str
    kind: NotificationKind
    severity: Severity = "info"
    channel: NotificationChannel = "side"
    title: str = ""
    body: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)

    # UI 展示提示
    render_hint: dict[str, Any] = Field(
        default_factory=lambda: {"collapsed": True, "pin": False, "muted_ok": True},
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    delivered_at: datetime | None = None
    acknowledged_at: datetime | None = None

    # Correlation
    task_ref: str | None = None
    causation_event_id: str | None = None

    def mark_delivered(self) -> None:
        self.delivered_at = datetime.now(UTC)

    def mark_acknowledged(self) -> None:
        self.acknowledged_at = datetime.now(UTC)
