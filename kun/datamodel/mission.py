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


class MissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1)
    project_id: str | None = None
    risk_level: RiskLevel = "medium"
    budget_cap_usd: float = Field(default=0.0, ge=0.0)
    success_metrics: list[str] = Field(default_factory=list)
    strategy: dict[str, Any] = Field(default_factory=dict)
    review_interval_hours: int = Field(default=24, ge=1, le=24 * 30)


class MissionNextStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=1000)
    reason: str = Field(default="", max_length=1000)
    task_id: str | None = None
    action_type: str = "continue"
    due_at: datetime | None = None
    created_at: datetime | None = None


class MissionReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=2000)
    budget_notes: str = Field(default="", max_length=1000)
    risk_notes: str = Field(default="", max_length=1000)
    next_step: MissionNextStep | None = None


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
    completed_by_task_id: str | None = None
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
    budget_used_usd: float = 0.0
    blocked_reason: str = ""
    next_step: MissionNextStep | None = None
    review_interval_hours: int = 24
    success_metrics: list[str] = Field(default_factory=list)
    strategy: dict[str, Any] = Field(default_factory=dict)
    tasks: list[MissionTaskLink] = Field(default_factory=list)
    milestones: list[MissionMilestone] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    last_reviewed_at: datetime | None = None


class MissionStoryEvent(BaseModel):
    """One durable event in a mission-level story line."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    occurred_at: datetime
    task_id: str | None = None
    summary: str = ""
    reason: str = ""
    cost_usd: float = 0.0


class MissionTaskStory(BaseModel):
    """Compact replay summary for one task inside a Mission."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    role: str = "primary"
    status: MissionTaskStatus = "planned"
    resume_attempts: int = 0
    event_count: int = 0
    decision_count: int = 0
    world_action_count: int = 0
    external_action_count: int = 0
    total_cost_usd: float = 0.0
    latest_reason: str = ""
    current_action: str = ""
    reconstruction_confidence: float = 0.0
    gaps: list[str] = Field(default_factory=list)


class MissionStory(BaseModel):
    """Mission-level long-horizon story, replayed from task/event history."""

    model_config = ConfigDict(extra="forbid")

    mission_id: str
    title: str
    objective: str
    status: MissionStatus
    risk_level: RiskLevel
    task_count: int = 0
    done_task_count: int = 0
    blocked_task_count: int = 0
    event_count: int = 0
    decision_count: int = 0
    world_action_count: int = 0
    external_action_count: int = 0
    total_event_cost_usd: float = 0.0
    budget_used_usd: float = 0.0
    budget_cap_usd: float = 0.0
    latest_reason: str = ""
    current_action: str = ""
    pending_confirmations: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    reconstruction_confidence: float = 0.0
    history_limit_reached: bool = False
    next_step: MissionNextStep | None = None
    tasks: list[MissionTaskStory] = Field(default_factory=list)
    timeline: list[MissionStoryEvent] = Field(default_factory=list)


class ResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mission_id: str
    task_id: str
    runtime_status: str
    resume_attempts: int
    reason: str


__all__ = [
    "MilestoneStatus",
    "MissionCreate",
    "MissionMilestone",
    "MissionNextStep",
    "MissionReview",
    "MissionSnapshot",
    "MissionStatus",
    "MissionStory",
    "MissionStoryEvent",
    "MissionTaskLink",
    "MissionTaskStatus",
    "MissionTaskStory",
    "ResumeRequest",
]
