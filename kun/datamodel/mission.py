"""Mission models for long-horizon KUN work.

Mission is the durable product-level object: a real-world goal that can span
many TASK.md objects, approvals, checkpoints, and resume attempts.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.core.ids import new_id
from kun.datamodel.task import RiskLevel

MissionStatus = Literal["planned", "running", "paused", "done", "failed", "cancelled"]
MissionTaskStatus = Literal[
    "planned",
    "queued",
    "running",
    "paused",
    "blocked",
    "done",
    "failed",
    "cancelled",
]
MilestoneStatus = Literal["planned", "active", "done", "blocked", "cancelled"]
MissionLedgerAuditStatus = Literal["pass", "warn", "fail"]
MissionLedgerAuditSeverity = Literal["info", "warn", "error"]


class MissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1)
    project_id: str | None = None
    risk_level: RiskLevel = "medium"
    budget_cap_usd: float = Field(default=0.0, ge=0.0)
    success_metrics: list[str] = Field(default_factory=list)
    strategy: dict[str, Any] = Field(default_factory=dict)


class MissionTaskLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    role: str = "primary"
    sequence_no: int = Field(default=0, ge=0)
    status: MissionTaskStatus = "planned"
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    resume_attempts: int = 0
    last_resume_requested_at: datetime | None = None


class MissionMilestone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    milestone_id: str = Field(default_factory=lambda: new_id("milestone"))
    title: str = Field(min_length=1, max_length=256)
    status: MilestoneStatus = "planned"
    sequence_no: int = Field(default=0, ge=0)
    task_ref: str | None = None
    due_at: datetime | None = None
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    completed_at: datetime | None = None


class MissionSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    tenant_id: str
    user_id: str | None = None
    project_id: str | None = None
    title: str
    objective: str
    status: MissionStatus
    risk_level: RiskLevel
    budget_cap_usd: float = 0.0
    success_metrics: list[str] = Field(default_factory=list)
    strategy: dict[str, Any] = Field(default_factory=dict)
    tasks: list[MissionTaskLink] = Field(default_factory=list)
    milestones: list[MissionMilestone] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class MissionReaperResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    task_id: str
    previous_status: str
    status: MissionTaskStatus = "failed"
    reason: str
    stale_for_sec: int


class MissionBlockedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    task_id: str
    previous_status: str
    runtime_status: str
    status: MissionTaskStatus = "blocked"
    reason: str
    resume_attempts: int
    max_attempts: int


class MissionBudgetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    budget_cap_usd: float = 0.0
    spent_actual_usd: float = 0.0
    spent_equivalent_usd: float = 0.0
    remaining_equivalent_usd: float = 0.0
    usage_fraction: float = 0.0


class MissionCheckpointSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    role: str
    status: MissionTaskStatus
    runtime_status: str | None = None
    resume_attempts: int = 0
    last_resume_requested_at: datetime | None = None
    last_runtime_updated_at: datetime | None = None
    cost_usd_actual: float = 0.0
    cost_usd_equivalent: float = 0.0
    checkpoint: dict[str, Any] = Field(default_factory=dict)


class MissionExecutionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    tenant_id: str
    status: MissionStatus
    budget: MissionBudgetSummary
    task_status_counts: dict[str, int] = Field(default_factory=dict)
    checkpoints: list[MissionCheckpointSummary] = Field(default_factory=list)
    updated_at: datetime


class MissionTimelineEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    occurred_at: datetime
    subject: str
    mission_id: str | None = None
    task_id: str | None = None
    status: str | None = None
    reason: str | None = None
    cost_usd_actual: float = 0.0
    cost_usd_equivalent: float = 0.0
    checkpoint: dict[str, Any] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class MissionTimeline(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    tenant_id: str
    event_count: int = 0
    status_counts: dict[str, int] = Field(default_factory=dict)
    recent_reasons: list[str] = Field(default_factory=list)
    total_cost_usd_actual: float = 0.0
    total_cost_usd_equivalent: float = 0.0
    events: list[MissionTimelineEvent] = Field(default_factory=list)


class MissionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    tenant_id: str
    milestone_id: str
    status: MissionStatus
    generated_at: datetime
    budget: MissionBudgetSummary
    task_status_counts: dict[str, int] = Field(default_factory=dict)
    checkpoint_count: int = 0
    timeline_event_count: int = 0
    recent_reasons: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    next_checkpoint: str
    checkpoint: dict[str, Any] = Field(default_factory=dict)


class MissionLedgerAuditIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: MissionLedgerAuditSeverity
    message: str
    task_id: str | None = None
    event_id: str | None = None


class MissionLedgerAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    tenant_id: str
    status: MissionLedgerAuditStatus = "pass"
    checked_at: datetime
    summary_task_count: int = 0
    checkpoint_count: int = 0
    timeline_event_count: int = 0
    review_event_count: int = 0
    budget: MissionBudgetSummary
    task_status_counts: dict[str, int] = Field(default_factory=dict)
    event_status_counts: dict[str, int] = Field(default_factory=dict)
    recent_reasons: list[str] = Field(default_factory=list)
    issue_count: int = 0
    issues: list[MissionLedgerAuditIssue] = Field(default_factory=list)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    task_id: str
    runtime_status: str
    resume_attempts: int
    reason: str


__all__ = [
    "MilestoneStatus",
    "MissionBlockedResult",
    "MissionBudgetSummary",
    "MissionCheckpointSummary",
    "MissionCreate",
    "MissionExecutionSummary",
    "MissionLedgerAudit",
    "MissionLedgerAuditIssue",
    "MissionLedgerAuditSeverity",
    "MissionLedgerAuditStatus",
    "MissionMilestone",
    "MissionReaperResult",
    "MissionReview",
    "MissionSnapshot",
    "MissionStatus",
    "MissionTaskLink",
    "MissionTaskStatus",
    "MissionTimeline",
    "MissionTimelineEvent",
    "ResumeRequest",
]
