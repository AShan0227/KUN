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
from kun.context.importance import ImportanceScorer
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
    "compiler_recompile",
    "low_value",
    "stale_or_risky",
    "duplicate_merge",
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
    duplicate_merged: int = 0
    compiler_review: int = 0
    compiler_recompile_recommended: int = 0
    low_value_marked: int = 0
    stale_or_risky_marked: int = 0
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
    merge_duplicates: bool = False,
    store: AssetStore | None = None,
) -> ContextMaintenanceReport:
    store = store or get_store()
    report = ContextMaintenanceReport(tenant_id=tenant_id, dry_run=dry_run)
    seen_summaries: dict[tuple[str, str], str] = {}
    now = datetime.now(UTC)
    importance = ImportanceScorer()
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
                if not dry_run:
                    asset.l1_metadata["duplicate_candidate"] = True
                    asset.l1_metadata["duplicate_of"] = seen_summaries[summary_key]
                    asset.tags = sorted({*asset.tags, "duplicate_candidate"})
                    await store.put(asset)
                    await _emit_maintenance_event(tenant_id, asset, "duplicate")
                continue
            if summary_key[1]:
                seen_summaries[summary_key] = asset.asset_id

            compiler_reason = _compiler_review_reason(asset)
            compiler_quality = _compiler_quality(asset)
            if compiler_reason:
                report.compiler_review += 1
                report.findings.append(_finding(asset, "compiler_review", compiler_reason, dry_run))
                if not dry_run:
                    asset.l1_metadata["compiler_review_required"] = True
                    asset.l1_metadata["compiler_review_reason"] = compiler_reason
                    asset.tags = sorted({*asset.tags, "compiler_review_required"})
                    await store.put(asset)
                    await _emit_maintenance_event(tenant_id, asset, "compiler_review")
            if compiler_quality is not None:
                if not dry_run:
                    asset.l1_metadata["compiler_quality_score"] = compiler_quality.score
                    asset.l1_metadata["compiler_quality_reasons"] = compiler_quality.reasons
                if compiler_quality.recompile_recommended:
                    report.compiler_recompile_recommended += 1
                    report.findings.append(
                        _finding(
                            asset,
                            "compiler_recompile",
                            compiler_quality.recompile_reason,
                            dry_run,
                        )
                    )
                    if not dry_run:
                        asset.l1_metadata["compiler_recompile_recommended"] = True
                        asset.l1_metadata["compiler_recompile_reason"] = (
                            compiler_quality.recompile_reason
                        )
                        asset.tags = sorted({*asset.tags, "compiler_recompile_recommended"})
                        await store.put(asset)
                        await _emit_maintenance_event(
                            tenant_id,
                            asset,
                            "compiler_recompile",
                        )
                elif not dry_run:
                    await store.put(asset)

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

            fade_score = importance.score(
                asset=asset,
                query=None,
                now=now,
                weights_override={
                    "semantic": 0.0,
                    "frequency": 0.45,
                    "recency": 0.45,
                    "dependency": 0.05,
                    "pin": 0.05,
                },
            )
            governance_marks = _governance_label_reasons(
                asset,
                age_days=age_days,
                fade_score=fade_score.overall,
            )
            if governance_marks:
                changed = False
                for label, reason in governance_marks:
                    if label == "low_value":
                        report.low_value_marked += 1
                    else:
                        report.stale_or_risky_marked += 1
                    report.findings.append(_finding(asset, label, reason, dry_run))
                    if not dry_run and label not in set(asset.tags):
                        asset.l1_metadata[label] = True
                        asset.l1_metadata[f"{label}_reason"] = reason
                        asset.l1_metadata["fade_score"] = round(fade_score.overall, 4)
                        asset.l1_metadata["fade_score_rationale"] = fade_score.rationale
                        asset.tags = sorted({*asset.tags, label})
                        changed = True
                if changed:
                    await store.put(asset)
                    await _emit_maintenance_event(tenant_id, asset, "stale_or_risky")

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
    if merge_duplicates and not dry_run:
        from kun.context.deduplicate import DuplicateAssetMerger

        merge_report = await DuplicateAssetMerger(store=store).merge_duplicates(
            tenant_id=tenant_id,
            dry_run=False,
            max_assets=max_assets,
        )
        report.duplicate_merged = merge_report.merged
        for result in merge_report.results:
            if result.status == "merged":
                merged_asset = await store.get(result.asset_id, tenant_id=tenant_id)
                if merged_asset is not None:
                    report.findings.append(
                        _finding(
                            merged_asset,
                            "duplicate_merge",
                            f"merged into {result.canonical_asset_id}",
                            dry_run,
                        )
                    )
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
    event_type: EventKind = (
        "context.forgotten" if action in {"soft_forget", "hard_delete"} else "context.updated"
    )
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


class _CompilerQuality(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score: float
    reasons: list[str]
    recompile_recommended: bool = False
    recompile_reason: str = ""


def _compiler_quality(asset: LayeredAsset) -> _CompilerQuality | None:
    meta = asset.l1_metadata or {}
    has_compiler_meta = "compiler_profile" in meta or str(meta.get("compiler") or "").startswith(
        "kun.compiler"
    )
    if not has_compiler_meta:
        return None

    score = 1.0
    reasons: list[str] = []
    risk = meta.get("risk")
    if isinstance(risk, dict):
        level = str(risk.get("level") or "low")
        flags = risk.get("flags")
        if level == "high":
            score -= 0.35
            reasons.append("risk_high")
        elif level == "medium":
            score -= 0.20
            reasons.append("risk_medium")
        if isinstance(flags, list) and flags:
            score -= min(0.25, 0.05 * len(flags))
            reasons.extend(f"risk_flag:{flag}" for flag in flags[:5])

    provenance = meta.get("provenance")
    if isinstance(provenance, dict) and not provenance.get("input_sha256"):
        score -= 0.25
        reasons.append("missing_input_sha256")

    profile = meta.get("compiler_profile")
    if isinstance(profile, dict):
        limitations = profile.get("limitations")
        if isinstance(limitations, list):
            for limitation in limitations[:8]:
                limitation_text = str(limitation).lower()
                if any(
                    needle in limitation_text
                    for needle in ("placeholder", "ocr", "audio", "office", "unavailable")
                ):
                    score -= 0.15
                    reasons.append(f"limitation:{limitation}")

    text = asset.l2_summary or ""
    if len(text.strip()) < 20:
        score -= 0.25
        reasons.append("summary_too_short")
    if "text extraction unavailable" in text.lower():
        score -= 0.25
        reasons.append("text_extraction_unavailable")
    if _looks_like_encoding_noise(text):
        score -= 0.20
        reasons.append("encoding_noise")

    score = round(max(0.0, min(1.0, score)), 3)
    recompile_recommended = score < 0.65 or any(
        reason.startswith("limitation:") or reason in {"text_extraction_unavailable"}
        for reason in reasons
    )
    recompile_reason = (
        f"compiler_quality_score={score:.2f}; reasons={','.join(reasons[:6]) or 'none'}"
        if recompile_recommended
        else ""
    )
    return _CompilerQuality(
        score=score,
        reasons=reasons,
        recompile_recommended=recompile_recommended,
        recompile_reason=recompile_reason,
    )


def _looks_like_encoding_noise(text: str) -> bool:
    if not text:
        return False
    noisy = sum(text.count(ch) for ch in ("�", "\x00", "\ufffd"))
    return noisy >= 3 or noisy / max(1, len(text)) > 0.02


def _governance_label_reasons(
    asset: LayeredAsset,
    *,
    age_days: float,
    fade_score: float,
) -> list[tuple[Literal["low_value", "stale_or_risky"], str]]:
    """Create explicit NUO governance labels for future sparse recall.

    ContextPacker already consumes ``low_value`` and ``stale_or_risky``.  This
    function is the missing producer side: it makes decay/risk decisions
    auditable instead of leaving them as invisible ranking math.
    """

    if asset.l1_metadata.get("tier") == "permanent":
        return []

    labels = {str(tag).lower() for tag in asset.tags}
    reasons: list[tuple[Literal["low_value", "stale_or_risky"], str]] = []
    if (
        "low_value" not in labels
        and asset.access_count == 0
        and age_days >= 7
        and fade_score < 0.25
    ):
        reasons.append(
            (
                "low_value",
                (
                    f"fade_score={fade_score:.2f}; unused for {age_days:.0f} days; "
                    "downrank before stronger forget/delete actions"
                ),
            )
        )

    risk_level = _asset_risk_level(asset)
    compiler_quality = asset.l1_metadata.get("compiler_quality_score")
    poor_compiler_quality = isinstance(compiler_quality, int | float) and compiler_quality < 0.5
    if (
        "stale_or_risky" not in labels
        and age_days >= 3
        and (
            risk_level in {"medium", "high", "critical"}
            or poor_compiler_quality
            or asset.l1_metadata.get("compiler_recompile_recommended") is True
            or asset.l1_metadata.get("compiler_review_required") is True
        )
    ):
        reasons.append(
            (
                "stale_or_risky",
                (
                    f"fade_score={fade_score:.2f}; risk={risk_level}; "
                    f"compiler_quality={compiler_quality if compiler_quality is not None else 'unknown'}"
                ),
            )
        )
    return reasons


def _asset_risk_level(asset: LayeredAsset) -> str:
    risk = asset.l1_metadata.get("risk")
    if isinstance(risk, dict):
        return str(risk.get("level") or "low").lower()
    return str(asset.l1_metadata.get("risk_level") or "low").lower()


def _emit_metrics(report: ContextMaintenanceReport) -> None:
    dry_run = "true" if report.dry_run else "false"
    counts = {
        "compress": report.compressed,
        "soft_forget": report.soft_forgotten,
        "hard_delete": report.hard_deleted,
        "duplicate": report.duplicate_candidates,
        "duplicate_merge": report.duplicate_merged,
        "compiler_review": report.compiler_review,
        "compiler_recompile": report.compiler_recompile_recommended,
        "low_value": report.low_value_marked,
        "stale_or_risky": report.stale_or_risky_marked,
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
