"""Qi lab replay for review-only strategy drafts.

This module gives Qi a real sandbox hook: take a review-only StrategyPack draft
and replay it against historical task targets in KUN-Lab.  It is opt-in and
never promotes a strategy by itself.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kun.core.logging import get_logger
from kun.qi.idle_replay import StrategyPackDraft, TaskHistorySummary

log = get_logger("kun.qi.lab_replay")

QiLabReplayStatus = Literal[
    "evaluated",
    "skipped_disabled",
    "skipped_no_history",
    "skipped_no_match",
    "skipped_budget_exhausted",
    "error",
]

QI_LAB_REPLAY_ENABLED_ENV = "KUN_QI_LAB_REPLAY_ENABLED"
QI_LAB_REPLAY_MAX_ITEMS_ENV = "KUN_QI_LAB_REPLAY_MAX_ITEMS"
QI_LAB_REPLAY_MAX_COST_ENV = "KUN_QI_LAB_REPLAY_MAX_COST_USD"
QI_LAB_REPLAY_PATHS_ENV = "KUN_QI_LAB_REPLAY_PATHS"


class QiLabReplayBudget(BaseModel):
    """Small budget for optional Qi lab replay."""

    model_config = ConfigDict(extra="forbid")

    max_items: int = 2
    max_cost_usd: float = 0.5
    paths: int = 3


class QiLabReplayRecord(BaseModel):
    """Evidence from replaying one draft against one historical task."""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    history_id: str
    task_type: str = "general"
    status: QiLabReplayStatus
    score: float = 0.0
    cost_usd: float = 0.0
    experiment_id: str = ""
    replay_winning_strategy: str = ""
    original_winning_strategy: str | None = None
    matches_original: bool | None = None
    notes: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    promotion_allowed: Literal[False] = False
    production_action: Literal[False] = False


class QiLabReplayPoolResult(BaseModel):
    """Review-only replay batch result."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    records: list[QiLabReplayRecord] = Field(default_factory=list)
    evaluated: int = 0
    skipped: int = 0
    errors: int = 0
    budget_limit_usd: float = 0.0
    budget_used_usd: float = 0.0
    production_action: Literal[False] = False


class QiLabReplayRunner(Protocol):
    async def __call__(
        self,
        draft: StrategyPackDraft,
        history: TaskHistorySummary,
        budget: QiLabReplayBudget,
    ) -> QiLabReplayRecord: ...


ReplayRunnerFunc = Callable[
    [StrategyPackDraft, TaskHistorySummary, QiLabReplayBudget],
    Awaitable[QiLabReplayRecord],
]


async def run_qi_lab_replay_pool(
    drafts: Iterable[StrategyPackDraft],
    histories: Iterable[TaskHistorySummary | dict[str, Any]],
    *,
    enabled: bool = False,
    budget: QiLabReplayBudget | None = None,
    runner: QiLabReplayRunner | ReplayRunnerFunc | None = None,
) -> QiLabReplayPoolResult:
    """Run bounded lab replay for matching drafts and histories.

    The default is disabled.  When enabled without a runner, KUN uses the real
    KUN-Lab replay runner, which requires KUN_LAB_MODE=1 and configured LLM
    providers.
    """

    budget = budget or QiLabReplayBudget()
    normalized_histories = _normalize_histories(histories)
    normalized_drafts = list(drafts)
    if not enabled:
        return QiLabReplayPoolResult(
            enabled=False,
            skipped=len(normalized_drafts),
            budget_limit_usd=max(0.0, budget.max_cost_usd),
            records=[
                _skip_record(draft, "", "skipped_disabled", "qi_lab_replay_disabled")
                for draft in normalized_drafts[: max(0, budget.max_items)]
            ],
        )
    if not normalized_histories:
        return QiLabReplayPoolResult(
            enabled=True,
            skipped=len(normalized_drafts),
            budget_limit_usd=max(0.0, budget.max_cost_usd),
            records=[
                _skip_record(draft, "", "skipped_no_history", "no_completed_task_history")
                for draft in normalized_drafts[: max(0, budget.max_items)]
            ],
        )

    replay_runner = runner or RealQiLabReplayRunner()
    records: list[QiLabReplayRecord] = []
    spent = 0.0
    used = 0
    for draft in normalized_drafts:
        if used >= max(0, budget.max_items):
            records.append(
                _skip_record(draft, "", "skipped_budget_exhausted", "item_limit_exhausted")
            )
            continue
        history = _matching_history(draft, normalized_histories)
        if history is None:
            records.append(_skip_record(draft, "", "skipped_no_match", "no_matching_history"))
            continue
        if spent >= max(0.0, budget.max_cost_usd):
            records.append(
                _skip_record(
                    draft,
                    history.history_id,
                    "skipped_budget_exhausted",
                    "budget_exhausted",
                )
            )
            continue
        used += 1
        try:
            record = await replay_runner(draft, history, budget)
        except Exception as exc:
            record = _error_record(draft, history, exc)
        spent += max(0.0, record.cost_usd)
        if spent > max(0.0, budget.max_cost_usd) and record.status == "evaluated":
            record.notes.append("budget_exceeded_after_replay")
        records.append(record)

    return QiLabReplayPoolResult(
        enabled=True,
        records=records,
        evaluated=sum(1 for record in records if record.status == "evaluated"),
        skipped=sum(1 for record in records if record.status.startswith("skipped")),
        errors=sum(1 for record in records if record.status == "error"),
        budget_limit_usd=max(0.0, budget.max_cost_usd),
        budget_used_usd=round(spent, 6),
    )


class RealQiLabReplayRunner:
    """Run actual KUN-Lab historical replay with the draft injected as guidance."""

    async def __call__(
        self,
        draft: StrategyPackDraft,
        history: TaskHistorySummary,
        budget: QiLabReplayBudget,
    ) -> QiLabReplayRecord:
        from kun.lab import (
            BenchmarkRunOptions,
            EnsembleExecutor,
            HistoricalTaskReplayTarget,
            load_historical_task_for_replay,
            make_default_adapter,
            replay_historical_task,
        )

        tenant_id = _history_tenant_id(history)
        target = await load_historical_task_for_replay(history.history_id, tenant_id=tenant_id)
        guidance = _draft_guidance(draft)
        replay_target = HistoricalTaskReplayTarget(
            task_id=target.task_id,
            tenant_id=target.tenant_id,
            task_type=target.task_type,
            prompt=(
                f"{target.prompt}\n\n"
                "KUN-Lab strategy draft to test, do not treat as production truth:\n"
                f"{guidance}"
            ),
            original_answer=target.original_answer,
            original_winning_strategy=target.original_winning_strategy,
            result_metadata=target.result_metadata,
        )

        async def loader(
            task_id: str, *, tenant_id: str = "u-sylvan"
        ) -> HistoricalTaskReplayTarget:
            _ = task_id, tenant_id
            return replay_target

        executor = EnsembleExecutor(
            make_default_adapter(
                max_tokens=int(os.getenv("KUN_QI_LAB_REPLAY_MAX_TOKENS", "1200") or "1200"),
                task_type=f"qi.lab_replay.{history.task_type}",
            ),
            require_lab_mode=True,
        )
        report = await replay_historical_task(
            history.history_id,
            tenant_id=tenant_id,
            executor=executor,
            options=BenchmarkRunOptions(
                paths=max(2, min(10, budget.paths)),
                cost_budget_total_usd=max(0.0, budget.max_cost_usd),
            ),
            task_loader=loader,
        )
        score = 0.55
        notes = ["lab_replay_executed", "review_required_before_any_adoption"]
        if report.matches_original is True:
            score += 0.1
            notes.append("matches_original_winner")
        elif report.matches_original is False:
            notes.append("different_winner_found")
        if report.replay_winning_strategy:
            score += 0.05
        return QiLabReplayRecord(
            draft_id=draft.draft_id,
            history_id=history.history_id,
            task_type=history.task_type,
            status="evaluated",
            score=round(min(1.0, score), 4),
            cost_usd=round(max(0.0, report.total_cost_usd), 6),
            experiment_id=report.experiment_id,
            replay_winning_strategy=report.replay_winning_strategy,
            original_winning_strategy=report.original_winning_strategy,
            matches_original=report.matches_original,
            notes=notes,
            evidence={
                "strategy_pack_draft": draft.model_dump(mode="json"),
                "winning_output_preview": report.winning_output_preview,
                "production_action": False,
            },
        )


def configured_qi_lab_replay_budget_from_env() -> QiLabReplayBudget:
    return QiLabReplayBudget(
        max_items=_env_int(QI_LAB_REPLAY_MAX_ITEMS_ENV, 2),
        max_cost_usd=_env_float(QI_LAB_REPLAY_MAX_COST_ENV, 0.5),
        paths=_env_int(QI_LAB_REPLAY_PATHS_ENV, 3),
    )


def qi_lab_replay_enabled_from_env() -> bool:
    return _env_bool(QI_LAB_REPLAY_ENABLED_ENV, default=False)


def _normalize_histories(
    histories: Iterable[TaskHistorySummary | dict[str, Any]],
) -> list[TaskHistorySummary]:
    out: list[TaskHistorySummary] = []
    for item in histories:
        try:
            out.append(
                item
                if isinstance(item, TaskHistorySummary)
                else TaskHistorySummary.model_validate(item)
            )
        except Exception as exc:
            log.debug("qi.lab_replay.invalid_history", error=str(exc))
            continue
    return out


def _matching_history(
    draft: StrategyPackDraft,
    histories: list[TaskHistorySummary],
) -> TaskHistorySummary | None:
    for history in histories:
        if not history.history_id:
            continue
        if _draft_matches_task_type(draft, history.task_type):
            return history
    return None


def _draft_matches_task_type(draft: StrategyPackDraft, task_type: str) -> bool:
    normalized = (task_type or "general").lower()
    for pattern in draft.task_type_patterns or ["*"]:
        candidate = pattern.lower()
        if candidate == "*" or candidate == normalized:
            return True
        if candidate.endswith("*") and normalized.startswith(candidate[:-1]):
            return True
    return False


def _skip_record(
    draft: StrategyPackDraft,
    history_id: str,
    status: QiLabReplayStatus,
    reason: str,
) -> QiLabReplayRecord:
    return QiLabReplayRecord(
        draft_id=draft.draft_id,
        history_id=history_id,
        task_type=",".join(draft.task_type_patterns) or "general",
        status=status,
        notes=[reason, "promotion_blocked"],
        evidence={"review_only": True, "production_action": False},
    )


def _error_record(
    draft: StrategyPackDraft,
    history: TaskHistorySummary,
    error: Exception,
) -> QiLabReplayRecord:
    return QiLabReplayRecord(
        draft_id=draft.draft_id,
        history_id=history.history_id,
        task_type=history.task_type,
        status="error",
        notes=["lab_replay_error", "promotion_blocked"],
        evidence={
            "error": str(error),
            "review_only": True,
            "production_action": False,
        },
    )


def _draft_guidance(draft: StrategyPackDraft) -> str:
    return "\n".join(
        [
            f"display_name: {draft.display_name}",
            f"proposed_pack_id: {draft.proposed_pack_id}",
            f"task_type_patterns: {', '.join(draft.task_type_patterns)}",
            f"methodology_refs: {', '.join(draft.methodology_refs)}",
            f"context_tags: {', '.join(draft.context_tags)}",
            f"skill_hints: {', '.join(draft.skill_hints)}",
            f"risk_watch: {', '.join(draft.risk_watch)}",
            f"default_execution_mode: {draft.default_execution_mode}",
            "production_action: false",
        ]
    )


def _history_tenant_id(history: TaskHistorySummary) -> str:
    evidence_tenant = (
        history.evidence.get("tenant_id") if isinstance(history.evidence, dict) else None
    )
    tenant_id = str(history.tenant_id or evidence_tenant or "").strip()
    if not tenant_id:
        raise ValueError("history tenant_id is required for real Qi lab replay")
    return tenant_id


def _env_bool(name: str, *, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return float(value)
    except ValueError:
        return default


__all__ = [
    "QI_LAB_REPLAY_ENABLED_ENV",
    "QiLabReplayBudget",
    "QiLabReplayPoolResult",
    "QiLabReplayRecord",
    "RealQiLabReplayRunner",
    "configured_qi_lab_replay_budget_from_env",
    "qi_lab_replay_enabled_from_env",
    "run_qi_lab_replay_pool",
]
