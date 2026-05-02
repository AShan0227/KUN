"""Batch ingestion for the KUN compiler layer.

This is the conservative "资料入口" path: callers can submit a manifest of
inline text, local files, and allowlisted URLs. Every item still goes through
the same compiler safety checks before it can enter the shared AssetStore.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from kun.compiler.ingestion import CompilerIngestionResult, CompilerIngestor
from kun.context.assets import AssetLayer

CompilerBatchItemType = Literal["text", "path", "url"]
CompilerBatchItemStatus = Literal["stored", "skipped", "error"]


class CompilerBatchItem(BaseModel):
    """One source item in a compiler batch manifest."""

    id: str | None = None
    type: CompilerBatchItemType
    value: str
    tenant_id: str | None = None
    source_uri: str | None = None
    declared_kind: str | None = None
    allowed_root: str | None = None
    layer: AssetLayer | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("value")
    @classmethod
    def _non_empty_value(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("batch item value cannot be empty")
        return value


class CompilerBatchManifest(BaseModel):
    """Manifest accepted by the batch compiler CLI/API adapters."""

    tenant_id: str
    layer: AssetLayer = AssetLayer.L1_TASK
    allowed_root: str | None = None
    items: list[CompilerBatchItem]
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompilerBatchItemResult(BaseModel):
    """Result for one item in a compiler batch."""

    id: str
    type: CompilerBatchItemType
    status: CompilerBatchItemStatus
    stored: bool = False
    asset_id: str | None = None
    material_status: str | None = None
    reason: str = ""
    source_uri: str | None = None
    summary: str = ""
    risk_level: str | None = None
    risk_flags: list[str] = Field(default_factory=list)


class CompilerBatchReport(BaseModel):
    """Aggregate report for a compiler batch run."""

    tenant_id: str
    total: int
    stored: int
    skipped: int
    errors: int
    results: list[CompilerBatchItemResult]


class CompilerBatchIngestor:
    """Compile and store a bounded list of material sources."""

    def __init__(self, *, ingestor: CompilerIngestor | None = None) -> None:
        self.ingestor = ingestor or CompilerIngestor()

    async def ingest_manifest(self, manifest: CompilerBatchManifest) -> CompilerBatchReport:
        return await self.ingest_items(
            manifest.items,
            tenant_id=manifest.tenant_id,
            default_layer=manifest.layer,
            default_allowed_root=manifest.allowed_root,
            batch_metadata=manifest.metadata,
        )

    async def ingest_items(
        self,
        items: list[CompilerBatchItem],
        *,
        tenant_id: str,
        default_layer: AssetLayer = AssetLayer.L1_TASK,
        default_allowed_root: str | None = None,
        batch_metadata: dict[str, Any] | None = None,
    ) -> CompilerBatchReport:
        results: list[CompilerBatchItemResult] = []
        for index, item in enumerate(items):
            item_id = item.id or f"item-{index + 1}"
            results.append(
                await self._ingest_one(
                    item,
                    item_id=item_id,
                    tenant_id=item.tenant_id or tenant_id,
                    default_layer=default_layer,
                    default_allowed_root=default_allowed_root,
                    batch_metadata=batch_metadata or {},
                )
            )

        return CompilerBatchReport(
            tenant_id=tenant_id,
            total=len(results),
            stored=sum(1 for result in results if result.status == "stored"),
            skipped=sum(1 for result in results if result.status == "skipped"),
            errors=sum(1 for result in results if result.status == "error"),
            results=results,
        )

    async def _ingest_one(
        self,
        item: CompilerBatchItem,
        *,
        item_id: str,
        tenant_id: str,
        default_layer: AssetLayer,
        default_allowed_root: str | None,
        batch_metadata: dict[str, Any],
    ) -> CompilerBatchItemResult:
        layer = item.layer or default_layer
        metadata = {
            "batch": True,
            "batch_item_id": item_id,
            "batch_item_type": item.type,
            **batch_metadata,
            **item.metadata,
        }
        try:
            result = await self._dispatch_item(
                item,
                tenant_id=tenant_id,
                layer=layer,
                allowed_root=item.allowed_root or default_allowed_root,
                metadata=metadata,
            )
        except Exception as exc:
            return CompilerBatchItemResult(
                id=item_id,
                type=item.type,
                status="error",
                reason=f"{type(exc).__name__}: {exc}",
                source_uri=item.source_uri or item.value,
            )
        return _to_item_result(item_id=item_id, item_type=item.type, result=result)

    async def _dispatch_item(
        self,
        item: CompilerBatchItem,
        *,
        tenant_id: str,
        layer: AssetLayer,
        allowed_root: str | None,
        metadata: dict[str, Any],
    ) -> CompilerIngestionResult:
        if item.type == "text":
            return await self.ingestor.ingest_text(
                item.value,
                tenant_id=tenant_id,
                source_uri=item.source_uri or f"inline:{item.id or 'batch-text'}",
                declared_kind=item.declared_kind,
                layer=layer,
                metadata=metadata,
            )
        if item.type == "path":
            if not allowed_root:
                raise ValueError("path batch item requires allowed_root")
            return await self.ingestor.ingest_path(
                Path(item.value),
                tenant_id=tenant_id,
                allowed_root=Path(allowed_root),
                layer=layer,
                metadata=metadata,
            )
        return await self.ingestor.ingest_url(
            item.value,
            tenant_id=tenant_id,
            layer=layer,
            metadata=metadata,
        )


def _to_item_result(
    *,
    item_id: str,
    item_type: CompilerBatchItemType,
    result: CompilerIngestionResult,
) -> CompilerBatchItemResult:
    material = result.material
    status: CompilerBatchItemStatus = "stored" if result.stored else "skipped"
    return CompilerBatchItemResult(
        id=item_id,
        type=item_type,
        status=status,
        stored=result.stored,
        asset_id=result.asset_id,
        material_status=material.status,
        reason="stored_as_layered_asset" if result.stored else result.reason,
        source_uri=material.source.uri,
        summary=material.l1,
        risk_level=material.risk.level,
        risk_flags=material.risk.flags,
    )


__all__ = [
    "CompilerBatchIngestor",
    "CompilerBatchItem",
    "CompilerBatchItemResult",
    "CompilerBatchItemStatus",
    "CompilerBatchItemType",
    "CompilerBatchManifest",
    "CompilerBatchReport",
]
