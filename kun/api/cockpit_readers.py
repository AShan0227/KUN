"""V7 §20 cockpit DB readers — Phase X.B 接真 DB.

Phase E.A 给 cockpit endpoints 返 stub. Phase X.B 这条把 endpoint 切到真
DB 查 (mission_alignment_reviews / lifecycle_transitions / auditor_reports
三张表都已在 alembic 0014/0015/0016 落地).

设计:
1. 每个 reader 是纯函数: (tenant_id, limit, **filters) → list[dict]
2. Dict 用 V7 §20 cockpit response schema 风格 (中英混合, 普通用户可读)
3. 不抛 — 即使 DB 不可用也返 []. 失败信息进 log, 不污染 API response.
   理由: cockpit 是 dashboard, 优雅降级比硬错更友好.
4. session_scope 用 in-function import 便于 monkeypatch (跟 writer 风格一致).

Tenant 处理:
- caller (cockpit endpoint) 必传 tenant_id, 这层不做 default fallback.
- session_scope(tenant_id=...) 自动设 RLS GUC, 跨 tenant 数据自动隔离.
"""

from __future__ import annotations

from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.api.cockpit_readers")


# ============================================================
# Mission Director reviews
# ============================================================


async def list_recent_mission_reviews(
    *,
    tenant_id: str,
    task_id: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List MissionAlignmentReview 行, 最新在前.

    Args:
        tenant_id: RLS tenant scope
        task_id: 可选, 限定单 task 的 reviews
        limit: max 行数 (1-100)

    Returns:
        list[dict] V7 §20 cockpit-friendly shape.
    """
    limit = max(1, min(100, limit))

    try:
        from sqlalchemy import select

        from kun.core.db import session_scope
        from kun.core.orm import MissionAlignmentReviewRow

        async with session_scope(tenant_id=tenant_id) as s:
            stmt = select(MissionAlignmentReviewRow).where(
                MissionAlignmentReviewRow.tenant_id == tenant_id,
            )
            if task_id is not None:
                stmt = stmt.where(MissionAlignmentReviewRow.task_id == task_id)
            stmt = stmt.order_by(
                MissionAlignmentReviewRow.reviewed_at.desc()
            ).limit(limit)
            rows = (await s.execute(stmt)).scalars().all()
    except Exception as e:
        log.warning(
            "cockpit_readers.mission_reviews_failed",
            tenant_id=tenant_id,
            task_id=task_id,
            error=f"{type(e).__name__}: {e}",
        )
        return []

    return [
        {
            "review_id": r.review_id,
            "task_id": r.task_id,
            "task_plan_version": r.task_plan_version,
            "reviewed_at": r.reviewed_at.isoformat(),
            "verdict": r.verdict,
            "alignment_score": float(r.alignment_score),
            "findings": list(r.findings or []),
            "info_gap_coverage": float(r.info_gap_coverage),
            "decomposition_coverage": float(r.decomposition_coverage),
            "evidence_coverage": float(r.evidence_coverage),
            "plan_change_proposed": bool(r.plan_change_proposed),
            "plan_change_proposal_id": r.plan_change_proposal_id,
        }
        for r in rows
    ]


# ============================================================
# Capability lifecycle transitions
# ============================================================


async def list_recent_lifecycle_transitions(
    *,
    tenant_id: str,
    capability_id: str | None = None,
    to_stage: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List LifecycleTransition 行, 最新在前 (V7 §15)."""
    limit = max(1, min(100, limit))

    try:
        from sqlalchemy import select

        from kun.core.db import session_scope
        from kun.core.orm import LifecycleTransitionRow

        async with session_scope(tenant_id=tenant_id) as s:
            stmt = select(LifecycleTransitionRow).where(
                LifecycleTransitionRow.tenant_id == tenant_id,
            )
            if capability_id is not None:
                stmt = stmt.where(
                    LifecycleTransitionRow.capability_id == capability_id
                )
            if to_stage is not None:
                stmt = stmt.where(
                    LifecycleTransitionRow.to_stage == to_stage
                )
            stmt = stmt.order_by(
                LifecycleTransitionRow.decided_at.desc()
            ).limit(limit)
            rows = (await s.execute(stmt)).scalars().all()
    except Exception as e:
        log.warning(
            "cockpit_readers.lifecycle_transitions_failed",
            tenant_id=tenant_id,
            capability_id=capability_id,
            error=f"{type(e).__name__}: {e}",
        )
        return []

    return [
        {
            "transition_id": r.transition_id,
            "capability_id": r.capability_id,
            "from_stage": r.from_stage,
            "to_stage": r.to_stage,
            "decided_at": r.decided_at.isoformat(),
            "decision_rationale": r.decision_rationale,
            "user_approval_ticket_id": r.user_approval_ticket_id,
            "evidence_refs": list(r.evidence_refs or []),
            "metrics_snapshot": dict(r.metrics_snapshot or {}),
        }
        for r in rows
    ]


# ============================================================
# Auditor reports
# ============================================================


async def list_recent_auditor_reports(
    *,
    tenant_id: str,
    audited_capability: str | None = None,
    risk_level: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List AuditorReport 行, 最新在前 (V7 §16.6)."""
    limit = max(1, min(100, limit))

    try:
        from sqlalchemy import select

        from kun.core.db import session_scope
        from kun.core.orm import AuditorReportRow

        async with session_scope(tenant_id=tenant_id) as s:
            stmt = select(AuditorReportRow).where(
                AuditorReportRow.tenant_id == tenant_id,
            )
            if audited_capability is not None:
                stmt = stmt.where(
                    AuditorReportRow.audited_capability == audited_capability
                )
            if risk_level is not None:
                stmt = stmt.where(AuditorReportRow.risk_level == risk_level)
            stmt = stmt.order_by(
                AuditorReportRow.audited_at.desc()
            ).limit(limit)
            rows = (await s.execute(stmt)).scalars().all()
    except Exception as e:
        log.warning(
            "cockpit_readers.auditor_reports_failed",
            tenant_id=tenant_id,
            audited_capability=audited_capability,
            error=f"{type(e).__name__}: {e}",
        )
        return []

    return [
        {
            "report_id": r.report_id,
            "audited_capability": r.audited_capability,
            "audited_at": r.audited_at.isoformat(),
            "auditor_provider": r.auditor_provider,
            "design_promise": r.design_promise,
            "real_code_path": r.real_code_path,
            "bypass_methods": list(r.bypass_methods or []),
            "min_repro_steps": r.min_repro_steps,
            "risk_level": r.risk_level,
            "must_fix": list(r.must_fix or []),
            "acceptance_tests": list(r.acceptance_tests or []),
            "allow_release": bool(r.allow_release),
            "rationale": r.rationale,
        }
        for r in rows
    ]


__all__ = [
    "list_recent_auditor_reports",
    "list_recent_lifecycle_transitions",
    "list_recent_mission_reviews",
]
