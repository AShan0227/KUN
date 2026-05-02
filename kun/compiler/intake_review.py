"""Compiler intake review package.

This module is the audit gate in front of material compilation.  It does not
try to make unsafe inputs work; it tells NUO/Qi/humans whether the intake can be
compiled now, which backend should be used, and why some input is being held.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from kun.compiler.ingestion import material_to_layered_asset
from kun.compiler.markitdown import MarkItDownMaterialCompiler
from kun.compiler.material import LightweightMaterialCompiler
from kun.compiler.models import CanonicalMaterial, MaterialSource
from kun.context.assets import AssetLayer, LayeredAsset

IntakeSourceType = Literal["raw_text", "path", "url", "bytes"]
CompilerBackend = Literal["plain", "markitdown", "manual"]
BackendStatus = Literal["available", "disabled", "unavailable", "not_required"]
IntakeDecision = Literal[
    "compiled_to_asset",
    "compiled_hold_for_review",
    "blocked",
    "backend_unavailable",
    "manual_review_required",
]
QualityLevel = Literal["empty", "low", "medium", "high"]

_MARKITDOWN_SUFFIXES = {
    ".doc",
    ".docx",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".msg",
    ".epub",
}


class CompilerIntakeRequest(BaseModel):
    """User/API facing request to review one incoming material."""

    tenant_id: str
    source_type: IntakeSourceType
    value: str
    raw_bytes: bytes | None = Field(default=None, repr=False)
    declared_kind: str | None = None
    mime_type: str | None = None
    allowed_root: str | None = None
    requested_backend: CompilerBackend | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CompilerBackendReview(BaseModel):
    """The backend selected or required by the intake gate."""

    name: CompilerBackend
    status: BackendStatus
    reason: str = ""


class CompilerQualityReview(BaseModel):
    """Small deterministic quality signal for the compiled material."""

    level: QualityLevel
    score: float
    flags: list[str] = Field(default_factory=list)
    text_chars: int = 0
    tokens_estimate: int = 0


class CompilerReviewPackage(BaseModel):
    """Stable package that NUO/Qi/humans can consume before trusting material."""

    package_id: str
    tenant_id: str
    source: MaterialSource
    requested_backend: CompilerBackend | None = None
    suggested_backend: CompilerBackend
    backend: CompilerBackendReview
    decision: IntakeDecision
    risk_level: Literal["low", "medium", "high"]
    risk_flags: list[str] = Field(default_factory=list)
    quality: CompilerQualityReview
    needs_recompile: bool
    needs_human_review: bool
    store_allowed: bool
    reasons: list[str] = Field(default_factory=list)
    next_actions: list[str] = Field(default_factory=list)
    material: CanonicalMaterial | None = None
    asset: LayeredAsset | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def as_review_ticket(self) -> dict[str, Any]:
        """Return a compact dict for future NUO/Qi queues."""

        return {
            "package_id": self.package_id,
            "tenant_id": self.tenant_id,
            "source": self.source.model_dump(mode="json"),
            "suggested_backend": self.suggested_backend,
            "backend": self.backend.model_dump(mode="json"),
            "decision": self.decision,
            "risk_level": self.risk_level,
            "risk_flags": self.risk_flags,
            "quality": self.quality.model_dump(mode="json"),
            "needs_recompile": self.needs_recompile,
            "needs_human_review": self.needs_human_review,
            "store_allowed": self.store_allowed,
            "material_id": self.material.asset_id if self.material else None,
            "asset_id": self.asset.asset_id if self.asset else None,
            "reasons": self.reasons,
            "next_actions": self.next_actions,
        }


async def build_compiler_review_package(
    request: CompilerIntakeRequest,
    *,
    compiler: LightweightMaterialCompiler | None = None,
    markitdown_compiler: MarkItDownMaterialCompiler | None = None,
    layer: AssetLayer = AssetLayer.L1_TASK,
) -> CompilerReviewPackage:
    """Review and, when safe, compile one intake material.

    The function is conservative by design:
    - paths are not read without an explicit allowed_root;
    - URLs rely on the compiler's explicit URL policy and do not fetch by default;
    - MarkItDown is only claimed when the optional backend is enabled/available.
    """

    selected_compiler = compiler or LightweightMaterialCompiler()
    suggested_backend = _suggest_backend(request)
    package_id = _package_id(request, suggested_backend)
    material: CanonicalMaterial | None = None
    source = _request_source(request)
    backend = CompilerBackendReview(
        name=suggested_backend,
        status="available" if suggested_backend == "plain" else "not_required",
        reason="plain deterministic compiler is available",
    )
    reasons: list[str] = []
    next_actions: list[str] = []
    metadata = dict(request.metadata)

    if request.source_type == "path" and not request.allowed_root:
        return _blocked_package(
            request,
            source=source,
            package_id=package_id,
            suggested_backend=suggested_backend,
            backend=CompilerBackendReview(
                name=suggested_backend,
                status="unavailable",
                reason="path intake requires allowed_root before reading local files",
            ),
            reason="path_allowed_root_required",
            next_action="retry with an explicit allowed_root or send the material through manual review",
        )

    if request.source_type == "bytes" and request.raw_bytes is None:
        return _blocked_package(
            request,
            source=source,
            package_id=package_id,
            suggested_backend=suggested_backend,
            backend=CompilerBackendReview(
                name=suggested_backend,
                status="unavailable",
                reason="bytes intake requires raw_bytes",
            ),
            reason="raw_bytes_required",
            next_action="attach raw bytes or use raw_text/path/url intake",
        )

    if request.source_type == "raw_text":
        material = await selected_compiler.compile_text(
            request.value,
            tenant_id=request.tenant_id,
            source_uri=request.value if request.value.startswith("inline:") else "inline:intake",
            declared_kind=request.declared_kind,
            metadata=metadata,
        )
    elif request.source_type == "bytes":
        material = await selected_compiler.compile_bytes(
            request.raw_bytes or b"",
            tenant_id=request.tenant_id,
            source_uri=request.value or "inline:bytes",
            declared_kind=request.declared_kind,
            mime_type=request.mime_type,
            metadata=metadata,
        )
    elif request.source_type == "url":
        material = await selected_compiler.compile_url(
            request.value,
            tenant_id=request.tenant_id,
            metadata=metadata,
        )
        backend = _url_backend_review(material)
    elif suggested_backend == "markitdown":
        selected_markitdown = markitdown_compiler or MarkItDownMaterialCompiler()
        material = await selected_markitdown.compile_path(
            request.value,
            tenant_id=request.tenant_id,
            allowed_root=request.allowed_root or ".",
            metadata=metadata,
        )
        backend = _markitdown_backend_review(material)
    else:
        material = await selected_compiler.compile_path(
            request.value,
            tenant_id=request.tenant_id,
            allowed_root=request.allowed_root or ".",
            metadata=metadata,
        )

    quality = _quality_review(material)
    reasons.extend(_material_reasons(material, quality))
    next_actions.extend(_next_actions(material, quality, suggested_backend))
    risk_level = _combined_risk_level(material, quality)
    risk_flags = sorted({*material.risk.flags, *quality.flags})
    needs_recompile = _needs_recompile(material, quality, backend)
    needs_human_review = _needs_human_review(material, quality, backend)
    store_allowed = (
        material.status == "compiled"
        and risk_level != "high"
        and quality.level not in {"empty", "low"}
        and not needs_human_review
    )
    asset = material_to_layered_asset(material, layer=layer) if store_allowed else None

    decision = _decision(
        material=material,
        backend=backend,
        store_allowed=store_allowed,
        needs_human_review=needs_human_review,
    )

    return CompilerReviewPackage(
        package_id=package_id,
        tenant_id=request.tenant_id,
        source=material.source,
        requested_backend=request.requested_backend,
        suggested_backend=suggested_backend,
        backend=backend,
        decision=decision,
        risk_level=risk_level,
        risk_flags=risk_flags,
        quality=quality,
        needs_recompile=needs_recompile,
        needs_human_review=needs_human_review,
        store_allowed=store_allowed,
        reasons=reasons,
        next_actions=next_actions,
        material=material,
        asset=asset,
        metadata={
            "compiler_intake_review": True,
            "material_status": material.status,
            "material_kind": material.kind,
            "source_type": request.source_type,
        },
    )


def _blocked_package(
    request: CompilerIntakeRequest,
    *,
    source: MaterialSource,
    package_id: str,
    suggested_backend: CompilerBackend,
    backend: CompilerBackendReview,
    reason: str,
    next_action: str,
) -> CompilerReviewPackage:
    return CompilerReviewPackage(
        package_id=package_id,
        tenant_id=request.tenant_id,
        source=source,
        requested_backend=request.requested_backend,
        suggested_backend=suggested_backend,
        backend=backend,
        decision="blocked",
        risk_level="high",
        risk_flags=[reason],
        quality=CompilerQualityReview(level="empty", score=0.0, flags=["not_compiled"]),
        needs_recompile=True,
        needs_human_review=True,
        store_allowed=False,
        reasons=[reason],
        next_actions=[next_action],
        metadata={"compiler_intake_review": True, "source_type": request.source_type},
    )


def _suggest_backend(request: CompilerIntakeRequest) -> CompilerBackend:
    if request.requested_backend:
        return request.requested_backend
    if request.source_type == "path":
        suffix = Path(urlparse(request.value).path).suffix.lower()
        if suffix in _MARKITDOWN_SUFFIXES:
            return "markitdown"
    if request.source_type == "url":
        return "manual"
    return "plain"


def _request_source(request: CompilerIntakeRequest) -> MaterialSource:
    source_type_map: dict[IntakeSourceType, Literal["inline", "path", "url", "bytes"]] = {
        "raw_text": "inline",
        "path": "path",
        "url": "url",
        "bytes": "bytes",
    }
    source_type = source_type_map[request.source_type]
    return MaterialSource(
        type=source_type,
        uri=request.value or f"inline:{request.source_type}",
        detected_kind=request.declared_kind,
        mime_type=request.mime_type,
    )


def _package_id(request: CompilerIntakeRequest, backend: CompilerBackend) -> str:
    raw_digest = hashlib.sha256(request.raw_bytes or b"").hexdigest()[:16]
    base = "|".join(
        [
            request.tenant_id,
            request.source_type,
            request.value,
            request.declared_kind or "",
            request.mime_type or "",
            request.allowed_root or "",
            backend,
            raw_digest,
        ]
    )
    return f"compiler_review_{hashlib.sha256(base.encode()).hexdigest()[:24]}"


def _markitdown_backend_review(material: CanonicalMaterial) -> CompilerBackendReview:
    status = material.metadata.get("backend_status")
    if isinstance(status, dict):
        raw_status = str(status.get("status") or "unavailable")
        normalized: BackendStatus = (
            "available"
            if raw_status == "available"
            else "disabled"
            if raw_status == "disabled"
            else "unavailable"
        )
        return CompilerBackendReview(
            name="markitdown",
            status=normalized,
            reason=str(status.get("reason") or material.risk.reason),
        )
    return CompilerBackendReview(
        name="markitdown",
        status="available" if material.status == "compiled" else "unavailable",
        reason=material.risk.reason,
    )


def _url_backend_review(material: CanonicalMaterial) -> CompilerBackendReview:
    if material.status == "compiled":
        return CompilerBackendReview(
            name="plain",
            status="available",
            reason="url was fetched by explicit compiler policy",
        )
    if material.risk.reason == "url_fetch_not_enabled":
        return CompilerBackendReview(
            name="manual",
            status="unavailable",
            reason="url fetch is disabled; manual review or allowlisted fetch is required",
        )
    return CompilerBackendReview(name="manual", status="unavailable", reason=material.risk.reason)


def _quality_review(material: CanonicalMaterial) -> CompilerQualityReview:
    text = material.l2 or material.l1 or ""
    text_chars = len(text.strip())
    flags: list[str] = []
    if material.status != "compiled":
        return CompilerQualityReview(
            level="empty",
            score=0.0,
            flags=["not_compiled"],
            text_chars=0,
            tokens_estimate=0,
        )
    if not text.strip():
        flags.append("empty_text")
        return CompilerQualityReview(
            level="empty",
            score=0.0,
            flags=flags,
            text_chars=text_chars,
            tokens_estimate=material.tokens_estimate,
        )
    replacement_count = text.count("\ufffd")
    if replacement_count and replacement_count / max(1, len(text)) > 0.02:
        flags.append("decode_replacement_chars")
    if text_chars < 24:
        flags.append("too_short")
    if material.kind == "pdf" and material.metadata.get("pdf_text_unavailable"):
        flags.append("pdf_text_unavailable")
    score = min(1.0, text_chars / 160)
    if flags:
        score = min(score, 0.35)
    if score < 0.2:
        level: QualityLevel = "low"
    elif score < 0.6:
        level = "medium"
    else:
        level = "high"
    return CompilerQualityReview(
        level=level,
        score=round(score, 3),
        flags=flags,
        text_chars=text_chars,
        tokens_estimate=material.tokens_estimate,
    )


def _material_reasons(
    material: CanonicalMaterial,
    quality: CompilerQualityReview,
) -> list[str]:
    reasons: list[str] = []
    if material.status != "compiled":
        reasons.append(f"material_status_{material.status}")
    if material.risk.reason:
        reasons.append(material.risk.reason)
    reasons.extend(quality.flags)
    return _dedupe(reasons)


def _next_actions(
    material: CanonicalMaterial,
    quality: CompilerQualityReview,
    backend: CompilerBackend,
) -> list[str]:
    actions: list[str] = []
    if backend == "markitdown" and material.status != "compiled":
        actions.append("enable/install MarkItDown backend or route this file to manual review")
    if material.source.type == "url" and material.status != "compiled":
        actions.append("enable URL fetch with host allowlist or capture the content manually")
    if material.status == "rejected":
        actions.append("fix the source boundary before retrying")
    if quality.level in {"empty", "low"}:
        actions.append("review source quality before storing or sending to LLM")
    if quality.flags and "pdf_text_unavailable" in quality.flags:
        actions.append("use OCR or MarkItDown/manual extraction before relying on this PDF")
    return _dedupe(actions)


def _combined_risk_level(
    material: CanonicalMaterial,
    quality: CompilerQualityReview,
) -> Literal["low", "medium", "high"]:
    if material.risk.level == "high" or material.status == "rejected":
        return "high"
    if material.risk.level == "medium" or quality.level in {"empty", "low"}:
        return "medium"
    return "low"


def _needs_recompile(
    material: CanonicalMaterial,
    quality: CompilerQualityReview,
    backend: CompilerBackendReview,
) -> bool:
    return (
        material.status != "compiled"
        or quality.level in {"empty", "low"}
        or backend.status in {"disabled", "unavailable"}
        or "pdf_text_unavailable" in quality.flags
    )


def _needs_human_review(
    material: CanonicalMaterial,
    quality: CompilerQualityReview,
    backend: CompilerBackendReview,
) -> bool:
    return (
        material.status in {"rejected", "placeholder", "unsupported", "unavailable"}
        or material.risk.level == "high"
        or backend.status in {"disabled", "unavailable"}
        or quality.level in {"empty", "low"}
    )


def _decision(
    *,
    material: CanonicalMaterial,
    backend: CompilerBackendReview,
    store_allowed: bool,
    needs_human_review: bool,
) -> IntakeDecision:
    if material.status == "rejected":
        return "blocked"
    if backend.status in {"disabled", "unavailable"} and material.status != "compiled":
        return "backend_unavailable"
    if store_allowed:
        return "compiled_to_asset"
    if material.status == "compiled":
        return "compiled_hold_for_review"
    if needs_human_review:
        return "manual_review_required"
    return "manual_review_required"


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value and value not in seen:
            out.append(value)
            seen.add(value)
    return out


__all__ = [
    "BackendStatus",
    "CompilerBackend",
    "CompilerBackendReview",
    "CompilerIntakeRequest",
    "CompilerQualityReview",
    "CompilerReviewPackage",
    "IntakeDecision",
    "IntakeSourceType",
    "QualityLevel",
    "build_compiler_review_package",
]
