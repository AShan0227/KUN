"""Governance helpers for KUN runtime capability profiles.

Capability evolution can produce many candidates from AB gaps, real tasks, and
external behavior samples.  Runtime consumption must keep that library tidy:
only production profiles are default-consumable, duplicate behaviors are
collapsed, and the selected profile stays auditable by source/version.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.v6 import CapabilityProfile

_SPACE_RE = re.compile(r"[^a-z0-9]+")


class CapabilityGovernanceDecision(BaseModel):
    """One deterministic dedupe/merge decision for a capability behavior."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    governance_key: str
    kept_profile_ref: str
    merged_profile_refs: list[str] = Field(default_factory=list)
    source_versions: list[str] = Field(default_factory=list)
    reason: str


class CapabilityGovernanceReport(BaseModel):
    """The governed runtime view plus audit decisions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_refs: list[str] = Field(default_factory=list)
    duplicate_profile_refs: list[str] = Field(default_factory=list)
    decisions: list[CapabilityGovernanceDecision] = Field(default_factory=list)
    source_versions: list[str] = Field(default_factory=list)


def normalize_capability_governance_key(value: str) -> str:
    """Return a stable behavior key for capability-library dedupe."""

    normalized = _SPACE_RE.sub("-", value.strip().lower()).strip("-")
    return normalized or "capability"


def govern_default_runtime_capabilities(
    profiles: Sequence[CapabilityProfile],
) -> tuple[list[CapabilityProfile], CapabilityGovernanceReport]:
    """Return production default profiles after dedupe and source-version review."""

    eligible = [
        profile
        for profile in profiles
        if profile.promotion_stage == "production" and profile.runtime_enabled
    ]
    groups: dict[str, list[CapabilityProfile]] = defaultdict(list)
    for profile in eligible:
        groups[_profile_governance_key(profile)].append(profile)

    kept: list[CapabilityProfile] = []
    decisions: list[CapabilityGovernanceDecision] = []
    duplicate_refs: list[str] = []
    source_versions: list[str] = []
    for key in sorted(groups):
        profiles_for_key = sorted(groups[key], key=_profile_rank, reverse=True)
        selected = profiles_for_key[0]
        kept.append(selected)
        source_versions.extend(_profile_source_versions(selected))
        duplicates = profiles_for_key[1:]
        if duplicates:
            duplicate_refs.extend(profile.capability_id for profile in duplicates)
            source_versions.extend(
                version for profile in duplicates for version in _profile_source_versions(profile)
            )
            decisions.append(
                CapabilityGovernanceDecision(
                    governance_key=key,
                    kept_profile_ref=selected.capability_id,
                    merged_profile_refs=[profile.capability_id for profile in duplicates],
                    source_versions=_dedupe(
                        version
                        for profile in profiles_for_key
                        for version in _profile_source_versions(profile)
                    ),
                    reason=(
                        "Duplicate production runtime capabilities collapsed by behavior key; "
                        "the strongest verified profile remains the default runtime input."
                    ),
                )
            )

    return kept, CapabilityGovernanceReport(
        profile_refs=[profile.capability_id for profile in kept],
        duplicate_profile_refs=_dedupe(duplicate_refs),
        decisions=decisions,
        source_versions=_dedupe(source_versions),
    )


def _profile_governance_key(profile: CapabilityProfile) -> str:
    return profile.governance_key or normalize_capability_governance_key(profile.capability_name)


def _profile_rank(profile: CapabilityProfile) -> tuple[int, int, float, str]:
    proof_count = (
        len(profile.evidence_refs) + len(profile.holdout_refs) + len(profile.regression_refs)
    )
    verified_at = _timestamp(profile.last_verified_at)
    source_count = len(_profile_source_versions(profile))
    return proof_count, source_count, verified_at, profile.capability_id


def _profile_source_versions(profile: CapabilityProfile) -> list[str]:
    versions = list(profile.source_versions)
    if not versions:
        versions = [f"source:{ref}" for ref in profile.source_refs]
    if not versions:
        versions = [f"evidence:{ref}" for ref in profile.evidence_refs]
    return _dedupe(versions)


def _timestamp(value: datetime | None) -> float:
    return value.timestamp() if value is not None else 0.0


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


__all__ = [
    "CapabilityGovernanceDecision",
    "CapabilityGovernanceReport",
    "govern_default_runtime_capabilities",
    "normalize_capability_governance_key",
]
