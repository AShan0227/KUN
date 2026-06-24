"""V7 §12.4 trifecta coordinator unit tests."""

from __future__ import annotations

import pytest
from kun.agents.trifecta import (
    TrifectaCoordinator,
    TrifectaState,
)

pytestmark = pytest.mark.asyncio


# ============================================================
# Master env switch — default OFF
# ============================================================


async def test_master_disabled_returns_all_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KUN_V7_TRIFECTA_ENABLED", raising=False)
    coord = TrifectaCoordinator()
    report = await coord.run(task_id="tk-x")
    assert report.past.state == TrifectaState.DISABLED
    assert report.present.state == TrifectaState.DISABLED
    assert report.future.state == TrifectaState.DISABLED
    assert report.total_cost_usd == 0.0
    assert report.n_findings == 0


# ============================================================
# All 3 lines fire when master + per-line on
# ============================================================


async def test_all_three_lines_fire_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")

    async def _past(task_id: str, recent: list) -> tuple[list, float, str | None]:
        return ([{"finding": "past-1"}], 0.001, None)

    async def _present(task_id: str, cur: dict) -> tuple[list, float, str | None]:
        return ([{"finding": "present-1"}, {"finding": "present-2"}], 0.005, None)

    async def _future(task_id: str, plan: dict, n: int) -> tuple[list, float, str | None]:
        return ([{"candidate": f"f-{i}"} for i in range(n)], 0.02, None)

    coord = TrifectaCoordinator(
        past_hook=_past,
        present_hook=_present,
        future_hook=_future,
    )
    report = await coord.run(
        task_id="tk-parallel",
        recent_steps=[{"step": 1}],
        current_step={"step": 2},
        future_plan={"goal": "explore"},
        n_future_candidates=3,
        baseline_cost_usd=0.01,
    )

    assert report.past.state == TrifectaState.OK
    assert report.present.state == TrifectaState.OK
    assert report.future.state == TrifectaState.OK
    assert report.n_findings == 1 + 2 + 3
    assert report.total_cost_usd == pytest.approx(0.001 + 0.005 + 0.02)


# ============================================================
# Per-line opt-out
# ============================================================


async def test_past_line_can_be_disabled_individually(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_TRIFECTA_PAST_ENABLED", "false")

    async def _past(task_id: str, recent: list) -> tuple[list, float, str | None]:
        # Should NEVER be called when env disabled
        raise AssertionError("past line should not run when env disabled")

    async def _present(task_id: str, cur: dict) -> tuple[list, float, str | None]:
        return ([], 0.001, None)

    coord = TrifectaCoordinator(past_hook=_past, present_hook=_present)
    report = await coord.run(task_id="tk-past-off")
    assert report.past.state == TrifectaState.DISABLED
    assert report.present.state == TrifectaState.OK


# ============================================================
# Line failure isolated — others continue
# ============================================================


async def test_one_line_failure_does_not_kill_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")

    async def _past_boom(task_id: str, recent: list) -> tuple[list, float, str | None]:
        raise RuntimeError("simulated past line failure")

    async def _present_ok(task_id: str, cur: dict) -> tuple[list, float, str | None]:
        return ([{"finding": "still-fine"}], 0.002, None)

    async def _future_ok(task_id: str, plan: dict, n: int) -> tuple[list, float, str | None]:
        return ([{"candidate": "yes"}], 0.01, None)

    coord = TrifectaCoordinator(
        past_hook=_past_boom,
        present_hook=_present_ok,
        future_hook=_future_ok,
    )
    report = await coord.run(task_id="tk-failure")

    assert report.past.state == TrifectaState.FAILED
    assert report.past.error_detail is not None
    assert "simulated" in report.past.error_detail
    # Others still ran
    assert report.present.state == TrifectaState.OK
    assert report.future.state == TrifectaState.OK
    assert report.any_line_failed is True


# ============================================================
# Missing hook → DISABLED
# ============================================================


async def test_missing_hook_marks_line_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")

    async def _past(task_id: str, recent: list) -> tuple[list, float, str | None]:
        return ([], 0.0, None)

    # No present, no future hooks
    coord = TrifectaCoordinator(past_hook=_past)
    report = await coord.run(task_id="tk-no-hooks")
    assert report.past.state == TrifectaState.OK
    # Present + future env-enabled but no hook provided → SKIPPED (not DISABLED).
    # DISABLED is reserved for explicit env-off; SKIPPED means "couldn't run".
    assert report.present.state == TrifectaState.SKIPPED
    assert report.future.state == TrifectaState.SKIPPED


# ============================================================
# Cost multiplier
# ============================================================


async def test_cost_multiplier_with_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_TRIFECTA_ENABLED", "true")

    async def _past(task_id: str, recent: list) -> tuple[list, float, str | None]:
        return ([], 0.005, None)

    async def _present(task_id: str, cur: dict) -> tuple[list, float, str | None]:
        return ([], 0.010, None)

    async def _future(task_id: str, plan: dict, n: int) -> tuple[list, float, str | None]:
        return ([], 0.035, None)

    coord = TrifectaCoordinator(
        past_hook=_past, present_hook=_present, future_hook=_future
    )
    # baseline 0.01 → total 0.05 → multiplier 5x (V7 §12.4 estimate matches)
    report = await coord.run(task_id="tk-cost", baseline_cost_usd=0.01)
    assert report.total_cost_usd == pytest.approx(0.05)
    assert report.cost_multiplier_vs_baseline == pytest.approx(5.0)
