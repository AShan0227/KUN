"""Repeatable compiler sync sources.

This is the first honest enterprise-ingestion bridge: it does not connect to
SharePoint/Drive/Notion yet.  It lets operators define a bounded manifest-file
source that can be run repeatedly by CLI or a future scheduler.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import anyio
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from kun.compiler.batch import CompilerBatchIngestor, CompilerBatchManifest, CompilerBatchReport
from kun.context.assets import AssetLayer

CompilerSyncSourceType = Literal["manifest_file"]
CompilerSyncStatus = Literal["synced", "skipped_disabled", "error"]


class CompilerSyncSource(BaseModel):
    """Config for one repeatable compiler sync source."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    tenant_id: str
    type: CompilerSyncSourceType = "manifest_file"
    manifest_path: str
    enabled: bool = True
    allowed_root: str | None = None
    layer: AssetLayer | None = None
    metadata: dict[str, object] = Field(default_factory=dict)


class CompilerSyncReport(BaseModel):
    """Result for one sync source run."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    tenant_id: str
    source_type: CompilerSyncSourceType
    status: CompilerSyncStatus
    reason: str = ""
    manifest_path: str = ""
    started_at: datetime
    finished_at: datetime
    batch_report: CompilerBatchReport | None = None


class CompilerSyncRunner:
    """Run repeatable compiler sync sources."""

    def __init__(self, *, batch_ingestor: CompilerBatchIngestor | None = None) -> None:
        self._batch_ingestor = batch_ingestor or CompilerBatchIngestor()

    async def sync_source_file(
        self,
        config_path: str | Path,
        *,
        config_root: str | Path | None = None,
        tenant_override: str | None = None,
    ) -> CompilerSyncReport:
        root = await anyio.to_thread.run_sync(_resolve_config_root, config_path, config_root)
        source_config_path = await anyio.to_thread.run_sync(
            _resolve_under_root,
            config_path,
            root,
        )
        payload = await anyio.to_thread.run_sync(_read_json_object, source_config_path)
        source = CompilerSyncSource.model_validate(payload)
        if tenant_override:
            source = source.model_copy(update={"tenant_id": tenant_override})
        return await self.sync_source(source, config_root=root)

    async def sync_source(
        self,
        source: CompilerSyncSource,
        *,
        config_root: str | Path,
    ) -> CompilerSyncReport:
        started_at = datetime.now(UTC)
        manifest_path = ""
        try:
            if not source.enabled:
                return CompilerSyncReport(
                    source_id=source.source_id,
                    tenant_id=source.tenant_id,
                    source_type=source.type,
                    status="skipped_disabled",
                    reason="sync_source_disabled",
                    manifest_path="",
                    started_at=started_at,
                    finished_at=datetime.now(UTC),
                )
            root = await anyio.to_thread.run_sync(lambda: Path(config_root).resolve())
            manifest_file = await anyio.to_thread.run_sync(
                _resolve_under_root,
                source.manifest_path,
                root,
            )
            manifest_path = str(manifest_file)
            manifest_payload = await anyio.to_thread.run_sync(_read_json_object, manifest_file)
            if source.tenant_id:
                manifest_payload["tenant_id"] = source.tenant_id
            if source.allowed_root:
                manifest_payload["allowed_root"] = source.allowed_root
            if source.layer is not None:
                manifest_payload["layer"] = source.layer.value
            raw_manifest_metadata = manifest_payload.get("metadata")
            manifest_metadata = (
                dict(raw_manifest_metadata) if isinstance(raw_manifest_metadata, dict) else {}
            )
            manifest_payload["metadata"] = {
                **manifest_metadata,
                **source.metadata,
                "compiler_sync_source_id": source.source_id,
                "compiler_sync_source_type": source.type,
                "compiler_sync_run_at": started_at.isoformat(),
            }
            manifest = CompilerBatchManifest.model_validate(manifest_payload)
            report = await self._batch_ingestor.ingest_manifest(manifest)
            return CompilerSyncReport(
                source_id=source.source_id,
                tenant_id=source.tenant_id,
                source_type=source.type,
                status="synced",
                reason="manifest_file_synced",
                manifest_path=manifest_path,
                started_at=started_at,
                finished_at=datetime.now(UTC),
                batch_report=report,
            )
        except (OSError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            return CompilerSyncReport(
                source_id=getattr(source, "source_id", "unknown"),
                tenant_id=getattr(source, "tenant_id", ""),
                source_type=getattr(source, "type", "manifest_file"),
                status="error",
                reason=f"{type(exc).__name__}: {exc}",
                manifest_path=manifest_path,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )


def _read_json_object(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root must be an object")
    return payload


def _resolve_config_root(
    config_path: str | Path,
    config_root: str | Path | None,
) -> Path:
    if config_root is not None:
        return Path(config_root).resolve()
    return Path(config_path).parent.resolve()


def _resolve_under_root(path: str | Path, root: Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise ValueError("sync source path is outside config_root")
    if not resolved.is_file():
        raise FileNotFoundError(str(resolved))
    return resolved


__all__ = [
    "CompilerSyncReport",
    "CompilerSyncRunner",
    "CompilerSyncSource",
    "CompilerSyncSourceType",
    "CompilerSyncStatus",
]
