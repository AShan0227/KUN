"""V7 §12.4 RSI 三线 trifecta coordinator implementation.

The 3 lines, each as an injectable callable so prod/tests can swap impl:

  past_line:    PastLineHook(task_id, recent_steps) → list[Finding]
  present_line: PresentLineHook(task_id, current_step) → list[Critique]
  future_line:  FutureLineHook(task_id, plan, n_candidates) → list[Candidate]

The coordinator runs all 3 in parallel via asyncio.gather (return_exceptions
so one line failing doesn't kill the others). Returns TrifectaRunReport
with per-line results + aggregate cost multiplier estimate.

Env opt-out:
  KUN_V7_TRIFECTA_ENABLED=true (default false)
  KUN_V7_TRIFECTA_PAST_ENABLED=true
  KUN_V7_TRIFECTA_PRESENT_ENABLED=true
  KUN_V7_TRIFECTA_FUTURE_ENABLED=true

Cost telemetry: each line reports its cost_usd; coordinator computes the
total + the multiplier vs baseline (where baseline = single LLM tick cost
the caller provides).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.agents.trifecta.coordinator")


class TrifectaState(StrEnum):
    """Per-line execution state."""

    DISABLED = "disabled"  # env-disabled
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"  # hook returned None / no input


@dataclass(frozen=True)
class TrifectaLineResult:
    """One line's result snapshot (V7 §13.6 frozen IO)."""

    line_name: str  # "past" / "present" / "future"
    state: TrifectaState
    findings: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0
    error_detail: str | None = None
    duration_sec: float = 0.0


@dataclass(frozen=True)
class TrifectaRunReport:
    """Aggregate report of one trifecta run."""

    task_id: str
    invoked_at: datetime
    past: TrifectaLineResult
    present: TrifectaLineResult
    future: TrifectaLineResult
    total_cost_usd: float
    cost_multiplier_vs_baseline: float
    # ^ total / baseline_cost (where baseline = single LLM tick cost).
    #   Caller passes baseline; if omitted, multiplier defaults to None.

    @property
    def n_findings(self) -> int:
        return (
            len(self.past.findings)
            + len(self.present.findings)
            + len(self.future.findings)
        )

    @property
    def any_line_failed(self) -> bool:
        return any(
            r.state == TrifectaState.FAILED
            for r in (self.past, self.present, self.future)
        )


# Hook signatures — each takes a task_id + per-line input, returns
# (findings, cost_usd, error_detail). Production callers wire real hooks;
# tests pass stubs.

PastLineHook = Callable[
    [str, list[dict[str, Any]]],
    Awaitable[tuple[list[dict[str, Any]], float, str | None]],
]
PresentLineHook = Callable[
    [str, dict[str, Any]],
    Awaitable[tuple[list[dict[str, Any]], float, str | None]],
]
FutureLineHook = Callable[
    [str, dict[str, Any], int],
    Awaitable[tuple[list[dict[str, Any]], float, str | None]],
]


def _line_enabled(env_name: str, master_default: bool = False) -> bool:
    raw = os.environ.get(env_name, "").strip().lower()
    if not raw:
        return master_default
    return raw in {"true", "1", "yes", "on"}


class TrifectaCoordinator:
    """V7 §12.4 trifecta coordinator.

    Inject 3 hooks at construction; call ``run(task_id, ...)`` per long-task
    tick / milestone. Returns a TrifectaRunReport.
    """

    def __init__(
        self,
        *,
        past_hook: PastLineHook | None = None,
        present_hook: PresentLineHook | None = None,
        future_hook: FutureLineHook | None = None,
    ) -> None:
        self._past_hook = past_hook
        self._present_hook = present_hook
        self._future_hook = future_hook

    async def run(
        self,
        *,
        task_id: str,
        recent_steps: list[dict[str, Any]] | None = None,
        current_step: dict[str, Any] | None = None,
        future_plan: dict[str, Any] | None = None,
        n_future_candidates: int = 3,
        baseline_cost_usd: float | None = None,
    ) -> TrifectaRunReport:
        """Run all 3 lines in parallel, return aggregate report.

        Master switch + per-line switches:
          KUN_V7_TRIFECTA_ENABLED=true (default false) — required
          KUN_V7_TRIFECTA_{PAST,PRESENT,FUTURE}_ENABLED — per-line, default true
            when master is true
        """
        invoked_at = datetime.now(UTC)
        master_on = _line_enabled("KUN_V7_TRIFECTA_ENABLED", master_default=False)

        if not master_on:
            return TrifectaRunReport(
                task_id=task_id,
                invoked_at=invoked_at,
                past=TrifectaLineResult(
                    line_name="past", state=TrifectaState.DISABLED
                ),
                present=TrifectaLineResult(
                    line_name="present", state=TrifectaState.DISABLED
                ),
                future=TrifectaLineResult(
                    line_name="future", state=TrifectaState.DISABLED
                ),
                total_cost_usd=0.0,
                cost_multiplier_vs_baseline=1.0,
            )

        past_on = (
            _line_enabled("KUN_V7_TRIFECTA_PAST_ENABLED", master_default=True)
            and self._past_hook is not None
        )
        present_on = (
            _line_enabled("KUN_V7_TRIFECTA_PRESENT_ENABLED", master_default=True)
            and self._present_hook is not None
        )
        future_on = (
            _line_enabled("KUN_V7_TRIFECTA_FUTURE_ENABLED", master_default=True)
            and self._future_hook is not None
        )

        async def _run_past() -> TrifectaLineResult:
            if not past_on:
                return TrifectaLineResult(
                    line_name="past",
                    state=TrifectaState.DISABLED
                    if not _line_enabled(
                        "KUN_V7_TRIFECTA_PAST_ENABLED", master_default=True
                    )
                    else TrifectaState.SKIPPED,
                )
            t0 = asyncio.get_event_loop().time()
            try:
                findings, cost, err = await self._past_hook(  # type: ignore[misc]
                    task_id, recent_steps or []
                )
            except Exception as e:
                return TrifectaLineResult(
                    line_name="past",
                    state=TrifectaState.FAILED,
                    error_detail=f"{type(e).__name__}: {e}",
                    duration_sec=asyncio.get_event_loop().time() - t0,
                )
            return TrifectaLineResult(
                line_name="past",
                state=TrifectaState.OK if err is None else TrifectaState.FAILED,
                findings=findings,
                cost_usd=cost,
                error_detail=err,
                duration_sec=asyncio.get_event_loop().time() - t0,
            )

        async def _run_present() -> TrifectaLineResult:
            if not present_on:
                return TrifectaLineResult(
                    line_name="present",
                    state=TrifectaState.DISABLED
                    if not _line_enabled(
                        "KUN_V7_TRIFECTA_PRESENT_ENABLED", master_default=True
                    )
                    else TrifectaState.SKIPPED,
                )
            t0 = asyncio.get_event_loop().time()
            try:
                findings, cost, err = await self._present_hook(  # type: ignore[misc]
                    task_id, current_step or {}
                )
            except Exception as e:
                return TrifectaLineResult(
                    line_name="present",
                    state=TrifectaState.FAILED,
                    error_detail=f"{type(e).__name__}: {e}",
                    duration_sec=asyncio.get_event_loop().time() - t0,
                )
            return TrifectaLineResult(
                line_name="present",
                state=TrifectaState.OK if err is None else TrifectaState.FAILED,
                findings=findings,
                cost_usd=cost,
                error_detail=err,
                duration_sec=asyncio.get_event_loop().time() - t0,
            )

        async def _run_future() -> TrifectaLineResult:
            if not future_on:
                return TrifectaLineResult(
                    line_name="future",
                    state=TrifectaState.DISABLED
                    if not _line_enabled(
                        "KUN_V7_TRIFECTA_FUTURE_ENABLED", master_default=True
                    )
                    else TrifectaState.SKIPPED,
                )
            t0 = asyncio.get_event_loop().time()
            try:
                findings, cost, err = await self._future_hook(  # type: ignore[misc]
                    task_id, future_plan or {}, n_future_candidates
                )
            except Exception as e:
                return TrifectaLineResult(
                    line_name="future",
                    state=TrifectaState.FAILED,
                    error_detail=f"{type(e).__name__}: {e}",
                    duration_sec=asyncio.get_event_loop().time() - t0,
                )
            return TrifectaLineResult(
                line_name="future",
                state=TrifectaState.OK if err is None else TrifectaState.FAILED,
                findings=findings,
                cost_usd=cost,
                error_detail=err,
                duration_sec=asyncio.get_event_loop().time() - t0,
            )

        # Parallel execution
        past_result, present_result, future_result = await asyncio.gather(
            _run_past(), _run_present(), _run_future()
        )

        total_cost = (
            past_result.cost_usd
            + present_result.cost_usd
            + future_result.cost_usd
        )
        multiplier = (
            (total_cost / baseline_cost_usd)
            if baseline_cost_usd and baseline_cost_usd > 0
            else 1.0 + (total_cost / max(0.001, baseline_cost_usd or 0.001))
        )

        report = TrifectaRunReport(
            task_id=task_id,
            invoked_at=invoked_at,
            past=past_result,
            present=present_result,
            future=future_result,
            total_cost_usd=total_cost,
            cost_multiplier_vs_baseline=multiplier,
        )

        log.info(
            "trifecta.run_complete",
            task_id=task_id,
            past_state=past_result.state.value,
            present_state=present_result.state.value,
            future_state=future_result.state.value,
            n_findings=report.n_findings,
            total_cost_usd=round(total_cost, 4),
            multiplier=round(multiplier, 2),
            any_failed=report.any_line_failed,
        )

        return report


__all__ = [
    "FutureLineHook",
    "PastLineHook",
    "PresentLineHook",
    "TrifectaCoordinator",
    "TrifectaLineResult",
    "TrifectaRunReport",
    "TrifectaState",
]
