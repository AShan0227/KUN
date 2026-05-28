"""V6 control_plane runner → V7 §9.7 service bridge (V7 Phase X.B.MF-1, P0 wiring).

Pre-existing ``kun.control_plane.mission_director.MissionDirectorRunner`` is the
V6 worker runner that handles ``review``/``governance``/``plan_change`` work items,
producing ``WorkItemResult`` + ``ArtifactRecord`` + ``GateEvaluation``. The
V7 §9.7 ``kun.agents.mission_director.service.MissionDirectorService`` is the
periodic alignment-review service producing ``MissionAlignmentReview`` frozen
IO that emits to ``mission_alignment_reviews`` table.

**Before this bridge existed, the production daemon did NOT call the V7 service.**
Phase X.B.MD wrote the table + emitter machinery but no production caller. The
attacker audit (V7 §16.6) flagged this as P0 反模式 1 ("代码写完但 runtime path
不通"). This bridge closes that gap.

How it wires:
  - Each time the V6 runner finishes building its payload for a review item,
    it calls ``emit_v7_review_for_work_item_sync(...)`` (this module).
  - The bridge runs a fire-and-forget thread that calls the V7 service
    with DB emitter wired. Thread-based dispatch so the V6 sync runner does
    not need to become async.
  - Emit failures are **best-effort**: log + swallow. V6 path is unaffected.
  - DB unavailable (no PG) → graceful no-op (existing 1898 tests stay passing
    without setting up a DB).

Coverage estimation (V7 §9.7 needs 3 numbers in [0,1]):
  - ``info_gap_coverage`` = ``1 - n_open_info_gap_tickets / max(1, n_info_gaps)``
  - ``decomposition_coverage`` = ``n_closed_work_items / max(1, n_total_work_items)``
  - ``evidence_coverage`` = ``n_supporting_artifacts / max(1, n_evidence_plan_items)``

These are crude proxies derived from the V6 payload the v6 runner already built.
A future Phase X.B+ refinement would compute from ``TaskPlan.evidence_plan`` /
``work_items.status`` directly, but for MF-1 (P0 wiring) the goal is **row 真
有进 DB**, not coverage精确度.

Opt-out: set ``KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED=false`` to disable.
Default is enabled. When disabled, this module's entry is a no-op.

Audit note (per V7 §16.6 — production-path-必经 evidence):
  - grep ``kun.integration.mission_director_v7_bridge`` ⇒
    ``kun/control_plane/mission_director.py`` (1 line)
    + tests + this module. The control_plane runner真import了 bridge,
    所以 daemon 真 tick 时 bridge 真被调用.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import TYPE_CHECKING, Any

from kun.core.logging import get_logger

if TYPE_CHECKING:
    from kun.control_plane.runtime import InMemoryControlPlane
    from kun.control_plane.v6 import Mission, WorkItem

log = get_logger("kun.integration.mission_director_v7_bridge")


_BRIDGE_ENABLED_ENV = "KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED"


def _bridge_enabled() -> bool:
    """Default on; set KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED=false to disable."""
    raw = os.environ.get(_BRIDGE_ENABLED_ENV, "true").strip().lower()
    return raw not in {"false", "0", "no", "off"}


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _estimate_coverage_from_v6_state(
    *,
    control_plane: InMemoryControlPlane,
    mission: Mission,
    payload: dict[str, Any],
) -> tuple[float, float, float, list[str]]:
    """Compute (info_gap_coverage, decomposition_coverage, evidence_coverage,
    findings) from the V6 control_plane state.

    Phase X.B+ would refine these with real TaskPlan + work_item status; this
    function gives the MF-1 P0 wiring a sensible baseline (NOT a final figure).
    """
    # Work items for this mission
    work_items = [
        wi
        for wi in control_plane.work_items.values()
        if wi.mission_id == mission.mission_id
    ]
    n_total = len(work_items)
    n_closed = sum(1 for wi in work_items if wi.status in {"done", "completed"})

    # Open collaboration tickets
    open_tickets = [
        t
        for t in control_plane.collaboration_tickets.values()
        if t.mission_id == mission.mission_id
        and t.status in {"open", "waiting", "escalated"}
    ]
    info_gap_tickets = [
        t for t in open_tickets if "info_gap" in (t.reason or "").lower()
    ]

    # Plan info_gaps / evidence_plan from current plan (best-effort)
    plan = None
    if mission.current_plan_version is not None:
        # Plans live in control_plane.task_plans keyed by plan_id; find latest matching
        for p in control_plane.task_plans.values():
            if (
                p.mission_id == mission.mission_id
                and p.version == mission.current_plan_version
            ):
                plan = p
                break
    n_info_gaps = len(plan.info_gaps) if plan is not None else 0
    n_evidence_plan = len(plan.evidence_plan) if plan is not None else 0

    # Artifacts supporting the mission
    n_artifacts = sum(
        1
        for art in control_plane.artifacts.values()
        if art.mission_id == mission.mission_id
    )

    info_gap_coverage = _clamp01(
        1.0 - (len(info_gap_tickets) / max(1, n_info_gaps))
        if n_info_gaps > 0
        else 1.0
    )
    decomposition_coverage = _clamp01(
        n_closed / max(1, n_total) if n_total > 0 else 1.0
    )
    evidence_coverage = _clamp01(
        n_artifacts / max(1, n_evidence_plan) if n_evidence_plan > 0 else 1.0
    )

    findings: list[str] = []
    summary = str(payload.get("summary", "")).strip()
    if summary:
        findings.append(f"v6_summary: {summary[:240]}")
    if info_gap_tickets:
        findings.append(
            f"open info_gap tickets: {len(info_gap_tickets)} (V7 §9.7 信息缺口未补齐)"
        )
    if n_total > 0 and n_closed < n_total:
        findings.append(
            f"work_items {n_closed}/{n_total} closed (decomposition coverage)"
        )
    return info_gap_coverage, decomposition_coverage, evidence_coverage, findings


async def _emit_v7_review_async(
    *,
    tenant_id: str | None,
    task_id: str,
    task_plan_version: str,
    info_gap_coverage: float,
    decomposition_coverage: float,
    evidence_coverage: float,
    observed_findings: list[str],
) -> None:
    """Inner async: build V7 service with DB emitter, call review_mission()."""
    from kun.agents.mission_director.service import MissionDirectorService
    from kun.integration.mission_director_db import make_mission_review_emitter

    # tenant_id None → make_mission_review_emitter binds to caller's tenant_id;
    # we resolve the default tenant explicitly so the row carries a real value.
    if tenant_id is None or not tenant_id.strip():
        from kun.core.tenancy import current_tenant

        tenant_id = current_tenant().tenant_id

    service = MissionDirectorService(
        review_emitter=make_mission_review_emitter(tenant_id),
    )
    await service.review_mission(
        task_id=task_id,
        task_plan_version=task_plan_version,
        info_gap_coverage=info_gap_coverage,
        decomposition_coverage=decomposition_coverage,
        evidence_coverage=evidence_coverage,
        observed_findings=list(observed_findings),
    )


def emit_v7_review_for_work_item_sync(
    *,
    control_plane: InMemoryControlPlane,
    mission: Mission,
    work_item: WorkItem,
    payload: dict[str, Any],
    tenant_id: str | None = None,
) -> None:
    """Sync fire-and-forget bridge call from V6 control_plane runner.

    Spawns a daemon thread that runs ``asyncio.run(_emit_v7_review_async(...))``.
    Daemon thread = won't block process exit; emit failures logged & swallowed.

    Safe to call from any sync code path. Does nothing when env-disabled.
    """
    if not _bridge_enabled():
        return

    try:
        (
            info_gap_coverage,
            decomposition_coverage,
            evidence_coverage,
            findings,
        ) = _estimate_coverage_from_v6_state(
            control_plane=control_plane,
            mission=mission,
            payload=payload,
        )
    except Exception as e:
        log.warning(
            "mission_director_v7_bridge.coverage_estimate_failed",
            mission_id=getattr(mission, "mission_id", None),
            work_item_id=getattr(work_item, "work_item_id", None),
            error=f"{type(e).__name__}: {e}",
        )
        return

    # task_id mapping: V6 has no task_id field on Mission; use mission_id as
    # the V7 task_id since V7 §9.7 review keys on "task_id".
    task_id = mission.mission_id
    task_plan_version = mission.current_plan_version or "v0"

    log.info(
        "mission_director_v7_bridge.emit_dispatched",
        task_id=task_id,
        task_plan_version=task_plan_version,
        info_gap_coverage=round(info_gap_coverage, 3),
        decomposition_coverage=round(decomposition_coverage, 3),
        evidence_coverage=round(evidence_coverage, 3),
        n_findings=len(findings),
    )

    def _thread_target() -> None:
        try:
            asyncio.run(
                _emit_v7_review_async(
                    tenant_id=tenant_id,
                    task_id=task_id,
                    task_plan_version=task_plan_version,
                    info_gap_coverage=info_gap_coverage,
                    decomposition_coverage=decomposition_coverage,
                    evidence_coverage=evidence_coverage,
                    observed_findings=findings,
                )
            )
        except Exception as e:
            log.warning(
                "mission_director_v7_bridge.emit_failed",
                task_id=task_id,
                error=f"{type(e).__name__}: {e}",
            )

    thread = threading.Thread(
        target=_thread_target,
        name=f"v7-md-bridge-{task_id[:8]}",
        daemon=True,
    )
    thread.start()


__all__ = [
    "emit_v7_review_for_work_item_sync",
]
