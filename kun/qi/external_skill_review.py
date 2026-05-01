"""Review-only gate for external skill and engineering-template candidates.

This module does not fetch, install, register, or promote external skills. It
turns a task need plus offline candidate metadata into a small, auditable review
package for Qi / NUO / a human reviewer.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.engineering.external_scan import (
    ExternalSkillCandidate,
    ExternalSkillDemandKind,
    normalize_external_skill_candidate,
)
from kun.qi.problem_queue import QiProblemSignal, persist_problem_signals

ExternalSkillReviewStatus = Literal["blocked", "needs_evidence", "ready_for_human_review"]


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
    reasons: list[str] = Field(default_factory=list)
    safety_risks: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    suggested_validation_steps: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)
    candidate_summary: dict[str, Any] = Field(default_factory=dict)
    review_only: Literal[True] = True
    auto_install_allowed: Literal[False] = False
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


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

    return ExternalSkillReviewPackage(
        candidate_id=normalized.candidate_id,
        candidate_name=normalized.name,
        task_demand=task_demand,
        adapted_task_type=adapted_task_type,
        status=status,
        worth_review=worth_review,
        confidence=confidence,
        risk_level=safety.risk_level,
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
        candidate_summary={
            "summary": normalized.summary,
            "tags": list(normalized.tags),
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
            -package.confidence,
            package.risk_level,
            package.candidate_name,
        ),
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
        "reasons": list(package.reasons),
        "safety_risks": list(package.safety_risks),
        "missing_evidence": list(package.missing_evidence),
        "suggested_validation_steps": list(package.suggested_validation_steps),
        "source": dict(package.source),
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


def _task_fit_score(
    task_demand: ExternalSkillDemandKind, candidate: ExternalSkillCandidate
) -> float:
    if task_demand == "unknown" or candidate.demand_match.primary == "unknown":
        return 0.15
    if candidate.demand_match.primary == task_demand:
        return 0.85
    if task_demand in candidate.demand_match.categories:
        return 0.65
    if task_demand == "coding" and "review" in candidate.demand_match.categories:
        return 0.45
    if task_demand == "review" and "coding" in candidate.demand_match.categories:
        return 0.45
    return 0.1


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
    return reasons


def _safety_risks(candidate: ExternalSkillCandidate) -> list[str]:
    safety = candidate.safety
    risks = [f"risk_level:{safety.risk_level}"]
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
    if safety.risk_level in {"high", "critical"}:
        missing.append("human_security_review")
    if candidate.summary.strip() == "":
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
    if safety.risk_level in {"high", "critical"}:
        steps.append("require_security_reviewer_approval")
    if safety.risk_level in {"low", "medium"} and not safety.contains_execution_scripts:
        steps.append("dry_run_against_non_production_task")
    return steps


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
    "ExternalSkillReviewPackage",
    "ExternalSkillReviewStatus",
    "enqueue_external_skill_review_packages",
    "external_skill_review_package_to_problem_signal",
    "external_skill_review_packages_to_problem_signals",
    "review_external_skill_candidate",
    "review_external_skill_candidates",
]
