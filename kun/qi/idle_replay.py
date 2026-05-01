"""Idle replay strategy candidates for Qi.

This module is deliberately local and conservative. It turns completed-task
summaries or real Qi problem signals into candidate strategy drafts, but it does
not install those drafts into production paths.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.qi.problem_queue import QiProblemSignal

ReplayRisk = Literal["low", "medium", "high", "critical"]

HEURISTIC_IDLE_REPLAY_ENGINE = "heuristic_local"


class TaskHistorySummary(BaseModel):
    """Small, portable summary of a completed task used for idle replay."""

    model_config = ConfigDict(extra="forbid")

    history_id: str = ""
    task_type: str = "general"
    summary: str
    outcome: str = "completed"
    risk: ReplayRisk = "low"
    verification_status: str = ""
    cost_usd: float | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    completed_at: datetime | None = None


class StrategyCandidate(BaseModel):
    """A replay suggestion that still needs review before any production use."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    source_signal_id: str
    task_type: str
    summary: str
    proposed_change: str
    expected_benefit: str
    risk: ReplayRisk
    requires_strong_review: bool
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    engine: str = HEURISTIC_IDLE_REPLAY_ENGINE

    def to_problem_signal(
        self,
        *,
        tenant_id: str,
        severity: str | None = None,
    ) -> QiProblemSignal:
        """Convert the candidate into a non-production Qi signal for review."""

        return QiProblemSignal.build(
            tenant_id=tenant_id,
            category="unknown",
            severity=severity or _severity_from_risk(self.risk),
            source="qi.idle_replay.candidate",
            task_type=self.task_type,
            summary=self.summary,
            evidence={
                "candidate_id": self.candidate_id,
                "source_signal_id": self.source_signal_id,
                "engine": self.engine,
                "proposed_change": self.proposed_change,
                "expected_benefit": self.expected_benefit,
                "risk": self.risk,
                "requires_strong_review": self.requires_strong_review,
                "original_evidence": self.evidence,
                "production_action": False,
            },
        )

    def to_lab_recipe_draft(self) -> dict[str, Any]:
        """Return a lab-only recipe draft; callers must opt into any execution."""

        return {
            "draft_id": self.candidate_id,
            "task_type": self.task_type,
            "strategy": self.proposed_change,
            "expected_benefit": self.expected_benefit,
            "risk": self.risk,
            "requires_strong_review": self.requires_strong_review,
            "source_signal_id": self.source_signal_id,
            "engine": self.engine,
            "evidence": self.evidence,
            "production_action": False,
        }


ReplaySuggestion = StrategyCandidate


class IdleReplayGenerator:
    """Deterministic local heuristic generator for Qi idle replay."""

    engine = HEURISTIC_IDLE_REPLAY_ENGINE

    def generate(
        self,
        items: Iterable[QiProblemSignal | TaskHistorySummary | dict[str, Any]],
    ) -> list[StrategyCandidate]:
        candidates: list[StrategyCandidate] = []
        for item in items:
            if isinstance(item, QiProblemSignal):
                candidates.append(self.generate_from_signal(item))
                continue
            if isinstance(item, TaskHistorySummary):
                candidates.append(self.generate_from_history(item))
                continue
            candidates.append(self.generate_from_history(TaskHistorySummary.model_validate(item)))
        return candidates

    def generate_from_signal(self, signal: QiProblemSignal) -> StrategyCandidate:
        risk = _risk_for_signal(signal)
        source_signal_id = signal.signal_id
        proposed_change, expected_benefit = _strategy_for_signal(signal)
        evidence = {
            "source_kind": "qi_problem_signal",
            "source": signal.source,
            "category": signal.category,
            "severity": signal.severity,
            "engine": self.engine,
            "heuristic_notes": _notes_for_signal(signal),
            "original_evidence": signal.evidence,
        }
        summary = f"Idle replay candidate for {signal.category}: {signal.summary}"

        return _candidate(
            source_signal_id=source_signal_id,
            task_type=signal.task_type,
            summary=summary,
            proposed_change=proposed_change,
            expected_benefit=expected_benefit,
            risk=risk,
            requires_strong_review=_requires_strong_review(risk, signal.severity, signal.evidence),
            evidence=evidence,
        )

    def generate_from_history(self, history: TaskHistorySummary) -> StrategyCandidate:
        risk = _risk_for_history(history)
        source_signal_id = history.history_id or _history_source_id(history)
        proposed_change, expected_benefit = _strategy_for_history(history)
        evidence = {
            "source_kind": "task_history_summary",
            "outcome": history.outcome,
            "verification_status": history.verification_status,
            "cost_usd": history.cost_usd,
            "engine": self.engine,
            "completed_at": history.completed_at.isoformat() if history.completed_at else None,
            "original_evidence": history.evidence,
        }
        summary = f"Idle replay candidate from completed task: {history.summary}"

        return _candidate(
            source_signal_id=source_signal_id,
            task_type=history.task_type,
            summary=summary,
            proposed_change=proposed_change,
            expected_benefit=expected_benefit,
            risk=risk,
            requires_strong_review=risk in {"high", "critical"},
            evidence=evidence,
        )


def generate_idle_replay_candidates(
    items: Iterable[QiProblemSignal | TaskHistorySummary | dict[str, Any]],
) -> list[StrategyCandidate]:
    """Convenience wrapper around the default local heuristic generator."""

    return IdleReplayGenerator().generate(items)


def _candidate(
    *,
    source_signal_id: str,
    task_type: str,
    summary: str,
    proposed_change: str,
    expected_benefit: str,
    risk: ReplayRisk,
    requires_strong_review: bool,
    evidence: dict[str, Any],
) -> StrategyCandidate:
    candidate_id = _candidate_id(
        source_signal_id=source_signal_id,
        task_type=task_type,
        proposed_change=proposed_change,
    )
    return StrategyCandidate(
        candidate_id=candidate_id,
        source_signal_id=source_signal_id,
        task_type=task_type or "general",
        summary=summary,
        proposed_change=proposed_change,
        expected_benefit=expected_benefit,
        risk=risk,
        requires_strong_review=requires_strong_review,
        evidence=evidence,
    )


def _strategy_for_signal(signal: QiProblemSignal) -> tuple[str, str]:
    if signal.category == "world_gateway":
        return (
            "Draft a lab-only replay recipe that exercises the affected handler "
            "with idempotency, compensation, and rollback checks before promotion.",
            "Reduces repeat handler failures and makes side-effect safety auditable.",
        )
    if signal.category == "runtime":
        return (
            "Add a shadow replay case for the failing runtime path and capture "
            "the smallest regression check that reproduces the observed symptom.",
            "Turns a transient runtime issue into a reusable verification signal.",
        )
    if signal.category == "cost":
        return (
            "Evaluate a cheaper routing or bounded-step strategy in lab replay, "
            "with explicit quality and latency guardrails.",
            "May reduce recurring spend without silently lowering output quality.",
        )
    if signal.category in {"context", "memory"}:
        return (
            "Create a context slimming replay that removes low-value material and "
            "checks whether the task answer still preserves required facts.",
            "Improves context efficiency while keeping recall loss visible.",
        )
    if signal.category == "delivery":
        return (
            "Draft a pre-delivery verification recipe that compares claimed work "
            "against concrete file, test, or artifact evidence.",
            "Reduces mismatch between delivery status and actual completed work.",
        )
    if signal.category == "risk":
        return (
            "Prepare a review-gated mitigation recipe with rollback conditions "
            "and no automatic production adoption.",
            "Keeps risky changes inspectable before they influence live behavior.",
        )
    return (
        "Draft a narrow lab replay around the observed signal and require a "
        "measurable acceptance check before any adoption.",
        "Creates a grounded experiment from the problem signal instead of a generic idea.",
    )


def _strategy_for_history(history: TaskHistorySummary) -> tuple[str, str]:
    outcome = history.outcome.lower()
    verification = history.verification_status.lower()
    if "fail" in outcome or "fail" in verification or "error" in outcome:
        return (
            "Create a lab replay from the completed task trace that reproduces "
            "the failure, then test one minimal protocol change against it.",
            "Prevents the same failure mode from being treated as a one-off.",
        )
    if history.cost_usd is not None and history.cost_usd >= 1.0:
        return (
            "Replay the task with a bounded-cost route and compare quality, "
            "latency, and spend against the original run.",
            "Finds cost savings only when the replay keeps quality acceptable.",
        )
    return (
        "Extract a reusable protocol sketch from the completed task and validate "
        "it in lab replay before recommending it elsewhere.",
        "Turns a successful local pattern into a reviewed candidate strategy.",
    )


def _notes_for_signal(signal: QiProblemSignal) -> list[str]:
    notes = [f"category:{signal.category}", f"severity:{signal.severity}"]
    if signal.task_type and signal.task_type != "general":
        notes.append(f"task_type:{signal.task_type}")
    if signal.evidence:
        notes.append("has_evidence")
    return notes


def _risk_for_signal(signal: QiProblemSignal) -> ReplayRisk:
    evidence_risk = _normalize_risk(
        signal.evidence.get("risk") or signal.evidence.get("risk_level")
    )
    severity_risk = _risk_from_severity(signal.severity)
    category_floor: ReplayRisk = "low"
    if signal.category in {"risk", "world_gateway"}:
        category_floor = "medium"
    if signal.category == "cost" and bool(signal.evidence.get("budget_breach")):
        category_floor = "high"
    return _max_risk(evidence_risk, severity_risk, category_floor)


def _risk_for_history(history: TaskHistorySummary) -> ReplayRisk:
    risk = history.risk
    text = f"{history.outcome} {history.verification_status}".lower()
    if "critical" in text:
        risk = _max_risk(risk, "critical")
    elif "fail" in text or "error" in text:
        risk = _max_risk(risk, "high")
    if history.cost_usd is not None:
        if history.cost_usd >= 5.0:
            risk = _max_risk(risk, "high")
        elif history.cost_usd >= 1.0:
            risk = _max_risk(risk, "medium")
    return risk


def _requires_strong_review(
    risk: ReplayRisk,
    severity: str,
    evidence: dict[str, Any],
) -> bool:
    if bool(evidence.get("requires_strong_review")):
        return True
    return risk in {"high", "critical"} or _severity_rank(severity) >= 3


def _risk_from_severity(severity: str) -> ReplayRisk:
    rank = _severity_rank(severity)
    if rank >= 4:
        return "critical"
    if rank == 3:
        return "high"
    if rank == 2:
        return "medium"
    return "low"


def _severity_rank(severity: str) -> int:
    return {"critical": 4, "error": 3, "warning": 2, "warn": 2, "info": 1}.get(
        severity.lower(),
        0,
    )


def _severity_from_risk(risk: ReplayRisk) -> str:
    return {
        "critical": "critical",
        "high": "error",
        "medium": "warning",
        "low": "info",
    }[risk]


def _normalize_risk(value: Any) -> ReplayRisk:
    if str(value).lower() in {"low", "medium", "high", "critical"}:
        return str(value).lower()  # type: ignore[return-value]
    return "low"


def _max_risk(*risks: ReplayRisk) -> ReplayRisk:
    order = {"low": 0, "medium": 1, "high": 2, "critical": 3}
    return max(risks, key=lambda item: order[item])


def _candidate_id(*, source_signal_id: str, task_type: str, proposed_change: str) -> str:
    key = "|".join([HEURISTIC_IDLE_REPLAY_ENGINE, source_signal_id, task_type, proposed_change])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"qir_{digest}"


def _history_source_id(history: TaskHistorySummary) -> str:
    key = "|".join([history.task_type, history.summary, history.outcome])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"history_{digest}"


__all__ = [
    "HEURISTIC_IDLE_REPLAY_ENGINE",
    "IdleReplayGenerator",
    "ReplaySuggestion",
    "StrategyCandidate",
    "TaskHistorySummary",
    "generate_idle_replay_candidates",
]
