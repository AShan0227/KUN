"""File-copy workspace snapshots and rollback restore for KUN V6.

Hash manifests are useful evidence, but long-running autonomous work needs a
real rollback handle.  This module captures bounded file-copy snapshots for a
work item and restores them when a rollback item is executed.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from kun.control_plane.v6 import ArtifactRecord, WorkItem

if TYPE_CHECKING:
    from kun.control_plane.runtime import InMemoryControlPlane

_EXCLUDED_PARTS: frozenset[str] = frozenset(
    {
        ".git",
        ".kun-local",
        ".next",
        ".pytest_cache",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "workspace_snapshots",
    }
)
_MAX_FILE_COUNT = 2000
_MAX_FILE_BYTES = 2_000_000
_MAX_TOTAL_BYTES = 25_000_000


class WorkspaceSnapshotResult(BaseModel):
    """Metadata for one materialized workspace snapshot."""

    model_config = ConfigDict(extra="forbid")

    artifact: ArtifactRecord
    manifest_path: str
    snapshot_dir: str
    copied_file_count: int
    omitted_file_count: int
    total_copied_bytes: int


class WorkspaceRestoreResult(BaseModel):
    """Result of restoring a workspace from a materialized snapshot."""

    model_config = ConfigDict(extra="forbid")

    workspace: str
    snapshot_artifact_id: str
    restored_file_count: int = 0
    removed_extra_file_count: int = 0
    skipped_extra_file_count: int = 0
    manifest_path: str
    complete_restore: bool
    warnings: list[str] = Field(default_factory=list)


def create_workspace_snapshot(
    *,
    control_plane: InMemoryControlPlane,
    work_item: WorkItem,
    workspace_path: str,
    actor: str,
    observed_at: datetime,
) -> WorkspaceSnapshotResult | None:
    """Create a bounded file-copy snapshot and return the artifact record."""

    workspace = Path(os.path.expanduser(workspace_path)).resolve()
    if not workspace.exists() or not workspace.is_dir():
        return None
    snapshot_id = f"snapshot-{_slug(work_item.work_item_id)}-{_compact_time(observed_at)}"
    snapshot_dir = _snapshot_root(control_plane) / work_item.mission_id / snapshot_id
    files_dir = snapshot_dir / "files"
    manifest_path = snapshot_dir / "manifest.json"
    files: list[dict[str, Any]] = []
    omitted = 0
    total_bytes = 0
    for path in sorted(workspace.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        rel = path.relative_to(workspace)
        if _skip_snapshot_path(rel):
            omitted += 1
            continue
        try:
            stat = path.stat()
        except OSError:
            omitted += 1
            continue
        if (
            len(files) >= _MAX_FILE_COUNT
            or stat.st_size > _MAX_FILE_BYTES
            or total_bytes + stat.st_size > _MAX_TOTAL_BYTES
        ):
            omitted += 1
            continue
        target = files_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        digest = _file_sha256(target)
        files.append(
            {
                "path": rel.as_posix(),
                "size": stat.st_size,
                "sha256": digest,
                "snapshot_relpath": str(Path("files") / rel),
            }
        )
        total_bytes += stat.st_size
    manifest = {
        "schema": "kun-workspace-snapshot-v2",
        "created_at": observed_at.isoformat(),
        "mission_id": work_item.mission_id,
        "work_item_id": work_item.work_item_id,
        "workspace": str(workspace),
        "snapshot_dir": str(snapshot_dir),
        "restore_mode": "file_copy",
        "complete_restore": omitted == 0,
        "copied_file_count": len(files),
        "omitted_file_count": omitted,
        "total_copied_bytes": total_bytes,
        "limits": {
            "max_file_count": _MAX_FILE_COUNT,
            "max_file_bytes": _MAX_FILE_BYTES,
            "max_total_bytes": _MAX_TOTAL_BYTES,
        },
        "files": files,
    }
    _write_json_atomic(manifest_path, manifest)
    artifact = ArtifactRecord(
        artifact_id=f"artifact-checkpoint-{_slug(work_item.work_item_id)}-{_compact_time(observed_at)}",
        kind="snapshot",
        path_or_uri=str(manifest_path),
        content_hash=_hash_payload(manifest),
        created_by=actor,
        mission_id=work_item.mission_id,
        work_item_id=work_item.work_item_id,
        supports=[
            "workspace_snapshot",
            "workspace_checkpoint",
            "rollback_reference",
            "sandbox_boundary",
            "runtime_feature_activation",
            "restore_mode:file_copy",
        ],
        freshness="fresh",
        source_quality="primary",
    )
    return WorkspaceSnapshotResult(
        artifact=artifact,
        manifest_path=str(manifest_path),
        snapshot_dir=str(snapshot_dir),
        copied_file_count=len(files),
        omitted_file_count=omitted,
        total_copied_bytes=total_bytes,
    )


def restore_workspace_snapshot(snapshot_artifact: ArtifactRecord) -> WorkspaceRestoreResult:
    """Restore a workspace from a KUN workspace snapshot artifact."""

    manifest_path = Path(os.path.expanduser(snapshot_artifact.path_or_uri)).resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("restore_mode") != "file_copy":
        raise ValueError("workspace snapshot is not file-copy restorable")
    workspace = Path(os.path.expanduser(str(manifest["workspace"]))).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    snapshot_dir = Path(os.path.expanduser(str(manifest["snapshot_dir"]))).resolve()
    expected_paths = {
        _safe_relpath(str(file_record["path"]))
        for file_record in manifest.get("files", [])
        if isinstance(file_record, dict) and file_record.get("path")
    }
    complete_restore = bool(manifest.get("complete_restore"))
    removed = 0
    skipped = 0
    if complete_restore:
        for current in sorted(workspace.rglob("*"), reverse=True):
            if not current.is_file() or current.is_symlink():
                continue
            rel = current.relative_to(workspace)
            if _skip_snapshot_path(rel) or rel in expected_paths:
                skipped += 1
                continue
            current.unlink()
            removed += 1
        _remove_empty_dirs(workspace)
    restored = 0
    warnings: list[str] = []
    for file_record in manifest.get("files", []):
        if not isinstance(file_record, dict):
            continue
        rel = _safe_relpath(str(file_record["path"]))
        source = snapshot_dir / _safe_relpath(str(file_record["snapshot_relpath"]))
        if not source.exists():
            raise ValueError(f"snapshot source file missing: {source}")
        expected_hash = file_record.get("sha256")
        if isinstance(expected_hash, str) and _file_sha256(source) != expected_hash:
            raise ValueError(f"snapshot source hash mismatch: {rel.as_posix()}")
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        restored += 1
    if not complete_restore:
        warnings.append("Snapshot hit capture limits; rollback restored copied files without deleting extras.")
    return WorkspaceRestoreResult(
        workspace=str(workspace),
        snapshot_artifact_id=snapshot_artifact.artifact_id,
        restored_file_count=restored,
        removed_extra_file_count=removed,
        skipped_extra_file_count=skipped,
        manifest_path=str(manifest_path),
        complete_restore=complete_restore,
        warnings=warnings,
    )


def _snapshot_root(control_plane: InMemoryControlPlane) -> Path:
    store = getattr(control_plane, "store", None)
    store_path = getattr(store, "_path", None)
    if store_path is not None:
        return Path(store_path).parent / "workspace_snapshots"
    return Path(".kun-local") / "workspace_snapshots"


def _skip_snapshot_path(path: Path) -> bool:
    return bool(set(path.parts) & _EXCLUDED_PARTS)


def _safe_relpath(value: str) -> Path:
    rel = Path(value)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"unsafe snapshot path: {value}")
    return rel


def _remove_empty_dirs(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir() and not _skip_snapshot_path(path.relative_to(root)):
            with suppress(OSError):
                path.rmdir()


def _hash_payload(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(text)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        tmp = Path(handle.name)
    os.replace(tmp, path)


def _compact_time(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%SZ")


def _slug(value: str) -> str:
    safe = [ch if ch.isalnum() or ch in {"-", "_"} else "-" for ch in value]
    return "".join(safe).strip("-")[:80] or "item"


__all__ = [
    "WorkspaceRestoreResult",
    "WorkspaceSnapshotResult",
    "create_workspace_snapshot",
    "restore_workspace_snapshot",
]
