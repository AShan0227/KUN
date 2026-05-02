"""Events (ADR-005) — Postgres Outbox + NATS 通知.

Postgres `events` 表是**唯一真理源** (append-only).
NATS 做 fan-out 通知; 消费者用 event_id 回 Postgres 拉完整事件.

Subject 命名: kun.{tenant}.{domain}.{event}
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.core.ids import new_id

# 命名约定: <domain>.<verb> 或 <domain>.<subject>.<state>
EventKind = Literal[
    # Task lifecycle
    "task.created",
    "task.started",
    "task.route.selected",
    "task.execution_mode.selected",
    "task.step.completed",
    "task.pre_conflict_detected",
    "task.preflight_guard.evaluated",
    "task.pending_actions.created",
    "task.pending_action.executed",
    "task.pending_action.blocked",
    "task.pending_action.execution_failed",
    "task.paused",
    "task.paused.preflight",
    "task.resumed",
    "task.timed_out",
    "task.budget_warn",
    "task.budget_exceeded",
    "task.tool_skipped",
    "task.ooda.checkpoint",
    "hermes.step_action.selected",
    "task.metadata_updated",
    "task.done",
    "task.failed",
    "task.cancelled",
    "task.continuation.enqueued",
    "task.continuation.started",
    "task.continuation.finished",
    "task.continuation.failed",
    # Mission lifecycle
    "mission.created",
    "mission.started",
    "mission.paused",
    "mission.done",
    "mission.failed",
    "mission.cancelled",
    "mission.task.attached",
    "mission.task.resume_requested",
    "mission.task.resume_completed",
    "mission.task.resume_dispatched",
    "mission.task.resume_skipped",
    "mission.task.resume_failed",
    "mission.task.orchestrator_started",
    "mission.task.orchestrator_finished",
    "mission.task.reaped",
    "mission.milestone.recorded",
    "mission.review.recorded",
    "mission.budget.exceeded",
    "mission.next_step.updated",
    # Handoff
    "handoff.sent",
    "handoff.received",
    # LLM / Router
    "llm.call.started",
    "llm.call.completed",
    "llm.model_route.selected",
    "llm.fallback.triggered",
    # Capability card
    "capability.updated",
    # Watchtower / Guard
    "watchtower.intervention",
    "watchtower.decision_plan.created",
    "value_gate.decision.created",
    "guard.budget.exceeded",
    "guard.anomaly.detected",
    # Context
    "context.selected",
    "context.updated",
    "context.forgotten",
    "task.compiled_asset.created",
    "skill.selected",
    "memory.policy.selected",
    "memory.writeback.recorded",
    "credit.assignment.completed",
    # Code capability
    "code.change.proposed",
    # Evaluation / Validation
    "validation.tier.selected",
    "validation.run.completed",
    "scorecard.created",
    "debate.triggered",
    "debate.concluded",
    # Evolution
    "experiment.created",
    "experiment.promoted",
    "experiment.rolled_back",
    "emergent.switch.evaluated",
    # Notification
    "notification.emitted",
    "proactive.trigger_promoted",
    "proactive.tool_dispatch.evaluated",
    # Security
    "security.cross_tenant_attempt",
    "security.redteam.finding",
    # User interaction
    "user.message",
    "user.correction",
    "user.feedback",
    # V2.3 启 (Qi)
    "protocol.applied",
    "protocol.matched",
    "protocol.promoted",
    "protocol.rolled_back",
    "gaming.detected",
    "qi.window.opened",
    "qi.window.closed",
    "qi.budget.exhausted",
    "delivery.review_done",
    "delivery.needs_review",
    "delivery.failed",
    # NUO manager
    "nuo.health_report.generated",
]


class Event(BaseModel):
    """A single event row in the Outbox `events` table."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(default_factory=lambda: new_id("event"))
    tenant_id: str
    event_type: EventKind
    subject: str = Field(description="NATS subject, e.g. kun.u-sylvan.task.started")
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    published_at: datetime | None = None

    # Correlation — optional but highly encouraged for traceability
    trace_id: str | None = None
    span_id: str | None = None
    causation_event_id: str | None = None
    task_ref: str | None = None

    @classmethod
    def build(
        cls,
        tenant_id: str,
        event_type: EventKind,
        payload: dict[str, Any] | None = None,
        *,
        task_ref: str | None = None,
        causation_event_id: str | None = None,
    ) -> Event:
        """Construct an event with the standard subject format."""
        domain, _, _ = event_type.partition(".")
        subject = f"kun.{tenant_id}.{domain}.{event_type}"
        return cls(
            tenant_id=tenant_id,
            event_type=event_type,
            subject=subject,
            payload=payload or {},
            task_ref=task_ref,
            causation_event_id=causation_event_id,
        )
