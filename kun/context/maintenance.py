"""NUO context maintenance: diagnose, compress, and forget stale assets.

This is the real execution side of "傩定期给 context / memory 瘦身": it can run
in dry-run mode for diagnosis, or mutate the AssetStore when explicitly asked.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.context.assets import AssetKind, LayeredAsset
from kun.context.storage import AssetStore, get_store
from kun.core.db import session_scope
from kun.core.events import emit
from kun.core.metrics import context_maintenance_findings_total
from kun.datamodel.events import Event, EventKind

log = logging.getLogger(__name__)

ActionKind = Literal[
    "keep",
    "compress",
    "soft_forget",
    "hard_delete",
    "duplicate",
    "compiler_review",
]


class ContextMaintenanceFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_id: str
    asset_kind: AssetKind
    action: ActionKind
    reason: str
    dry_run: bool


class ContextMaintenanceReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    dry_run: bool = True
    total_seen: int = 0
    compressed: int = 0
    soft_forgotten: int = 0
    hard_deleted: int = 0
    duplicate_candidates: int = 0
    compiler_review: int = 0
    kept: int = 0
    findings: list[ContextMaintenanceFinding] = Field(default_factory=list)


async def run_context_maintenance(
    *,
    tenant_id: str,
    dry_run: bool = True,
    max_assets: int = 500,
    compress_summary_over_chars: int = 1200,
    soft_forget_after_days: int = 30,
    hard_delete_after_days: int = 90,
    store: AssetStore | None = None,
) -> ContextMaintenanceReport:
    store = store or get_store()
    report = ContextMaintenanceReport(tenant_id=tenant_id, dry_run=dry_run)
    seen_summaries: set[tuple[str, str]] = set()
    now = datetime.now(UTC)
    for kind in _ASSET_KINDS:
        assets = await store.list(tenant_id=tenant_id, asset_kind=kind, limit=max_assets)
        for asset in assets:
            report.total_seen += 1
            age_days = max(0.0, (now - asset.last_accessed).total_seconds() / 86400)
            summary_key = (asset.asset_kind, (asset.l2_summary or "").strip().lower())
            if summary_key[1] and summary_key in seen_summaries:
                report.duplicate_candidates += 1
                report.findings.append(
                    _finding(asset, "duplicate", "same kind and identical summary", dry_run)
                )
                continue
            if summary_key[1]:
                seen_summaries.add(summary_key)

            compiler_reason = _compiler_review_reason(asset)
            if compiler_reason:
                report.compiler_review += 1
                report.findings.append(_finding(asset, "compiler_review", compiler_reason, dry_run))

            if (
                age_days >= hard_delete_after_days
                and asset.access_count == 0
                and asset.l1_metadata.get("tier") != "permanent"
            ):
                report.hard_deleted += 1
                report.findings.append(
                    _finding(
                        asset,
                        "hard_delete",
                        f"unused for {age_days:.0f} days and not permanent",
                        dry_run,
                    )
                )
                if not dry_run:
                    await store.delete(asset.asset_id, tenant_id=tenant_id)
                    await _emit_maintenance_event(tenant_id, asset, "hard_delete")
                continue

            if (
                age_days >= soft_forget_after_days
                and asset.access_count == 0
                and not asset.l1_metadata.get("soft_forgotten")
            ):
                report.soft_forgotten += 1
                report.findings.append(
                    _finding(asset, "soft_forget", f"unused for {age_days:.0f} days", dry_run)
                )
                if not dry_run:
                    asset.l1_metadata["soft_forgotten"] = True
                    asset.tags = sorted({*asset.tags, "soft_forgotten"})
                    await store.put(asset)
                    await _emit_maintenance_event(tenant_id, asset, "soft_forget")
                continue

            if asset.l2_summary and len(asset.l2_summary) > compress_summary_over_chars:
                report.compressed += 1
                report.findings.append(
                    _finding(
                        asset,
                        "compress",
                        f"L2 summary length {len(asset.l2_summary)} exceeds threshold",
                        dry_run,
                    )
                )
                if not dry_run:
                    asset.l1_metadata["compressed_from_chars"] = len(asset.l2_summary)
                    asset.l2_summary = _compress_summary(asset.l2_summary)
                    await store.put(asset)
                    await _emit_maintenance_event(tenant_id, asset, "compress")
                continue

            report.kept += 1
            if len(report.findings) < 50:
                report.findings.append(_finding(asset, "keep", "healthy", dry_run))
    _emit_metrics(report)
    return report


def _finding(
    asset: LayeredAsset,
    action: ActionKind,
    reason: str,
    dry_run: bool,
) -> ContextMaintenanceFinding:
    return ContextMaintenanceFinding(
        asset_id=asset.asset_id,
        asset_kind=asset.asset_kind,
        action=action,
        reason=reason,
        dry_run=dry_run,
    )


async def _emit_maintenance_event(tenant_id: str, asset: LayeredAsset, action: ActionKind) -> None:
    event_type: EventKind = "context.updated" if action == "compress" else "context.forgotten"
    try:
        async with session_scope(tenant_id=tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant_id,
                    event_type=event_type,
                    payload={
                        "asset_id": asset.asset_id,
                        "asset_kind": asset.asset_kind,
                        "maintenance_action": action,
                    },
                ),
            )
    except Exception:
        log.debug("context_maintenance.emit_failed", exc_info=True)


def _compress_summary(text: str, *, max_chars: int = 900) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20].rstrip() + " ... [compressed]"


def _compiler_review_reason(asset: LayeredAsset) -> str:
    meta = asset.l1_metadata or {}
    has_compiler_meta = "compiler_profile" in meta or str(meta.get("compiler") or "").startswith(
        "kun.compiler"
    )
    if not has_compiler_meta:
        return ""
    risk = meta.get("risk")
    if isinstance(risk, dict):
        flags = risk.get("flags")
        level = str(risk.get("level") or "")
        if level in {"medium", "high"} or (isinstance(flags, list) and flags):
            return f"compiler asset has risk={level or 'unknown'} flags={flags or []}"
    provenance = meta.get("provenance")
    if isinstance(provenance, dict) and not provenance.get("input_sha256"):
        return "compiler asset is missing input_sha256 provenance"
    profile = meta.get("compiler_profile")
    if isinstance(profile, dict):
        limitations = profile.get("limitations")
        if isinstance(limitations, list) and any(
            "placeholder" in str(item) for item in limitations
        ):
            return "compiler asset came from a limited/placeholder compiler profile"
    return ""


def _emit_metrics(report: ContextMaintenanceReport) -> None:
    dry_run = "true" if report.dry_run else "false"
    counts = {
        "compress": report.compressed,
        "soft_forget": report.soft_forgotten,
        "hard_delete": report.hard_deleted,
        "duplicate": report.duplicate_candidates,
        "compiler_review": report.compiler_review,
        "keep": report.kept,
    }
    for action, count in counts.items():
        if count > 0:
            context_maintenance_findings_total.labels(
                tenant_id=report.tenant_id,
                action=action,
                dry_run=dry_run,
            ).inc(count)


_ASSET_KINDS: tuple[AssetKind, ...] = (
    "memory",
    "knowledge",
    "methodology",
    "skill",
    "role_template",
    "task",
    "handoff",
)


__all__ = [
    "ContextMaintenanceFinding",
    "ContextMaintenanceReport",
    "run_context_maintenance",
]
