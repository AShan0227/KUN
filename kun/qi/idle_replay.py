"""Idle replay strategy candidates for Qi.

This module is deliberately local and conservative. It turns completed-task
summaries or real Qi problem signals into candidate strategy drafts, but it does
not install those drafts into production paths.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kun.datamodel.task import ExecutionMode
from kun.qi.problem_queue import QiProblemSignal

ReplayRisk = Literal["low", "medium", "high", "critical"]
StrategyDraftStatus = Literal["draft", "needs_strong_review"]
ReplayEvaluationStatus = Literal[
    "evaluated",
    "skipped_budget_exhausted",
    "unavailable",
    "error",
]
ReplayEvaluatorKind = Literal["heuristic", "local_model"]

HEURISTIC_IDLE_REPLAY_ENGINE = "heuristic_local"
HEURISTIC_REPLAY_EVALUATOR = "heuristic_replay_pool_v1"
LOCAL_MODEL_REPLAY_EVALUATOR = "local_model_replay_pool"
LOCAL_MODEL_REPLAY_EVALUATOR_CMD_ENV = "KUN_QI_LOCAL_REPLAY_EVALUATOR_CMD"
LOCAL_MODEL_REPLAY_TIMEOUT_ENV = "KUN_QI_LOCAL_REPLAY_TIMEOUT_SEC"


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
                "strategy_pack_draft": self.to_strategy_pack_draft().model_dump(mode="json"),
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

    def to_strategy_pack_draft(self) -> StrategyPackDraft:
        """Return a review-only StrategyPack-like draft for Watchtower.

        Qi is allowed to discover better sparse MoE paths, but this method is
        intentionally not a promotion path.  It only emits a draft that a human
        or stronger judge can inspect before any Watchtower pack is installed.
        """

        task_patterns = _task_type_patterns(self.task_type)
        return StrategyPackDraft(
            draft_id=_strategy_pack_draft_id(self.candidate_id),
            candidate_id=self.candidate_id,
            source_signal_id=self.source_signal_id,
            proposed_pack_id=_proposed_pack_id(self.task_type, self.candidate_id),
            display_name=_draft_display_name(self.task_type),
            task_type_patterns=task_patterns,
            keyword_triggers=_keyword_triggers(self),
            methodology_refs=_methodology_refs(self),
            context_tags=_context_tags(self),
            skill_hints=_skill_hints(self),
            metric_dimensions=_metric_dimensions(self),
            risk_watch=_risk_watch(self),
            reward_weights=_reward_weights(self),
            default_execution_mode=_default_execution_mode(self),
            status="needs_strong_review" if self.requires_strong_review else "draft",
            requires_strong_review=self.requires_strong_review,
            promotion_conditions=_promotion_conditions(self),
            evidence={
                "source_candidate": self.model_dump(mode="json"),
                "task_type_patterns": task_patterns,
                "production_action": False,
            },
            created_at=self.created_at,
            engine=self.engine,
        )


class StrategyPackDraft(BaseModel):
    """Review-only StrategyPack proposal generated by Qi idle replay.

    This mirrors the fields Watchtower needs, but keeps explicit review gates so
    an idle experiment cannot silently alter production routing.
    """

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    candidate_id: str
    source_signal_id: str
    proposed_pack_id: str
    display_name: str
    task_type_patterns: list[str] = Field(default_factory=list)
    keyword_triggers: list[str] = Field(default_factory=list)
    methodology_refs: list[str] = Field(default_factory=list)
    context_tags: list[str] = Field(default_factory=list)
    skill_hints: list[str] = Field(default_factory=list)
    metric_dimensions: list[str] = Field(default_factory=list)
    risk_watch: list[str] = Field(default_factory=list)
    reward_weights: dict[str, float] = Field(default_factory=dict)
    default_execution_mode: ExecutionMode = "SMART"
    status: StrategyDraftStatus = "draft"
    requires_human_review: bool = True
    requires_strong_review: bool = False
    promotion_conditions: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    engine: str = HEURISTIC_IDLE_REPLAY_ENGINE
    production_action: Literal[False] = False


class ReplayEvaluationBudget(BaseModel):
    """Small resource guard for idle replay evaluation work."""

    model_config = ConfigDict(extra="forbid")

    max_items: int = 8
    max_cost_usd: float = 0.05
    max_concurrency: int = 2


class ReplayEvaluationRecord(BaseModel):
    """Review-only score for a replay candidate or StrategyPack draft."""

    model_config = ConfigDict(extra="forbid")

    evaluation_id: str
    target_id: str
    target_kind: Literal["strategy_candidate", "strategy_pack_draft"]
    evaluator: str
    evaluator_kind: ReplayEvaluatorKind
    status: ReplayEvaluationStatus
    score: float = 0.0
    cost_estimate_usd: float = 0.0
    risk: ReplayRisk = "low"
    requires_strong_review: bool = False
    promotion_allowed: Literal[False] = False
    production_action: Literal[False] = False
    notes: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ReplayEvaluationPoolResult(BaseModel):
    """Ordered review-only evaluation batch."""

    model_config = ConfigDict(extra="forbid")

    records: list[ReplayEvaluationRecord] = Field(default_factory=list)
    evaluated: int = 0
    skipped_budget_exhausted: int = 0
    unavailable: int = 0
    errors: int = 0
    budget_limit_usd: float = 0.0
    budget_used_usd: float = 0.0
    max_concurrency: int = 1
    promotion_allowed: Literal[False] = False
    production_action: Literal[False] = False


class LocalReplayModelEvaluator(Protocol):
    """Optional local model scorer.

    Implementations must be explicitly injected.  The default pool never claims
    a model score when no local evaluator is available.
    """

    async def evaluate(
        self,
        item: StrategyCandidate | StrategyPackDraft,
    ) -> ReplayEvaluationRecord: ...


class CommandLocalReplayModelEvaluator:
    """Run an operator-provided local evaluator command.

    This is Qi's cheap exploration hook.  It is deliberately opt-in and
    review-only: the command may score a strategy candidate, but the returned
    record still cannot promote anything into production by itself.
    """

    def __init__(
        self,
        command: list[str],
        *,
        timeout_sec: float = 30.0,
        evaluator_name: str | None = None,
    ) -> None:
        if not command:
            raise ValueError("local replay evaluator command cannot be empty")
        self.command = command
        self.timeout_sec = max(1.0, timeout_sec)
        self.evaluator_name = evaluator_name or f"{LOCAL_MODEL_REPLAY_EVALUATOR}:{command[0]}"

    async def evaluate(
        self,
        item: StrategyCandidate | StrategyPackDraft,
    ) -> ReplayEvaluationRecord:
        normalized = _normalize_evaluation_target(item)
        payload = {
            "target": normalized,
            "item": item.model_dump(mode="json"),
            "contract": {
                "stdout": "json",
                "fields": {
                    "score": "0..1 float",
                    "notes": "optional list[str]",
                    "risk": "optional low|medium|high|critical",
                    "requires_strong_review": "optional bool",
                    "evidence": "optional object",
                },
                "promotion_allowed": False,
            },
        }
        proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(json.dumps(payload, ensure_ascii=False).encode("utf-8")),
                timeout=self.timeout_sec,
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise RuntimeError("local replay evaluator timed out") from exc

        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(
                "local replay evaluator failed" + (f": {stderr_text[:300]}" if stderr_text else "")
            )

        raw_text = stdout.decode("utf-8", errors="replace").strip()
        try:
            raw = json.loads(raw_text or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("local replay evaluator returned non-JSON stdout") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("local replay evaluator stdout must be a JSON object")

        model_risk = _normalize_risk(raw.get("risk") or normalized["risk"])
        risk = _max_risk(_normalize_risk(normalized["risk"]), model_risk)
        requires_strong_review = bool(
            normalized["requires_strong_review"] or raw.get("requires_strong_review")
        )
        notes = _string_list(raw.get("notes"))
        notes.extend(["local_model_command", "review_required_before_any_adoption"])
        raw_evidence = raw.get("evidence")
        evidence: dict[str, Any] = raw_evidence if isinstance(raw_evidence, dict) else {}
        return ReplayEvaluationRecord(
            evaluation_id=_evaluation_id(
                normalized["target_id"],
                self.evaluator_name,
                "evaluated",
            ),
            target_id=normalized["target_id"],
            target_kind=normalized["target_kind"],
            evaluator=self.evaluator_name,
            evaluator_kind="local_model",
            status="evaluated",
            score=_clamped_float(raw.get("score"), default=0.0),
            cost_estimate_usd=_evaluation_cost_estimate(item, evaluator_kind="local_model"),
            risk=risk,
            requires_strong_review=requires_strong_review,
            notes=_dedupe(notes),
            evidence={
                "review_only": True,
                "promotion_allowed": False,
                "target_summary": normalized["summary"],
                "target_task_type": normalized["task_type"],
                "local_model_command": self.command[0],
                "local_model_stderr_preview": stderr_text[:300],
                **evidence,
            },
        )


def configured_local_replay_model_evaluator_from_env() -> LocalReplayModelEvaluator | None:
    """Return Qi's optional local replay evaluator if configured.

    The evaluator command is intentionally not guessed.  Operators must provide
    it explicitly so KUN does not pretend a local model exists.
    """

    command_text = (os.getenv(LOCAL_MODEL_REPLAY_EVALUATOR_CMD_ENV) or "").strip()
    if not command_text:
        return None
    timeout = _env_float(LOCAL_MODEL_REPLAY_TIMEOUT_ENV, default=30.0)
    return CommandLocalReplayModelEvaluator(
        shlex.split(command_text),
        timeout_sec=timeout,
    )


ReplaySuggestion = StrategyCandidate


class IdleReplayEvaluationPool:
    """Budgeted offline scorer for Qi replay proposals.

    The pool is deliberately review-only: every record blocks promotion by
    construction, even when a score is high.
    """

    def __init__(
        self,
        *,
        budget: ReplayEvaluationBudget | None = None,
        local_model_evaluator: LocalReplayModelEvaluator | None = None,
    ) -> None:
        self._budget = budget or ReplayEvaluationBudget()
        self._local_model_evaluator = local_model_evaluator

    async def evaluate(
        self,
        items: Iterable[StrategyCandidate | StrategyPackDraft],
        *,
        evaluator_kind: ReplayEvaluatorKind = "heuristic",
    ) -> ReplayEvaluationPoolResult:
        all_items = list(items)
        records: list[ReplayEvaluationRecord] = []
        spent = 0.0
        pending: list[StrategyCandidate | StrategyPackDraft] = []

        for idx, item in enumerate(all_items):
            if idx >= max(0, self._budget.max_items):
                records.append(
                    _budget_exhausted_record(
                        item,
                        evaluator_kind=evaluator_kind,
                        budget_limit_usd=self._budget.max_cost_usd,
                        budget_used_usd=spent,
                        requested_cost_usd=0.0,
                        reason="item_limit_exhausted",
                    )
                )
                continue
            estimate = _evaluation_cost_estimate(item, evaluator_kind=evaluator_kind)
            if spent + estimate > max(0.0, self._budget.max_cost_usd):
                records.append(
                    _budget_exhausted_record(
                        item,
                        evaluator_kind=evaluator_kind,
                        budget_limit_usd=self._budget.max_cost_usd,
                        budget_used_usd=spent,
                        requested_cost_usd=estimate,
                        reason="budget_exhausted",
                    )
                )
                continue
            spent += estimate
            pending.append(item)

        max_concurrency = max(1, self._budget.max_concurrency)
        semaphore = asyncio.Semaphore(max_concurrency)

        async def run_one(
            item: StrategyCandidate | StrategyPackDraft,
        ) -> ReplayEvaluationRecord:
            async with semaphore:
                try:
                    if evaluator_kind == "local_model":
                        if self._local_model_evaluator is None:
                            return _local_model_unavailable_record(item)
                        record = await self._local_model_evaluator.evaluate(item)
                        return _review_only_record(record)
                    return _heuristic_evaluation_record(item)
                except Exception as exc:
                    return _error_record(item, evaluator_kind=evaluator_kind, error=exc)

        if pending:
            records.extend(await asyncio.gather(*(run_one(item) for item in pending)))

        records.sort(key=lambda item: _record_sort_key(item), reverse=True)
        return ReplayEvaluationPoolResult(
            records=records,
            evaluated=sum(1 for record in records if record.status == "evaluated"),
            skipped_budget_exhausted=sum(
                1 for record in records if record.status == "skipped_budget_exhausted"
            ),
            unavailable=sum(1 for record in records if record.status == "unavailable"),
            errors=sum(1 for record in records if record.status == "error"),
            budget_limit_usd=max(0.0, self._budget.max_cost_usd),
            budget_used_usd=round(spent, 6),
            max_concurrency=max_concurrency,
        )


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


async def evaluate_idle_replay_pool(
    items: Iterable[StrategyCandidate | StrategyPackDraft],
    *,
    budget: ReplayEvaluationBudget | None = None,
    evaluator_kind: ReplayEvaluatorKind = "heuristic",
    local_model_evaluator: LocalReplayModelEvaluator | None = None,
) -> ReplayEvaluationPoolResult:
    """Score replay proposals without enabling promotion."""

    return await IdleReplayEvaluationPool(
        budget=budget,
        local_model_evaluator=local_model_evaluator,
    ).evaluate(items, evaluator_kind=evaluator_kind)


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


def _heuristic_evaluation_record(
    item: StrategyCandidate | StrategyPackDraft,
) -> ReplayEvaluationRecord:
    normalized = _normalize_evaluation_target(item)
    score, notes = _heuristic_score(normalized)
    return ReplayEvaluationRecord(
        evaluation_id=_evaluation_id(
            normalized["target_id"],
            HEURISTIC_REPLAY_EVALUATOR,
            "evaluated",
        ),
        target_id=normalized["target_id"],
        target_kind=normalized["target_kind"],
        evaluator=HEURISTIC_REPLAY_EVALUATOR,
        evaluator_kind="heuristic",
        status="evaluated",
        score=score,
        cost_estimate_usd=_evaluation_cost_estimate(item, evaluator_kind="heuristic"),
        risk=normalized["risk"],
        requires_strong_review=normalized["requires_strong_review"],
        notes=notes,
        evidence={
            "review_only": True,
            "promotion_allowed": False,
            "target_summary": normalized["summary"],
            "target_task_type": normalized["task_type"],
            "quality_signals": normalized["quality_signals"],
        },
    )


def _heuristic_score(normalized: dict[str, Any]) -> tuple[float, list[str]]:
    notes = ["heuristic_local_only", "review_required_before_any_adoption"]
    score = 0.45
    quality_signals = {str(item) for item in normalized["quality_signals"]}
    if "has_acceptance_checks" in quality_signals:
        score += 0.12
        notes.append("has_acceptance_checks")
    if "has_guardrails" in quality_signals:
        score += 0.10
        notes.append("has_guardrails")
    if "has_replay_methodology" in quality_signals:
        score += 0.08
        notes.append("has_replay_methodology")
    if "has_reuse_value" in quality_signals:
        score += 0.06
        notes.append("has_reuse_value")
    if "has_cost_focus" in quality_signals:
        score += 0.04
        notes.append("has_cost_focus")

    risk_penalty = {"low": 0.0, "medium": 0.08, "high": 0.18, "critical": 0.28}
    risk = _normalize_risk(normalized["risk"])
    score -= risk_penalty[risk]
    if normalized["requires_strong_review"]:
        score -= 0.05
        notes.append("strong_review_required")

    return round(max(0.0, min(1.0, score)), 4), notes


def _normalize_evaluation_target(
    item: StrategyCandidate | StrategyPackDraft,
) -> dict[str, Any]:
    if isinstance(item, StrategyCandidate):
        text = " ".join(
            [
                item.task_type,
                item.summary,
                item.proposed_change,
                item.expected_benefit,
            ]
        ).lower()
        return {
            "target_id": item.candidate_id,
            "target_kind": "strategy_candidate",
            "task_type": item.task_type,
            "summary": item.summary,
            "risk": item.risk,
            "requires_strong_review": item.requires_strong_review,
            "quality_signals": _quality_signals_for_text(text),
        }

    source_candidate = item.evidence.get("source_candidate")
    source_risk = "low"
    if isinstance(source_candidate, dict):
        source_risk = str(source_candidate.get("risk") or "low")
    text = " ".join(
        [
            item.display_name,
            item.proposed_pack_id,
            " ".join(item.methodology_refs),
            " ".join(item.metric_dimensions),
            " ".join(item.risk_watch),
            " ".join(item.promotion_conditions),
        ]
    ).lower()
    return {
        "target_id": item.draft_id,
        "target_kind": "strategy_pack_draft",
        "task_type": ",".join(item.task_type_patterns),
        "summary": item.display_name,
        "risk": _max_risk(_normalize_risk(source_risk), _risk_from_draft_watch(item)),
        "requires_strong_review": item.requires_strong_review,
        "quality_signals": _quality_signals_for_text(text),
    }


def _quality_signals_for_text(text: str) -> list[str]:
    signals: list[str] = []
    if any(word in text for word in ("acceptance", "verification", "metric", "quality")):
        signals.append("has_acceptance_checks")
    if any(word in text for word in ("guardrail", "rollback", "compensation", "idempotency")):
        signals.append("has_guardrails")
    if any(word in text for word in ("replay", "lab", "shadow", "benchmark")):
        signals.append("has_replay_methodology")
    if any(word in text for word in ("reusable", "reuse", "pattern", "protocol")):
        signals.append("has_reuse_value")
    if any(word in text for word in ("cost", "budget", "bounded")):
        signals.append("has_cost_focus")
    return _dedupe(signals)


def _risk_from_draft_watch(draft: StrategyPackDraft) -> ReplayRisk:
    text = " ".join([draft.status, " ".join(draft.risk_watch)]).lower()
    if "critical" in text:
        return "critical"
    if "high_risk" in text or draft.status == "needs_strong_review":
        return "high"
    if draft.risk_watch:
        return "medium"
    return "low"


def _budget_exhausted_record(
    item: StrategyCandidate | StrategyPackDraft,
    *,
    evaluator_kind: ReplayEvaluatorKind,
    budget_limit_usd: float,
    budget_used_usd: float,
    requested_cost_usd: float,
    reason: str,
) -> ReplayEvaluationRecord:
    normalized = _normalize_evaluation_target(item)
    return ReplayEvaluationRecord(
        evaluation_id=_evaluation_id(
            normalized["target_id"],
            _evaluator_name(evaluator_kind),
            "skipped_budget_exhausted",
        ),
        target_id=normalized["target_id"],
        target_kind=normalized["target_kind"],
        evaluator=_evaluator_name(evaluator_kind),
        evaluator_kind=evaluator_kind,
        status="skipped_budget_exhausted",
        score=0.0,
        cost_estimate_usd=requested_cost_usd,
        risk=normalized["risk"],
        requires_strong_review=normalized["requires_strong_review"],
        notes=[
            reason,
            "not_evaluated",
            "promotion_blocked",
        ],
        evidence={
            "budget_limit_usd": max(0.0, budget_limit_usd),
            "budget_used_usd": round(max(0.0, budget_used_usd), 6),
            "requested_cost_usd": requested_cost_usd,
        },
    )


def _local_model_unavailable_record(
    item: StrategyCandidate | StrategyPackDraft,
) -> ReplayEvaluationRecord:
    normalized = _normalize_evaluation_target(item)
    return ReplayEvaluationRecord(
        evaluation_id=_evaluation_id(
            normalized["target_id"],
            LOCAL_MODEL_REPLAY_EVALUATOR,
            "unavailable",
        ),
        target_id=normalized["target_id"],
        target_kind=normalized["target_kind"],
        evaluator=LOCAL_MODEL_REPLAY_EVALUATOR,
        evaluator_kind="local_model",
        status="unavailable",
        score=0.0,
        cost_estimate_usd=_evaluation_cost_estimate(item, evaluator_kind="local_model"),
        risk=normalized["risk"],
        requires_strong_review=normalized["requires_strong_review"],
        notes=[
            "local_model_evaluator_unavailable",
            "no_model_score_claimed",
            "promotion_blocked",
        ],
    )


def _error_record(
    item: StrategyCandidate | StrategyPackDraft,
    *,
    evaluator_kind: ReplayEvaluatorKind,
    error: Exception,
) -> ReplayEvaluationRecord:
    normalized = _normalize_evaluation_target(item)
    return ReplayEvaluationRecord(
        evaluation_id=_evaluation_id(
            normalized["target_id"],
            _evaluator_name(evaluator_kind),
            "error",
        ),
        target_id=normalized["target_id"],
        target_kind=normalized["target_kind"],
        evaluator=_evaluator_name(evaluator_kind),
        evaluator_kind=evaluator_kind,
        status="error",
        score=0.0,
        cost_estimate_usd=0.0,
        risk=normalized["risk"],
        requires_strong_review=normalized["requires_strong_review"],
        notes=["evaluation_error", "promotion_blocked"],
        evidence={"error": str(error)},
    )


def _review_only_record(record: ReplayEvaluationRecord) -> ReplayEvaluationRecord:
    data = record.model_dump()
    data["promotion_allowed"] = False
    data["production_action"] = False
    return ReplayEvaluationRecord.model_validate(data)


def _evaluation_cost_estimate(
    item: StrategyCandidate | StrategyPackDraft,
    *,
    evaluator_kind: ReplayEvaluatorKind,
) -> float:
    if evaluator_kind == "local_model":
        return 0.01
    text_size = len(str(item.model_dump(mode="json"))) if hasattr(item, "model_dump") else 0
    return round(0.001 + min(text_size, 4000) / 4_000_000, 6)


def _evaluator_name(evaluator_kind: ReplayEvaluatorKind) -> str:
    if evaluator_kind == "local_model":
        return LOCAL_MODEL_REPLAY_EVALUATOR
    return HEURISTIC_REPLAY_EVALUATOR


def _evaluation_id(target_id: str, evaluator: str, status: str) -> str:
    key = "|".join([target_id, evaluator, status])
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"qire_{digest}"


def _record_sort_key(record: ReplayEvaluationRecord) -> tuple[int, float, int, str]:
    status_rank = {
        "evaluated": 3,
        "unavailable": 2,
        "skipped_budget_exhausted": 1,
        "error": 0,
    }
    review_rank = 1 if record.requires_strong_review else 0
    return (status_rank[record.status], record.score, review_rank, record.target_id)


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


def _strategy_pack_draft_id(candidate_id: str) -> str:
    digest = hashlib.sha256(f"strategy_pack_draft|{candidate_id}".encode()).hexdigest()[:16]
    return f"spd_{digest}"


def _proposed_pack_id(task_type: str, candidate_id: str) -> str:
    prefix = _task_family(task_type)
    suffix = candidate_id.removeprefix("qir_")[:8]
    return f"qi_{prefix}_{suffix}"


def _draft_display_name(task_type: str) -> str:
    family = _task_family(task_type).replace("_", " ")
    return f"Qi draft strategy for {family}"


def _task_type_patterns(task_type: str) -> list[str]:
    normalized = (task_type or "general").strip().lower() or "general"
    if normalized in {"general", "*"}:
        return ["*"]
    family = _task_family(normalized)
    patterns = [normalized]
    if not normalized.endswith("*"):
        patterns.append(f"{family}*")
    return _dedupe(patterns)


def _keyword_triggers(candidate: StrategyCandidate) -> list[str]:
    text = (
        f"{candidate.task_type} {candidate.summary} {candidate.proposed_change} "
        f"{candidate.expected_benefit}"
    ).lower()
    keywords: list[str] = []
    for token in _task_family_tokens(candidate.task_type):
        if token not in {"general", "task"}:
            keywords.append(token)
    keyword_map = {
        "world": ["external", "handler", "rollback", "approval"],
        "email": ["email", "recipient", "idempotency"],
        "cost": ["cost", "budget", "bounded"],
        "runtime": ["runtime", "stalled", "resume"],
        "context": ["context", "slimming", "recall"],
        "memory": ["memory", "recall", "forget"],
        "coding": ["code", "test", "regression"],
        "marketing": ["marketing", "conversion", "campaign"],
        "delivery": ["delivery", "verification", "artifact"],
    }
    for needle, mapped in keyword_map.items():
        if needle in text:
            keywords.extend(mapped)
    return _dedupe(keywords)[:12]


def _methodology_refs(candidate: StrategyCandidate) -> list[str]:
    text = f"{candidate.task_type} {candidate.proposed_change}".lower()
    refs = ["lab_replay_before_promotion"]
    if any(word in text for word in ("cost", "budget", "bounded")):
        refs.append("budget_guardrail_replay")
    if any(word in text for word in ("world", "handler", "email", "external")):
        refs.append("side_effect_preflight")
    if any(word in text for word in ("context", "memory", "slimming")):
        refs.append("context_loss_check")
    if any(word in text for word in ("delivery", "artifact", "verification")):
        refs.append("evidence_based_delivery")
    if any(word in text for word in ("failure", "regression", "runtime")):
        refs.append("minimal_repro_replay")
    return _dedupe(refs)


def _context_tags(candidate: StrategyCandidate) -> list[str]:
    tags = _task_family_tokens(candidate.task_type)
    if candidate.risk in {"high", "critical"}:
        tags.append("high_risk_review")
    if candidate.requires_strong_review:
        tags.append("strong_review")
    return _dedupe(tags)[:10]


def _skill_hints(candidate: StrategyCandidate) -> list[str]:
    text = f"{candidate.task_type} {candidate.proposed_change}".lower()
    hints: list[str] = []
    if any(word in text for word in ("code", "coding", "regression", "test")):
        hints.extend(["code_reader", "test_runner", "code_reviewer"])
    if any(word in text for word in ("world", "handler", "email", "external")):
        hints.extend(["approval_drafter", "rollback_planner"])
    if any(word in text for word in ("cost", "budget", "bounded")):
        hints.extend(["cost_profiler", "route_optimizer"])
    if any(word in text for word in ("context", "memory", "slimming")):
        hints.extend(["context_auditor", "memory_curator"])
    if any(word in text for word in ("marketing", "ad", "conversion")):
        hints.extend(["content_planner", "conversion_reviewer"])
    if any(word in text for word in ("delivery", "artifact", "verification")):
        hints.extend(["evidence_checker", "pre_delivery_reviewer"])
    return _dedupe(hints)[:8]


def _metric_dimensions(candidate: StrategyCandidate) -> list[str]:
    dimensions = [
        "success_rate",
        "quality",
        "cost",
        "latency",
        "risk",
        "reversibility",
        "reuse_value",
        "surprise",
    ]
    text = f"{candidate.task_type} {candidate.proposed_change}".lower()
    if "cost" in text or "budget" in text:
        dimensions.append("budget_accuracy")
    if any(word in text for word in ("world", "handler", "email", "external")):
        dimensions.extend(["side_effect_safety", "compensation_coverage"])
    if any(word in text for word in ("context", "memory")):
        dimensions.extend(["recall_precision", "context_loss"])
    if any(word in text for word in ("delivery", "artifact", "verification")):
        dimensions.append("evidence_match")
    return _dedupe(dimensions)


def _risk_watch(candidate: StrategyCandidate) -> list[str]:
    text = f"{candidate.task_type} {candidate.proposed_change}".lower()
    watch = ["unreviewed_strategy_promotion", "replay_overfit"]
    if candidate.requires_strong_review:
        watch.append("strong_review_required")
    if any(word in text for word in ("world", "handler", "email", "external")):
        watch.extend(["unauthorized_side_effect", "missing_compensation", "idempotency_gap"])
    if any(word in text for word in ("cost", "budget", "bounded")):
        watch.extend(["quality_regression_from_cost_cut", "budget_breach"])
    if any(word in text for word in ("context", "memory", "slimming")):
        watch.extend(["lost_required_context", "false_memory_recall"])
    if any(word in text for word in ("delivery", "artifact", "verification")):
        watch.append("claim_without_evidence")
    if candidate.risk == "critical":
        watch.append("critical_risk_draft")
    return _dedupe(watch)


def _reward_weights(candidate: StrategyCandidate) -> dict[str, float]:
    weights = {
        "quality": 0.35,
        "success_rate": 0.20,
        "risk": 0.15,
        "cost": 0.10,
        "latency": 0.05,
        "reuse_value": 0.10,
        "surprise": 0.05,
    }
    text = f"{candidate.task_type} {candidate.proposed_change}".lower()
    if candidate.risk in {"high", "critical"}:
        weights["risk"] += 0.10
        weights["quality"] += 0.05
        weights["latency"] = max(0.02, weights["latency"] - 0.03)
    if "cost" in text or "budget" in text:
        weights["cost"] += 0.10
        weights["quality"] += 0.03
    if any(word in text for word in ("world", "handler", "email", "external")):
        weights["risk"] += 0.10
        weights["reversibility"] = 0.12
    return _normalize_weights(weights)


def _default_execution_mode(candidate: StrategyCandidate) -> ExecutionMode:
    if candidate.risk == "critical":
        return "MAX"
    if candidate.risk == "high" or candidate.requires_strong_review:
        return "MAX"
    if "cost" in f"{candidate.task_type} {candidate.proposed_change}".lower():
        return "SMART"
    return "SMART"


def _promotion_conditions(candidate: StrategyCandidate) -> list[str]:
    conditions = [
        "human_review_approved",
        "lab_replay_passed",
        "guardrails_not_breached",
        "no_automatic_production_install",
    ]
    if candidate.requires_strong_review:
        conditions.append("strong_model_review_passed")
    if candidate.risk in {"high", "critical"}:
        conditions.append("rollback_plan_reviewed")
    return conditions


def _task_family(task_type: str) -> str:
    tokens = _task_family_tokens(task_type)
    return tokens[0] if tokens else "general"


def _task_family_tokens(task_type: str) -> list[str]:
    cleaned = (task_type or "general").strip().lower()
    for char in ".:-/":
        cleaned = cleaned.replace(char, "_")
    return [token for token in cleaned.split("_") if token]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _clamped_float(value: Any, *, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = default
    return round(max(0.0, min(1.0, parsed)), 4)


def _env_float(name: str, *, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    total = sum(value for value in weights.values() if value > 0)
    if total <= 0:
        return weights
    return {key: round(max(0.0, value) / total, 4) for key, value in weights.items()}


__all__ = [
    "HEURISTIC_IDLE_REPLAY_ENGINE",
    "HEURISTIC_REPLAY_EVALUATOR",
    "LOCAL_MODEL_REPLAY_EVALUATOR",
    "LOCAL_MODEL_REPLAY_EVALUATOR_CMD_ENV",
    "CommandLocalReplayModelEvaluator",
    "IdleReplayEvaluationPool",
    "IdleReplayGenerator",
    "LocalReplayModelEvaluator",
    "ReplayEvaluationBudget",
    "ReplayEvaluationPoolResult",
    "ReplayEvaluationRecord",
    "ReplaySuggestion",
    "StrategyCandidate",
    "StrategyPackDraft",
    "TaskHistorySummary",
    "configured_local_replay_model_evaluator_from_env",
    "evaluate_idle_replay_pool",
    "generate_idle_replay_candidates",
]
