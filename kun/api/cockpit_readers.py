"""V7 §20 cockpit DB readers — Phase X.B 接真 DB.

Phase E.A 给 cockpit endpoints 返 stub. Phase X.B 这条把 endpoint 切到真
DB 查 (mission_alignment_reviews / lifecycle_transitions / auditor_reports
三张表都已在 alembic 0014/0015/0016 落地).

设计:
1. 每个 reader 是纯函数: (tenant_id, limit, **filters) → ReaderResult
2. Rows: list[dict] V7 §20 cockpit-friendly shape (中英混合, 普通用户可读)
3. 不抛 — 即使 DB 不可用也返 ReaderResult(rows=[], error_kind=<classified>).
   理由: cockpit 是 dashboard, 优雅降级比硬错更友好.
4. **V7 §16.6 MF-6**: error_kind 区分 "tenant 真没数据" vs "DB 异常".
   - error_kind=None + rows=[]  ⇒ tenant 真无数据 (honest empty)
   - error_kind=<str> + rows=[] ⇒ DB 出错, 数据未读到 (cockpit 必须surface警告)
5. session_scope 用 in-function import 便于 monkeypatch (跟 writer 风格一致).

Tenant 处理:
- caller (cockpit endpoint) 必传 tenant_id, 这层不做 default fallback.
- session_scope(tenant_id=...) 自动设 RLS GUC, 跨 tenant 数据自动隔离.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.api.cockpit_readers")


# ============================================================
# ReaderResult — V7 §16.6 MF-6 (error_kind discrimination)
# ============================================================


@dataclass(frozen=True)
class ReaderResult:
    """Discriminated result for cockpit DB readers.

    - ``rows`` is always a list (empty on failure).
    - ``error_kind`` is None when the read completed successfully; otherwise
      classifies the failure into a small enum-like vocabulary so cockpit
      endpoints can surface honest signal:
        * "db_connection_refused" — Postgres unreachable
        * "table_not_found"       — alembic upgrade head not run for this table
        * "permission_denied"     — RLS / role config wrong
        * "session_scope_failure" — kun.core.db.session_scope failed early
        * "unknown_db_error"      — anything else from SQLAlchemy / asyncpg
    - ``error_detail`` is the short ``f"{type}: {msg}"`` for log/audit; never
      include stack traces (those go to log records).
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    error_kind: str | None = None
    error_detail: str | None = None

    @property
    def is_ok(self) -> bool:
        return self.error_kind is None

    @property
    def is_empty_honest(self) -> bool:
        """True when read succeeded but tenant truly has no data."""
        return self.error_kind is None and not self.rows


def _classify_db_error(exc: BaseException) -> str:
    """Map exceptions to error_kind enum value (MF-6 vocabulary)."""
    name = type(exc).__name__
    msg = str(exc).lower()
    if "connection refused" in msg or "could not connect" in msg:
        return "db_connection_refused"
    if "undefinedtableerror" in name.lower() or "does not exist" in msg:
        return "table_not_found"
    if "insufficientprivilegeerror" in name.lower() or "permission denied" in msg:
        return "permission_denied"
    if name in {"OperationalError", "InterfaceError"}:
        return "db_connection_refused"
    if name in {"ProgrammingError", "DataError"}:
        # ProgrammingError covers UndefinedTableError etc on some paths
        if "does not exist" in msg or "undefined" in msg:
            return "table_not_found"
        return "unknown_db_error"
    return "unknown_db_error"


# ============================================================
# Mission Director reviews
# ============================================================


async def list_recent_mission_reviews(
    *,
    tenant_id: str,
    task_id: str | None = None,
    limit: int = 20,
) -> ReaderResult:
    """List MissionAlignmentReview 行, 最新在前 — returns ReaderResult.

    Args:
        tenant_id: RLS tenant scope
        task_id: 可选, 限定单 task 的 reviews
        limit: max 行数 (1-100)
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
        kind = _classify_db_error(e)
        log.warning(
            "cockpit_readers.mission_reviews_failed",
            tenant_id=tenant_id,
            task_id=task_id,
            error_kind=kind,
            error=f"{type(e).__name__}: {e}",
        )
        return ReaderResult(
            rows=[], error_kind=kind, error_detail=f"{type(e).__name__}: {e}"
        )

    return ReaderResult(
        rows=[
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
    )


# ============================================================
# Capability lifecycle transitions
# ============================================================


async def list_recent_lifecycle_transitions(
    *,
    tenant_id: str,
    capability_id: str | None = None,
    to_stage: str | None = None,
    limit: int = 20,
) -> ReaderResult:
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
        kind = _classify_db_error(e)
        log.warning(
            "cockpit_readers.lifecycle_transitions_failed",
            tenant_id=tenant_id,
            capability_id=capability_id,
            error_kind=kind,
            error=f"{type(e).__name__}: {e}",
        )
        return ReaderResult(
            rows=[], error_kind=kind, error_detail=f"{type(e).__name__}: {e}"
        )

    return ReaderResult(
        rows=[
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
    )


# ============================================================
# Auditor reports
# ============================================================


async def list_recent_auditor_reports(
    *,
    tenant_id: str,
    audited_capability: str | None = None,
    risk_level: str | None = None,
    limit: int = 20,
) -> ReaderResult:
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
        kind = _classify_db_error(e)
        log.warning(
            "cockpit_readers.auditor_reports_failed",
            tenant_id=tenant_id,
            audited_capability=audited_capability,
            error_kind=kind,
            error=f"{type(e).__name__}: {e}",
        )
        return ReaderResult(
            rows=[], error_kind=kind, error_detail=f"{type(e).__name__}: {e}"
        )

    return ReaderResult(
        rows=[
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
    )


__all__ = [
    "ReaderResult",
    "list_recent_auditor_reports",
    "list_recent_lifecycle_transitions",
    "list_recent_mission_reviews",
]
