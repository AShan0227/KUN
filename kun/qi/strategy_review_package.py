"""Review-only strategy packages for Qi background search.

Qi may cheaply explore better routes during idle time, but that exploration
must not silently alter production behavior.  This module turns candidates,
drafts, local/cheap evaluations, lab replay, tree-search evidence, and optional
strong-review results into a single inspectable package.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from kun.qi.idle_replay import (
    ReplayEvaluationRecord,
    ReplayRisk,
    StrategyCandidate,
    StrategyPackDraft,
)
from kun.qi.lab_replay import QiLabReplayRecord
from kun.qi.replay_tree_search import QiReplayTreeSearchRecord

QiReviewPackageStatus = Literal[
    "needs_local_exploration",
    "needs_strong_review",
    "ready_for_human_review",
    "reject",
]
QiExplorationChannel = Literal[
    "heuristic",
    "local_model",
    "lab_replay",
    "tree_search",
]
QiStrongGateStatus = Literal[
    "not_required",
    "missing",
    "completed",
    "failed",
    "unavailable",
]


class QiExplorationEvidence(BaseModel):
    """One cheap/local evidence line in a Qi review package."""

    model_config = ConfigDict(extra="forbid")

    channel: QiExplorationChannel
    evidence_id: str
    status: str
    score: float = 0.0
    cost_usd: float = 0.0
    notes: list[str] = Field(default_factory=list)
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


class QiStrongReviewGate(BaseModel):
    """Strong-review gate state for a strategy candidate."""

    model_config = ConfigDict(extra="forbid")

    required: bool
    status: QiStrongGateStatus
    evaluated: bool = False
    score: float = 0.0
    cost_usd: float = 0.0
    reason: str = ""
    evidence_id: str = ""
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


class QiStrategyReviewPackage(BaseModel):
    """A single review-only strategy package.

    This is a package for humans / strong judges / lab rollout planners.  It is
    not a Watchtower pack and cannot promote a route by construction.
    """

    model_config = ConfigDict(extra="forbid")

    package_id: str
    draft_id: str
    candidate_id: str
    source_signal_id: str
    task_type_patterns: list[str] = Field(default_factory=list)
    risk: ReplayRisk = "low"
    status: QiReviewPackageStatus
    recommendation: str
    missing_evidence: list[str] = Field(default_factory=list)
    local_exploration: list[QiExplorationEvidence] = Field(default_factory=list)
    strong_review_gate: QiStrongReviewGate
    best_local_score: float = 0.0
    total_review_cost_usd: float = 0.0
    next_review_action: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    review_only: Literal[True] = True
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


def build_qi_strategy_review_packages(
    *,
    candidates: list[StrategyCandidate],
    drafts: list[StrategyPackDraft],
    evaluation_records: Sequence[ReplayEvaluationRecord | dict[str, Any]] | None = None,
    strong_review_records: Sequence[ReplayEvaluationRecord | dict[str, Any]] | None = None,
    lab_replay_records: Sequence[QiLabReplayRecord | dict[str, Any]] | None = None,
    tree_search_records: Sequence[QiReplayTreeSearchRecord | dict[str, Any]] | None = None,
) -> list[QiStrategyReviewPackage]:
    """Build review packages from Qi's cheap and strong-review evidence."""

    candidates_by_id = {candidate.candidate_id: candidate for candidate in candidates}
    evals_by_target = _records_by_target_id(evaluation_records or [])
    strong_by_target = _records_by_target_id(strong_review_records or [])
    lab_by_draft = _records_by_key(lab_replay_records or [], "draft_id")
    tree_by_target = _records_by_target_id(tree_search_records or [])

    packages: list[QiStrategyReviewPackage] = []
    for draft in drafts:
        candidate = candidates_by_id.get(draft.candidate_id)
        risk = _risk_for_draft(draft, candidate)
        local = _local_exploration_for_draft(
            draft,
            evaluation_records=evals_by_target.get(draft.draft_id, []),
            lab_replay_records=lab_by_draft.get(draft.draft_id, []),
            tree_search_records=tree_by_target.get(draft.draft_id, []),
        )
        strong_gate = _strong_gate_for_draft(
            draft,
            risk=risk,
            strong_records=strong_by_target.get(draft.draft_id, []),
        )
        best_local_score = max((item.score for item in local), default=0.0)
        missing = _missing_evidence(
            local=local,
            strong_gate=strong_gate,
            draft=draft,
        )
        status = _package_status(
            local=local,
            best_local_score=best_local_score,
            strong_gate=strong_gate,
            missing=missing,
        )
        packages.append(
            QiStrategyReviewPackage(
                package_id=_package_id(draft.draft_id),
                draft_id=draft.draft_id,
                candidate_id=draft.candidate_id,
                source_signal_id=draft.source_signal_id,
                task_type_patterns=list(draft.task_type_patterns),
                risk=risk,
                status=status,
                recommendation=_recommendation(status),
                missing_evidence=missing,
                local_exploration=local,
                strong_review_gate=strong_gate,
                best_local_score=round(best_local_score, 4),
                total_review_cost_usd=round(
                    sum(item.cost_usd for item in local) + strong_gate.cost_usd, 6
                ),
                next_review_action=_next_review_action(status),
            )
        )
    return packages


def _local_exploration_for_draft(
    draft: StrategyPackDraft,
    *,
    evaluation_records: list[dict[str, Any]],
    lab_replay_records: list[dict[str, Any]],
    tree_search_records: list[dict[str, Any]],
) -> list[QiExplorationEvidence]:
    out: list[QiExplorationEvidence] = []
    for record in evaluation_records:
        kind = str(record.get("evaluator_kind") or "")
        if kind == "strong_model":
            continue
        if kind not in {"heuristic", "local_model"}:
            continue
        out.append(
            QiExplorationEvidence(
                channel=cast(QiExplorationChannel, kind),
                evidence_id=str(record.get("evaluation_id") or ""),
                status=str(record.get("status") or "unknown"),
                score=_score(record),
                cost_usd=_float(record.get("cost_estimate_usd")),
                notes=_string_list(record.get("notes")),
            )
        )
    for record in lab_replay_records:
        out.append(
            QiExplorationEvidence(
                channel="lab_replay",
                evidence_id=str(record.get("experiment_id") or ""),
                status=str(record.get("status") or "unknown"),
                score=_score(record),
                cost_usd=_float(record.get("cost_usd")),
                notes=_string_list(record.get("notes")),
            )
        )
    for record in tree_search_records:
        out.append(
            QiExplorationEvidence(
                channel="tree_search",
                evidence_id=str(record.get("evaluation_id") or ""),
                status=str(record.get("status") or "unknown"),
                score=_score(record),
                cost_usd=_float(record.get("total_cost_usd")),
                notes=_string_list(record.get("notes")),
            )
        )
    return sorted(out, key=lambda item: (item.score, item.channel, item.evidence_id), reverse=True)


def _strong_gate_for_draft(
    draft: StrategyPackDraft,
    *,
    risk: ReplayRisk,
    strong_records: list[dict[str, Any]],
) -> QiStrongReviewGate:
    required = bool(draft.requires_strong_review or risk in {"high", "critical"})
    if not required:
        return QiStrongReviewGate(
            required=False, status="not_required", reason="risk_allows_local_review"
        )

    if not strong_records:
        return QiStrongReviewGate(
            required=True,
            status="missing",
            reason="strong_review_required_but_missing",
        )

    best = max(strong_records, key=_score)
    status = str(best.get("status") or "")
    if status == "evaluated":
        return QiStrongReviewGate(
            required=True,
            status="completed",
            evaluated=True,
            score=_score(best),
            cost_usd=_float(best.get("cost_estimate_usd")),
            reason="strong_review_completed",
            evidence_id=str(best.get("evaluation_id") or ""),
        )
    if status == "unavailable":
        gate_status: QiStrongGateStatus = "unavailable"
    else:
        gate_status = "failed"
    return QiStrongReviewGate(
        required=True,
        status=gate_status,
        score=_score(best),
        cost_usd=_float(best.get("cost_estimate_usd")),
        reason=f"strong_review_{status or 'failed'}",
        evidence_id=str(best.get("evaluation_id") or ""),
    )


def _package_status(
    *,
    local: list[QiExplorationEvidence],
    best_local_score: float,
    strong_gate: QiStrongReviewGate,
    missing: list[str],
) -> QiReviewPackageStatus:
    if not local:
        return "needs_local_exploration"
    if best_local_score < 0.35:
        return "reject"
    if strong_gate.status in {"missing", "failed", "unavailable"}:
        return "needs_strong_review"
    if missing:
        return "needs_local_exploration"
    return "ready_for_human_review"


def _missing_evidence(
    *,
    local: list[QiExplorationEvidence],
    strong_gate: QiStrongReviewGate,
    draft: StrategyPackDraft,
) -> list[str]:
    missing: list[str] = []
    channels = {item.channel for item in local if item.status == "evaluated"}
    if not channels:
        missing.append("cheap_or_local_exploration")
    if "tree_search" not in channels and any(
        word in draft.proposed_pack_id for word in ("code", "runtime", "cost")
    ):
        missing.append("tree_search_evidence")
    if strong_gate.status in {"missing", "failed", "unavailable"}:
        missing.append("strong_review_gate")
    return _dedupe(missing)


def _recommendation(status: QiReviewPackageStatus) -> str:
    return {
        "needs_local_exploration": "continue_cheap_local_exploration",
        "needs_strong_review": "queue_strong_review_before_human_rollout_review",
        "ready_for_human_review": "ready_for_human_review_only",
        "reject": "reject_or_rewrite_candidate",
    }[status]


def _next_review_action(status: QiReviewPackageStatus) -> str:
    return {
        "needs_local_exploration": "run local evaluator, lab replay, or tree search; do not promote",
        "needs_strong_review": "run strong reviewer or mark unavailable; do not promote",
        "ready_for_human_review": "human can inspect package and decide whether to create rollout plan",
        "reject": "rewrite candidate with clearer metric, guardrail, and replay evidence",
    }[status]


def _risk_for_draft(
    draft: StrategyPackDraft,
    candidate: StrategyCandidate | None,
) -> ReplayRisk:
    if candidate is not None:
        return candidate.risk
    if draft.requires_strong_review or draft.status == "needs_strong_review":
        return "high"
    return "low"


def _records_by_target_id(records: Sequence[Any]) -> dict[str, list[dict[str, Any]]]:
    return _records_by_key(records, "target_id")


def _records_by_key(records: Sequence[Any], key: str) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        payload = _payload(record)
        value = str(payload.get(key) or "")
        if value:
            out.setdefault(value, []).append(payload)
    return out


def _payload(record: Any) -> dict[str, Any]:
    if hasattr(record, "model_dump"):
        dumped = record.model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, dict) else {}
    if isinstance(record, dict):
        return dict(record)
    return {}


def _score(record: dict[str, Any]) -> float:
    return max(
        _float(record.get("score")),
        _float(record.get("best_score")),
        _float(record.get("quality_score")),
    )


def _float(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return round(max(0.0, min(1.0, parsed)), 4)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _package_id(draft_id: str) -> str:
    digest = hashlib.sha256(f"qi_strategy_review_package|{draft_id}".encode()).hexdigest()[:16]
    return f"qisrp_{digest}"


__all__ = [
    "QiExplorationEvidence",
    "QiStrategyReviewPackage",
    "QiStrongReviewGate",
    "build_qi_strategy_review_packages",
]
