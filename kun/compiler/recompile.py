"""Recompile low-quality compiler assets into fresh LayeredAssets.

This is the execution side of NUO's compiler diagnosis loop.  Context
maintenance can mark assets as `compiler_recompile_recommended`; this module
turns those findings into a safe, auditable action.

Design rules:
- dry-run by default;
- never delete or overwrite the original asset;
- only re-read local files under explicit allowed roots;
- URL fetch still goes through the compiler's HTTPS allowlist gate.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.compiler.ingestion import material_to_layered_asset
from kun.compiler.material import LightweightMaterialCompiler
from kun.context.assets import LayeredAsset
from kun.context.storage import AssetStore, get_store

RecompileStatus = Literal["planned", "stored", "skipped", "error"]


class RecompileCandidateResult(BaseModel):
    """Result for one compiler recompile candidate."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    status: RecompileStatus
    reason: str
    dry_run: bool
    source_type: str | None = None
    source_uri: str | None = None
    new_asset_id: str | None = None
    original_marked_soft_forgotten: bool = False


class RecompileReport(BaseModel):
    """Aggregate report for a recompile pass."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    dry_run: bool
    scanned: int = 0
    candidates: int = 0
    planned: int = 0
    stored: int = 0
    skipped: int = 0
    errors: int = 0
    results: list[RecompileCandidateResult] = Field(default_factory=list)


class CompilerRecompiler:
    """Safely re-run the compiler for assets NUO marked as low quality."""

    def __init__(
        self,
        *,
        compiler: LightweightMaterialCompiler | None = None,
        store: AssetStore | None = None,
    ) -> None:
        self.compiler = compiler or LightweightMaterialCompiler()
        self.store = store or get_store()

    async def recompile_candidates(
        self,
        *,
        tenant_id: str,
        allowed_roots: Iterable[str | Path] = (),
        dry_run: bool = True,
        max_assets: int = 500,
        allow_inline_summary: bool = False,
        mark_original_soft_forgotten: bool = True,
    ) -> RecompileReport:
        """Scan and optionally recompile assets marked by context maintenance."""

        roots = [_normalize_root(root) for root in allowed_roots]
        report = RecompileReport(tenant_id=tenant_id, dry_run=dry_run)
        assets = await self.store.list(tenant_id=tenant_id, limit=max_assets)
        for asset in assets:
            report.scanned += 1
            if not _is_recompile_candidate(asset):
                continue
            report.candidates += 1
            try:
                result = await self._recompile_one(
                    asset,
                    allowed_roots=roots,
                    dry_run=dry_run,
                    allow_inline_summary=allow_inline_summary,
                    mark_original_soft_forgotten=mark_original_soft_forgotten,
                )
            except Exception as exc:  # pragma: no cover - defensive guard for ops runs
                result = RecompileCandidateResult(
                    asset_id=asset.asset_id,
                    status="error",
                    reason=f"{type(exc).__name__}: {exc}",
                    dry_run=dry_run,
                )
            report.results.append(result)
            if result.status == "planned":
                report.planned += 1
            elif result.status == "stored":
                report.stored += 1
            elif result.status == "skipped":
                report.skipped += 1
            else:
                report.errors += 1
        return report

    async def _recompile_one(
        self,
        asset: LayeredAsset,
        *,
        allowed_roots: list[Path],
        dry_run: bool,
        allow_inline_summary: bool,
        mark_original_soft_forgotten: bool,
    ) -> RecompileCandidateResult:
        source = _source_from_asset(asset)
        if source is None:
            return _skipped(asset, "compiler_source_missing", dry_run)
        source_type = source["type"]
        source_uri = source["uri"]

        if source_type == "path":
            root = _matching_allowed_root(source_uri, allowed_roots)
            if root is None:
                return _skipped(
                    asset,
                    "path_source_requires_matching_allowed_root",
                    dry_run,
                    source_type=source_type,
                    source_uri=source_uri,
                )
            material = await self.compiler.compile_path(
                source_uri,
                tenant_id=asset.tenant_id,
                allowed_root=root,
                metadata=_recompile_metadata(asset),
            )
        elif source_type == "url":
            material = await self.compiler.compile_url(
                source_uri,
                tenant_id=asset.tenant_id,
                metadata=_recompile_metadata(asset),
            )
        elif source_type == "inline" and allow_inline_summary and asset.l2_summary:
            material = await self.compiler.compile_text(
                asset.l2_summary,
                tenant_id=asset.tenant_id,
                source_uri=source_uri,
                metadata={
                    **_recompile_metadata(asset),
                    "recompile_inline_from_l2_summary": True,
                },
            )
        else:
            return _skipped(
                asset,
                f"source_type_{source_type}_not_reconstructable",
                dry_run,
                source_type=source_type,
                source_uri=source_uri,
            )

        compiled = material_to_layered_asset(material, layer=asset.layer)
        if compiled is None:
            return _skipped(
                asset,
                f"material_status_{material.status}",
                dry_run,
                source_type=source_type,
                source_uri=source_uri,
            )

        new_asset = _as_recompiled_asset(asset, compiled)
        if dry_run:
            return RecompileCandidateResult(
                asset_id=asset.asset_id,
                status="planned",
                reason="would_store_recompiled_asset",
                dry_run=True,
                source_type=source_type,
                source_uri=source_uri,
                new_asset_id=new_asset.asset_id,
                original_marked_soft_forgotten=mark_original_soft_forgotten,
            )

        await self.store.put(new_asset)
        _mark_original_recompiled(
            asset,
            new_asset_id=new_asset.asset_id,
            mark_soft_forgotten=mark_original_soft_forgotten,
        )
        await self.store.put(asset)
        return RecompileCandidateResult(
            asset_id=asset.asset_id,
            status="stored",
            reason="stored_recompiled_asset_and_marked_original",
            dry_run=False,
            source_type=source_type,
            source_uri=source_uri,
            new_asset_id=new_asset.asset_id,
            original_marked_soft_forgotten=mark_original_soft_forgotten,
        )


def _is_recompile_candidate(asset: LayeredAsset) -> bool:
    meta = asset.l1_metadata or {}
    return meta.get(
        "compiler_recompile_recommended"
    ) is True or "compiler_recompile_recommended" in set(asset.tags)


def _source_from_asset(asset: LayeredAsset) -> dict[str, str] | None:
    raw = (asset.l1_metadata or {}).get("source")
    if not isinstance(raw, dict):
        return None
    source_type = raw.get("type")
    source_uri = raw.get("uri")
    if not isinstance(source_type, str) or not isinstance(source_uri, str):
        return None
    if not source_type.strip() or not source_uri.strip():
        return None
    return {"type": source_type.strip(), "uri": source_uri.strip()}


def _normalize_root(root: str | Path) -> Path:
    return Path(root).expanduser().resolve(strict=False)


def _matching_allowed_root(source_uri: str, allowed_roots: list[Path]) -> Path | None:
    if not allowed_roots:
        return None
    candidate = Path(source_uri).expanduser()
    if not candidate.is_absolute():
        return allowed_roots[0]
    resolved = candidate.resolve(strict=False)
    for root in allowed_roots:
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        return root
    return None


def _recompile_metadata(asset: LayeredAsset) -> dict[str, Any]:
    return {
        "recompile": True,
        "recompiled_from_asset_id": asset.asset_id,
        "original_version": asset.version,
        "original_compiler_quality_score": asset.l1_metadata.get("compiler_quality_score"),
        "original_recompile_reason": asset.l1_metadata.get("compiler_recompile_reason"),
    }


def _as_recompiled_asset(original: LayeredAsset, compiled: LayeredAsset) -> LayeredAsset:
    new_asset = compiled.model_copy(deep=True)
    new_asset.asset_id = _recompiled_asset_id(original, compiled)
    new_asset.version = 1
    new_asset.l1_metadata = {
        **new_asset.l1_metadata,
        "recompiled_from_asset_id": original.asset_id,
        "recompiled_from_version": original.version,
        "recompile_reason": original.l1_metadata.get("compiler_recompile_reason"),
        "recompile_created_at": datetime.now(UTC).isoformat(),
    }
    new_asset.tags = sorted(
        {
            *new_asset.tags,
            "compiler_recompiled",
            f"recompiled_from:{original.asset_id}",
        }
    )
    return new_asset


def _recompiled_asset_id(original: LayeredAsset, compiled: LayeredAsset) -> str:
    seed = ":".join(
        [
            original.asset_id,
            str(original.version + 1),
            compiled.asset_id,
            str(compiled.l1_metadata.get("provenance", {}).get("input_sha256") or ""),
        ]
    )
    suffix = hashlib.sha256(seed.encode()).hexdigest()[:10]
    return f"{compiled.asset_id}_rc{suffix}"


def _mark_original_recompiled(
    asset: LayeredAsset,
    *,
    new_asset_id: str,
    mark_soft_forgotten: bool,
) -> None:
    asset.version += 1
    asset.l1_metadata["compiler_recompile_recommended"] = False
    asset.l1_metadata["compiler_recompile_applied"] = True
    asset.l1_metadata["compiler_recompiled_to_asset_id"] = new_asset_id
    asset.l1_metadata["compiler_recompiled_at"] = datetime.now(UTC).isoformat()
    asset.tags = sorted(tag for tag in set(asset.tags) if tag != "compiler_recompile_recommended")
    asset.tags = sorted({*asset.tags, "compiler_recompiled_original"})
    if mark_soft_forgotten:
        asset.l1_metadata["soft_forgotten"] = True
        asset.tags = sorted({*asset.tags, "soft_forgotten"})


def _skipped(
    asset: LayeredAsset,
    reason: str,
    dry_run: bool,
    *,
    source_type: str | None = None,
    source_uri: str | None = None,
) -> RecompileCandidateResult:
    return RecompileCandidateResult(
        asset_id=asset.asset_id,
        status="skipped",
        reason=reason,
        dry_run=dry_run,
        source_type=source_type,
        source_uri=source_uri,
    )


__all__ = [
    "CompilerRecompiler",
    "RecompileCandidateResult",
    "RecompileReport",
    "RecompileStatus",
]
