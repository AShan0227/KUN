"""DB writer adapter for CapabilityLifecycle — V7 §15 Phase X.B 接真 DB.

Phase X.A 在 kun/governance/capability_lifecycle.py 给了 service + frozen
LifecycleTransition + transition_emitter callback, 但 emitter 默认 None.
本模块给 emitter 提供真实现 (写 lifecycle_transitions 表), 加 factory 让
caller (daemon / 启 Qi runner / runtime gate) 一行装配.

设计跟 mission_director_db.py 一致:
  1. write_lifecycle_transition: 纯 LifecycleTransition → Row 翻译 + flush
  2. make_lifecycle_transition_emitter: factory 闭包 tenant_id
  3. 不强校验业务规则 — service 已经做了 (V7 §12.2 邻接/三证据/user_approval),
     DB CHECK constraint 是双保险

LifecycleTransition 没有 to_row_payload (frozen IO 没加 method), 翻译在这里做.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from kun.core.logging import get_logger
from kun.governance.capability_lifecycle import LifecycleTransition

log = get_logger("kun.integration.capability_lifecycle_db")


# ============================================================
# LifecycleTransition → Row 翻译 (LifecycleTransition 自身没 to_row_payload)
# ============================================================


def _transition_to_row_payload(
    *, tenant_id: str, transition: LifecycleTransition
) -> dict[str, Any]:
    return {
        "tenant_id": tenant_id,
        "transition_id": transition.transition_id,
        "capability_id": transition.capability_id,
        "from_stage": transition.from_stage.value,
        "to_stage": transition.to_stage.value,
        "decided_at": transition.decided_at,
        "decision_rationale": transition.decision_rationale,
        "user_approval_ticket_id": transition.user_approval_ticket_id,
        "evidence_refs": list(transition.evidence_refs),
        "metrics_snapshot": dict(transition.metrics_snapshot),
    }


# ============================================================
# Writer
# ============================================================


async def write_lifecycle_transition(
    *,
    tenant_id: str,
    transition: LifecycleTransition,
) -> str:
    """Persist a LifecycleTransition to lifecycle_transitions table.

    Returns transition_id (echo of input — service already minted with
    new_id("lifecycle_transition") which gives 'lct-...' prefix).

    Caller responsibility: ensure tenant_id is set on the Postgres session
    GUC before calling — session_scope(tenant_id=...) does this.

    Note: DB CHECK constraints are the bottom-of-stack invariants. Service
    layer (CapabilityLifecycleService) enforces the same rules upstream
    (邻接 + 三证据 + user_approval), but if a caller ever bypasses the
    service and constructs LifecycleTransition manually, the DB still
    catches:
      - to_stage='production' without user_approval_ticket_id → raises
      - to_stage='replay' with empty evidence_refs → raises
      - unknown stage value → raises
    """
    # Imported inside the function so monkeypatch on
    # "kun.core.db.session_scope" / "kun.core.orm.LifecycleTransitionRow"
    # reaches us at call time (consistent with plan_review_db /
    # mission_director_db pattern).
    from kun.core.db import session_scope
    from kun.core.orm import LifecycleTransitionRow

    payload = _transition_to_row_payload(
        tenant_id=tenant_id, transition=transition
    )
    row = LifecycleTransitionRow(**payload)

    async with session_scope(tenant_id=tenant_id) as session:
        session.add(row)
        await session.flush()

    log.info(
        "lifecycle.transition_persisted",
        tenant_id=tenant_id,
        transition_id=transition.transition_id,
        capability_id=transition.capability_id,
        from_stage=transition.from_stage.value,
        to_stage=transition.to_stage.value,
        has_user_approval=bool(transition.user_approval_ticket_id),
        n_evidence=len(transition.evidence_refs),
    )
    return transition.transition_id


# ============================================================
# Factory — bind tenant_id, return emitter for service injection
# ============================================================


LifecycleTransitionEmitter = Callable[[LifecycleTransition], Awaitable[None]]


def make_lifecycle_transition_emitter(
    tenant_id: str,
) -> LifecycleTransitionEmitter:
    """Return an async emitter with tenant_id pre-bound for service injection.

    Usage::

        from kun.governance.capability_lifecycle import (
            CapabilityLifecycleService,
        )
        from kun.integration.capability_lifecycle_db import (
            make_lifecycle_transition_emitter,
        )

        service = CapabilityLifecycleService(
            transition_emitter=make_lifecycle_transition_emitter("tenant-a"),
        )
        await service.transition(
            capability_id="cap-foo",
            from_stage=Stage.CANDIDATE,
            to_stage=Stage.REPLAY,
            evidence_refs=[
                "strategy_replay_report:rr-1",
                "process_audit:pa-1",
                "capability_candidate:cc-1",
            ],
        )  # 自动落 DB
    """

    async def _emit(transition: LifecycleTransition) -> None:
        await write_lifecycle_transition(
            tenant_id=tenant_id, transition=transition
        )

    return _emit


__all__ = [
    "LifecycleTransitionEmitter",
    "make_lifecycle_transition_emitter",
    "write_lifecycle_transition",
]
