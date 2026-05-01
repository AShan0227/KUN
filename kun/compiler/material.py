"""Deterministic materialization helpers for the KUN V5 compiler slice."""

from __future__ import annotations

import csv
import hashlib
import html.parser
import io
import json
import os
from collections.abc import Awaitable, Callable
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

SUPPORTED_KINDS: set[str] = {"plain_text", "markdown", "html", "json", "csv", "pdf"}
_KIND_MAP: dict[str, CanonicalKind] = {
    "plain_text": "text",
    "markdown": "markdown",
    "html": "html",
    "json": "json",
    "csv": "csv",
    "pdf": "pdf",
}
UrlFetcher = Callable[[str, int], Awaitable[tuple[str, bytes]]]


class LightweightMaterialCompiler:
    """Compile small local inputs into canonical materials.

    This first V5 slice is intentionally deterministic and conservative. URL
    fetching is opt-in and allowlist-only; OCR and MarkItDown remain explicit
    optional future backends surfaced in the compiler profile.
    """

    def __init__(
        self,
        *,
        translator: RealWorldTranslator | None = None,
        url_fetcher: UrlFetcher | None = None,
        url_fetch_enabled: bool | None = None,
        allowed_url_hosts: set[str] | None = None,
        max_url_bytes: int = 1_000_000,
    ) -> None:
        self._translator = translator or RealWorldTranslator()
        self._url_fetcher = url_fetcher or _default_url_fetcher
        self._url_fetch_enabled = (
            _env_bool("KUN_COMPILER_URL_FETCH_ENABLED")
            if url_fetch_enabled is None
            else url_fetch_enabled
        )
        self._allowed_url_hosts = (
            _csv_set(os.getenv("KUN_COMPILER_URL_ALLOWED_HOSTS"))
            if allowed_url_hosts is None
            else {host.lower() for host in allowed_url_hosts}
        )
        self._max_url_bytes = max_url_bytes

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

    async def compile_bytes(
        self,
        raw: bytes,
        *,
        tenant_id: str,
        source_uri: str = "inline:bytes",
        declared_kind: str | None = None,
        mime_type: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> CanonicalMaterial:
        """Compile raw bytes without first flattening them into a text asset."""

        descriptor = await self._detect_bytes(
            raw,
            declared_kind=declared_kind,
            mime_type=mime_type,
            source_uri=source_uri,
        )
        text = _extract_material_text(descriptor, raw) if descriptor.kind in SUPPORTED_KINDS else ""
        return self._compile_detected(
            text,
            tenant_id=tenant_id,
            source=MaterialSource(
                type="bytes",
                uri=source_uri,
                detected_kind=descriptor.kind,
                mime_type=mime_type or descriptor.mime_type,
            ),
            descriptor=descriptor,
            input_bytes=raw,
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
        text = _extract_material_text(descriptor, raw) if descriptor.kind in SUPPORTED_KINDS else ""
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
        if parsed.scheme != "https" or not parsed.netloc:
            return self._rejected(
                tenant_id=tenant_id,
                source=MaterialSource(type="url", uri=url),
                reason="invalid_or_unsupported_url",
                risk_flags=["unsupported_url"],
                metadata=metadata,
            )
        host = (parsed.hostname or "").lower()
        if not self._url_fetch_enabled:
            return self._unsupported(
                tenant_id=tenant_id,
                source=MaterialSource(type="url", uri=url),
                reason="url_fetch_not_enabled",
                metadata=metadata,
                status="placeholder",
            )
        if not self._allowed_url_hosts or host not in self._allowed_url_hosts:
            return self._rejected(
                tenant_id=tenant_id,
                source=MaterialSource(type="url", uri=url),
                reason="url_host_not_allowlisted",
                risk_flags=["url_host_not_allowlisted"],
                metadata={
                    **(metadata or {}),
                    "host": host,
                    "allowed_hosts": sorted(self._allowed_url_hosts),
                },
            )
        content_type, raw = await self._url_fetcher(url, self._max_url_bytes)
        if len(raw) > self._max_url_bytes:
            return self._rejected(
                tenant_id=tenant_id,
                source=MaterialSource(type="url", uri=url),
                reason="url_response_too_large",
                risk_flags=["url_too_large"],
                metadata={**(metadata or {}), "max_url_bytes": self._max_url_bytes},
            )
        suffix_kind = _kind_from_suffix(parsed.path)
        content_kind = _kind_from_content_type(content_type)
        descriptor = self._descriptor_for(suffix_kind or content_kind or "plain_text")
        text = raw.decode("utf-8", errors="replace")
        return self._compile_detected(
            text,
            tenant_id=tenant_id,
            source=MaterialSource(
                type="url",
                uri=url,
                detected_kind=descriptor.kind,
                mime_type=content_type or descriptor.mime_type,
            ),
            descriptor=descriptor,
            input_bytes=raw,
            metadata={
                **(metadata or {}),
                "url_fetch_enabled": True,
                "host": host,
                "allowed_hosts": sorted(self._allowed_url_hosts),
                "content_type": content_type,
                "bytes": len(raw),
            },
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

    async def _detect_bytes(
        self,
        raw: bytes,
        *,
        declared_kind: str | None,
        mime_type: str | None,
        source_uri: str,
    ) -> InputDescriptor:
        if declared_kind:
            return self._descriptor_for(declared_kind, mime_type=mime_type)
        suffix_kind = _kind_from_suffix(source_uri)
        if suffix_kind is not None:
            return self._descriptor_for(suffix_kind, mime_type=mime_type)
        content_kind = _kind_from_content_type(mime_type or "")
        if content_kind is not None:
            return self._descriptor_for(content_kind, mime_type=mime_type)
        return await self._translator.detect_file_kind(raw)

    def _descriptor_for(self, kind: str, *, mime_type: str | None = None) -> InputDescriptor:
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
        default_mime_type = {
            "plain_text": "text/plain",
            "markdown": "text/markdown",
            "html": "text/html",
            "json": "application/json",
            "csv": "text/csv",
            "pdf": "application/pdf",
        }.get(normalized, "application/octet-stream")
        return InputDescriptor(
            kind=normalized,
            mime_type=mime_type or default_mime_type,
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
                notes=_provenance_notes(kind),
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
    if kind == "pdf":
        metadata: dict[str, Any] = {"pdf_text_extract_limited": True}
        if text.startswith("PDF document; text extraction unavailable"):
            risk_flags.append("pdf_text_unavailable")
            metadata["pdf_text_unavailable"] = True
        return text.strip(), metadata, risk_flags
    return text.strip(), {}, risk_flags


async def _default_url_fetcher(url: str, max_bytes: int) -> tuple[str, bytes]:
    import httpx

    async with httpx.AsyncClient(follow_redirects=False, timeout=15.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        raw = response.content
    if len(raw) > max_bytes:
        return response.headers.get("content-type", ""), raw[: max_bytes + 1]
    return response.headers.get("content-type", ""), raw


def _kind_from_content_type(content_type: str) -> str | None:
    value = content_type.split(";", 1)[0].strip().lower()
    if value in {"text/plain"}:
        return "plain_text"
    if value in {"text/markdown", "text/x-markdown"}:
        return "markdown"
    if value in {"text/html", "application/xhtml+xml"}:
        return "html"
    if value in {"application/json", "application/ld+json"} or value.endswith("+json"):
        return "json"
    if value in {"text/csv", "application/csv"}:
        return "csv"
    if value == "application/pdf":
        return "pdf"
    return None


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _csv_set(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _profile() -> CompilerProfile:
    return CompilerProfile(
        optional_backends=["markitdown"],
        unsupported_backends=["ocr", "office_extract"],
        limitations=[
            "supports lightweight text, markdown, html, json, csv, and local PDF text summary inputs",
            "urls are fetched only when explicitly enabled and host-allowlisted",
            "pdf support is deterministic text extraction only; scanned/OCR PDFs need a future backend",
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
        ".pdf": "pdf",
    }.get(suffix)


def _extract_material_text(descriptor: InputDescriptor, raw: bytes) -> str:
    if descriptor.kind == "pdf":
        return _extract_pdf_text(raw)
    return _decode_bytes(raw)


def _extract_pdf_text(raw: bytes) -> str:
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw))
        page_texts: list[str] = []
        for page in reader.pages[:5]:
            text = (page.extract_text() or "").strip()
            if text:
                page_texts.append(text)
        if page_texts:
            return "\n\n".join(page_texts)[:8000]
        return f"PDF document; pages: {len(reader.pages)}"
    except Exception:
        return "PDF document; text extraction unavailable"


def _provenance_notes(kind: CanonicalKind) -> list[str]:
    if kind == "pdf":
        return [
            "local pypdf text extraction invoked",
            "no OCR or external reformat backend invoked",
        ]
    return ["no external reformat backend invoked"]


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
