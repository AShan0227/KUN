"""DB writer adapter for PlanReviewOutcome → plan_reviews row.

ADR-022 Layer 4 持久化: PlanReviewService.submit_self_report 给出
PlanReviewOutcome (内部使用 {aligned, drifting, off_track} +
{continue, pause_for_anchor_recheck, trigger_rcdh_level_0} 语义),
但 plan_reviews 表的 CHECK constraint 用更紧的 ops 词表
({ok, mild_drift, heavy_drift} + {continue, remind, pause, rsi_trigger}).

本模块是 outcome → row 的翻译层. 单一职责:
  1. 翻译 verdict / action 词表
  2. 拼装 PlanReviewRow 字段
  3. 在 session_scope 里 add + flush

ORM / Service 不可改 (上游已稳定), 翻译表是这里的真理.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from kun.agents.supervisor.plan_review_service import PlanReviewOutcome
from kun.core.ids import new_id
from kun.core.logging import get_logger

log = get_logger("kun.integration.plan_review_db")


# ---------------------------------------------------------------------------
# Translation tables — outcome (service-domain) → row (DB-domain).
# Keep aligned with CHECK constraints in alembic/0011_rsi_data_spine.py
# (plan_review_verdict_valid / plan_review_action_valid).
# ---------------------------------------------------------------------------

_VERDICT_MAP: dict[str, str] = {
    "aligned": "ok",
    "drifting": "mild_drift",
    "off_track": "heavy_drift",
}

_ACTION_MAP: dict[str, str] = {
    "continue": "continue",
    "pause_for_anchor_recheck": "pause",
    "trigger_rcdh_level_0": "rsi_trigger",
}


def map_verdict(final_verdict: str) -> str:
    """Translate PlanReviewOutcome.final_verdict → supervisor_verdict column.

    Raises ValueError if the input is outside VerdictName Literal — defense
    against upstream drift even though the type system already constrains it.
    """
    try:
        return _VERDICT_MAP[final_verdict]
    except KeyError as exc:
        raise ValueError(
            f"Unknown final_verdict {final_verdict!r}; "
            f"expected one of {sorted(_VERDICT_MAP)}"
        ) from exc


def map_action(action: str) -> str:
    """Translate PlanReviewOutcome.action → action_taken column.

    Raises ValueError if the input is outside the derive_action range.
    """
    try:
        return _ACTION_MAP[action]
    except KeyError as exc:
        raise ValueError(
            f"Unknown action {action!r}; "
            f"expected one of {sorted(_ACTION_MAP)}"
        ) from exc


async def write_plan_review_outcome(
    *,
    tenant_id: str,
    task_id: str,
    anchor_id: str,
    triggered_at_step: int,
    outcome: PlanReviewOutcome,
    self_report: dict[str, Any] | None = None,
) -> str:
    """Persist a PlanReviewOutcome to the plan_reviews table.

    Returns the new review_id (always freshly minted with new_id("plan_review")
    rather than reused from outcome.trigger — outcome.trigger may be None
    because PlanReviewService.submit_self_report does not attach the original
    trigger object).

    Field translation (PlanReviewOutcome → PlanReviewRow):
      review_id              = new_id("plan_review")  (NOT outcome.trigger.review_id)
      task_id                = task_id  (param)
      anchor_id              = anchor_id  (param)
      triggered_at_step      = triggered_at_step  (param; current step idx)
      triggered_at_time      = datetime.now(UTC)
      executor_self_report   = self_report kwarg
                                 OR outcome.trigger.payload['executor_self_report']
                                 OR {}
      supervisor_verdict     = map_verdict(outcome.final_verdict)   ← TRANSLATE
      drift_evidence         = outcome.drift_evidence
      external_supervisor_verify = {"verdict": outcome.external_verdict}
                                   if outcome.external_verdict is not None
                                   else None
      action_taken           = map_action(outcome.action)           ← TRANSLATE
      created_at             = datetime.now(UTC)
    """
    # Imported inside the function so monkeypatch on
    # "kun.core.db.session_scope" / "kun.core.orm.PlanReviewRow" reaches us at
    # call time (consistent with kun.watchtower.handlers pattern).
    from kun.core.db import session_scope
    from kun.core.orm import PlanReviewRow

    review_id = new_id("plan_review")
    supervisor_verdict = map_verdict(outcome.final_verdict)
    action_taken = map_action(outcome.action)

    if self_report is not None:
        executor_self_report: dict[str, Any] = dict(self_report)
    elif outcome.trigger is not None:
        payload_report = outcome.trigger.payload.get("executor_self_report")
        executor_self_report = (
            dict(payload_report) if isinstance(payload_report, dict) else {}
        )
    else:
        executor_self_report = {}

    external_verify: dict[str, Any] | None = (
        {"verdict": outcome.external_verdict}
        if outcome.external_verdict is not None
        else None
    )

    now = datetime.now(UTC)

    row = PlanReviewRow(
        tenant_id=tenant_id,
        review_id=review_id,
        task_id=task_id,
        anchor_id=anchor_id,
        triggered_at_step=triggered_at_step,
        triggered_at_time=now,
        executor_self_report=executor_self_report,
        supervisor_verdict=supervisor_verdict,
        drift_evidence=list(outcome.drift_evidence),
        external_supervisor_verify=external_verify,
        action_taken=action_taken,
        created_at=now,
    )

    async with session_scope(tenant_id=tenant_id) as session:
        session.add(row)
        await session.flush()

    log.info(
        "plan_review.persisted",
        tenant_id=tenant_id,
        review_id=review_id,
        task_id=task_id,
        anchor_id=anchor_id,
        supervisor_verdict=supervisor_verdict,
        action_taken=action_taken,
        external_present=external_verify is not None,
    )
    return review_id


PlanReviewWriter = Callable[..., Awaitable[str]]
"""Type alias for the bound writer returned by make_plan_review_writer.

Signature at call site:
    await writer(outcome, *, triggered_at_step, self_report=None) -> review_id
"""


def make_plan_review_writer(
    tenant_id: str,
    task_id: str,
    anchor_id: str,
) -> PlanReviewWriter:
    """Return an async writer with tenant / task / anchor pre-bound.

    Lets PlanReviewService (or a runtime composition root) hand a single
    callable to the heartbeat / submit path without leaking persistence
    parameters into the service surface.

    Example::

        writer = make_plan_review_writer("tenant-a", "task-1", "ga-x")
        review_id = await writer(outcome, triggered_at_step=3)
    """

    async def _writer(
        outcome: PlanReviewOutcome,
        *,
        triggered_at_step: int,
        self_report: dict[str, Any] | None = None,
    ) -> str:
        return await write_plan_review_outcome(
            tenant_id=tenant_id,
            task_id=task_id,
            anchor_id=anchor_id,
            triggered_at_step=triggered_at_step,
            outcome=outcome,
            self_report=self_report,
        )

    return _writer


__all__ = [
    "PlanReviewWriter",
    "make_plan_review_writer",
    "map_action",
    "map_verdict",
    "write_plan_review_outcome",
]
