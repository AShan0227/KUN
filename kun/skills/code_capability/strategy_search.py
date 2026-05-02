"""Review-only strategy tree search for CodeCapability outcomes."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.qi.ai_scientist import AIScientistTreeSearch, ScientistTreeNode

CodeStrategySearchStatus = Literal["evaluated", "skipped_disabled", "error"]


class CodeStrategySearchBudget(BaseModel):
    """Small opt-in budget for code workflow strategy exploration."""

    model_config = ConfigDict(extra="forbid")

    max_cost_usd: float = 0.03
    beam_width: int = 2
    max_depth: int = 2


class CodeStrategySearchInput(BaseModel):
    """Compact facts from one CodeCapability propose-change outcome."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    path: str
    mode: str
    phase: str
    checks_passed: bool
    review_ok: bool | None = None
    applied: bool = False
    rolled_back: bool = False
    bytes_changed: int = 0
    lint_failed_count: int = 0
    test_failed_count: int = 0
    reason: str = ""
    diff_sha256: str = ""


class CodeStrategySearchRecord(BaseModel):
    """Review-only tree-search evidence for one code change pattern."""

    model_config = ConfigDict(extra="forbid")

    evaluation_id: str
    target_id: str
    evaluator_kind: Literal["code_tree_search"] = "code_tree_search"
    status: CodeStrategySearchStatus
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


CodeStrategyRunner = Callable[
    [CodeStrategySearchInput, dict[str, Any]], Awaitable[tuple[float, float]]
]


async def run_code_change_strategy_tree_search(
    data: CodeStrategySearchInput,
    *,
    enabled: bool = False,
    budget: CodeStrategySearchBudget | None = None,
    runner: CodeStrategyRunner | None = None,
) -> CodeStrategySearchRecord:
    """Explore safer reusable workflows for a code change outcome.

    This is a reviewer, not an executor.  It never changes files, promotes a
    skill, or installs anything.  The output is evidence for NUO/Qi/humans.
    """

    budget = budget or CodeStrategySearchBudget()
    target_id = _target_id(data)
    if not enabled:
        return _skipped_record(data, target_id, "code_strategy_tree_search_disabled")

    active_runner = runner or _heuristic_runner

    async def tree_runner(prompt: str, strategy: dict[str, Any]) -> tuple[float, float]:
        _ = prompt
        return await active_runner(data, strategy)

    try:
        search = AIScientistTreeSearch(
            tree_runner,
            beam_width=max(1, budget.beam_width),
            max_depth=max(0, budget.max_depth),
            total_budget_usd=max(0.0, budget.max_cost_usd),
            candidate_generator=_candidate_generator,
        )
        result = await search.search(_prompt(data), root_strategy=_root_strategy(data))
    except Exception as exc:
        return CodeStrategySearchRecord(
            evaluation_id=_evaluation_id(target_id, "error"),
            target_id=target_id,
            status="error",
            stopped_reason=type(exc).__name__,
            notes=[str(exc), "review_only", "promotion_blocked"],
            evidence=_evidence(data),
        )

    best_score = round(float(result.best_score), 4)
    return CodeStrategySearchRecord(
        evaluation_id=_evaluation_id(target_id, "evaluated"),
        target_id=target_id,
        status="evaluated",
        score=best_score,
        best_score=best_score,
        total_cost_usd=round(max(0.0, result.total_cost_usd), 6),
        nodes_evaluated=len(result.nodes),
        stopped_reason=result.stopped_reason,
        best_strategy=dict(result.best_strategy),
        best_path=[_node_payload(node) for node in result.path_to_best()],
        notes=["review_only_code_strategy_search", "promotion_blocked"],
        evidence=_evidence(data),
    )


async def _heuristic_runner(
    data: CodeStrategySearchInput,
    strategy: dict[str, Any],
) -> tuple[float, float]:
    score = 0.4
    if data.checks_passed:
        score += 0.15
    if data.review_ok is True:
        score += 0.08
    if data.lint_failed_count == 0 and data.test_failed_count == 0:
        score += 0.08
    if strategy.get("checks") == "targeted_tests":
        score += 0.08
    if strategy.get("sandbox") == "strict":
        score += 0.06
    if strategy.get("write_mode") == "dry_run_first":
        score += 0.05
    if strategy.get("learning_output") == "draft_skill_review":
        score += 0.05
    if data.rolled_back or data.phase != "done":
        score -= 0.18
    if data.applied and strategy.get("approval_gate") != "required":
        score -= 0.08
    return round(max(0.0, min(1.0, score)), 4), 0.002


def _candidate_generator(parent: ScientistTreeNode) -> list[dict[str, Any]]:
    base = dict(parent.strategy)
    variants: list[dict[str, Any]] = []

    safety = dict(base)
    safety.update(
        {
            "mutation": "safety_first",
            "sandbox": "strict",
            "approval_gate": "required",
            "checks": "targeted_tests",
            "rollback_policy": "required",
        }
    )
    variants.append(safety)

    learning = dict(base)
    learning.update(
        {
            "mutation": "learn_reusable_pattern",
            "learning_output": "draft_skill_review",
            "resource_credit": "write_back",
            "checks": "lint_and_tests",
        }
    )
    variants.append(learning)

    fast = dict(base)
    fast.update(
        {
            "mutation": "fast_low_risk",
            "write_mode": "dry_run_first",
            "checks": "lint_only",
            "approval_gate": "skip_for_dry_run",
        }
    )
    variants.append(fast)
    return variants


def _root_strategy(data: CodeStrategySearchInput) -> dict[str, Any]:
    return {
        "strategy": "baseline_code_workflow",
        "write_mode": "dry_run_first",
        "checks": "targeted_tests" if data.test_failed_count == 0 else "lint_and_tests",
        "sandbox": "soft",
        "approval_gate": "required" if data.applied else "skip_for_dry_run",
        "learning_output": "draft_skill_review" if data.checks_passed else "none",
    }


def _prompt(data: CodeStrategySearchInput) -> str:
    return "\n".join(
        [
            f"task_id={data.task_id}",
            f"path={data.path}",
            f"mode={data.mode}",
            f"phase={data.phase}",
            f"checks_passed={data.checks_passed}",
            f"review_ok={data.review_ok}",
            f"rolled_back={data.rolled_back}",
            f"bytes_changed={data.bytes_changed}",
            f"reason={data.reason}",
        ]
    )


def _target_id(data: CodeStrategySearchInput) -> str:
    raw = "|".join([data.task_id, data.path, data.diff_sha256, data.mode, data.phase])
    return f"code_strategy_{hashlib.sha256(raw.encode()).hexdigest()[:16]}"


def _evaluation_id(target_id: str, status: str) -> str:
    return f"ccs_{hashlib.sha256(f'{target_id}|{status}'.encode()).hexdigest()[:16]}"


def _skipped_record(
    data: CodeStrategySearchInput,
    target_id: str,
    reason: str,
) -> CodeStrategySearchRecord:
    return CodeStrategySearchRecord(
        evaluation_id=_evaluation_id(target_id, "skipped_disabled"),
        target_id=target_id,
        status="skipped_disabled",
        stopped_reason=reason,
        notes=[reason, "review_only", "promotion_blocked"],
        evidence=_evidence(data),
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


def _evidence(data: CodeStrategySearchInput) -> dict[str, Any]:
    return {
        "review_only": True,
        "task_id": data.task_id,
        "path": data.path,
        "mode": data.mode,
        "phase": data.phase,
        "checks_passed": data.checks_passed,
        "review_ok": data.review_ok,
        "applied": data.applied,
        "rolled_back": data.rolled_back,
        "bytes_changed": data.bytes_changed,
        "diff_sha256": data.diff_sha256,
    }


def code_strategy_tree_search_enabled_from_env() -> bool:
    value = os.getenv("KUN_CODE_STRATEGY_TREE_SEARCH_ENABLED")
    return bool(value and value.strip().lower() in {"1", "true", "yes", "on"})


def configured_code_strategy_search_budget_from_env() -> CodeStrategySearchBudget:
    return CodeStrategySearchBudget(
        max_cost_usd=_env_float("KUN_CODE_STRATEGY_TREE_SEARCH_MAX_COST_USD", 0.03),
        beam_width=_env_int("KUN_CODE_STRATEGY_TREE_SEARCH_BEAM_WIDTH", 2),
        max_depth=_env_int("KUN_CODE_STRATEGY_TREE_SEARCH_MAX_DEPTH", 2),
    )


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except ValueError:
        return default


__all__ = [
    "CodeStrategySearchBudget",
    "CodeStrategySearchInput",
    "CodeStrategySearchRecord",
    "CodeStrategySearchStatus",
    "code_strategy_tree_search_enabled_from_env",
    "configured_code_strategy_search_budget_from_env",
    "run_code_change_strategy_tree_search",
]
