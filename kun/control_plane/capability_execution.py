"""Runtime execution policy derived from production capability profiles.

This is the adapter between Qi capability governance and the actual Control
Plane loops.  A production profile should not just sit in a catalog; it should
become explicit planner, runner, supervisor, diagnostic, and collaboration
directives that execution modules can bind to.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.capability_governance import (
    CapabilityGovernanceDecision,
    govern_default_runtime_capabilities,
)
from kun.control_plane.v6 import CapabilityProfile

CapabilityDirectiveCategory = Literal[
    "planner",
    "worker_distribution",
    "runner",
    "supervisor",
    "diagnostics",
    "approval",
    "context",
    "evaluation",
]


class CapabilityExecutionDirective(BaseModel):
    """One executable instruction derived from governed production capability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    directive_id: str
    category: CapabilityDirectiveCategory
    capability_refs: list[str] = Field(default_factory=list)
    summary: str
    runtime_hooks: list[str] = Field(default_factory=list)


class CapabilityExecutionPolicy(BaseModel):
    """The governed capability input that daemon/planner/runner modules consume."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str
    built_at: datetime
    capability_profile_refs: list[str] = Field(default_factory=list)
    source_versions: list[str] = Field(default_factory=list)
    governance_decisions: list[CapabilityGovernanceDecision] = Field(default_factory=list)
    directives: list[CapabilityExecutionDirective] = Field(default_factory=list)

    def directives_for(
        self,
        category: CapabilityDirectiveCategory,
    ) -> list[CapabilityExecutionDirective]:
        """Return directives in one execution category."""

        return [directive for directive in self.directives if directive.category == category]


def build_capability_execution_policy(
    profiles: Sequence[CapabilityProfile],
    *,
    policy_id: str = "policy-runtime-capabilities",
    built_at: datetime | None = None,
) -> CapabilityExecutionPolicy:
    """Collapse production profiles into KUN-native execution directives."""

    governed_profiles, governance = govern_default_runtime_capabilities(profiles)
    directives: list[CapabilityExecutionDirective] = []
    for profile in governed_profiles:
        directives.extend(_directives_for_profile(profile))
    directives = _dedupe_directives(directives)
    return CapabilityExecutionPolicy(
        policy_id=policy_id,
        built_at=built_at or datetime.now(UTC),
        capability_profile_refs=[profile.capability_id for profile in governed_profiles],
        source_versions=list(governance.source_versions),
        governance_decisions=list(governance.decisions),
        directives=directives,
    )


def _directives_for_profile(profile: CapabilityProfile) -> list[CapabilityExecutionDirective]:
    text = _profile_text(profile)
    refs = [profile.capability_id]
    directives: list[CapabilityExecutionDirective] = []

    if _has_any(text, "gateway", "session", "tool event", "local-first"):
        directives.append(
            _directive(
                "planner",
                "Route work through local session/tool-event boundaries before externalizing.",
                refs,
                hooks=["task_plan", "runner_selection"],
            )
        )
        directives.append(
            _directive(
                "runner",
                "Persist gateway/session/tool-event evidence before summarizing work output.",
                refs,
                hooks=["run_record", "artifact_record"],
            )
        )

    if _has_any(text, "isolated", "workspace", "multi-agent", "multi worker", "worker"):
        directives.append(
            _directive(
                "worker_distribution",
                "Split parallel work by explicit ownership and isolated workspace boundaries.",
                refs,
                hooks=["work_item_dag", "resource_locks"],
            )
        )

    if _has_any(text, "large tool", "persisted large", "artifact", "tool result"):
        directives.append(
            _directive(
                "runner",
                "Store large tool outputs as artifacts and pass compact refs through context.",
                refs,
                hooks=["artifact_record", "working_context"],
            )
        )
        directives.append(
            _directive(
                "context",
                "Compress long context around artifact refs instead of raw tool output.",
                refs,
                hooks=["working_context", "artifact_manifest"],
            )
        )

    if _has_any(text, "structured log", "diagnostic", "progress", "dashboard"):
        directives.append(
            _directive(
                "diagnostics",
                "Emit user-readable progress, risk, next action, blocker, and recovery status.",
                refs,
                hooks=["daemon_progress", "task_cockpit"],
            )
        )

    if _has_any(text, "background", "resume", "completion", "notification"):
        directives.append(
            _directive(
                "supervisor",
                "Treat background completion and resume state as first-class persisted state.",
                refs,
                hooks=["daemon_heartbeat", "run_record", "mission_resume"],
            )
        )

    if _has_any(text, "approval", "resumable", "ticket", "human"):
        directives.append(
            _directive(
                "approval",
                "Create resumable human tickets for risky actions, with timeout fallback policy.",
                refs,
                hooks=["collaboration_ticket", "resume_after_response"],
            )
        )

    if _has_any(text, "timeout", "heartbeat", "activity"):
        directives.append(
            _directive(
                "supervisor",
                "Classify stale heartbeat, timeout, and EOF as recoverable system conditions first.",
                refs,
                hooks=["supervisor_recovery", "nuo_pollution"],
            )
        )

    if _has_any(text, "benchmark", "regression", "holdout", "ab"):
        directives.append(
            _directive(
                "evaluation",
                "Attach holdout, regression, dogfood, and AB evidence before production use.",
                refs,
                hooks=["gate_evaluation", "capability_promotion"],
            )
        )

    if not directives:
        directives.append(
            _directive(
                "planner",
                "Apply this production capability as governed planning guidance only.",
                refs,
                hooks=["task_plan"],
            )
        )
    return directives


def _directive(
    category: CapabilityDirectiveCategory,
    summary: str,
    capability_refs: list[str],
    *,
    hooks: list[str],
) -> CapabilityExecutionDirective:
    return CapabilityExecutionDirective(
        directive_id=f"directive-{category}-{_slug(summary)}",
        category=category,
        capability_refs=capability_refs,
        summary=summary,
        runtime_hooks=hooks,
    )


def _dedupe_directives(
    directives: Sequence[CapabilityExecutionDirective],
) -> list[CapabilityExecutionDirective]:
    grouped: dict[tuple[str, str], CapabilityExecutionDirective] = {}
    for directive in directives:
        key = (directive.category, directive.summary)
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = directive
            continue
        grouped[key] = existing.model_copy(
            update={
                "capability_refs": _dedupe([*existing.capability_refs, *directive.capability_refs]),
                "runtime_hooks": _dedupe([*existing.runtime_hooks, *directive.runtime_hooks]),
            }
        )
    return [grouped[key] for key in sorted(grouped)]


def _profile_text(profile: CapabilityProfile) -> str:
    return " ".join(
        [
            profile.capability_id,
            profile.capability_name,
            profile.governance_key,
            *profile.source_refs,
            *profile.source_versions,
            *profile.evidence_refs,
            *profile.known_limits,
        ]
    ).lower()


def _has_any(text: str, *needles: str) -> bool:
    return any(needle in text for needle in needles)


def _slug(value: str) -> str:
    return "-".join(part for part in value.lower().split() if part)[:80]


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


__all__ = [
    "CapabilityDirectiveCategory",
    "CapabilityExecutionDirective",
    "CapabilityExecutionPolicy",
    "build_capability_execution_policy",
]
