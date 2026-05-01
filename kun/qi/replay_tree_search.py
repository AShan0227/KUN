"""Review-only tree search for Qi replay candidates.

This connects the AI Scientist beam-search primitive to Qi's idle replay world:
Qi can explore alternative strategy knobs for a draft, but the result is only
review evidence.  It never promotes a StrategyPack or changes Watchtower live
routing by itself.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kun.qi.ai_scientist import AIScientistTreeSearch, ScientistTreeNode
from kun.qi.idle_replay import StrategyCandidate, StrategyPackDraft

QiReplayTreeSearchStatus = Literal[
    "evaluated",
    "skipped_disabled",
    "skipped_budget_exhausted",
    "error",
]


class QiReplayTreeSearchBudget(BaseModel):
    """Small budget for idle-time strategy tree search."""

    model_config = ConfigDict(extra="forbid")

    max_items: int = 2
    max_cost_usd: float = 0.05
    beam_width: int = 2
    max_depth: int = 2


class QiReplayTreeSearchRecord(BaseModel):
    """Review-only tree-search evidence for one Qi candidate/draft."""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    target_kind: Literal["strategy_candidate", "strategy_pack_draft"]
    evaluation_id: str
    evaluator_kind: Literal["tree_search"] = "tree_search"
    status: QiReplayTreeSearchStatus
    score: float = 0.0
    best_score: float = 0.0
    total_cost_usd: float = 0.0
    nodes_evaluated: int = 0
    stopped_reason: str = ""
    best_strategy: dict[str, Any] = Field(default_factory=dict)
    best_path: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    promotion_allowed: Literal[False] = False
    production_action: Literal[False] = False


class QiReplayTreeSearchPoolResult(BaseModel):
    """Bounded batch result for review-only tree search."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    records: list[QiReplayTreeSearchRecord] = Field(default_factory=list)
    evaluated: int = 0
    skipped: int = 0
    errors: int = 0
    budget_limit_usd: float = 0.0
    budget_used_usd: float = 0.0
    promotion_allowed: Literal[False] = False
    production_action: Literal[False] = False


class QiReplayTreeRunner(Protocol):
    async def __call__(
        self,
        item: StrategyCandidate | StrategyPackDraft,
        strategy: dict[str, Any],
    ) -> tuple[float, float]: ...


ReplayTreeRunnerFunc = Callable[
    [StrategyCandidate | StrategyPackDraft, dict[str, Any]],
    Awaitable[tuple[float, float]],
]


async def run_qi_replay_tree_search_pool(
    items: Iterable[StrategyCandidate | StrategyPackDraft],
    *,
    enabled: bool = False,
    budget: QiReplayTreeSearchBudget | None = None,
    runner: QiReplayTreeRunner | ReplayTreeRunnerFunc | None = None,
) -> QiReplayTreeSearchPoolResult:
    """Run bounded review-only tree search for Qi candidates/drafts."""

    budget = budget or QiReplayTreeSearchBudget()
    normalized_items = list(items)
    if not enabled:
        return QiReplayTreeSearchPoolResult(
            enabled=False,
            skipped=len(normalized_items),
            budget_limit_usd=max(0.0, budget.max_cost_usd),
            records=[
                _skip_record(item, "skipped_disabled", "qi_tree_search_disabled")
                for item in normalized_items[: max(0, budget.max_items)]
            ],
        )

    records: list[QiReplayTreeSearchRecord] = []
    spent = 0.0
    used = 0
    for item in normalized_items:
        if used >= max(0, budget.max_items):
            records.append(_skip_record(item, "skipped_budget_exhausted", "item_limit_exhausted"))
            continue
        if spent >= max(0.0, budget.max_cost_usd):
            records.append(_skip_record(item, "skipped_budget_exhausted", "budget_exhausted"))
            continue
        remaining = max(0.0, budget.max_cost_usd - spent)
        used += 1
        try:
            record = await _run_one_tree_search(
                item,
                budget=QiReplayTreeSearchBudget(
                    max_items=1,
                    max_cost_usd=remaining,
                    beam_width=budget.beam_width,
                    max_depth=budget.max_depth,
                ),
                runner=runner,
            )
        except Exception as exc:
            record = _error_record(item, exc)
        spent += max(0.0, record.total_cost_usd)
        records.append(record)

    return QiReplayTreeSearchPoolResult(
        enabled=True,
        records=records,
        evaluated=sum(1 for record in records if record.status == "evaluated"),
        skipped=sum(1 for record in records if record.status.startswith("skipped")),
        errors=sum(1 for record in records if record.status == "error"),
        budget_limit_usd=max(0.0, budget.max_cost_usd),
        budget_used_usd=round(spent, 6),
    )


async def _run_one_tree_search(
    item: StrategyCandidate | StrategyPackDraft,
    *,
    budget: QiReplayTreeSearchBudget,
    runner: QiReplayTreeRunner | ReplayTreeRunnerFunc | None,
) -> QiReplayTreeSearchRecord:
    normalized = _normalize_item(item)
    active_runner = runner or _heuristic_tree_runner

    async def tree_runner(prompt: str, strategy: dict[str, Any]) -> tuple[float, float]:
        _ = prompt
        return await active_runner(item, strategy)

    search = AIScientistTreeSearch(
        tree_runner,
        beam_width=max(1, budget.beam_width),
        max_depth=max(0, budget.max_depth),
        total_budget_usd=max(0.0, budget.max_cost_usd),
        candidate_generator=_candidate_generator,
    )
    result = await search.search(
        normalized["prompt"],
        root_strategy=_root_strategy(normalized),
    )
    return QiReplayTreeSearchRecord(
        target_id=normalized["target_id"],
        target_kind=normalized["target_kind"],
        evaluation_id=_evaluation_id(normalized["target_id"], "evaluated"),
        status="evaluated",
        score=round(float(result.best_score), 4),
        best_score=round(float(result.best_score), 4),
        total_cost_usd=round(max(0.0, result.total_cost_usd), 6),
        nodes_evaluated=len(result.nodes),
        stopped_reason=result.stopped_reason,
        best_strategy=dict(result.best_strategy),
        best_path=[_node_payload(node) for node in result.path_to_best()],
        notes=["review_only_tree_search", "promotion_blocked"],
        evidence={
            "review_only": True,
            "target_summary": normalized["summary"],
            "risk": normalized["risk"],
            "requires_strong_review": normalized["requires_strong_review"],
        },
    )


async def _heuristic_tree_runner(
    item: StrategyCandidate | StrategyPackDraft,
    strategy: dict[str, Any],
) -> tuple[float, float]:
    normalized = _normalize_item(item)
    score = 0.42
    notes = " ".join(str(value).lower() for value in strategy.values())
    if strategy.get("memory_depth") in {"targeted", "deep"}:
        score += 0.08
    if strategy.get("evaluation_tier") in {"local_model", "lab_replay", "strong_model"}:
        score += 0.08
    if strategy.get("risk_gate") in {"human_approval", "rollback_required"}:
        score += 0.10
    if strategy.get("context_policy") == "sparse_credit_guided":
        score += 0.06
    if "budget" in notes or strategy.get("branching") == "bounded":
        score += 0.04
    if normalized["requires_strong_review"] and strategy.get("evaluation_tier") != "strong_model":
        score -= 0.08
    if normalized["risk"] in {"high", "critical"} and strategy.get("risk_gate") == "none":
        score -= 0.18
    return round(max(0.0, min(1.0, score)), 4), 0.003


def _candidate_generator(parent: ScientistTreeNode) -> list[dict[str, Any]]:
    base = dict(parent.strategy)
    variants: list[dict[str, Any]] = []

    cost_guarded = dict(base)
    cost_guarded.update(
        {
            "mutation": "cost_guarded",
            "memory_depth": "light",
            "context_policy": "sparse_credit_guided",
            "branching": "bounded",
            "budget_posture": "tight",
        }
    )
    variants.append(cost_guarded)

    evidence_deep = dict(base)
    evidence_deep.update(
        {
            "mutation": "evidence_deep",
            "memory_depth": "deep",
            "context_policy": "sparse_credit_guided",
            "evaluation_tier": "lab_replay",
            "branching": "bounded",
        }
    )
    variants.append(evidence_deep)

    safety_first = dict(base)
    safety_first.update(
        {
            "mutation": "safety_first",
            "risk_gate": "human_approval",
            "evaluation_tier": "strong_model",
            "rollback_policy": "required",
        }
    )
    variants.append(safety_first)
    return variants


def _root_strategy(normalized: dict[str, Any]) -> dict[str, Any]:
    risk = normalized["risk"]
    return {
        "strategy": "baseline_review",
        "execution_mode": "MAX" if risk in {"high", "critical"} else "SMART",
        "memory_depth": "targeted",
        "context_policy": "sparse_credit_guided",
        "evaluation_tier": "heuristic",
        "risk_gate": "human_approval" if risk in {"high", "critical"} else "none",
        "branching": "single",
    }


def _normalize_item(item: StrategyCandidate | StrategyPackDraft) -> dict[str, Any]:
    if isinstance(item, StrategyCandidate):
        risk: Any = item.risk
        summary = item.summary
        target_id = item.candidate_id
        target_kind = "strategy_candidate"
        requires_strong = item.requires_strong_review
        prompt = "\n".join(
            [item.task_type, item.summary, item.proposed_change, item.expected_benefit]
        )
    else:
        source_candidate = item.evidence.get("source_candidate")
        risk = "high" if item.requires_strong_review else "low"
        if isinstance(source_candidate, dict):
            risk = str(source_candidate.get("risk") or risk)
        summary = item.display_name
        target_id = item.draft_id
        target_kind = "strategy_pack_draft"
        requires_strong = item.requires_strong_review
        prompt = "\n".join(
            [
                item.display_name,
                item.proposed_pack_id,
                " ".join(item.task_type_patterns),
                " ".join(item.methodology_refs),
                " ".join(item.metric_dimensions),
                " ".join(item.risk_watch),
            ]
        )
    return {
        "target_id": target_id,
        "target_kind": target_kind,
        "summary": summary,
        "risk": _normalize_risk(risk),
        "requires_strong_review": requires_strong,
        "prompt": prompt,
    }


def _normalize_risk(raw: Any) -> str:
    risk = str(raw or "low").lower()
    if risk in {"low", "medium", "high", "critical"}:
        return risk
    return "low"


def _skip_record(
    item: StrategyCandidate | StrategyPackDraft,
    status: QiReplayTreeSearchStatus,
    reason: str,
) -> QiReplayTreeSearchRecord:
    normalized = _normalize_item(item)
    return QiReplayTreeSearchRecord(
        target_id=normalized["target_id"],
        target_kind=normalized["target_kind"],
        evaluation_id=_evaluation_id(normalized["target_id"], status),
        status=status,
        stopped_reason=reason,
        notes=[reason, "promotion_blocked"],
        evidence={"review_only": True, "risk": normalized["risk"]},
    )


def _error_record(
    item: StrategyCandidate | StrategyPackDraft, exc: Exception
) -> QiReplayTreeSearchRecord:
    normalized = _normalize_item(item)
    return QiReplayTreeSearchRecord(
        target_id=normalized["target_id"],
        target_kind=normalized["target_kind"],
        evaluation_id=_evaluation_id(normalized["target_id"], "error"),
        status="error",
        stopped_reason=type(exc).__name__,
        notes=[str(exc), "promotion_blocked"],
        evidence={"review_only": True, "risk": normalized["risk"]},
    )


def _node_payload(node: ScientistTreeNode) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "parent_id": node.parent_id,
        "depth": node.depth,
        "score": round(float(node.score), 4),
        "cost_usd": round(float(node.cost_usd), 6),
        "strategy": dict(node.strategy),
        "notes": node.notes,
    }


def _evaluation_id(target_id: str, status: str) -> str:
    raw = f"qi_tree_search|{target_id}|{status}"
    return f"qits_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _env_bool(name: str, *, default: bool = False) -> bool:
    import os

    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    import os

    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    import os

    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def qi_replay_tree_search_enabled_from_env() -> bool:
    return _env_bool("KUN_QI_TREE_SEARCH_ENABLED", default=False)


def configured_qi_replay_tree_search_budget_from_env() -> QiReplayTreeSearchBudget:
    return QiReplayTreeSearchBudget(
        max_items=_env_int("KUN_QI_TREE_SEARCH_MAX_ITEMS", 2),
        max_cost_usd=_env_float("KUN_QI_TREE_SEARCH_MAX_COST_USD", 0.05),
        beam_width=_env_int("KUN_QI_TREE_SEARCH_BEAM_WIDTH", 2),
        max_depth=_env_int("KUN_QI_TREE_SEARCH_MAX_DEPTH", 2),
    )


__all__ = [
    "QiReplayTreeRunner",
    "QiReplayTreeSearchBudget",
    "QiReplayTreeSearchPoolResult",
    "QiReplayTreeSearchRecord",
    "ReplayTreeRunnerFunc",
    "configured_qi_replay_tree_search_budget_from_env",
    "qi_replay_tree_search_enabled_from_env",
    "run_qi_replay_tree_search_pool",
]
