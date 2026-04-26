"""V2.2 Wire 13 — EmergentSwitch evaluate + commit_switch wire 测试.

验证 orchestrator 在 detect_signals 触发后真调 evaluate_switch + commit_switch,
而不是只 emit signal.
"""

from __future__ import annotations

from kun.core.emergent_solution import (
    EmergentSolution,
    EmergentSolutionLibrary,
    EmergentSource,
)
from kun.engineering.emergent_switch import EmergentSwitchManager


def _add_solution(library: EmergentSolutionLibrary, task_type: str, status: str = "stable"):
    sol = EmergentSolution(
        task_type=task_type,
        action_kind="alt_strategy",
        action_payload={"strategy": "use_cheap_tier"},
        rationale="cheap is good for this task type",
        status=status,  # type: ignore[arg-type]
        estimated_outcome_delta=0.3,
        estimated_cost_delta=-0.05,
        discovered_by="watchtower_signal",
        source=EmergentSource(kind="internal_history"),
    )
    library.add(sol)
    return sol


# ---- evaluate_switch + commit_switch ----


def test_evaluate_switch_with_signals_should_switch_when_score_above_threshold() -> None:
    library = EmergentSolutionLibrary()
    _add_solution(library, "execution", status="stable")
    mgr = EmergentSwitchManager(library=library, switch_threshold=0.10)
    mgr.register_task("tk-1", estimated_steps=3)
    mgr.step_completed("tk-1", surprise_score=0.7)

    signals = mgr.detect_signals("tk-1", "execution")
    assert "surprise_high" in signals or "external_emergent_found" in signals

    eval_result = mgr.evaluate_switch(
        task_id="tk-1",
        task_type="execution",
        current_strategy_outcome=0.6,
        current_remaining_cost_usd=0.10,
        signals=list(signals),
    )
    assert eval_result.should_switch is True
    assert eval_result.chosen_solution is not None


def test_commit_switch_increments_stats() -> None:
    library = EmergentSolutionLibrary()
    _add_solution(library, "execution")
    mgr = EmergentSwitchManager(library=library, switch_threshold=0.10)
    mgr.register_task("tk-2", estimated_steps=3)
    st_before = mgr.get_stats("tk-2")
    assert st_before is not None
    assert st_before.switches_done == 0

    mgr.commit_switch("tk-2")
    st_after = mgr.get_stats("tk-2")
    assert st_after is not None
    assert st_after.switches_done == 1
    assert st_after.last_switch_at is not None


def test_max_switches_per_task_blocks_further_switches() -> None:
    """commit_switch 后 cooldown 期内 → blocked_by=cooldown; 跳过 cooldown 才检 max."""
    from datetime import UTC, datetime, timedelta

    library = EmergentSolutionLibrary()
    _add_solution(library, "execution")
    mgr = EmergentSwitchManager(library=library, max_switches_per_task=1, switch_threshold=0.10)
    mgr.register_task("tk-3", estimated_steps=10)
    mgr.step_completed("tk-3", surprise_score=0.7)

    mgr.commit_switch("tk-3")
    # 强制 cooldown 已过 (拉回 last_switch_at), 应该看到 max_switches_reached
    st = mgr.get_stats("tk-3")
    assert st is not None
    st.last_switch_at = datetime.now(UTC) - timedelta(hours=1)

    eval_result = mgr.evaluate_switch(
        task_id="tk-3",
        task_type="execution",
        current_strategy_outcome=0.6,
        current_remaining_cost_usd=0.1,
        signals=["surprise_high"],
    )
    assert eval_result.should_switch is False
    assert eval_result.blocked_by == "max_switches_reached"


def test_cooldown_blocks_immediate_re_switch() -> None:
    library = EmergentSolutionLibrary()
    _add_solution(library, "execution")
    mgr = EmergentSwitchManager(library=library, cooldown_minutes=5, switch_threshold=0.10)
    mgr.register_task("tk-4", estimated_steps=5)
    mgr.commit_switch("tk-4")  # 刚切过

    eval_result = mgr.evaluate_switch(
        task_id="tk-4",
        task_type="execution",
        current_strategy_outcome=0.6,
        current_remaining_cost_usd=0.1,
        signals=["surprise_high"],
    )
    assert eval_result.should_switch is False
    assert eval_result.blocked_by == "cooldown"


def test_no_candidate_blocks_switch() -> None:
    """没有候选 solution → blocked_by no_candidate."""
    library = EmergentSolutionLibrary()
    # 只加一个不同 task_type 的 → execution 没候选
    _add_solution(library, "judge", status="stable")
    mgr = EmergentSwitchManager(library=library, switch_threshold=0.10)
    mgr.register_task("tk-5", estimated_steps=5)
    mgr.step_completed("tk-5", surprise_score=0.7)

    eval_result = mgr.evaluate_switch(
        task_id="tk-5",
        task_type="execution",
        current_strategy_outcome=0.6,
        current_remaining_cost_usd=0.1,
        signals=["surprise_high"],
    )
    assert eval_result.should_switch is False
    assert eval_result.blocked_by == "no_candidate"
