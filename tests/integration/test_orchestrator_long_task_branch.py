"""LT.INT-F — Orchestrator long-task branching verification.

Verifies the new branching logic in `Orchestrator.stream`:
  - Short task (complexity=simple, no goal_anchor) → existing short path
  - Long task (complexity=complex with goal_anchor) → LongTaskOrchestrator path

Tests don't try to spin up real DB / real LLM — they exercise the eligibility
helper + the integration plumbing at the unit boundary.
"""

from __future__ import annotations

from kun.agents.director.anchor import GoalAnchor
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.engineering.orchestrator import _is_long_task_branch_eligible


def _meta(
    *,
    complexity_score: float = 0.2,
    risk_level: str = "low",
    estimated_steps: int = 1,
    estimated_duration_sec: float = 30.0,
    complexity: str = "simple",
) -> TaskMeta:
    return TaskMeta(
        fingerprint="sha256:" + "0" * 64,
        task_type="coding.general",
        risk_level=risk_level,
        complexity_score=complexity_score,
        complexity=complexity,
        priority_profile="cost_first",
        owner=Owner(tenant_id="t-acme"),
        estimated_cost_usd=0.05,
        estimated_duration_sec=estimated_duration_sec,
        estimated_steps=estimated_steps,
        success_criteria_short="x",
    )


def _attach_anchor(ref: TaskRef) -> TaskRef:
    anchor = GoalAnchor(
        task_id=ref.meta.task_id,
        goal_statement="implement oauth login",
        success_criteria=["login works"],
    )
    # TaskRef has extra="allow"
    ref.goal_anchor = anchor  # type: ignore[attr-defined]
    return ref


# ---- _is_long_task_branch_eligible ----


def test_eligible_true_complex_with_anchor() -> None:
    meta = _meta(complexity="complex", complexity_score=0.7)
    ref = TaskRef(meta=meta, spec=TaskSpec(goal_detail="x"))
    _attach_anchor(ref)
    assert _is_long_task_branch_eligible(ref) is True


def test_eligible_false_simple_task() -> None:
    meta = _meta(complexity="simple")
    ref = TaskRef(meta=meta, spec=TaskSpec(goal_detail="x"))
    # No goal_anchor either
    assert _is_long_task_branch_eligible(ref) is False


def test_eligible_false_complex_but_no_anchor() -> None:
    """Even complex tasks fall back to short path if anchor wasn't pinned."""
    meta = _meta(complexity="complex", complexity_score=0.8)
    ref = TaskRef(meta=meta, spec=TaskSpec(goal_detail="x"))
    # No anchor attached
    assert _is_long_task_branch_eligible(ref) is False


def test_eligible_true_long_duration_with_anchor() -> None:
    """estimated_duration_sec > 600 triggers long-task even if complexity simple."""
    meta = _meta(
        complexity="simple",
        complexity_score=0.2,
        estimated_duration_sec=900.0,  # > 600
    )
    ref = TaskRef(meta=meta, spec=TaskSpec(goal_detail="x"))
    _attach_anchor(ref)
    assert _is_long_task_branch_eligible(ref) is True


def test_eligible_true_many_steps_with_anchor() -> None:
    """estimated_steps > 5 triggers long-task."""
    meta = _meta(estimated_steps=6)
    ref = TaskRef(meta=meta, spec=TaskSpec(goal_detail="x"))
    _attach_anchor(ref)
    assert _is_long_task_branch_eligible(ref) is True


def test_eligible_true_high_risk_with_anchor() -> None:
    meta = _meta(risk_level="high")
    ref = TaskRef(meta=meta, spec=TaskSpec(goal_detail="x"))
    _attach_anchor(ref)
    assert _is_long_task_branch_eligible(ref) is True


def test_eligible_true_critical_risk_with_anchor() -> None:
    meta = _meta(risk_level="critical")
    ref = TaskRef(meta=meta, spec=TaskSpec(goal_detail="x"))
    _attach_anchor(ref)
    assert _is_long_task_branch_eligible(ref) is True


def test_eligible_false_high_risk_but_no_anchor() -> None:
    """High risk without anchor still falls back to short path."""
    meta = _meta(risk_level="high")
    ref = TaskRef(meta=meta, spec=TaskSpec(goal_detail="x"))
    assert _is_long_task_branch_eligible(ref) is False


# ---- Branch path imports cleanly ----


def test_long_task_branch_imports_are_resolvable() -> None:
    """Ensure the lazy imports inside _run_long_task_branch all resolve.

    Direct check rather than spinning up Orchestrator — avoids needing DB.
    """
    from kun.engineering.long_task_orchestrator import LongTaskOrchestrator
    from kun.integration.checkpoint_db import (
        make_checkpoint_reader,
        make_checkpoint_status_marker,
        make_checkpoint_writer,
    )
    from kun.integration.llm_invoker import make_llm_invoker
    from kun.integration.tool_executor import make_tool_executor

    assert LongTaskOrchestrator is not None
    assert callable(make_checkpoint_reader)
    assert callable(make_checkpoint_status_marker)
    assert callable(make_checkpoint_writer)
    assert callable(make_llm_invoker)
    assert callable(make_tool_executor)


# ---- Status mapping contract (sanity check) ----


def test_status_map_keys_cover_loop_status_literals() -> None:
    """The status_map in _run_long_task_branch must cover all LoopStatus values."""
    from typing import get_args

    from kun.agents.executor.exec_loop import LoopStatus

    # All LoopStatus literal values (set the source of truth)
    expected = set(get_args(LoopStatus))
    # The mapping is defined inline; here we just assert the set itself is
    # what _run_long_task_branch was written against (defense for future
    # additions to LoopStatus).
    assert expected == {
        "final",
        "max_steps",
        "budget_exceeded",
        "wall_clock_exceeded",
        "stuck",
        "failed",
        "user_cancelled",
    }, (
        "LoopStatus literal added/removed without updating "
        "_run_long_task_branch.status_map — please sync."
    )
