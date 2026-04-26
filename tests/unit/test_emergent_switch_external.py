"""Tests for emergent_switch + external_scan (V2.1 §5.8 + §3.10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kun.core.emergent_solution import (
    EmergentSolution,
    EmergentSolutionLibrary,
    EmergentSource,
)
from kun.engineering.emergent_switch import (
    EmergentSwitchManager,
)
from kun.engineering.external_scan import (
    ExternalInfoScanner,
)

# ---- EmergentSwitchManager ----


def _add_candidate(
    lib: EmergentSolutionLibrary,
    task_type: str = "coding.py",
    outcome_delta: float = 0.1,
    status: str = "stable",
):
    sol = EmergentSolution(
        task_type=task_type,
        discovered_by="external_scan",
        source=EmergentSource(kind="reddit"),
        estimated_outcome_delta=outcome_delta,
        estimated_cost_delta=-0.2,
        status=status,  # type: ignore[arg-type]
    )
    lib.add(sol)
    return sol


def test_emergent_no_signal_no_switch() -> None:
    lib = EmergentSolutionLibrary()
    mgr = EmergentSwitchManager(lib)
    mgr.register_task("tk-1", estimated_steps=5)
    decision = mgr.evaluate_switch(
        "tk-1",
        "coding.py",
        current_strategy_outcome=0.7,
        current_remaining_cost_usd=0.05,
        signals=[],
    )
    assert decision.should_switch is False
    assert decision.blocked_by == "no_signal"


def test_emergent_no_candidate_no_switch() -> None:
    lib = EmergentSolutionLibrary()
    mgr = EmergentSwitchManager(lib)
    mgr.register_task("tk-1")
    decision = mgr.evaluate_switch(
        "tk-1",
        "coding.py",
        0.7,
        0.05,
        signals=["surprise_high"],
    )
    assert decision.should_switch is False
    assert decision.blocked_by == "no_candidate"


def test_emergent_switch_when_score_passes_threshold() -> None:
    lib = EmergentSolutionLibrary()
    _add_candidate(lib, "coding.py", outcome_delta=0.3, status="stable")
    mgr = EmergentSwitchManager(lib, switch_threshold=0.15)
    mgr.register_task("tk-1")
    decision = mgr.evaluate_switch(
        "tk-1",
        "coding.py",
        0.7,
        0.05,
        signals=["surprise_high", "external_emergent_found"],
    )
    assert decision.should_switch is True
    assert decision.switch_score >= 0.15
    assert decision.chosen_solution is not None


def test_emergent_below_threshold_no_switch() -> None:
    lib = EmergentSolutionLibrary()
    _add_candidate(lib, "coding.py", outcome_delta=0.01, status="stable")
    mgr = EmergentSwitchManager(lib, switch_threshold=0.15)
    mgr.register_task("tk-1")
    decision = mgr.evaluate_switch(
        "tk-1",
        "coding.py",
        0.7,
        0.05,
        signals=["surprise_high"],
    )
    assert decision.should_switch is False
    assert "below" in decision.blocked_by


def test_emergent_low_interruption_tolerance_higher_threshold() -> None:
    """interruption_tolerance=low → 阈值升 0.30."""
    lib = EmergentSolutionLibrary()
    _add_candidate(lib, "coding.py", outcome_delta=0.20, status="stable")
    mgr = EmergentSwitchManager(lib, switch_threshold=0.15)
    mgr.register_task("tk-1")

    # medium tolerance: 应切
    d_med = mgr.evaluate_switch(
        "tk-1",
        "coding.py",
        0.7,
        0.05,
        signals=["surprise_high"],
        user_interruption_tolerance="medium",
    )
    assert d_med.should_switch is True

    # 重置 task
    mgr.cleanup("tk-1")
    mgr.register_task("tk-2")
    # low tolerance: 阈值升 0.30, 同样 score 不切
    d_low = mgr.evaluate_switch(
        "tk-2",
        "coding.py",
        0.7,
        0.05,
        signals=["surprise_high"],
        user_interruption_tolerance="low",
    )
    assert d_low.should_switch is False or d_low.switch_score >= 0.30


def test_emergent_max_switches_block() -> None:
    lib = EmergentSolutionLibrary()
    _add_candidate(lib, "coding.py", outcome_delta=0.5, status="stable")
    mgr = EmergentSwitchManager(lib, max_switches_per_task=2)
    st = mgr.register_task("tk-1")
    st.switches_done = 2  # 已切 2 次
    decision = mgr.evaluate_switch(
        "tk-1",
        "coding.py",
        0.5,
        0.05,
        signals=["surprise_high"],
    )
    assert decision.blocked_by == "max_switches_reached"


def test_emergent_cooldown_block() -> None:
    lib = EmergentSolutionLibrary()
    _add_candidate(lib, "coding.py", outcome_delta=0.5, status="stable")
    mgr = EmergentSwitchManager(lib, cooldown_minutes=5)
    st = mgr.register_task("tk-1")
    st.last_switch_at = datetime.now(UTC) - timedelta(minutes=2)  # 2 分钟前
    decision = mgr.evaluate_switch(
        "tk-1",
        "coding.py",
        0.5,
        0.05,
        signals=["surprise_high"],
    )
    assert decision.blocked_by == "cooldown"


def test_emergent_detect_signals_surprise_high() -> None:
    lib = EmergentSolutionLibrary()
    mgr = EmergentSwitchManager(lib)
    mgr.register_task("tk-1")
    mgr.step_completed("tk-1", surprise_score=0.8)
    signals = mgr.detect_signals("tk-1", "coding.py")
    assert "surprise_high" in signals


def test_emergent_detect_signals_step_exceeded() -> None:
    lib = EmergentSolutionLibrary()
    mgr = EmergentSwitchManager(lib)
    mgr.register_task("tk-1", estimated_steps=2)
    for _ in range(5):
        mgr.step_completed("tk-1", surprise_score=0.0)
    signals = mgr.detect_signals("tk-1", "coding.py")
    assert "step_count_exceeded" in signals


def test_emergent_commit_switch_updates_stats() -> None:
    lib = EmergentSolutionLibrary()
    mgr = EmergentSwitchManager(lib)
    mgr.register_task("tk-1")
    mgr.commit_switch("tk-1")
    st = mgr.get_stats("tk-1")
    assert st is not None
    assert st.switches_done == 1
    assert st.replan_count == 1
    assert st.last_switch_at is not None


# ---- ExternalInfoScanner ----


@pytest.mark.asyncio
async def test_external_scan_disabled_user_no_op() -> None:
    lib = EmergentSolutionLibrary()
    scanner = ExternalInfoScanner(
        lib,
        user_telemetry_enabled=lambda uid: False,
        user_top_task_types_lookup=lambda uid: ["x"],
    )
    result = await scanner.scan_for_user("u-1")
    assert result.candidates_added == 0
    assert result.scanned_task_types == []


@pytest.mark.asyncio
async def test_external_scan_no_top_types_no_op() -> None:
    lib = EmergentSolutionLibrary()
    scanner = ExternalInfoScanner(
        lib,
        user_telemetry_enabled=lambda uid: True,
        user_top_task_types_lookup=lambda uid: [],
    )
    result = await scanner.scan_for_user("u-1")
    assert result.scanned_task_types == []


@pytest.mark.asyncio
async def test_external_scan_adds_candidates() -> None:
    lib = EmergentSolutionLibrary()

    async def fake_fetcher(task_type: str) -> list[dict[str, Any]]:
        return [
            {
                "url": "https://x.com/1",
                "snippet": "use SQLModel for postgres",
                "estimated_outcome_delta": 0.05,
            },
        ]

    async def fake_reviewer(task_type: str, raw: dict) -> tuple[bool, str]:
        return (True, raw.get("snippet", ""))

    scanner = ExternalInfoScanner(
        lib,
        fetchers={"reddit": fake_fetcher},
        llm_reviewer=fake_reviewer,
        user_top_task_types_lookup=lambda uid: ["coding.python"],
        user_telemetry_enabled=lambda uid: True,
    )
    result = await scanner.scan_for_user("u-1")
    assert result.candidates_added == 1
    assert len(lib.list_for_task_type("coding.python")) == 1


@pytest.mark.asyncio
async def test_external_scan_llm_rejects_irrelevant() -> None:
    lib = EmergentSolutionLibrary()

    async def fake_fetcher(task_type: str) -> list[dict[str, Any]]:
        return [{"url": "https://noise/", "snippet": "spam"}]

    async def reject_reviewer(task_type: str, raw: dict) -> tuple[bool, str]:
        return (False, "irrelevant")

    scanner = ExternalInfoScanner(
        lib,
        fetchers={"reddit": fake_fetcher},
        llm_reviewer=reject_reviewer,
        user_top_task_types_lookup=lambda uid: ["x"],
        user_telemetry_enabled=lambda uid: True,
    )
    result = await scanner.scan_for_user("u-1")
    assert result.candidates_rejected == 1
    assert result.candidates_added == 0


@pytest.mark.asyncio
async def test_external_scan_budget_exhausts() -> None:
    lib = EmergentSolutionLibrary()

    async def fake_fetcher(task_type: str) -> list[dict[str, Any]]:
        return [{"url": "x", "snippet": "ok"}]

    scanner = ExternalInfoScanner(
        lib,
        fetchers={"reddit": fake_fetcher, "github_issue": fake_fetcher, "arxiv": fake_fetcher},
        user_top_task_types_lookup=lambda uid: ["a", "b", "c"],
        user_telemetry_enabled=lambda uid: True,
        default_daily_limit=5,  # 极小预算
    )
    result = await scanner.scan_for_user("u-1")
    # 3 task_types × 3 sources = 9 次, 但预算只 5
    assert result.sources_queried <= 5
    status = scanner.get_budget_status("u-1")
    assert status["used_today"] <= 5


@pytest.mark.asyncio
async def test_external_scan_fetcher_failure_non_fatal() -> None:
    lib = EmergentSolutionLibrary()

    async def fail_fetcher(task_type: str) -> list[dict[str, Any]]:
        raise RuntimeError("network down")

    async def ok_fetcher(task_type: str) -> list[dict[str, Any]]:
        return [{"url": "x", "snippet": "ok"}]

    scanner = ExternalInfoScanner(
        lib,
        fetchers={"reddit": fail_fetcher, "github_issue": ok_fetcher},
        user_top_task_types_lookup=lambda uid: ["x"],
        user_telemetry_enabled=lambda uid: True,
    )
    result = await scanner.scan_for_user("u-1")
    # ok_fetcher 仍能加候选
    assert result.candidates_added >= 1


def test_external_scan_budget_status_starts_zero() -> None:
    lib = EmergentSolutionLibrary()
    scanner = ExternalInfoScanner(lib)
    status = scanner.get_budget_status("u-1")
    assert status["used_today"] == 0
    assert status["remaining"] == 100  # 默认
