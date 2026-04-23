"""Experiments SDK (ADR-009) — 带状态的 Feature Flag / AB / 进化实验.

状态机: draft → shadow → canary → rollout → stable (可 rolled_back)

用法 (在业务代码里):

    async with experiment("new_router_rule_v2") as variant:
        if variant == "treatment":
            result = new_route(task)
        else:
            result = old_route(task)
        # metrics 自动记录

状态流转 (CLI / admin):
    exp.promote("shadow")
    exp.promote("canary", rollout_percent=1)
    exp.rollback()
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select, update

from kun.core.db import session_scope
from kun.core.logging import get_logger
from kun.core.orm import ExperimentRow
from kun.core.tenancy import current_tenant

log = get_logger("kun.engineering.experiments")

ExperimentStatus = Literal["draft", "shadow", "canary", "rollout", "stable", "rolled_back"]
ExperimentKind = Literal["skill", "route_rule", "prompt", "model_choice", "skill_variant"]
Variant = Literal["control", "treatment"]


def pick_variant(
    experiment_id: str,
    subject_key: str,
    rollout_percent: int,
) -> Variant:
    """Pick variant by consistent hash (subject_key, experiment_id).

    Same subject_key always gets the same variant at a given rollout_percent.
    When rollout_percent grows, new subjects may flip into treatment,
    but ones already in treatment stay.
    """
    if rollout_percent <= 0:
        return "control"
    if rollout_percent >= 100:
        return "treatment"
    digest = hashlib.blake2b(
        f"{experiment_id}|{subject_key}".encode(),
        digest_size=4,
    ).digest()
    bucket = int.from_bytes(digest, "big") % 100
    return "treatment" if bucket < rollout_percent else "control"


async def get_experiment(experiment_id: str) -> ExperimentRow | None:
    tenant = current_tenant()
    async with session_scope() as s:
        row = (
            await s.execute(
                select(ExperimentRow).where(
                    ExperimentRow.id == experiment_id,
                    ExperimentRow.tenant_id == tenant.tenant_id,
                )
            )
        ).scalar_one_or_none()
    return row


@asynccontextmanager
async def experiment(
    experiment_id: str,
    *,
    subject_key: str | None = None,
) -> AsyncIterator[Variant]:
    """Open an experiment context for a task / request."""
    tenant = current_tenant()
    if subject_key is None:
        subject_key = tenant.user_id or tenant.tenant_id

    row = await get_experiment(experiment_id)
    if row is None or row.status in ("draft", "rolled_back"):
        # No active experiment → control.
        yield "control"
        return

    variant = pick_variant(experiment_id, subject_key, int(row.rollout_percent))
    if row.status == "shadow":
        # Shadow: always behave as control, but emit events under treatment label
        log.debug("experiment.shadow", id=experiment_id, subject=subject_key, variant=variant)
        yield "control"
        return

    log.debug(
        "experiment.chosen",
        id=experiment_id,
        subject=subject_key,
        variant=variant,
        rollout=row.rollout_percent,
    )
    yield variant


async def create(
    experiment_id: str,
    kind: ExperimentKind,
    *,
    control_variant: dict[str, Any] | None = None,
    treatment_variant: dict[str, Any] | None = None,
    guardrails: dict[str, Any] | None = None,
) -> None:
    """Create an experiment in 'draft'."""
    tenant = current_tenant()
    async with session_scope() as s:
        s.add(
            ExperimentRow(
                id=experiment_id,
                tenant_id=tenant.tenant_id,
                kind=kind,
                status="draft",
                rollout_percent=0,
                control_variant=control_variant,
                treatment_variant=treatment_variant,
                guardrails=guardrails or {},
                metrics={},
                created_at=datetime.now(UTC),
            )
        )
    log.info("experiment.created", id=experiment_id, kind=kind)


async def promote(
    experiment_id: str,
    target: ExperimentStatus,
    *,
    rollout_percent: int = 0,
) -> None:
    """Move an experiment forward in the state machine.

    Valid transitions:
      draft → shadow → canary → rollout → stable
      any → rolled_back
    """
    transitions: dict[ExperimentStatus, tuple[ExperimentStatus, ...]] = {
        "draft": ("shadow", "rolled_back"),
        "shadow": ("canary", "rolled_back"),
        "canary": ("rollout", "rolled_back", "stable"),
        "rollout": ("stable", "rolled_back"),
        "stable": ("rolled_back",),
        "rolled_back": (),
    }
    tenant = current_tenant()
    async with session_scope() as s:
        row = (
            await s.execute(
                select(ExperimentRow).where(
                    ExperimentRow.id == experiment_id,
                    ExperimentRow.tenant_id == tenant.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            raise RuntimeError(f"experiment not found: {experiment_id}")
        current: ExperimentStatus = row.status  # type: ignore[assignment]
        if target not in transitions[current]:
            raise RuntimeError(f"invalid transition: {current} → {target}")

        await s.execute(
            update(ExperimentRow)
            .where(ExperimentRow.id == experiment_id)
            .values(
                status=target,
                rollout_percent=rollout_percent if target in ("canary", "rollout") else 0,
                promoted_at=datetime.now(UTC),
            )
        )
    log.info("experiment.promoted", id=experiment_id, from_=current, to=target)


async def rollback(experiment_id: str) -> None:
    """Shortcut for promote(..., 'rolled_back')."""
    await promote(experiment_id, "rolled_back")
