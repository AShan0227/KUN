"""傩 · 能力画像面板 — 让 capability_card 的数据真正"被看见"。

This is the read side of the capability_writeback feedback loop. The
orchestrator writes per-(model, task_type) outcomes after every task; this
endpoint reads them back so:

  - Operators can see which models perform best on which task types
  - The router can (eventually) consult these stats to pick a candidate
  - The hand-written ``playbook.yaml`` priors can be confirmed/refuted
    by real evidence

Pairs with the model playbook (``kun/interface/llm/playbook.yaml``).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from kun.core.db import session_scope
from kun.core.orm import CapabilityCardRow
from kun.core.tenancy import current_tenant
from kun.interface.llm.playbook import ModelEntry, get_playbook

router = APIRouter()


class CapabilitySnapshot(BaseModel):
    """One model's measured profile across all task types."""

    entity_type: str
    entity_id: str
    display_name: str = ""
    family: str = ""
    maturity: str = "cold_start"
    overall_reliability: float = 0.0
    primary_strength: str = ""
    primary_weakness: str = ""
    capabilities: list[dict[str, Any]] = Field(default_factory=list)
    playbook_notes: str = Field(
        default="", description="Hand-written playbook entry, if registered"
    )


class CapabilityPanel(BaseModel):
    """Top-level response for /nuo/capability/summary."""

    tenant_id: str
    snapshots: list[CapabilitySnapshot]
    playbook_unregistered: list[str] = Field(
        default_factory=list,
        description="Models tracked in capability_cards but missing from playbook.yaml",
    )


def _entry_for(model_id: str) -> ModelEntry | None:
    return get_playbook().by_id(model_id)


def _capabilities_summary(card_json: dict[str, Any]) -> list[dict[str, Any]]:
    """Project a CapabilityCard JSON into a stable summary the UI can render."""
    if not isinstance(card_json, dict):
        return []
    out: list[dict[str, Any]] = []
    for cap in card_json.get("capabilities") or []:
        if not isinstance(cap, dict):
            continue
        stats = cap.get("stats") or {}
        quality = cap.get("quality") or {}
        out.append(
            {
                "task_type": cap.get("task_type", "unknown"),
                "total_invocations": int(stats.get("total_invocations") or 0),
                "success_rate": round(float(stats.get("success_rate") or 0.0), 4),
                "avg_cost_usd": round(float(stats.get("avg_cost_usd") or 0.0), 6),
                "avg_duration_sec": round(float(stats.get("avg_duration_sec") or 0.0), 3),
                "rubric_score": round(float(quality.get("avg_rubric_score") or 0.0), 3),
                "surprise_rate": round(float(quality.get("surprise_rate") or 0.0), 3),
            }
        )
    # Sort by call volume so the most-exercised task types come first
    out.sort(key=lambda r: r["total_invocations"], reverse=True)
    return out


@router.get("/summary", response_model=CapabilityPanel)
async def capability_summary(
    entity_type: str = Query(default="model", description="model | role_template | skill"),
) -> CapabilityPanel:
    """Per-model (or per-role) capability picture for the current tenant.

    Combines hand-written playbook priors with measured capability_card data.
    """
    tenant = current_tenant()
    async with session_scope() as s:
        rows = (
            (
                await s.execute(
                    select(CapabilityCardRow)
                    .where(CapabilityCardRow.tenant_id == tenant.tenant_id)
                    .where(CapabilityCardRow.entity_type == entity_type)
                    .order_by(CapabilityCardRow.overall_reliability.desc())
                )
            )
            .scalars()
            .all()
        )

    snapshots: list[CapabilitySnapshot] = []
    unregistered: list[str] = []

    for row in rows:
        playbook_entry = _entry_for(row.entity_id) if entity_type == "model" else None
        if entity_type == "model" and playbook_entry is None:
            unregistered.append(row.entity_id)

        snapshots.append(
            CapabilitySnapshot(
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                display_name=playbook_entry.display_name if playbook_entry else row.entity_id,
                family=playbook_entry.family if playbook_entry else "",
                maturity=row.maturity,
                overall_reliability=float(row.overall_reliability or 0.0),
                primary_strength=row.primary_strength or "",
                primary_weakness=row.primary_weakness or "",
                capabilities=_capabilities_summary(row.card_json or {}),
                playbook_notes=playbook_entry.notes if playbook_entry else "",
            )
        )

    return CapabilityPanel(
        tenant_id=tenant.tenant_id,
        snapshots=snapshots,
        playbook_unregistered=unregistered,
    )


@router.get("/playbook")
async def capability_playbook() -> dict[str, Any]:
    """Return the hand-written model playbook as JSON (for NUO frontend / tools)."""
    pb = get_playbook()
    return {
        "version": pb.version,
        "updated_at": pb.updated_at,
        "models": [
            {
                "model_id": e.model_id,
                "family": e.family,
                "display_name": e.display_name,
                "tier_default": e.tier_default,
                "context_tokens": e.context_tokens,
                "strengths": list(e.strengths),
                "weaknesses": list(e.weaknesses),
                "notes": e.notes,
                "pricing_usd_per_mtok": e.pricing_usd_per_mtok,
                "subscription_quota": e.subscription_quota,
                "audience_default": e.audience_default,
            }
            for e in pb.entries
        ],
    }
