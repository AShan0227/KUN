"""DB writer adapter for Mission Director — V7 §9.7 Phase X.B 接真 DB.

Phase X.A 在 kun/agents/mission_director/service.py 给了 service + frozen IO
+ emitter callback, 但 emitter 默认 None — service 跑完不落 DB.
本模块给 emitter 提供真实现 (写 mission_alignment_reviews / plan_change_proposals
两张表), 加 factory 让 caller (daemon / Orchestrator) 一行装配.

设计 (跟 plan_review_db.py 风格一致):
  1. 纯 dict → Row 的翻译 (无业务逻辑)
  2. 在 session_scope 里 add + flush
  3. factory 返回 emitter callable, 给 MissionDirectorService 注入

V7 §10.3.3 决策权 3 档不变量 (PlanChangeProposal):
  high severity → user_approval_required=True 强制 (DB CHECK constraint 兜底)
  这里只是写入, 不强校验 — proposal 来自 service 时本来就保证了, DB 是双保险.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from kun.agents.mission_director.service import (
    MissionAlignmentReview,
    PlanChangeProposal,
)
from kun.core.logging import get_logger

log = get_logger("kun.integration.mission_director_db")


# ============================================================
# 1) MissionAlignmentReview writer
# ============================================================


async def write_mission_review(
    *,
    tenant_id: str,
    review: MissionAlignmentReview,
) -> str:
    """Persist a MissionAlignmentReview to mission_alignment_reviews.

    Returns review_id (echo of input, not freshly minted — Mission Director
    service already mints with new_id("plan_review") which gives 'mar-...' style
    or 'pr-...' depending on prefix; we don't rewrite it here).

    Caller responsibility: ensure tenant_id is set on the postgres session
    (app.tenant_id GUC) before calling — session_scope(tenant_id=...) does this.
    """
    # Imported inside the function so monkeypatch on
    # "kun.core.db.session_scope" / "kun.core.orm.MissionAlignmentReviewRow"
    # reaches us at call time (consistent with plan_review_db.py pattern).
    from kun.core.db import session_scope
    from kun.core.orm import MissionAlignmentReviewRow

    payload: dict[str, Any] = review.to_row_payload(tenant_id)
    row = MissionAlignmentReviewRow(**payload)

    async with session_scope(tenant_id=tenant_id) as session:
        session.add(row)
        await session.flush()

    log.info(
        "mission_director.review_persisted",
        tenant_id=tenant_id,
        review_id=review.review_id,
        task_id=review.task_id,
        verdict=review.verdict.value,
        alignment_score=round(review.alignment_score, 3),
    )
    return review.review_id


# ============================================================
# 2) PlanChangeProposal writer
# ============================================================


async def write_plan_change_proposal(
    *,
    tenant_id: str,
    proposal: PlanChangeProposal,
) -> str:
    """Persist a PlanChangeProposal to plan_change_proposals.

    Returns proposal_id (echo of input).

    Note: DB CheckConstraint 'pcp_high_severity_needs_approval' enforces that
    high-severity proposals must have user_approval_required=True. Service
    layer already enforces this (V7 §10.3.3) but DB is the bottom-of-stack
    invariant — if a writer ever drifts, the insert raises.
    """
    from kun.core.db import session_scope
    from kun.core.orm import PlanChangeProposalRow

    payload: dict[str, Any] = proposal.to_row_payload(tenant_id)
    row = PlanChangeProposalRow(**payload)

    async with session_scope(tenant_id=tenant_id) as session:
        session.add(row)
        await session.flush()

    log.info(
        "mission_director.proposal_persisted",
        tenant_id=tenant_id,
        proposal_id=proposal.proposal_id,
        task_id=proposal.task_id,
        severity=proposal.severity.value,
        change_type=proposal.change_type,
        user_approval_required=proposal.user_approval_required,
    )
    return proposal.proposal_id


# ============================================================
# 3) Factories — bind tenant_id, return emitter for service injection
# ============================================================


MissionReviewEmitter = Callable[[MissionAlignmentReview], Awaitable[None]]
PlanChangeProposalEmitter = Callable[[PlanChangeProposal], Awaitable[None]]


def make_mission_review_emitter(tenant_id: str) -> MissionReviewEmitter:
    """Return an async emitter with tenant_id pre-bound for service injection.

    Usage::

        from kun.agents.mission_director import MissionDirectorService
        from kun.integration.mission_director_db import (
            make_mission_review_emitter,
            make_plan_change_proposal_emitter,
        )

        service = MissionDirectorService(
            review_emitter=make_mission_review_emitter("tenant-a"),
            proposal_emitter=make_plan_change_proposal_emitter("tenant-a"),
        )
        await service.review_mission(...)  # 自动落 DB
    """

    async def _emit(review: MissionAlignmentReview) -> None:
        await write_mission_review(tenant_id=tenant_id, review=review)

    return _emit


def make_plan_change_proposal_emitter(
    tenant_id: str,
) -> PlanChangeProposalEmitter:
    """Return an async emitter with tenant_id pre-bound for service injection."""

    async def _emit(proposal: PlanChangeProposal) -> None:
        await write_plan_change_proposal(tenant_id=tenant_id, proposal=proposal)

    return _emit


__all__ = [
    "MissionReviewEmitter",
    "PlanChangeProposalEmitter",
    "make_mission_review_emitter",
    "make_plan_change_proposal_emitter",
    "write_mission_review",
    "write_plan_change_proposal",
]
