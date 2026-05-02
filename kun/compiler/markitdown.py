"""Optional MarkItDown compiler backend.

This adapter is deliberately opt-in. It never claims MarkItDown support unless
the caller enables the backend and the package can be imported at runtime.
"""

from __future__ import annotations

import importlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import anyio

from kun.compiler.material import LightweightMaterialCompiler, _read_safe_path
from kun.compiler.models import (
    CanonicalMaterial,
    CompilerProfile,
    CompileStatus,
    MaterialProvenance,
    MaterialSource,
)

BackendStatus = Literal["disabled", "available", "unavailable"]
ConverterFactory = Callable[[], Any]


class MarkItDownMaterialCompiler:
    """Compile safe local paths through the optional MarkItDown package."""

    backend_name = "markitdown"

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        converter_factory: ConverterFactory | None = None,
        fallback_compiler: LightweightMaterialCompiler | None = None,
    ) -> None:
        self._enabled = _env_bool("KUN_COMPILER_MARKITDOWN_ENABLED") if enabled is None else enabled
        self._converter_factory = converter_factory or _load_markitdown_converter
        self._fallback = fallback_compiler or LightweightMaterialCompiler()

    def backend_status(self) -> dict[str, str]:
        """Return the configured status without importing optional dependencies."""

        if not self._enabled:
            return _backend_status(
                "disabled",
                "MarkItDown backend is not enabled. Set KUN_COMPILER_MARKITDOWN_ENABLED=1 "
                "or pass enabled=True to use it.",
            )
        return _backend_status("available", "MarkItDown backend is enabled.")

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
        source = MaterialSource(type="path", uri=source_uri)
        if path_status == "path_traversal_blocked":
            return self._fallback._rejected(
                tenant_id=tenant_id,
                source=source,
                reason="path_traversal_blocked",
                risk_flags=["path_traversal"],
                metadata=_merge_backend_status(
                    metadata,
                    _backend_status("unavailable", "Path is outside the allowed root."),
                ),
            )
        if path_status != "ok" or raw is None:
            return self._fallback._rejected(
                tenant_id=tenant_id,
                source=source,
                reason="path_not_found_or_not_file",
                risk_flags=["invalid_path"],
                metadata=_merge_backend_status(
                    metadata,
                    _backend_status("unavailable", "Path was not found or is not a file."),
                ),
            )

        if not self._enabled:
            return self._backend_unavailable_material(
                tenant_id=tenant_id,
                source=source,
                reason="markitdown_backend_not_enabled",
                status="unsupported",
                input_bytes=raw,
                metadata=metadata,
                backend_status=_backend_status(
                    "disabled",
                    "MarkItDown backend is not enabled. Set "
                    "KUN_COMPILER_MARKITDOWN_ENABLED=1 or pass enabled=True to use it.",
                ),
            )

        try:
            converter = self._converter_factory()
        except ImportError:
            return self._backend_unavailable_material(
                tenant_id=tenant_id,
                source=source,
                reason="markitdown_package_not_installed",
                status="unavailable",
                input_bytes=raw,
                metadata=metadata,
                backend_status=_backend_status(
                    "unavailable",
                    "The markitdown package is not installed in this runtime.",
                ),
            )
        except Exception as exc:
            return self._backend_unavailable_material(
                tenant_id=tenant_id,
                source=source,
                reason="markitdown_backend_initialization_failed",
                status="unavailable",
                input_bytes=raw,
                metadata=metadata,
                backend_status=_backend_status(
                    "unavailable",
                    f"MarkItDown backend could not be initialized: {exc}",
                ),
            )

        try:
            text = await anyio.to_thread.run_sync(_convert_path_to_text, converter, source_uri)
        except Exception as exc:
            return self._backend_unavailable_material(
                tenant_id=tenant_id,
                source=source,
                reason="markitdown_conversion_failed",
                status="unavailable",
                input_bytes=raw,
                metadata=metadata,
                backend_status=_backend_status(
                    "unavailable",
                    f"MarkItDown failed to convert this file: {exc}",
                ),
            )

        backend_status = _backend_status(
            "available",
            "MarkItDown converted the local file to markdown text.",
        )
        descriptor = self._fallback._descriptor_for("markdown")
        material = self._fallback._compile_detected(
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
            metadata=_merge_backend_status(
                {
                    **(metadata or {}),
                    "markitdown_source_bytes": len(raw),
                },
                backend_status,
            ),
        )
        return material.model_copy(
            update={
                "provenance": MaterialProvenance(
                    input_sha256=material.provenance.input_sha256,
                    detector=material.provenance.detector,
                    backend=self.backend_name,
                    notes=[
                        "markitdown conversion invoked for local path",
                        "no OCR support is asserted by this adapter",
                    ],
                ),
                "compiler_profile": CompilerProfile(
                    name="kun-v5-markitdown-adapter",
                    version="0.1",
                    optional_backends=["markitdown"],
                    unsupported_backends=["ocr"],
                    limitations=[
                        "MarkItDown is optional and must be explicitly enabled",
                        "backend is unavailable when the markitdown package is not installed",
                        "local paths are accepted only under the caller-provided allowed root",
                        "OCR support is not claimed by this adapter",
                    ],
                ),
            }
        )

    def _backend_unavailable_material(
        self,
        *,
        tenant_id: str,
        source: MaterialSource,
        reason: str,
        status: CompileStatus,
        input_bytes: bytes,
        metadata: dict[str, Any] | None,
        backend_status: dict[str, str],
    ) -> CanonicalMaterial:
        return self._fallback._unsupported(
            tenant_id=tenant_id,
            source=source,
            reason=reason,
            input_bytes=input_bytes,
            metadata=_merge_backend_status(metadata, backend_status),
            status=status,
        )


def _load_markitdown_converter() -> Any:
    module = importlib.import_module("markitdown")
    return module.MarkItDown()


def _convert_path_to_text(converter: Any, source_uri: str) -> str:
    result = converter.convert(source_uri)
    if isinstance(result, str):
        return result
    for attribute in ("text_content", "markdown", "text"):
        value = getattr(result, attribute, None)
        if isinstance(value, str):
            return value
    raise ValueError("MarkItDown result did not expose text_content, markdown, or text")


def _backend_status(status: BackendStatus, reason: str) -> dict[str, str]:
    return {"name": "markitdown", "status": status, "reason": reason}


def _merge_backend_status(
    metadata: dict[str, Any] | None,
    backend_status: dict[str, str],
) -> dict[str, Any]:
    return {**(metadata or {}), "backend_status": backend_status}


def _env_bool(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


__all__ = ["MarkItDownMaterialCompiler"]
