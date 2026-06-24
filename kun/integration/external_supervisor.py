"""ExternalSupervisorService → PlanReviewService.ExternalSupervisorVerify adapter.

Wrap ``ExternalSupervisorService.analyze_observation`` (which expects a structured
observation request with explicit ``obs_kind`` / ``target_task_id`` /
``target_anchor_id`` kwargs) into the simpler 2-arg ``ExternalSupervisorVerify``
callback shape that ``PlanReviewService.submit_self_report`` injects.

Per ADR-023 + LT.B design:
  - ``PlanReviewService`` calls ``external_supervisor_verify(self_report, anchor)``
    and inspects ``.verdict`` / ``.rationale`` via ``getattr``.
  - We close over ``obs_kind`` / ``target_task_id`` / ``target_anchor_id`` at
    factory time so caller can bind once per task and reuse the callable for
    every ``submit_self_report`` round in that task.
  - We do NOT catch exceptions in the returned callable —
    ``PlanReviewService.submit_self_report`` already wraps the call in
    ``try/except`` and falls back to internal-only verdict on raise.
"""

from __future__ import annotations

from typing import Any

from kun.agents.supervisor.plan_review_service import ExternalSupervisorVerify
from kun.core.logging import get_logger
from kun.external_supervisor.service import (
    ExternalSupervisorObservation,
    ExternalSupervisorService,
)

log = get_logger("kun.integration.external_supervisor")


def make_external_supervisor_verify(
    service: ExternalSupervisorService,
    *,
    obs_kind: str = "drift_check",
    target_task_id: str | None = None,
    target_anchor_id: str | None = None,
) -> ExternalSupervisorVerify:
    """Wrap ``ExternalSupervisorService.analyze_observation`` into a callable
    suitable for ``PlanReviewService.external_supervisor_verify`` injection.

    The returned async callable accepts ``(self_report_dict, anchor_dict | None)``
    and forwards to ``service.analyze_observation`` with::

        obs_kind            = obs_kind (default 'drift_check')
        observation_payload = self_report_dict
        anchor              = anchor_dict
        target_task_id      = target_task_id (closure)
        target_anchor_id    = target_anchor_id (closure)

    Returns the :class:`ExternalSupervisorObservation` as-is —
    ``PlanReviewService`` inspects ``.verdict`` + ``.rationale`` via ``getattr``.

    The factory takes optional ``target_task_id`` / ``target_anchor_id`` at
    construction time so caller can bind once per task and reuse the callable
    for multiple ``submit_self_report`` calls in the same task.
    """

    async def _verify(
        self_report: dict[str, Any],
        anchor: dict[str, Any] | None,
    ) -> ExternalSupervisorObservation:
        observation = await service.analyze_observation(
            obs_kind=obs_kind,
            observation_payload=self_report,
            anchor=anchor,
            target_task_id=target_task_id,
            target_anchor_id=target_anchor_id,
        )
        log.info(
            "integration.external_supervisor.verified",
            task_id=target_task_id,
            anchor_id=target_anchor_id,
            obs_kind=obs_kind,
            verdict=getattr(observation, "verdict", None),
        )
        return observation

    return _verify


__all__ = ["make_external_supervisor_verify"]
