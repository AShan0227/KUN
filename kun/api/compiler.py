"""Compiler ingestion API.

This is the HTTP hot path for external/RAG material entering KUN.  It reuses
the same conservative compiler gates as the CLI: path reads need an explicit
allowed root, URL fetch needs the compiler allowlist, and rejected/placeholder
materials do not enter normal context retrieval.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from kun.compiler import (
    CompilerBatchItem,
    CompilerBatchItemResult,
    CompilerBatchReport,
    CompilerIntakeRequest,
    CompilerReviewPackage,
    IntakeSourceType,
    build_compiler_review_package,
    enqueue_compiler_review_packages,
)
from kun.context.assets import AssetLayer
from kun.context.storage import get_store
from kun.core.config import settings
from kun.core.tenancy import current_tenant, require_scope

router = APIRouter(prefix="/api/compiler", tags=["compiler"])


class CompilerHotIngestRequest(BaseModel):
    """Tenant-scoped material ingestion request."""

    model_config = ConfigDict(extra="forbid")

    layer: AssetLayer = AssetLayer.L1_TASK
    allowed_root: str | None = None
    items: list[CompilerBatchItem] = Field(default_factory=list, min_length=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.post("/ingest-manifest", response_model=CompilerBatchReport)
async def ingest_manifest(req: CompilerHotIngestRequest) -> CompilerBatchReport:
    """Compile and store supported material for the current tenant.

    Client-supplied item tenant IDs are ignored.  The active TenantContext is
    the single source of truth so this endpoint cannot be used to write into
    another tenant's AssetStore.
    """

    tenant = current_tenant()
    _require_scope_when_enforced("context:write")
    if any(
        item.type == "path" and not (item.allowed_root or req.allowed_root) for item in req.items
    ):
        raise HTTPException(status_code=422, detail="path items require allowed_root")
    sanitized_items = [item.model_copy(update={"tenant_id": None}) for item in req.items]
    return await _review_and_store_items(
        sanitized_items,
        tenant_id=tenant.tenant_id,
        default_layer=req.layer,
        default_allowed_root=req.allowed_root,
        batch_metadata={
            "api": "compiler.ingest_manifest",
            "requested_by": tenant.user_id or tenant.tenant_id,
            **req.metadata,
        },
    )


async def _review_and_store_items(
    items: list[CompilerBatchItem],
    *,
    tenant_id: str,
    default_layer: AssetLayer,
    default_allowed_root: str | None,
    batch_metadata: dict[str, Any],
) -> CompilerBatchReport:
    results: list[CompilerBatchItemResult] = []
    review_packages: list[CompilerReviewPackage] = []
    for index, item in enumerate(items):
        item_id = item.id or f"item-{index + 1}"
        result, package = await _review_and_store_one(
            item,
            item_id=item_id,
            tenant_id=tenant_id,
            default_layer=default_layer,
            default_allowed_root=default_allowed_root,
            batch_metadata=batch_metadata,
        )
        results.append(result)
        if package is not None and (
            package.decision != "compiled_to_asset" or package.needs_recompile
        ):
            review_packages.append(package)
    if review_packages:
        await enqueue_compiler_review_packages(tenant_id=tenant_id, packages=review_packages)
    return CompilerBatchReport(
        tenant_id=tenant_id,
        total=len(results),
        stored=sum(1 for result in results if result.status == "stored"),
        skipped=sum(1 for result in results if result.status == "skipped"),
        errors=sum(1 for result in results if result.status == "error"),
        results=results,
    )


async def _review_and_store_one(
    item: CompilerBatchItem,
    *,
    item_id: str,
    tenant_id: str,
    default_layer: AssetLayer,
    default_allowed_root: str | None,
    batch_metadata: dict[str, Any],
) -> tuple[CompilerBatchItemResult, CompilerReviewPackage | None]:
    layer = item.layer or default_layer
    metadata = {
        "batch": True,
        "batch_item_id": item_id,
        "batch_item_type": item.type,
        **batch_metadata,
        **item.metadata,
    }
    try:
        package = await build_compiler_review_package(
            _intake_request_from_item(
                item,
                item_id=item_id,
                tenant_id=tenant_id,
                allowed_root=item.allowed_root or default_allowed_root,
                metadata=metadata,
            ),
            layer=layer,
        )
        if package.asset is not None and package.store_allowed:
            await get_store().put(package.asset)
        return _review_package_to_batch_result(item_id=item_id, item=item, package=package), package
    except Exception as exc:
        return (
            CompilerBatchItemResult(
                id=item_id,
                type=item.type,
                status="error",
                reason=f"{type(exc).__name__}: {exc}",
                source_uri=item.source_uri or item.value,
            ),
            None,
        )


def _intake_request_from_item(
    item: CompilerBatchItem,
    *,
    item_id: str,
    tenant_id: str,
    allowed_root: str | None,
    metadata: dict[str, Any],
) -> CompilerIntakeRequest:
    source_type: IntakeSourceType = "raw_text" if item.type == "text" else item.type
    return CompilerIntakeRequest(
        tenant_id=tenant_id,
        source_type=source_type,
        value=item.value,
        declared_kind=item.declared_kind,
        allowed_root=allowed_root,
        metadata={
            **metadata,
            "compiler_hot_ingest_review": True,
            "source_uri": item.source_uri or item.value,
            "batch_item_id": item_id,
        },
    )


def _review_package_to_batch_result(
    *,
    item_id: str,
    item: CompilerBatchItem,
    package: CompilerReviewPackage,
) -> CompilerBatchItemResult:
    stored = package.asset is not None and package.store_allowed
    material = package.material
    return CompilerBatchItemResult(
        id=item_id,
        type=item.type,
        status="stored" if stored else "skipped",
        stored=stored,
        asset_id=package.asset.asset_id if package.asset else None,
        material_status=material.status if material else None,
        reason="stored_after_compiler_review" if stored else _review_skip_reason(package),
        source_uri=package.source.uri,
        summary=(material.l1 if material else "") or "",
        risk_level=package.risk_level,
        risk_flags=package.risk_flags,
    )


def _review_skip_reason(package: CompilerReviewPackage) -> str:
    suffix = ""
    if package.reasons:
        suffix = ":" + ",".join(package.reasons[:3])
    return f"compiler_review_{package.decision}{suffix}"


def _require_scope_when_enforced(scope: str) -> None:
    tenant = current_tenant()
    if settings().env != "production" and not tenant.scopes:
        return
    try:
        require_scope(scope, ctx=tenant)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


__all__ = ["CompilerHotIngestRequest", "router"]
