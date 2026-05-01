"""Review-only gate for external skill and engineering-template candidates.

This module does not fetch, install, register, or promote external skills. It
turns a task need plus offline candidate metadata into a small, auditable review
package for Qi / NUO / a human reviewer.
"""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.engineering.external_scan import (
    ExternalSkillCandidate,
    ExternalSkillDemandKind,
    ExternalSkillSourceRegistration,
    normalize_external_skill_candidate,
    normalize_external_skill_candidates,
    normalize_external_skill_source_registration,
    score_external_skill_license,
    score_external_skill_safety,
    score_external_skill_task_fit,
)
from kun.qi.problem_queue import QiProblemSignal, persist_problem_signals

ExternalSkillReviewStatus = Literal["blocked", "needs_evidence", "ready_for_human_review"]
_LICENSE_NEEDS_LEGAL_REVIEW = {
    "",
    "unknown",
    "noassertion",
    "other",
    "none",
    "unlicensed",
    "proprietary",
    "all rights reserved",
    "gpl-2.0",
    "gpl-3.0",
    "agpl-3.0",
    "lgpl-2.1",
    "lgpl-3.0",
    "sspl-1.0",
}


class KnownExternalSkillSourceProfile(BaseModel):
    """Curated source note, not an allowlist.

    This tells Qi/NUO why a source is worth looking at.  It deliberately does
    not approve fetching, installing, or registering anything.
    """

    model_config = ConfigDict(extra="forbid")

    repo_ref: str
    source_kind: str = "github_repo"
    credibility_score: float = 0.0
    expected_use_cases: list[ExternalSkillDemandKind] = Field(default_factory=list)
    why_review: list[str] = Field(default_factory=list)
    required_evidence: list[str] = Field(default_factory=list)
    safety_rules: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    review_only: Literal[True] = True
    auto_fetch_allowed: Literal[False] = False
    auto_install_allowed: Literal[False] = False
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


_KNOWN_EXTERNAL_SKILL_SOURCE_PROFILES: dict[str, KnownExternalSkillSourceProfile] = {
    "mattpocock/skills": KnownExternalSkillSourceProfile(
        repo_ref="mattpocock/skills",
        credibility_score=0.82,
        expected_use_cases=["coding", "review"],
        why_review=[
            "known_engineering_behavior_template_source",
            "useful_for_code_review_and_developer_workflows",
            "good_candidate_for_skill_pattern_learning",
        ],
        required_evidence=[
            "pinned_commit_sha",
            "license_and_notice_review",
            "skill_md_inventory",
            "static_safety_review",
            "sandbox_dry_run_if_any_support_scripts_exist",
            "human_review_before_registration",
        ],
        safety_rules=[
            "metadata_fetch_only_until_human_selects_candidate",
            "no_auto_install",
            "no_auto_trigger_without_policy_review",
            "no_secret_or_network_access_in_sandbox_by_default",
            "never_promote_whole_repo_to_production",
        ],
        known_limitations=[
            "external_repo_can_change_after_recommendation",
            "popular_source_is_not_a_security_guarantee",
            "individual_skill_files_must_be_reviewed_one_by_one",
        ],
    )
}


class ExternalSkillReviewScorecard(BaseModel):
    """Normalized offline scores used for review prioritization only."""

    model_config = ConfigDict(extra="forbid")

    safety_score: float = 0.0
    license_score: float = 0.0
    maintenance_score: float = 0.0
    adaptability_score: float = 0.0
    overall_score: float = 0.0


class ExternalSkillReviewPackage(BaseModel):
    """Stable, review-only decision package for one external skill candidate."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    candidate_name: str
    task_demand: ExternalSkillDemandKind
    adapted_task_type: ExternalSkillDemandKind
    status: ExternalSkillReviewStatus
    worth_review: bool
    confidence: float = 0.0
    risk_level: str = "unknown"
    scorecard: ExternalSkillReviewScorecard = Field(default_factory=ExternalSkillReviewScorecard)
    reasons: list[str] = Field(default_factory=list)
    safety_risks: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    suggested_validation_steps: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)
    known_source_profile: KnownExternalSkillSourceProfile | None = None
    candidate_summary: dict[str, Any] = Field(default_factory=dict)
    review_only: Literal[True] = True
    auto_install_allowed: Literal[False] = False
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


class ExternalSkillSourceReview(BaseModel):
    """Review-only decision package for a registered candidate source."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    source_name: str
    task_demand: ExternalSkillDemandKind
    source_demand: ExternalSkillDemandKind
    status: ExternalSkillReviewStatus
    worth_review: bool
    confidence: float = 0.0
    risk_level: str = "unknown"
    license_id: str = "unknown"
    maintenance_status: str = "unknown"
    candidate_count: int = 0
    scorecard: ExternalSkillReviewScorecard = Field(default_factory=ExternalSkillReviewScorecard)
    reasons: list[str] = Field(default_factory=list)
    safety_risks: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    suggested_validation_steps: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)
    known_source_profile: KnownExternalSkillSourceProfile | None = None
    review_only: Literal[True] = True
    auto_fetch_allowed: Literal[False] = False
    auto_install_allowed: Literal[False] = False
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


class ExternalSkillScoutPlan(BaseModel):
    """Review-only plan for looking for external skills/templates.

    KUN should not blindly install random GitHub skills.  This plan is the
    missing middle layer: given a real task need, it records what kind of
    external capability would be useful, where a human/Qi scout may look, and
    what safety checks are required before any candidate can become usable.
    """

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    task_demand: ExternalSkillDemandKind
    task_type: str = "general"
    need_summary: str = ""
    scout_queries: list[str] = Field(default_factory=list)
    recommended_repo_refs: list[str] = Field(default_factory=list)
    known_source_profiles: list[KnownExternalSkillSourceProfile] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    required_review_steps: list[str] = Field(default_factory=list)
    review_only: Literal[True] = True
    auto_fetch_allowed: Literal[False] = False
    auto_install_allowed: Literal[False] = False
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


class ExternalSkillCandidateSourcePlan(BaseModel):
    """Closed-loop, offline plan from task gap to source/candidate review."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    task_demand: ExternalSkillDemandKind
    task_type: str = "general"
    need_summary: str = ""
    scout_queries: list[str] = Field(default_factory=list)
    recommended_repo_refs: list[str] = Field(default_factory=list)
    known_source_profiles: list[KnownExternalSkillSourceProfile] = Field(default_factory=list)
    source_types: list[str] = Field(default_factory=list)
    source_reviews: list[ExternalSkillSourceReview] = Field(default_factory=list)
    candidate_reviews: list[ExternalSkillReviewPackage] = Field(default_factory=list)
    recommended_next_actions: list[str] = Field(default_factory=list)
    required_review_steps: list[str] = Field(default_factory=list)
    source_registry_size: int = 0
    reviewed_candidate_count: int = 0
    reasons: list[str] = Field(default_factory=list)
    review_only: Literal[True] = True
    offline_only: Literal[True] = True
    auto_fetch_allowed: Literal[False] = False
    auto_install_allowed: Literal[False] = False
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


def build_external_skill_scout_plan(
    task_need: str | dict[str, Any],
) -> ExternalSkillScoutPlan:
    """Create a safe external-skill scout plan from a real KUN task need.

    This deliberately stops at a plan.  A later operator/Qi step may decide to
    put an approved repo into ``KUN_EXTERNAL_SKILL_GITHUB_REPOS`` or provide
    offline metadata, but this function itself never fetches or installs.
    """

    raw = {"description": task_need} if isinstance(task_need, str) else dict(task_need)
    task_demand = _task_need_demand(raw)
    task_type = str(raw.get("task_type") or raw.get("category") or "general").strip() or "general"
    summary = str(
        raw.get("summary")
        or raw.get("goal")
        or raw.get("description")
        or raw.get("name")
        or task_type
    ).strip()[:500]
    recommended = _recommended_repos_for_demand(task_demand)
    known_profiles = _known_source_profiles_for_repos(recommended)
    queries = _scout_queries_for_demand(task_demand, task_type=task_type, summary=summary)
    source_types = ["github_repo", "skill_marketplace_metadata", "engineering_template"]
    if task_demand in {"research", "ops"}:
        source_types.append("vendor_docs")
    reasons = [
        "task_need_requires_external_capability_search",
        f"detected_task_demand:{task_demand}",
    ]
    if recommended:
        reasons.append("known_review_only_repo_suggestions_available")
    if known_profiles:
        reasons.append("known_source_profiles_attached_for_review_context")
    return ExternalSkillScoutPlan(
        plan_id=_stable_scout_plan_id(
            task_demand=task_demand, task_type=task_type, summary=summary
        ),
        task_demand=task_demand,
        task_type=task_type,
        need_summary=summary,
        scout_queries=queries,
        recommended_repo_refs=recommended,
        known_source_profiles=known_profiles,
        source_types=source_types,
        reasons=reasons,
        required_review_steps=[
            "fetch_metadata_only",
            "static_safety_review",
            "license_check",
            "verify_known_source_profile_against_current_repo_metadata",
            "sandbox_dry_run_if_scripts_exist",
            "human_review_before_install",
            "no_production_registration_without_canary",
        ],
    )


def external_skill_scout_plan_to_problem_signal(
    *,
    tenant_id: str,
    plan: ExternalSkillScoutPlan,
) -> QiProblemSignal:
    """Queue the scout plan for Qi/NUO review without performing the search."""

    return QiProblemSignal.build(
        tenant_id=tenant_id,
        category="context",
        severity="info" if plan.task_demand != "unknown" else "warning",
        summary=f"Plan external skill scout: {plan.task_type} ({plan.task_demand})",
        source="external_skill.scout_plan",
        task_type=f"external_skill_scout:{plan.task_demand}",
        evidence={
            **plan.model_dump(mode="json"),
            "queue_intent": "external_skill_scout_review_only",
        },
    )


async def enqueue_external_skill_scout_plans(
    *,
    tenant_id: str,
    plans: list[ExternalSkillScoutPlan],
) -> int:
    """Persist scout plans as review-only Qi signals."""

    return await persist_problem_signals(
        [
            external_skill_scout_plan_to_problem_signal(
                tenant_id=tenant_id,
                plan=plan,
            )
            for plan in plans
        ]
    )


def review_external_skill_candidate(
    *,
    task_need: str | dict[str, Any],
    candidate: ExternalSkillCandidate | dict[str, Any],
) -> ExternalSkillReviewPackage:
    """Build a safe review package for an external skill/template candidate.

    The decision is intentionally conservative:
    - production promotion is always forbidden;
    - executable / installer candidates always require sandbox + human review;
    - low-evidence candidates can be queued for review but never treated as
      ready for production.
    """

    normalized = (
        candidate
        if isinstance(candidate, ExternalSkillCandidate)
        else normalize_external_skill_candidate(candidate)
    )
    task_demand = _task_need_demand(task_need)
    if normalized is None:
        return ExternalSkillReviewPackage(
            candidate_id="invalid_external_skill_candidate",
            candidate_name="invalid external skill candidate",
            task_demand=task_demand,
            adapted_task_type="unknown",
            status="blocked",
            worth_review=False,
            confidence=0.0,
            risk_level="critical",
            scorecard=ExternalSkillReviewScorecard(),
            reasons=["candidate_metadata_could_not_be_normalized"],
            safety_risks=["invalid_candidate_metadata"],
            missing_evidence=["valid_candidate_metadata"],
            suggested_validation_steps=[
                "provide_repo_or_url",
                "provide_skill_metadata_or_skill_md",
                "do_not_install",
            ],
        )

    safety = normalized.safety
    adapted_task_type = normalized.demand_match.primary
    task_fit = _task_fit_score(task_demand, normalized)
    confidence = round(min(0.95, max(normalized.demand_match.confidence, task_fit)), 2)
    reasons = _review_reasons(task_demand, normalized, task_fit)
    safety_risks = _safety_risks(normalized)
    missing = _missing_evidence(task_demand, normalized, task_fit)
    validation_steps = _validation_steps(normalized, task_fit)
    status = _review_status(normalized, task_fit, missing)
    worth_review = status != "blocked" and task_fit >= 0.25
    scorecard = _candidate_scorecard(normalized, task_fit)
    known_profile = _known_source_profile_for_repo(normalized.source.repo)

    return ExternalSkillReviewPackage(
        candidate_id=normalized.candidate_id,
        candidate_name=normalized.name,
        task_demand=task_demand,
        adapted_task_type=adapted_task_type,
        status=status,
        worth_review=worth_review,
        confidence=confidence,
        risk_level=safety.risk_level,
        scorecard=scorecard,
        reasons=_dedupe(reasons),
        safety_risks=_dedupe(safety_risks),
        missing_evidence=_dedupe(missing),
        suggested_validation_steps=_dedupe(validation_steps),
        source={
            "kind": normalized.source.kind,
            "repo": normalized.source.repo,
            "url": normalized.source.url,
            "commit_sha": normalized.source.commit_sha,
        },
        known_source_profile=known_profile,
        candidate_summary={
            "summary": normalized.summary,
            "tags": list(normalized.tags),
            "maintenance": {
                "status": normalized.maintenance.status,
                "score": normalized.maintenance.score,
                "reasons": list(normalized.maintenance.reasons),
            },
            "demand_match": {
                "primary": normalized.demand_match.primary,
                "categories": list(normalized.demand_match.categories),
                "confidence": normalized.demand_match.confidence,
                "matched_keywords": {
                    key: list(value)
                    for key, value in normalized.demand_match.matched_keywords.items()
                },
            },
        },
    )


def review_external_skill_candidates(
    *,
    task_need: str | dict[str, Any],
    candidates: list[ExternalSkillCandidate | dict[str, Any]],
) -> list[ExternalSkillReviewPackage]:
    """Review a small batch and return highest-value packages first."""

    packages = [
        review_external_skill_candidate(task_need=task_need, candidate=candidate)
        for candidate in candidates
    ]
    return sorted(
        packages,
        key=lambda package: (
            package.status == "blocked",
            not package.worth_review,
            -package.scorecard.overall_score,
            -package.confidence,
            package.risk_level,
            package.candidate_name,
        ),
    )


def build_external_skill_candidate_source_plan(
    task_need: str | dict[str, Any],
    *,
    source_registry: list[ExternalSkillSourceRegistration | dict[str, Any]] | None = None,
    candidates: list[ExternalSkillCandidate | dict[str, Any]] | None = None,
) -> ExternalSkillCandidateSourcePlan:
    """Build the closed-loop, offline external capability review plan.

    This does not fetch network metadata, install packages, register skills, or
    promote production capability. It only scores supplied source/candidate
    evidence and packages it for Qi/NUO/human review.
    """

    scout_plan = build_external_skill_scout_plan(task_need)
    raw_sources = _coerce_source_registry(source_registry, scout_plan=scout_plan)
    source_reviews = [
        _review_external_skill_source(
            task_demand=scout_plan.task_demand,
            source=source,
        )
        for source in _normalize_source_registry(raw_sources)
    ]
    candidate_reviews = review_external_skill_candidates(
        task_need=task_need,
        candidates=_candidate_review_inputs(
            raw_sources=raw_sources,
            candidates=candidates or [],
        ),
    )
    source_reviews = _sort_source_reviews(source_reviews)
    next_actions = _source_plan_next_actions(source_reviews, candidate_reviews)
    return ExternalSkillCandidateSourcePlan(
        plan_id=_stable_candidate_source_plan_id(
            scout_plan=scout_plan,
            source_reviews=source_reviews,
            candidate_reviews=candidate_reviews,
        ),
        task_demand=scout_plan.task_demand,
        task_type=scout_plan.task_type,
        need_summary=scout_plan.need_summary,
        scout_queries=list(scout_plan.scout_queries),
        recommended_repo_refs=list(scout_plan.recommended_repo_refs),
        known_source_profiles=list(scout_plan.known_source_profiles),
        source_types=list(scout_plan.source_types),
        source_reviews=source_reviews,
        candidate_reviews=candidate_reviews,
        recommended_next_actions=next_actions,
        required_review_steps=[
            *list(scout_plan.required_review_steps),
            "score_source_safety_license_maintenance_fit",
            "compare_offline_candidates_against_task_gap",
            "qi_nuo_or_human_review_before_fetch_or_install",
        ],
        source_registry_size=len(raw_sources),
        reviewed_candidate_count=len(candidate_reviews),
        reasons=[
            *list(scout_plan.reasons),
            "offline_source_registry_scored",
            "offline_candidates_scored",
        ],
    )


def external_skill_candidate_source_plan_to_problem_signal(
    *,
    tenant_id: str,
    plan: ExternalSkillCandidateSourcePlan,
) -> QiProblemSignal:
    """Queue a candidate source plan for Qi/NUO/human review."""

    severity = _source_plan_signal_severity(plan)
    return QiProblemSignal.build(
        tenant_id=tenant_id,
        category="risk" if severity in {"error", "critical"} else "context",
        severity=severity,
        summary=f"Plan external capability sources: {plan.task_type} ({plan.task_demand})",
        source="external_skill.source_plan",
        task_type=f"external_skill_source_plan:{plan.task_demand}",
        evidence={
            **plan.model_dump(mode="json"),
            "queue_intent": "external_skill_source_plan_review_only",
        },
    )


async def enqueue_external_skill_candidate_source_plans(
    *,
    tenant_id: str,
    plans: list[ExternalSkillCandidateSourcePlan],
) -> int:
    """Persist source/candidate plans as review-only Qi signals."""

    return await persist_problem_signals(
        [
            external_skill_candidate_source_plan_to_problem_signal(
                tenant_id=tenant_id,
                plan=plan,
            )
            for plan in plans
        ]
    )


def external_skill_review_package_to_problem_signal(
    *,
    tenant_id: str,
    package: ExternalSkillReviewPackage,
) -> QiProblemSignal:
    """Turn a review-only package into a Qi/NUO-consumable problem signal.

    This is a review queue bridge, not an installation path. The evidence keeps
    the no-production flags explicit so later Qi / NUO / human reviewers cannot
    accidentally treat an external candidate as approved capability.
    """

    evidence = {
        "candidate_id": package.candidate_id,
        "candidate_name": package.candidate_name,
        "task_demand": package.task_demand,
        "adapted_task_type": package.adapted_task_type,
        "status": package.status,
        "worth_review": package.worth_review,
        "confidence": package.confidence,
        "risk_level": package.risk_level,
        "scorecard": package.scorecard.model_dump(mode="json"),
        "reasons": list(package.reasons),
        "safety_risks": list(package.safety_risks),
        "missing_evidence": list(package.missing_evidence),
        "suggested_validation_steps": list(package.suggested_validation_steps),
        "source": dict(package.source),
        "known_source_profile": package.known_source_profile.model_dump(mode="json")
        if package.known_source_profile is not None
        else None,
        "candidate_summary": dict(package.candidate_summary),
        "review_only": package.review_only,
        "auto_install_allowed": package.auto_install_allowed,
        "production_action": package.production_action,
        "promotion_allowed": package.promotion_allowed,
        "queue_intent": "external_skill_review_only",
    }
    return QiProblemSignal.build(
        tenant_id=tenant_id,
        category=_signal_category(package),
        severity=_signal_severity(package),
        summary=_signal_summary(package),
        source="external_skill.review.package",
        task_type=f"external_skill:{package.adapted_task_type}",
        evidence=evidence,
    )


def external_skill_review_packages_to_problem_signals(
    *,
    tenant_id: str,
    packages: list[ExternalSkillReviewPackage],
) -> list[QiProblemSignal]:
    """Convert review packages to dedupable Qi signals."""

    return [
        external_skill_review_package_to_problem_signal(
            tenant_id=tenant_id,
            package=package,
        )
        for package in packages
    ]


async def enqueue_external_skill_review_packages(
    *,
    tenant_id: str,
    packages: list[ExternalSkillReviewPackage],
) -> int:
    """Persist review-only external skill signals for Qi/NUO consumption."""

    signals = external_skill_review_packages_to_problem_signals(
        tenant_id=tenant_id,
        packages=packages,
    )
    return await persist_problem_signals(signals)


def _task_need_demand(task_need: str | dict[str, Any]) -> ExternalSkillDemandKind:
    raw = {"description": task_need} if isinstance(task_need, str) else dict(task_need)
    pseudo = normalize_external_skill_candidate(
        {
            "source_kind": "task_need",
            "repo": "kun/task-need",
            "name": raw.get("name") or raw.get("task_type") or "task need",
            "description": raw.get("description") or raw.get("goal") or raw.get("summary") or "",
            "topics": raw.get("topics") or raw.get("tags") or [],
            "files": [],
            "license": "internal",
        }
    )
    if pseudo is None:
        return "unknown"
    return pseudo.demand_match.primary


def _stable_scout_plan_id(
    *,
    task_demand: ExternalSkillDemandKind,
    task_type: str,
    summary: str,
) -> str:
    digest = hashlib.sha256(f"{task_demand}|{task_type}|{summary[:200]}".encode()).hexdigest()[:16]
    return f"esk_plan_{digest}"


def _recommended_repos_for_demand(task_demand: ExternalSkillDemandKind) -> list[str]:
    """Small curated hints, not an auto-fetch allowlist."""

    repos: list[str] = []
    for repo, profile in _KNOWN_EXTERNAL_SKILL_SOURCE_PROFILES.items():
        if task_demand in profile.expected_use_cases:
            repos.append(repo)
    return sorted(repos)


def _known_source_profiles_for_repos(
    repos: list[str],
) -> list[KnownExternalSkillSourceProfile]:
    profiles: list[KnownExternalSkillSourceProfile] = []
    for repo in repos:
        profile = _known_source_profile_for_repo(repo)
        if profile is not None:
            profiles.append(profile)
    return profiles


def _known_source_profile_for_repo(repo: str) -> KnownExternalSkillSourceProfile | None:
    normalized = str(repo or "").strip().removesuffix(".git").lower()
    return _KNOWN_EXTERNAL_SKILL_SOURCE_PROFILES.get(normalized)


def _scout_queries_for_demand(
    task_demand: ExternalSkillDemandKind,
    *,
    task_type: str,
    summary: str,
) -> list[str]:
    base = task_type.replace(".", " ").replace("_", " ").strip() or task_demand
    summary_hint = " ".join(summary.split()[:12])
    if task_demand == "coding":
        return [
            f"github SKILL.md engineering {base}",
            f"code review skill {summary_hint}",
            f"developer workflow template {base}",
        ]
    if task_demand == "review":
        return [
            f"github SKILL.md review {base}",
            f"engineering review checklist {summary_hint}",
            f"pull request review skill {base}",
        ]
    if task_demand == "writing":
        return [
            f"writing skill template {base}",
            f"editorial checklist {summary_hint}",
            f"copywriting workflow skill {base}",
        ]
    if task_demand == "research":
        return [
            f"research synthesis skill {base}",
            f"literature review workflow {summary_hint}",
            f"source finding template {base}",
        ]
    if task_demand == "ops":
        return [
            f"incident runbook skill {base}",
            f"deployment ops checklist {summary_hint}",
            f"monitoring troubleshooting workflow {base}",
        ]
    return [
        f"agent skill template {base}",
        f"workflow skill {summary_hint}",
    ]


def _task_fit_score(
    task_demand: ExternalSkillDemandKind, candidate: ExternalSkillCandidate
) -> float:
    return score_external_skill_task_fit(task_demand, candidate.demand_match)


def _candidate_scorecard(
    candidate: ExternalSkillCandidate,
    task_fit: float,
) -> ExternalSkillReviewScorecard:
    safety_score = score_external_skill_safety(candidate.safety)
    license_score = score_external_skill_license(candidate.safety.license_id)
    maintenance_score = candidate.maintenance.score
    return ExternalSkillReviewScorecard(
        safety_score=safety_score,
        license_score=license_score,
        maintenance_score=maintenance_score,
        adaptability_score=round(task_fit, 2),
        overall_score=_overall_review_score(
            safety_score=safety_score,
            license_score=license_score,
            maintenance_score=maintenance_score,
            adaptability_score=task_fit,
        ),
    )


def _overall_review_score(
    *,
    safety_score: float,
    license_score: float,
    maintenance_score: float,
    adaptability_score: float,
) -> float:
    score = (
        safety_score * 0.3
        + license_score * 0.2
        + maintenance_score * 0.2
        + adaptability_score * 0.3
    )
    return round(max(0.0, min(1.0, score)), 2)


def _coerce_source_registry(
    source_registry: list[ExternalSkillSourceRegistration | dict[str, Any]] | None,
    *,
    scout_plan: ExternalSkillScoutPlan,
) -> list[ExternalSkillSourceRegistration | dict[str, Any]]:
    if source_registry:
        return list(source_registry)
    raw_sources: list[ExternalSkillSourceRegistration | dict[str, Any]] = []
    for repo in scout_plan.recommended_repo_refs:
        profile = _known_source_profile_for_repo(repo)
        raw: dict[str, Any] = {
            "source_kind": "github_repo",
            "repo": repo,
            "name": repo,
            "description": (
                "Review-only curated source hint from scout planning; metadata still "
                "needs offline evidence before any fetch or installation."
            ),
            "license": "unknown",
            "topics": [scout_plan.task_demand, "external_skill"],
            "source_origin": "scout_recommendation",
        }
        if profile is not None:
            raw.update(
                {
                    "description": " ".join(profile.why_review),
                    "topics": [
                        *list(profile.expected_use_cases),
                        "external_skill",
                        "known_source_profile",
                    ],
                    "source_origin": "known_review_only_source_profile",
                    "known_source_profile": profile.model_dump(mode="json"),
                }
            )
        raw_sources.append(raw)
    return raw_sources


def _normalize_source_registry(
    raw_sources: list[ExternalSkillSourceRegistration | dict[str, Any]],
) -> list[ExternalSkillSourceRegistration]:
    sources: list[ExternalSkillSourceRegistration] = []
    for raw in raw_sources:
        if isinstance(raw, ExternalSkillSourceRegistration):
            sources.append(raw)
            continue
        if not isinstance(raw, dict):
            continue
        normalized = normalize_external_skill_source_registration(raw)
        if normalized is not None:
            sources.append(normalized)
    by_id = {source.source_id: source for source in sources}
    return list(by_id.values())


def _candidate_review_inputs(
    *,
    raw_sources: list[ExternalSkillSourceRegistration | dict[str, Any]],
    candidates: list[ExternalSkillCandidate | dict[str, Any]],
) -> list[ExternalSkillCandidate | dict[str, Any]]:
    inputs: list[ExternalSkillCandidate | dict[str, Any]] = list(candidates)
    source_dicts = [source for source in raw_sources if isinstance(source, dict)]
    inputs.extend(normalize_external_skill_candidates(source_dicts))
    by_key: dict[str, ExternalSkillCandidate | dict[str, Any]] = {}
    for item in inputs:
        if isinstance(item, ExternalSkillCandidate):
            key = item.candidate_id
        else:
            key = "|".join(
                str(item.get(part) or "")
                for part in ("source_kind", "repo", "url", "commit_sha", "name", "description")
            )
        by_key[key] = item
    return list(by_key.values())


def _review_external_skill_source(
    *,
    task_demand: ExternalSkillDemandKind,
    source: ExternalSkillSourceRegistration,
) -> ExternalSkillSourceReview:
    task_fit = score_external_skill_task_fit(task_demand, source.demand_match)
    safety_score = score_external_skill_safety(source.safety)
    license_score = score_external_skill_license(source.safety.license_id)
    maintenance_score = source.maintenance.score
    scorecard = ExternalSkillReviewScorecard(
        safety_score=safety_score,
        license_score=license_score,
        maintenance_score=maintenance_score,
        adaptability_score=round(task_fit, 2),
        overall_score=_overall_review_score(
            safety_score=safety_score,
            license_score=license_score,
            maintenance_score=maintenance_score,
            adaptability_score=task_fit,
        ),
    )
    missing = _source_missing_evidence(source, task_fit)
    status = _source_review_status(source, task_fit, missing)
    worth_review = status != "blocked" and task_fit >= 0.25 and scorecard.overall_score >= 0.35
    known_profile = _known_source_profile_for_repo(source.source.repo)
    return ExternalSkillSourceReview(
        source_id=source.source_id,
        source_name=source.name,
        task_demand=task_demand,
        source_demand=source.demand_match.primary,
        status=status,
        worth_review=worth_review,
        confidence=round(min(0.95, max(source.demand_match.confidence, task_fit)), 2),
        risk_level=source.safety.risk_level,
        license_id=source.safety.license_id,
        maintenance_status=source.maintenance.status,
        candidate_count=source.candidate_count,
        scorecard=scorecard,
        reasons=_dedupe(_source_review_reasons(task_demand, source, task_fit)),
        safety_risks=_dedupe(_source_safety_risks(source)),
        missing_evidence=_dedupe(missing),
        suggested_validation_steps=_dedupe(_source_validation_steps(source, task_fit)),
        source={
            "kind": source.source.kind,
            "repo": source.source.repo,
            "url": source.source.url,
            "commit_sha": source.source.commit_sha,
            "maintenance": {
                "status": source.maintenance.status,
                "score": source.maintenance.score,
                "reasons": list(source.maintenance.reasons),
            },
        },
        known_source_profile=known_profile,
    )


def _source_review_status(
    source: ExternalSkillSourceRegistration,
    task_fit: float,
    missing_evidence: list[str],
) -> ExternalSkillReviewStatus:
    if source.safety.risk_level == "critical" or source.maintenance.status == "deprecated":
        return "blocked"
    if task_fit < 0.2 and not source.source.repo and not source.source.url:
        return "blocked"
    if missing_evidence:
        return "needs_evidence"
    if source.safety.risk_level in {"high", "critical"}:
        return "needs_evidence"
    return "ready_for_human_review"


def _source_review_reasons(
    task_demand: ExternalSkillDemandKind,
    source: ExternalSkillSourceRegistration,
    task_fit: float,
) -> list[str]:
    reasons = ["review_only_source_plan_no_auto_fetch_or_install"]
    if task_fit >= 0.65:
        reasons.append("source_matches_task_demand")
    elif task_fit >= 0.25:
        reasons.append("source_partially_matches_task_demand")
    else:
        reasons.append("source_does_not_match_task_demand")
    reasons.append(f"task_demand:{task_demand}")
    reasons.append(f"source_demand:{source.demand_match.primary}")
    reasons.extend(source.demand_match.reasons)
    reasons.extend(source.safety.reasons)
    reasons.extend(source.maintenance.reasons)
    if _known_source_profile_for_repo(source.source.repo) is not None:
        reasons.append("known_source_profile_matched")
    return reasons


def _source_safety_risks(source: ExternalSkillSourceRegistration) -> list[str]:
    safety = source.safety
    risks = [f"risk_level:{safety.risk_level}"]
    if _license_requires_legal_review(safety.license_id):
        risks.append("license_requires_legal_review")
    if safety.license_unknown:
        risks.append("license_unknown")
    if safety.contains_execution_scripts:
        risks.append("contains_execution_scripts")
    if safety.external_network_risk:
        risks.append("external_network_risk")
    if safety.secret_access_risk:
        risks.append("secret_access_risk")
    if safety.file_write_risk:
        risks.append("file_write_risk")
    if source.maintenance.status in {"stale", "deprecated", "unknown"}:
        risks.append(f"maintenance_{source.maintenance.status}")
    return risks


def _source_missing_evidence(
    source: ExternalSkillSourceRegistration,
    task_fit: float,
) -> list[str]:
    missing: list[str] = []
    if not source.source.repo and not source.source.url:
        missing.append("source_repo_or_url")
    if source.safety.license_unknown:
        missing.append("known_license")
    if _license_requires_legal_review(source.safety.license_id):
        missing.append("legal_license_review")
    if not source.source.commit_sha:
        missing.append("pinned_source_revision_before_fetch")
    if task_fit < 0.65:
        missing.append("source_task_fit_evidence")
    if source.candidate_count <= 0:
        missing.append("candidate_inventory")
    if source.maintenance.status in {"stale", "unknown"}:
        missing.append("maintenance_evidence")
    if source.safety.contains_execution_scripts:
        missing.append("manual_install_script_review")
    if source.safety.external_network_risk:
        missing.append("network_access_review")
    if source.safety.secret_access_risk:
        missing.append("secret_access_review")
    if source.safety.file_write_risk:
        missing.append("file_write_review")
    if source.safety.risk_level in {"high", "critical"}:
        missing.append("human_security_review")
    if source.summary.strip() == "":
        missing.append("source_summary_or_manifest")
    profile = _known_source_profile_for_repo(source.source.repo)
    if profile is not None:
        for evidence in profile.required_evidence:
            if evidence in {"pinned_commit_sha", "pinned_source_revision_before_fetch"}:
                if not source.source.commit_sha:
                    missing.append("pinned_source_revision_before_fetch")
                continue
            if evidence in {"license_and_notice_review", "license_check"}:
                if _license_requires_legal_review(source.safety.license_id):
                    missing.append("legal_license_review")
                continue
            if evidence == "skill_md_inventory" and source.candidate_count <= 0:
                missing.append("candidate_inventory")
    return missing


def _source_validation_steps(
    source: ExternalSkillSourceRegistration,
    task_fit: float,
) -> list[str]:
    steps = [
        "do_not_auto_fetch",
        "do_not_auto_install",
        "review_source_manifest_and_license",
        "inventory_candidate_skills_or_templates_offline",
        "human_review_before_any_fetch_install_or_registration",
    ]
    if task_fit < 0.65:
        steps.append("verify_source_fits_current_task_gap")
    if source.maintenance.status in {"stale", "unknown"}:
        steps.append("verify_maintainer_activity_before_testing")
    if source.safety.contains_execution_scripts:
        steps.append("manual_review_install_or_support_scripts")
    if source.safety.risk_level in {"high", "critical"}:
        steps.append("require_security_reviewer_approval")
    profile = _known_source_profile_for_repo(source.source.repo)
    if profile is not None:
        steps.extend(profile.required_evidence)
        steps.extend(profile.safety_rules)
    return steps


def _sort_source_reviews(
    source_reviews: list[ExternalSkillSourceReview],
) -> list[ExternalSkillSourceReview]:
    return sorted(
        source_reviews,
        key=lambda review: (
            review.status == "blocked",
            not review.worth_review,
            -review.scorecard.overall_score,
            review.risk_level,
            review.source_name,
        ),
    )


def _source_plan_next_actions(
    source_reviews: list[ExternalSkillSourceReview],
    candidate_reviews: list[ExternalSkillReviewPackage],
) -> list[str]:
    actions = [
        "present_source_and_candidate_scorecards_to_qi_nuo_or_human_reviewer",
        "do_not_fetch_install_register_or_execute_from_this_plan",
    ]
    reviews: list[ExternalSkillSourceReview | ExternalSkillReviewPackage] = [
        *source_reviews,
        *candidate_reviews,
    ]
    if any(review.status == "blocked" for review in reviews):
        actions.append("route_blocked_items_to_risk_review")
    if any(review.status == "needs_evidence" for review in reviews):
        actions.append("collect_missing_offline_evidence_before_sandbox_testing")
    if any(review.status == "ready_for_human_review" for review in source_reviews):
        actions.append("human_may_select_source_for_separate_metadata_fetch_request")
    if any(review.status == "ready_for_human_review" for review in candidate_reviews):
        actions.append("human_may_select_candidate_for_disposable_sandbox_validation")
    return _dedupe(actions)


def _stable_candidate_source_plan_id(
    *,
    scout_plan: ExternalSkillScoutPlan,
    source_reviews: list[ExternalSkillSourceReview],
    candidate_reviews: list[ExternalSkillReviewPackage],
) -> str:
    source_part = "|".join(review.source_id for review in source_reviews[:20])
    candidate_part = "|".join(review.candidate_id for review in candidate_reviews[:20])
    digest = hashlib.sha256(
        f"{scout_plan.plan_id}|{source_part}|{candidate_part}".encode()
    ).hexdigest()[:16]
    return f"esk_src_plan_{digest}"


def _source_plan_signal_severity(plan: ExternalSkillCandidateSourcePlan) -> str:
    reviews: list[ExternalSkillSourceReview | ExternalSkillReviewPackage] = [
        *plan.source_reviews,
        *plan.candidate_reviews,
    ]
    if any(review.status == "blocked" and review.risk_level == "critical" for review in reviews):
        return "critical"
    if any(review.status == "blocked" for review in reviews):
        return "error"
    if any(review.status == "needs_evidence" for review in reviews):
        return "warning"
    return "info"


def _review_status(
    candidate: ExternalSkillCandidate,
    task_fit: float,
    missing_evidence: list[str],
) -> ExternalSkillReviewStatus:
    safety = candidate.safety
    if safety.risk_level == "critical":
        return "blocked"
    if (
        not candidate.source.repo
        and not candidate.source.url
        and safety.risk_level in {"high", "critical"}
    ):
        return "blocked"
    if task_fit < 0.2 and not candidate.source.repo and not candidate.source.url:
        return "blocked"
    if missing_evidence:
        return "needs_evidence"
    if safety.risk_level in {"high", "critical"}:
        return "needs_evidence"
    return "ready_for_human_review"


def _review_reasons(
    task_demand: ExternalSkillDemandKind,
    candidate: ExternalSkillCandidate,
    task_fit: float,
) -> list[str]:
    reasons = ["review_only_no_auto_install"]
    if task_fit >= 0.65:
        reasons.append("candidate_matches_task_demand")
    elif task_fit >= 0.25:
        reasons.append("candidate_partially_matches_task_demand")
    else:
        reasons.append("candidate_does_not_match_task_demand")
    if task_demand != "unknown":
        reasons.append(f"task_demand:{task_demand}")
    reasons.append(f"candidate_demand:{candidate.demand_match.primary}")
    reasons.extend(candidate.demand_match.reasons)
    reasons.extend(candidate.safety.reasons)
    if _known_source_profile_for_repo(candidate.source.repo) is not None:
        reasons.append("known_source_profile_matched")
    return reasons


def _safety_risks(candidate: ExternalSkillCandidate) -> list[str]:
    safety = candidate.safety
    risks = [f"risk_level:{safety.risk_level}"]
    if _license_requires_legal_review(safety.license_id):
        risks.append("license_requires_legal_review")
    if safety.license_unknown:
        risks.append("license_unknown")
    if safety.contains_execution_scripts:
        risks.append("contains_execution_scripts")
    if safety.external_network_risk:
        risks.append("external_network_risk")
    if safety.secret_access_risk:
        risks.append("secret_access_risk")
    if safety.file_write_risk:
        risks.append("file_write_risk")
    if safety.evidence.get("auto_trigger_entries"):
        risks.append("auto_trigger_policy_review_required")
    if safety.evidence.get("auto_trigger_issue_count"):
        risks.append("auto_trigger_risk")
    if not safety.sandbox_suitable:
        risks.append("sandbox_not_suitable_without_manual_controls")
    return risks


def _missing_evidence(
    task_demand: ExternalSkillDemandKind,
    candidate: ExternalSkillCandidate,
    task_fit: float,
) -> list[str]:
    safety = candidate.safety
    missing: list[str] = []
    if not candidate.source.repo and not candidate.source.url:
        missing.append("source_repo_or_url")
    if not candidate.source.commit_sha:
        missing.append("pinned_commit_sha")
    if safety.license_unknown:
        missing.append("known_license")
    if _license_requires_legal_review(safety.license_id):
        missing.append("legal_license_review")
    if task_demand == "unknown":
        missing.append("clear_task_need")
    if task_fit < 0.65:
        missing.append("task_fit_evidence")
    if safety.contains_execution_scripts:
        missing.extend(["sandbox_test_evidence", "manual_install_script_review"])
    if safety.external_network_risk:
        missing.append("network_access_review")
    if safety.secret_access_risk:
        missing.append("secret_access_review")
    if safety.file_write_risk:
        missing.append("file_write_review")
    if safety.evidence.get("auto_trigger_entries"):
        missing.append("auto_trigger_policy_review")
    if safety.risk_level in {"high", "critical"}:
        missing.append("human_security_review")
    if candidate.summary.strip() == "":
        missing.append("candidate_summary_or_skill_md")
    profile = _known_source_profile_for_repo(candidate.source.repo)
    if profile is not None:
        for evidence in profile.required_evidence:
            if evidence == "pinned_commit_sha" and not candidate.source.commit_sha:
                missing.append("pinned_commit_sha")
            elif evidence == "license_and_notice_review" and _license_requires_legal_review(
                safety.license_id
            ):
                missing.append("legal_license_review")
            elif evidence == "skill_md_inventory" and candidate.summary.strip() == "":
                missing.append("candidate_summary_or_skill_md")
    return missing


def _validation_steps(candidate: ExternalSkillCandidate, task_fit: float) -> list[str]:
    safety = candidate.safety
    steps = [
        "do_not_auto_install",
        "read_skill_md_and_license",
        "map_candidate_to_task_acceptance_tests",
        "human_review_before_registration",
    ]
    if task_fit < 0.65:
        steps.append("verify_candidate_fits_this_task_before_any_sandbox_run")
    if safety.contains_execution_scripts:
        steps.extend(
            [
                "manual_review_install_or_support_scripts",
                "run_in_disposable_sandbox_with_no_secrets",
                "start_with_network_disabled",
            ]
        )
    if safety.external_network_risk:
        steps.append("allowlist_network_targets_before_testing")
    if safety.secret_access_risk:
        steps.append("use_fake_secrets_only")
    if safety.file_write_risk:
        steps.append("mount_temp_workspace_read_write_only")
    if safety.evidence.get("auto_trigger_entries"):
        steps.append("manual_review_skill_auto_trigger_policy")
    if safety.risk_level in {"high", "critical"}:
        steps.append("require_security_reviewer_approval")
    if safety.risk_level in {"low", "medium"} and not safety.contains_execution_scripts:
        steps.append("dry_run_against_non_production_task")
    profile = _known_source_profile_for_repo(candidate.source.repo)
    if profile is not None:
        steps.extend(profile.required_evidence)
        steps.extend(profile.safety_rules)
    return steps


def _license_requires_legal_review(license_id: str) -> bool:
    """Copyleft / unknown / proprietary licenses never become ready by score alone."""

    normalized = str(license_id or "unknown").strip().lower()
    return normalized in _LICENSE_NEEDS_LEGAL_REVIEW


def _signal_category(package: ExternalSkillReviewPackage) -> Literal["risk", "context"]:
    if package.status == "blocked":
        return "risk"
    if package.risk_level in {"high", "critical"}:
        return "risk"
    if any("risk" in item or "security" in item for item in package.missing_evidence):
        return "risk"
    return "context"


def _signal_severity(package: ExternalSkillReviewPackage) -> str:
    if package.status == "blocked":
        return "critical" if package.risk_level == "critical" else "error"
    if package.status == "needs_evidence":
        return "warning"
    return "info"


def _signal_summary(package: ExternalSkillReviewPackage) -> str:
    if package.status == "blocked":
        prefix = "External skill blocked"
    elif package.status == "needs_evidence":
        prefix = "External skill needs evidence"
    else:
        prefix = "External skill ready for human review"
    return f"{prefix}: {package.candidate_name}"


def _dedupe(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


__all__ = [
    "ExternalSkillCandidateSourcePlan",
    "ExternalSkillReviewPackage",
    "ExternalSkillReviewScorecard",
    "ExternalSkillReviewStatus",
    "ExternalSkillScoutPlan",
    "ExternalSkillSourceReview",
    "build_external_skill_candidate_source_plan",
    "build_external_skill_scout_plan",
    "enqueue_external_skill_candidate_source_plans",
    "enqueue_external_skill_review_packages",
    "enqueue_external_skill_scout_plans",
    "external_skill_candidate_source_plan_to_problem_signal",
    "external_skill_review_package_to_problem_signal",
    "external_skill_review_packages_to_problem_signals",
    "external_skill_scout_plan_to_problem_signal",
    "review_external_skill_candidate",
    "review_external_skill_candidates",
]
