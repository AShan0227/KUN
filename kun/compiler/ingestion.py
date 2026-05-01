"""Compile incoming material and store it as a KUN context asset.

This is the first real bridge from the V5 compiler layer into the existing
Context system.  It stays deliberately conservative: no remote fetching, no
implicit L3 storage, and rejected/unsupported inputs are not persisted unless a
caller explicitly asks for audit storage later.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from kun.compiler.material import LightweightMaterialCompiler
from kun.compiler.models import CanonicalMaterial
from kun.context.assets import AssetLayer, LayeredAsset
from kun.context.storage import AssetStore, get_store


class CompilerIngestionResult(BaseModel):
    """Result of compiling and optionally storing one material."""

    material: CanonicalMaterial
    stored: bool
    asset_id: str | None = None
    reason: str = ""


class CompilerIngestor:
    """Compiler → LayeredAsset → AssetStore bridge."""

    def __init__(
        self,
        *,
        compiler: LightweightMaterialCompiler | None = None,
        store: AssetStore | None = None,
    ) -> None:
        self.compiler = compiler or LightweightMaterialCompiler()
        self.store = store or get_store()

    async def ingest_text(
        self,
        text: str,
        *,
        tenant_id: str,
        source_uri: str = "inline:text",
        declared_kind: str | None = None,
        layer: AssetLayer = AssetLayer.L1_TASK,
        metadata: dict[str, Any] | None = None,
    ) -> CompilerIngestionResult:
        material = await self.compiler.compile_text(
            text,
            tenant_id=tenant_id,
            source_uri=source_uri,
            declared_kind=declared_kind,
            metadata=metadata,
        )
        return await self.ingest_material(material, layer=layer)

    async def ingest_path(
        self,
        path: str | Path,
        *,
        tenant_id: str,
        allowed_root: str | Path,
        layer: AssetLayer = AssetLayer.L1_TASK,
        metadata: dict[str, Any] | None = None,
    ) -> CompilerIngestionResult:
        material = await self.compiler.compile_path(
            path,
            tenant_id=tenant_id,
            allowed_root=allowed_root,
            metadata=metadata,
        )
        return await self.ingest_material(material, layer=layer)

    async def ingest_material(
        self,
        material: CanonicalMaterial,
        *,
        layer: AssetLayer = AssetLayer.L1_TASK,
    ) -> CompilerIngestionResult:
        asset = material_to_layered_asset(material, layer=layer)
        if asset is None:
            return CompilerIngestionResult(
                material=material,
                stored=False,
                reason=_not_stored_reason(material),
            )
        await self.store.put(asset)
        return CompilerIngestionResult(
            material=material,
            stored=True,
            asset_id=asset.asset_id,
            reason="stored_as_layered_asset",
        )


def material_to_layered_asset(
    material: CanonicalMaterial,
    *,
    layer: AssetLayer = AssetLayer.L1_TASK,
) -> LayeredAsset | None:
    """Convert a compiled material into the shared LayeredAsset format.

    Unsupported/rejected materials are intentionally not stored by default. A
    future audit channel can persist those separately without polluting normal
    retrieval.
    """
    if material.status != "compiled":
        return None
    if not material.permissions.store_l1 and not material.permissions.store_l2:
        return None

    l2 = material.l2 if material.permissions.store_l2 else material.l1
    return LayeredAsset(
        asset_id=material.asset_id,
        asset_kind="knowledge",
        tenant_id=material.tenant_id,
        l1_metadata={
            "source": material.source.model_dump(mode="json"),
            "compiler": material.provenance.compiler,
            "compiler_backend": material.provenance.backend,
            "compiler_profile": material.compiler_profile.model_dump(mode="json"),
            "kind": material.kind,
            "status": material.status,
            "risk": material.risk.model_dump(mode="json"),
            "tokens_estimate": material.tokens_estimate,
            "provenance": material.provenance.model_dump(mode="json"),
            "material_metadata": material.metadata,
        },
        l2_summary=l2,
        l3_ref=material.l3_ref,
        layer=layer,
        tags=[
            "compiler",
            "canonical_material",
            f"kind:{material.kind}",
            f"source:{material.source.type}",
        ],
    )


def _not_stored_reason(material: CanonicalMaterial) -> str:
    if material.status != "compiled":
        return f"material_status_{material.status}"
    if not material.permissions.store_l1 and not material.permissions.store_l2:
        return "material_permissions_disallow_storage"
    return "not_stored"


__all__ = [
    "CompilerIngestionResult",
    "CompilerIngestor",
    "material_to_layered_asset",
]
