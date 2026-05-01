"""Deterministic materialization helpers for the KUN V5 compiler slice."""

from __future__ import annotations

import csv
import hashlib
import html.parser
import io
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import anyio

from kun.compiler.models import (
    CanonicalKind,
    CanonicalMaterial,
    CompilerProfile,
    MaterialPermissions,
    MaterialProvenance,
    MaterialRisk,
    MaterialSource,
)
from kun.interface.input_translator import InputDescriptor, RealWorldTranslator

SUPPORTED_KINDS: set[str] = {"plain_text", "markdown", "html", "json", "csv"}
_KIND_MAP: dict[str, CanonicalKind] = {
    "plain_text": "text",
    "markdown": "markdown",
    "html": "html",
    "json": "json",
    "csv": "csv",
}


class LightweightMaterialCompiler:
    """Compile small local inputs into canonical materials.

    This first V5 slice is intentionally local and deterministic. It does not
    fetch URLs, run OCR, or invoke MarkItDown; those remain explicit optional
    future backends surfaced in the compiler profile.
    """

    def __init__(self, *, translator: RealWorldTranslator | None = None) -> None:
        self._translator = translator or RealWorldTranslator()

    async def compile_text(
        self,
        text: str,
        *,
        tenant_id: str,
        source_uri: str = "inline:text",
        declared_kind: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CanonicalMaterial:
        descriptor = await self._detect_text(text, declared_kind, source_uri)
        return self._compile_detected(
            text,
            tenant_id=tenant_id,
            source=MaterialSource(
                type="inline",
                uri=source_uri,
                detected_kind=descriptor.kind,
                mime_type=descriptor.mime_type,
            ),
            descriptor=descriptor,
            metadata=metadata,
        )

    async def compile_path(
        self,
        path: str | Path,
        *,
        tenant_id: str,
        allowed_root: str | Path,
        metadata: dict[str, Any] | None = None,
    ) -> CanonicalMaterial:
        path_status, source_uri, raw = await anyio.to_thread.run_sync(
            _read_safe_path,
            path,
            allowed_root,
        )
        if path_status == "path_traversal_blocked":
            return self._rejected(
                tenant_id=tenant_id,
                source=MaterialSource(type="path", uri=source_uri),
                reason="path_traversal_blocked",
                risk_flags=["path_traversal"],
                metadata=metadata,
            )

        if path_status != "ok" or raw is None:
            return self._rejected(
                tenant_id=tenant_id,
                source=MaterialSource(type="path", uri=source_uri),
                reason="path_not_found_or_not_file",
                risk_flags=["invalid_path"],
                metadata=metadata,
            )

        suffix_kind = _kind_from_suffix(source_uri)
        descriptor = (
            self._descriptor_for(suffix_kind)
            if suffix_kind is not None
            else await self._translator.detect_file_kind(raw)
        )
        text = _decode_bytes(raw)
        return self._compile_detected(
            text,
            tenant_id=tenant_id,
            source=MaterialSource(
                type="path",
                uri=source_uri,
                detected_kind=descriptor.kind,
                mime_type=descriptor.mime_type,
            ),
            descriptor=descriptor,
            input_bytes=raw,
            metadata=metadata,
        )

    async def compile_url(
        self,
        url: str,
        *,
        tenant_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> CanonicalMaterial:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return self._rejected(
                tenant_id=tenant_id,
                source=MaterialSource(type="url", uri=url),
                reason="invalid_or_unsupported_url",
                risk_flags=["unsupported_url"],
                metadata=metadata,
            )
        return self._unsupported(
            tenant_id=tenant_id,
            source=MaterialSource(type="url", uri=url),
            reason="url_fetch_not_implemented",
            metadata=metadata,
            status="placeholder",
        )

    async def _detect_text(
        self,
        text: str,
        declared_kind: str | None,
        source_uri: str,
    ) -> InputDescriptor:
        if declared_kind:
            return self._descriptor_for(declared_kind)
        suffix_kind = _kind_from_suffix(source_uri)
        if suffix_kind is not None:
            return self._descriptor_for(suffix_kind)
        if _looks_csv(text):
            return self._descriptor_for("csv")
        return await self._translator.detect_text_kind(text)

    def _descriptor_for(self, kind: str) -> InputDescriptor:
        normalized = "plain_text" if kind == "text" else kind
        if normalized not in {
            "plain_text",
            "markdown",
            "html",
            "json",
            "csv",
            "pdf",
            "xlsx",
            "binary_unknown",
        }:
            normalized = "binary_unknown"
        mime_type = {
            "plain_text": "text/plain",
            "markdown": "text/markdown",
            "html": "text/html",
            "json": "application/json",
            "csv": "text/csv",
        }.get(normalized, "application/octet-stream")
        return InputDescriptor(
            kind=normalized,
            mime_type=mime_type,
            confidence=1.0,
            suggested_handler="kun.compiler",
            metadata={"detector": "declared_or_suffix"},
        )

    def _compile_detected(
        self,
        text: str,
        *,
        tenant_id: str,
        source: MaterialSource,
        descriptor: InputDescriptor,
        input_bytes: bytes | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CanonicalMaterial:
        if descriptor.kind not in SUPPORTED_KINDS:
            return self._unsupported(
                tenant_id=tenant_id,
                source=source,
                reason=f"unsupported_kind:{descriptor.kind}",
                input_bytes=input_bytes,
                metadata={
                    **(metadata or {}),
                    "detector_metadata": descriptor.metadata,
                },
            )

        kind = _KIND_MAP[descriptor.kind]
        normalized, material_metadata, risk_flags = _normalize(kind, text)
        merged_metadata = {
            "detector_metadata": descriptor.metadata,
            "confidence": descriptor.confidence,
            **material_metadata,
            **(metadata or {}),
        }
        input_digest = hashlib.sha256(
            input_bytes if input_bytes is not None else text.encode()
        ).hexdigest()
        asset_id = _asset_id(tenant_id, kind, input_digest)
        return CanonicalMaterial(
            asset_id=asset_id,
            kind=kind,
            source=source,
            tenant_id=tenant_id,
            l1=_make_l1(normalized),
            l2=normalized,
            l3_ref=None,
            tokens_estimate=_estimate_tokens(normalized),
            risk=MaterialRisk(
                level="medium" if risk_flags else "low",
                flags=risk_flags,
                reason=";".join(risk_flags) if risk_flags else "lightweight_local_compile",
            ),
            permissions=MaterialPermissions(
                notes=["l3 storage is disabled until a durable object backend is wired"]
            ),
            provenance=MaterialProvenance(
                input_sha256=input_digest,
                detector=descriptor.metadata.get("detector"),
                notes=["no external reformat backend invoked"],
            ),
            compiler_profile=_profile(),
            metadata=merged_metadata,
        )

    def _rejected(
        self,
        *,
        tenant_id: str,
        source: MaterialSource,
        reason: str,
        risk_flags: list[str],
        metadata: dict[str, Any] | None = None,
    ) -> CanonicalMaterial:
        digest = hashlib.sha256(
            f"{tenant_id}:{source.type}:{source.uri}:{reason}".encode()
        ).hexdigest()
        return CanonicalMaterial(
            asset_id=_asset_id(tenant_id, "unsupported", digest),
            kind="unsupported",
            source=source,
            tenant_id=tenant_id,
            l1="",
            l2="",
            l3_ref=None,
            tokens_estimate=0,
            risk=MaterialRisk(level="high", flags=risk_flags, reason=reason),
            permissions=MaterialPermissions(
                read=False,
                transform=False,
                store_l1=False,
                store_l2=False,
                notes=["input rejected before reading or fetching content"],
            ),
            provenance=MaterialProvenance(input_sha256=None, notes=[reason]),
            compiler_profile=_profile(),
            metadata=metadata or {},
            status="rejected",
        )

    def _unsupported(
        self,
        *,
        tenant_id: str,
        source: MaterialSource,
        reason: str,
        input_bytes: bytes | None = None,
        metadata: dict[str, Any] | None = None,
        status: str = "unsupported",
    ) -> CanonicalMaterial:
        digest = hashlib.sha256(
            input_bytes or f"{tenant_id}:{source.uri}:{reason}".encode()
        ).hexdigest()
        return CanonicalMaterial(
            asset_id=_asset_id(tenant_id, "unsupported", digest),
            kind="unsupported",
            source=source,
            tenant_id=tenant_id,
            l1="",
            l2="",
            l3_ref=None,
            tokens_estimate=0,
            risk=MaterialRisk(level="medium", flags=["unsupported"], reason=reason),
            permissions=MaterialPermissions(
                transform=False,
                store_l1=False,
                store_l2=False,
                notes=["content was not compiled by this lightweight profile"],
            ),
            provenance=MaterialProvenance(
                input_sha256=digest if input_bytes is not None else None,
                notes=[reason, "placeholder only; no remote fetch or external conversion occurred"],
            ),
            compiler_profile=_profile(),
            metadata=metadata or {},
            status=status,
        )


def _normalize(kind: CanonicalKind, text: str) -> tuple[str, dict[str, Any], list[str]]:
    risk_flags: list[str] = []
    if kind == "json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return text.strip(), {"json_valid": False}, ["invalid_json"]
        return (
            json.dumps(parsed, ensure_ascii=False, sort_keys=True, indent=2),
            {"json_valid": True, "json_type": type(parsed).__name__},
            risk_flags,
        )
    if kind == "csv":
        try:
            rows = _csv_rows(text)
        except csv.Error:
            return text.strip(), {"csv_valid": False}, ["invalid_csv"]
        normalized = "\n".join(",".join(row) for row in rows)
        return (
            normalized,
            {"csv_valid": True, "rows": len(rows), "columns": len(rows[0]) if rows else 0},
            risk_flags,
        )
    if kind == "html":
        plain = _HTMLTextExtractor.to_text(text)
        return plain, {"html_stripped": True}, risk_flags
    return text.strip(), {}, risk_flags


def _profile() -> CompilerProfile:
    return CompilerProfile(
        optional_backends=["markitdown"],
        unsupported_backends=["url_fetch", "ocr", "pdf_extract", "office_extract"],
        limitations=[
            "supports only lightweight text, markdown, html, json, and csv inputs",
            "urls are never fetched by this profile",
            "l3_ref is a placeholder until object storage is wired",
        ],
    )


def _kind_from_suffix(source_uri: str) -> str | None:
    suffix = Path(urlparse(source_uri).path).suffix.lower()
    return {
        ".txt": "plain_text",
        ".text": "plain_text",
        ".md": "markdown",
        ".markdown": "markdown",
        ".html": "html",
        ".htm": "html",
        ".json": "json",
        ".csv": "csv",
    }.get(suffix)


def _looks_csv(text: str) -> bool:
    try:
        sample = text[:2048]
        dialect = csv.Sniffer().sniff(sample)
        rows = [row for row in csv.reader(io.StringIO(text), dialect) if row]
    except csv.Error:
        return False
    if dialect.delimiter not in {",", "\t", ";"}:
        return False
    return len(rows) >= 2 and len(rows[0]) >= 2 and all(len(row) == len(rows[0]) for row in rows)


def _csv_rows(text: str) -> list[list[str]]:
    sample = text[:2048]
    dialect = csv.Sniffer().sniff(sample)
    return list(csv.reader(io.StringIO(text), dialect))


def _decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _read_safe_path(path: str | Path, allowed_root: str | Path) -> tuple[str, str, bytes | None]:
    root = Path(allowed_root).expanduser().resolve()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate

    try:
        resolved = candidate.resolve(strict=False)
        resolved.relative_to(root)
    except ValueError:
        return "path_traversal_blocked", str(path), None

    if not resolved.is_file():
        return "path_not_found_or_not_file", str(resolved), None
    return "ok", str(resolved), resolved.read_bytes()


def _make_l1(text: str) -> str:
    return " ".join(text.split())[:240]


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def _asset_id(tenant_id: str, kind: str, digest: str) -> str:
    short = hashlib.sha256(f"{tenant_id}:{kind}:{digest}".encode()).hexdigest()[:24]
    return f"asset_{short}"


class _HTMLTextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        stripped = data.strip()
        if stripped:
            self._parts.append(stripped)

    @classmethod
    def to_text(cls, html: str) -> str:
        parser = cls()
        parser.feed(html)
        return "\n".join(parser._parts)


__all__ = ["SUPPORTED_KINDS", "LightweightMaterialCompiler"]
