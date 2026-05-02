"""Task Router (§7.1 L3) — pick role template + model tier.

Fast path stays rule-based.  When a capability-card cache is available, the
router can apply a small learned override so role_template outcomes actually
feed back into future routing instead of only being shown in NUO.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from kun.datamodel.task import TaskMeta
from kun.interface.llm import TaskProfile
from kun.interface.llm.router import TaskPurpose

log = logging.getLogger(__name__)


class RouteChoice(BaseModel):
    role_template_id: str
    purpose: TaskPurpose
    task_profile: TaskProfile
    route_reason: str = "rule_match"
    capability_scores: dict[str, float] = Field(default_factory=dict)


class TaskRouter:
    """Map task → (role template, model purpose, profile)."""

    DEFAULT_ROLE = "rt-default"
    CODER_ROLE = "rt-coder"
    WRITER_ROLE = "rt-writer"
    RESEARCHER_ROLE = "rt-researcher"
    CANDIDATE_ROLES = (DEFAULT_ROLE, CODER_ROLE, WRITER_ROLE, RESEARCHER_ROLE)

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

    async def choose_with_capability(
        self,
        meta: TaskMeta,
        *,
        tenant_id: str,
        capability_cache: Any | None = None,
        override_margin: float = 0.12,
        min_override_score: float = 0.62,
    ) -> RouteChoice:
        """Choose route, then let measured role capability break bad defaults.

        This is intentionally conservative: rules still win for cold-start and
        simple cases.  A role_template only overrides the rule if its historical
        capability for this task type clears both an absolute threshold and a
        margin over the current role.  That keeps "best strategy" learning real
        without making every trivial route hit a complicated controller.
        """

        base = self.choose(meta)
        cache = capability_cache
        if cache is None:
            try:
                from kun.engineering.capability_cache import get_capability_card_cache

                cache = get_capability_card_cache()
            except Exception:
                cache = None
        if cache is None or not tenant_id:
            return base

        scores: dict[str, float] = {}
        for role_id in self.CANDIDATE_ROLES:
            try:
                capability = await cache.best_capability(
                    tenant_id=tenant_id,
                    entity_type="role_template",
                    entity_id=role_id,
                    task_type=meta.task_type,
                )
            except Exception as exc:
                log.debug(
                    "task_router.capability_lookup_failed",
                    extra={"role_template_id": role_id, "error": str(exc)},
                )
                continue
            if capability is None:
                continue
            score = capability.capability_score().value
            scores[role_id] = round(float(score), 4)

        if not scores:
            return base

        base_score = scores.get(base.role_template_id, 0.0)
        best_role, best_score = max(scores.items(), key=lambda item: (item[1], item[0]))
        if (
            best_role != base.role_template_id
            and best_score >= min_override_score
            and best_score >= base_score + override_margin
        ):
            overridden = self.choose(meta)
            overridden.role_template_id = best_role
            overridden.route_reason = (
                "capability_card_override:"
                f"{best_role}={best_score:.2f} > {base.role_template_id}={base_score:.2f}"
            )
            overridden.capability_scores = scores
            return overridden

        base.route_reason = "rule_match_with_capability_scores"
        base.capability_scores = scores
        return base
