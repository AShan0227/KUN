"""Integration: PlanReviewOutcome → plan_reviews row writer.

Validates the translation tables and bind-then-flush flow for
kun.integration.plan_review_db. Uses an in-process fake session_scope
(no Postgres needed) so the test stays in-memory but exercises the real
ORM PlanReviewRow construction path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kun.agents.supervisor.plan_review_heartbeat import ReviewTrigger
from kun.agents.supervisor.plan_review_service import PlanReviewOutcome
from kun.core.orm import PlanReviewRow
from kun.integration.plan_review_db import (
    make_plan_review_writer,
    map_action,
    map_verdict,
    write_plan_review_outcome,
)


class _CaptureSession:
    """Records add()'d rows and exposes them for assertion."""

    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def add(self, instance: Any) -> None:
        self._sink.append(instance)

    async def flush(self) -> None:  # pragma: no cover - trivial
        return None


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Any], list[dict[str, Any]]]:
    """Patch kun.core.db.session_scope with an in-memory recorder.

    Returns (added_rows, scope_calls). scope_calls captures the kwargs that
    write_plan_review_outcome passes into session_scope so we can assert
    tenant_id propagation.
    """
    added: list[Any] = []
    scope_calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_session_scope(**kwargs: Any) -> AsyncIterator[_CaptureSession]:
        scope_calls.append(kwargs)
        yield _CaptureSession(added)

    monkeypatch.setattr("kun.core.db.session_scope", fake_session_scope)
    return added, scope_calls


def _outcome(
    *,
    final_verdict: str = "aligned",
    action: str = "continue",
    external_verdict: str | None = None,
    drift_evidence: list[dict[str, Any]] | None = None,
    trigger: ReviewTrigger | None = None,
) -> PlanReviewOutcome:
    return PlanReviewOutcome(
        triggered=True,
        trigger=trigger,
        internal_verdict=final_verdict,  # type: ignore[arg-type]
        external_verdict=external_verdict,  # type: ignore[arg-type]
        final_verdict=final_verdict,  # type: ignore[arg-type]
        drift_evidence=list(drift_evidence or []),
        action=action,
        rationale=f"test::{final_verdict}/{action}",
        review_prompt=None,
    )


# ---------------------------------------------------------------------------
# Translation table coverage (verdict)
# ---------------------------------------------------------------------------


async def test_aligned_maps_to_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    added, _ = _install_fake_session(monkeypatch)

    review_id = await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=3,
        outcome=_outcome(final_verdict="aligned", action="continue"),
    )

    assert review_id.startswith("pr-")
    assert len(added) == 1
    row = added[0]
    assert isinstance(row, PlanReviewRow)
    assert row.supervisor_verdict == "ok"
    assert row.action_taken == "continue"


async def test_drifting_maps_to_mild_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=3,
        outcome=_outcome(
            final_verdict="drifting", action="pause_for_anchor_recheck"
        ),
    )

    assert added[0].supervisor_verdict == "mild_drift"


async def test_off_track_maps_to_heavy_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=6,
        outcome=_outcome(
            final_verdict="off_track", action="trigger_rcdh_level_0"
        ),
    )

    assert added[0].supervisor_verdict == "heavy_drift"


# ---------------------------------------------------------------------------
# Translation table coverage (action)
# ---------------------------------------------------------------------------


async def test_action_continue_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=1,
        outcome=_outcome(final_verdict="aligned", action="continue"),
    )

    assert added[0].action_taken == "continue"


async def test_action_pause_for_anchor_recheck_maps_to_pause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=2,
        outcome=_outcome(
            final_verdict="drifting", action="pause_for_anchor_recheck"
        ),
    )

    assert added[0].action_taken == "pause"


async def test_action_trigger_rcdh_level_0_maps_to_rsi_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=9,
        outcome=_outcome(
            final_verdict="off_track", action="trigger_rcdh_level_0"
        ),
    )

    assert added[0].action_taken == "rsi_trigger"


# ---------------------------------------------------------------------------
# external_supervisor_verify wrapping
# ---------------------------------------------------------------------------


async def test_external_verdict_wraps_into_jsonb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=4,
        outcome=_outcome(
            final_verdict="off_track",
            action="trigger_rcdh_level_0",
            external_verdict="off_track",
        ),
    )

    assert added[0].external_supervisor_verify == {"verdict": "off_track"}


async def test_external_verdict_none_stays_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=4,
        outcome=_outcome(final_verdict="aligned", action="continue"),
    )

    assert added[0].external_supervisor_verify is None


# ---------------------------------------------------------------------------
# id + propagation
# ---------------------------------------------------------------------------


async def test_review_id_has_pr_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    review_id = await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=3,
        outcome=_outcome(),
    )

    assert review_id.startswith("pr-")
    # And the row is keyed with that same id
    assert added[0].review_id == review_id
    # tenant / task / anchor / step propagate verbatim
    assert added[0].tenant_id == "tenant-a"
    assert added[0].task_id == "task-1"
    assert added[0].anchor_id == "ga-x"
    assert added[0].triggered_at_step == 3


async def test_drift_evidence_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    evidence = [
        {"type": "self_reported_off_anchor"},
        {"type": "criteria_regress", "from": 3, "to": 1},
    ]
    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=3,
        outcome=_outcome(
            final_verdict="off_track",
            action="trigger_rcdh_level_0",
            drift_evidence=evidence,
        ),
    )

    assert added[0].drift_evidence == evidence


# ---------------------------------------------------------------------------
# self_report sourcing
# ---------------------------------------------------------------------------


async def test_self_report_kwarg_wins_over_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    from datetime import UTC, datetime

    trigger = ReviewTrigger(
        review_id="pr-from-trigger",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=3,
        triggered_at_time=datetime.now(UTC),
        reason="step_threshold",
        payload={
            "executor_self_report": {"on_anchor": False, "src": "trigger"}
        },
    )
    outcome = _outcome(trigger=trigger)

    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=3,
        outcome=outcome,
        self_report={"on_anchor": True, "src": "kwarg"},
    )

    assert added[0].executor_self_report == {
        "on_anchor": True,
        "src": "kwarg",
    }


async def test_self_report_falls_back_to_trigger_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    from datetime import UTC, datetime

    trigger = ReviewTrigger(
        review_id="pr-from-trigger",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=3,
        triggered_at_time=datetime.now(UTC),
        reason="step_threshold",
        payload={
            "executor_self_report": {"on_anchor": True, "src": "trigger"}
        },
    )

    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=3,
        outcome=_outcome(trigger=trigger),
    )

    assert added[0].executor_self_report == {
        "on_anchor": True,
        "src": "trigger",
    }


async def test_self_report_defaults_empty_dict_when_no_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    await write_plan_review_outcome(
        tenant_id="tenant-a",
        task_id="task-1",
        anchor_id="ga-x",
        triggered_at_step=3,
        outcome=_outcome(),
    )

    assert added[0].executor_self_report == {}


# ---------------------------------------------------------------------------
# Defensive ValueError
# ---------------------------------------------------------------------------


async def test_invalid_verdict_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_session(monkeypatch)

    bad = _outcome(final_verdict="aligned", action="continue")
    # Bypass Literal-typed dataclass via object.__setattr__ (frozen dataclass).
    object.__setattr__(bad, "final_verdict", "exploded")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Unknown final_verdict"):
        await write_plan_review_outcome(
            tenant_id="tenant-a",
            task_id="task-1",
            anchor_id="ga-x",
            triggered_at_step=3,
            outcome=bad,
        )


async def test_invalid_action_raises_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_session(monkeypatch)

    bad = _outcome(final_verdict="aligned", action="continue")
    object.__setattr__(bad, "action", "noop-please")

    with pytest.raises(ValueError, match="Unknown action"):
        await write_plan_review_outcome(
            tenant_id="tenant-a",
            task_id="task-1",
            anchor_id="ga-x",
            triggered_at_step=3,
            outcome=bad,
        )


# Pure unit-style checks on the translation helpers — no DB at all.
def test_map_verdict_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown final_verdict"):
        map_verdict("nope")


def test_map_action_unknown() -> None:
    with pytest.raises(ValueError, match="Unknown action"):
        map_action("nope")


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def test_make_plan_review_writer_binds_tenant_task_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)

    writer = make_plan_review_writer("tenant-b", "task-99", "ga-y")
    review_id = await writer(
        _outcome(final_verdict="drifting", action="pause_for_anchor_recheck"),
        triggered_at_step=7,
    )

    assert review_id.startswith("pr-")
    assert len(added) == 1
    row = added[0]
    assert row.tenant_id == "tenant-b"
    assert row.task_id == "task-99"
    assert row.anchor_id == "ga-y"
    assert row.triggered_at_step == 7
    assert row.supervisor_verdict == "mild_drift"
    assert row.action_taken == "pause"
    # session_scope received the bound tenant_id
    assert scope_calls == [{"tenant_id": "tenant-b"}]


async def test_make_plan_review_writer_self_report_kwarg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    writer = make_plan_review_writer("tenant-b", "task-99", "ga-y")
    await writer(
        _outcome(),
        triggered_at_step=2,
        self_report={"on_anchor": True, "criteria_done_count": 4},
    )

    assert added[0].executor_self_report == {
        "on_anchor": True,
        "criteria_done_count": 4,
    }
