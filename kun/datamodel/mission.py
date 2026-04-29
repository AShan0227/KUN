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
    "MissionSnapshot",
    "MissionStatus",
    "MissionTaskLink",
    "MissionTaskStatus",
    "ResumeRequest",
]
