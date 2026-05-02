"""Review gate for Qi-generated StrategyPack drafts.

Qi can search cheaply and produce many strategy drafts.  This module is the
conservative evidence gate between "interesting candidate" and "ready for a
human / stronger reviewer to inspect".  It never promotes a strategy into
production.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.context.assets import LayeredAsset
from kun.context.storage import AssetStore, get_store
from kun.core.logging import get_logger

log = get_logger("kun.qi.strategy_pack_review")

StrategyPackReviewStatus = Literal["blocked", "needs_evidence", "ready_for_human_review"]

HIGH_RISKS = {"high", "critical"}
STRONG_REVIEW_MIN_SCORE = 0.6
BASE_EVALUATION_MIN_SCORE = 0.55
LAB_REPLAY_MIN_SCORE = 0.55
BLOCKING_SCORE = 0.35
HIGH_RISK_TASK_MARKERS = (
    "world",
    "payment",
    "finance",
    "email",
    "browser",
    "enterprise",
    "deploy",
    "delete",
    "send",
)


class StrategyPackReviewDecision(BaseModel):
    """Review-only decision for a Qi StrategyPack draft asset."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    status: StrategyPackReviewStatus
    confidence: float = 0.0
    score: float = 0.0
    risk: str = "low"
    reasons: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


class StrategyPackEvidenceSummary(BaseModel):
    """Compact evidence summary for downstream human/system review."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    status: StrategyPackReviewStatus
    score: float = 0.0
    confidence: float = 0.0
    risk: str = "low"
    why_worth_human_review: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    evidence_sources: list[dict[str, Any]] = Field(default_factory=list)
    review_reasons: list[str] = Field(default_factory=list)
    review_only: Literal[True] = True
    production_action: Literal[False] = False
    promotion_allowed: Literal[False] = False


class StrategyPackReviewReport(BaseModel):
    """Batch review result for stored Qi StrategyPack draft assets."""

    model_config = ConfigDict(extra="forbid")

    scanned: int = 0
    updated: int = 0
    ready_for_human_review: int = 0
    needs_evidence: int = 0
    blocked: int = 0
    dry_run: bool = True
    decisions: list[StrategyPackReviewDecision] = Field(default_factory=list)
    production_action: Literal[False] = False


def review_strategy_pack_draft_asset(asset: LayeredAsset) -> StrategyPackReviewDecision:
    """Classify one stored Qi strategy draft by its evidence chain."""

    metadata = asset.l1_metadata
    draft_payload = _as_dict(metadata.get("strategy_pack_draft"))
    draft_id = str(metadata.get("draft_id") or draft_payload.get("draft_id") or asset.asset_id)
    if metadata.get("source") != "qi.idle_replay.strategy_pack_draft":
        return StrategyPackReviewDecision(
            draft_id=draft_id,
            status="blocked",
            confidence=1.0,
            score=0.0,
            risk="unknown",
            reasons=["asset_is_not_qi_strategy_pack_draft"],
        )

    requires_strong_review = bool(
        metadata.get("requires_strong_review")
        or draft_payload.get("requires_strong_review")
        or str(draft_payload.get("status", "")) == "needs_strong_review"
    )
    risk = _draft_risk(metadata, draft_payload)
    task_patterns = _task_patterns(metadata, draft_payload)
    requires_lab_replay = risk in HIGH_RISKS or _looks_external_or_irreversible(task_patterns)
    evaluation_records = _record_list(metadata.get("evaluation_records"))
    lab_replay_records = _record_list(metadata.get("lab_replay_records"))
    tree_search_records = _record_list(metadata.get("tree_search_records"))

    reasons: list[str] = ["review_only_no_production_promotion"]
    missing: list[str] = []
    scores: list[float] = []

    base_records = [
        record
        for record in evaluation_records
        if str(record.get("evaluator_kind", "")) in {"heuristic", "local_model"}
        and str(record.get("status", "")) == "evaluated"
    ]
    base_records.extend(
        record
        for record in tree_search_records
        if str(record.get("evaluator_kind", "")) == "tree_search"
        and str(record.get("status", "")) == "evaluated"
    )
    if not base_records:
        missing.append("base_replay_evaluation")
    else:
        best_base_score = max(_float(record.get("score")) for record in base_records)
        scores.append(best_base_score)
        if best_base_score < BASE_EVALUATION_MIN_SCORE:
            reasons.append("base_evaluation_score_low")

    strong_records = [
        record
        for record in evaluation_records
        if str(record.get("evaluator_kind", "")) == "strong_model"
    ]
    strong_evaluated = [
        record for record in strong_records if str(record.get("status", "")) == "evaluated"
    ]
    if requires_strong_review and not strong_evaluated:
        missing.append("strong_model_review")
    elif strong_evaluated:
        best_strong_score = max(_float(record.get("score")) for record in strong_evaluated)
        scores.append(best_strong_score)
        if best_strong_score < STRONG_REVIEW_MIN_SCORE:
            reasons.append("strong_model_score_low")

    lab_evaluated = [
        record for record in lab_replay_records if str(record.get("status", "")) == "evaluated"
    ]
    if requires_lab_replay and not lab_evaluated:
        missing.append("lab_replay_evidence")
    elif lab_evaluated:
        best_lab_score = max(_float(record.get("score")) for record in lab_evaluated)
        scores.append(best_lab_score)
        if best_lab_score < LAB_REPLAY_MIN_SCORE:
            reasons.append("lab_replay_score_low")

    blocking_reason = _blocking_reason(
        base_records=base_records,
        strong_records=strong_evaluated,
        lab_records=lab_evaluated,
    )
    score = round(sum(scores) / len(scores), 4) if scores else 0.0
    if blocking_reason:
        status: StrategyPackReviewStatus = "blocked"
        reasons.append(blocking_reason)
    elif missing:
        status = "needs_evidence"
    elif score >= _required_score(risk, requires_strong_review=requires_strong_review):
        status = "ready_for_human_review"
        reasons.append("evidence_chain_sufficient_for_human_review")
    else:
        status = "needs_evidence"
        missing.append("higher_quality_evidence")

    confidence = _confidence(status=status, missing=missing, scores=scores)
    return StrategyPackReviewDecision(
        draft_id=draft_id,
        status=status,
        confidence=confidence,
        score=score,
        risk=risk,
        reasons=_dedupe(reasons),
        missing_evidence=_dedupe(missing),
    )


def summarize_strategy_pack_evidence(
    asset: LayeredAsset,
    decision: StrategyPackReviewDecision | None = None,
) -> StrategyPackEvidenceSummary:
    """Summarize review-only Qi evidence into a stable downstream payload."""

    decision = decision or review_strategy_pack_draft_asset(asset)
    metadata = asset.l1_metadata
    draft_payload = _as_dict(metadata.get("strategy_pack_draft"))
    task_patterns = _task_patterns(metadata, draft_payload)
    evaluation_records = _record_list(metadata.get("evaluation_records"))
    lab_replay_records = _record_list(metadata.get("lab_replay_records"))
    tree_search_records = _record_list(metadata.get("tree_search_records"))

    evidence_sources = [
        *_summarize_evaluation_records(evaluation_records),
        *_summarize_evaluation_records(tree_search_records),
        *_summarize_lab_replay_records(lab_replay_records),
    ]
    why = _why_worth_human_review(decision, evidence_sources)
    risks = _summary_risks(decision, task_patterns, evidence_sources)

    return StrategyPackEvidenceSummary(
        draft_id=decision.draft_id,
        status=decision.status,
        score=decision.score,
        confidence=decision.confidence,
        risk=decision.risk,
        why_worth_human_review=why,
        missing_evidence=decision.missing_evidence,
        risks=risks,
        evidence_sources=evidence_sources,
        review_reasons=decision.reasons,
    )


async def review_strategy_pack_draft_assets(
    *,
    tenant_id: str,
    store: AssetStore | None = None,
    dry_run: bool = True,
    limit: int = 1000,
) -> StrategyPackReviewReport:
    """Review stored Qi StrategyPack draft assets and optionally update metadata."""

    store = store or get_store()
    assets = await store.list(tenant_id=tenant_id, asset_kind="methodology", limit=limit)
    draft_assets = [
        asset
        for asset in assets
        if asset.l1_metadata.get("source") == "qi.idle_replay.strategy_pack_draft"
    ]
    decisions: list[StrategyPackReviewDecision] = []
    updated = 0
    for asset in draft_assets:
        decision = review_strategy_pack_draft_asset(asset)
        decisions.append(decision)
        if not dry_run:
            _apply_review_decision(asset, decision)
            await store.put(asset)
            updated += 1

    return StrategyPackReviewReport(
        scanned=len(draft_assets),
        updated=updated,
        ready_for_human_review=sum(
            1 for decision in decisions if decision.status == "ready_for_human_review"
        ),
        needs_evidence=sum(1 for decision in decisions if decision.status == "needs_evidence"),
        blocked=sum(1 for decision in decisions if decision.status == "blocked"),
        dry_run=dry_run,
        decisions=decisions,
    )


def _apply_review_decision(asset: LayeredAsset, decision: StrategyPackReviewDecision) -> None:
    evidence_summary = summarize_strategy_pack_evidence(asset, decision)
    asset.l1_metadata["qi_review_status"] = decision.status
    asset.l1_metadata["qi_review_confidence"] = decision.confidence
    asset.l1_metadata["qi_review_score"] = decision.score
    asset.l1_metadata["qi_review_risk"] = decision.risk
    asset.l1_metadata["qi_review_reasons"] = decision.reasons
    asset.l1_metadata["qi_missing_evidence"] = decision.missing_evidence
    asset.l1_metadata["qi_evidence_summary"] = evidence_summary.model_dump(mode="json")
    asset.l1_metadata["promotion_allowed"] = False
    asset.l1_metadata["production_action"] = False
    asset.tags = _review_tags(asset.tags, decision)


def _review_tags(tags: Iterable[str], decision: StrategyPackReviewDecision) -> list[str]:
    kept = [
        tag
        for tag in tags
        if not tag.startswith("qi_review:")
        and tag
        not in {
            "qi_ready_for_human_review",
            "qi_needs_evidence",
            "qi_blocked",
        }
    ]
    status_tag = {
        "ready_for_human_review": "qi_ready_for_human_review",
        "needs_evidence": "qi_needs_evidence",
        "blocked": "qi_blocked",
    }[decision.status]
    return sorted({*kept, f"qi_review:{decision.status}", status_tag})


def _draft_risk(metadata: dict[str, Any], draft_payload: dict[str, Any]) -> str:
    candidate = _as_dict(draft_payload.get("evidence")).get("source_candidate")
    candidate_payload = _as_dict(candidate)
    for value in (
        metadata.get("risk"),
        metadata.get("qi_review_risk"),
        candidate_payload.get("risk"),
    ):
        text = str(value or "").lower()
        if text in {"low", "medium", "high", "critical"}:
            return text
    if bool(metadata.get("requires_strong_review") or draft_payload.get("requires_strong_review")):
        return "high"
    return "low"


def _task_patterns(metadata: dict[str, Any], draft_payload: dict[str, Any]) -> list[str]:
    values: list[Any] = []
    values.extend(_as_list(draft_payload.get("task_type_patterns")))
    values.append(metadata.get("proposed_pack_id"))
    values.extend(_as_list(draft_payload.get("risk_watch")))
    values.extend(_as_list(draft_payload.get("skill_hints")))
    return [str(value).lower() for value in values if str(value or "").strip()]


def _looks_external_or_irreversible(values: Iterable[str]) -> bool:
    joined = " ".join(values).lower()
    return any(marker in joined for marker in HIGH_RISK_TASK_MARKERS)


def _blocking_reason(
    *,
    base_records: list[dict[str, Any]],
    strong_records: list[dict[str, Any]],
    lab_records: list[dict[str, Any]],
) -> str:
    for label, records in (
        ("base_evaluation_blocking_low_score", base_records),
        ("strong_model_blocking_low_score", strong_records),
        ("lab_replay_blocking_low_score", lab_records),
    ):
        if records and max(_float(record.get("score")) for record in records) < BLOCKING_SCORE:
            return label
    all_records = [*base_records, *strong_records, *lab_records]
    for record in all_records:
        notes = " ".join(str(note).lower() for note in _as_list(record.get("notes")))
        if "reject" in notes or "unsafe" in notes:
            return "review_record_rejected_or_unsafe"
    return ""


def _summarize_evaluation_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for record in records:
        kind = str(record.get("evaluator_kind") or "unknown")
        summaries.append(
            {
                "source": _evaluation_source_label(kind),
                "status": str(record.get("status") or "unknown"),
                "score": _float(record.get("score")),
                "record_id": str(record.get("evaluation_id") or ""),
                "review_only": bool(record.get("production_action") is False),
                "notes": [str(note) for note in _as_list(record.get("notes")) if str(note)],
            }
        )
    return summaries


def _summarize_lab_replay_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for record in records:
        summaries.append(
            {
                "source": "lab_replay_evidence",
                "status": str(record.get("status") or "unknown"),
                "score": _float(record.get("score")),
                "record_id": str(record.get("history_id") or ""),
                "review_only": bool(record.get("production_action") is False),
                "notes": [str(note) for note in _as_list(record.get("notes")) if str(note)],
            }
        )
    return summaries


def _evaluation_source_label(evaluator_kind: str) -> str:
    if evaluator_kind == "strong_model":
        return "strong_model_review"
    if evaluator_kind == "local_model":
        return "local_model_replay_evaluation"
    if evaluator_kind == "heuristic":
        return "idle_replay_evaluation"
    if evaluator_kind == "tree_search":
        return "qi_tree_search_evidence"
    return f"{evaluator_kind}_evaluation"


def _why_worth_human_review(
    decision: StrategyPackReviewDecision,
    evidence_sources: list[dict[str, Any]],
) -> list[str]:
    why: list[str] = []
    evaluated = [
        source
        for source in evidence_sources
        if source.get("status") == "evaluated" and _float(source.get("score")) > 0.0
    ]
    if evaluated:
        best = max(evaluated, key=lambda source: _float(source.get("score")))
        why.append(f"{best['source']}_score:{_float(best.get('score')):.2f}")
    if decision.status == "ready_for_human_review":
        why.append("review_gate_ready_for_human_review")
    elif decision.status == "needs_evidence":
        why.append("candidate_has_signal_but_evidence_gaps_remain")
    if decision.score >= _required_score(
        decision.risk,
        requires_strong_review="strong_model_review" in decision.missing_evidence,
    ):
        why.append("aggregate_score_clears_current_risk_threshold")
    return _dedupe(why)


def _summary_risks(
    decision: StrategyPackReviewDecision,
    task_patterns: list[str],
    evidence_sources: list[dict[str, Any]],
) -> list[str]:
    risks = ["review_only_not_production_evidence", f"risk_level:{decision.risk}"]
    if decision.missing_evidence:
        risks.append("missing_required_evidence")
    if decision.risk in HIGH_RISKS or _looks_external_or_irreversible(task_patterns):
        risks.append("high_or_external_impact_requires_extra_review")
    if decision.status == "blocked":
        risks.append("blocking_review_signal_present")
    if any(_float(source.get("score")) < BLOCKING_SCORE for source in evidence_sources):
        risks.append("very_low_score_in_evidence_chain")
    return _dedupe(risks)


def _required_score(risk: str, *, requires_strong_review: bool) -> float:
    if risk == "critical":
        return 0.68
    if risk == "high" or requires_strong_review:
        return 0.62
    return 0.55


def _confidence(
    *,
    status: StrategyPackReviewStatus,
    missing: list[str],
    scores: list[float],
) -> float:
    if status == "blocked":
        return 0.9
    if missing:
        return 0.45
    if not scores:
        return 0.3
    return round(min(0.95, 0.55 + 0.4 * (sum(scores) / len(scores))), 4)


def _record_list(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _as_list(value):
        payload = _as_dict(item)
        if payload:
            out.append(payload)
    return out


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        payload = value.model_dump(mode="json")
        return payload if isinstance(payload, dict) else {}
    return {}


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


__all__ = [
    "StrategyPackEvidenceSummary",
    "StrategyPackReviewDecision",
    "StrategyPackReviewReport",
    "review_strategy_pack_draft_asset",
    "review_strategy_pack_draft_assets",
    "summarize_strategy_pack_evidence",
]
