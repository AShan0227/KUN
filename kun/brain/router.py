"""Task Router (§7.1 L3) — pick role template + model tier.

Walking skeleton: simple rule-based. Future: capability-card lookup + learned rules.
"""

from __future__ import annotations

from pydantic import BaseModel

from kun.datamodel.task import TaskMeta
from kun.interface.llm import TaskProfile
from kun.interface.llm.router import TaskPurpose


class RouteChoice(BaseModel):
    role_template_id: str
    purpose: TaskPurpose
    task_profile: TaskProfile


class TaskRouter:
    """Map task → (role template, model purpose, profile)."""

    DEFAULT_ROLE = "rt-default"
    CODER_ROLE = "rt-coder"
    WRITER_ROLE = "rt-writer"
    RESEARCHER_ROLE = "rt-researcher"

    def choose(self, meta: TaskMeta) -> RouteChoice:
        task_type = meta.task_type
        needs_coding = task_type.startswith("coding.")
        needs_creative = task_type.startswith("writing.")
        needs_research = task_type.startswith("research.")

        if needs_coding:
            role = self.CODER_ROLE
            purpose: TaskPurpose = "coding"
        elif needs_creative:
            role = self.WRITER_ROLE
            purpose = "execution"
        elif needs_research:
            role = self.RESEARCHER_ROLE
            purpose = "execution"
        else:
            role = self.DEFAULT_ROLE
            purpose = "execution"

        profile = TaskProfile(
            task_type=task_type,
            risk_level=meta.risk_level,
            needs_coding=needs_coding,
            needs_creative=needs_creative,
            needs_reasoning=meta.complexity_score > 0.6,
            prefer_speed=meta.risk_level == "low" and meta.complexity_score < 0.3,
        )
        return RouteChoice(role_template_id=role, purpose=purpose, task_profile=profile)
