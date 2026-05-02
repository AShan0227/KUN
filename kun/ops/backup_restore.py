"""Local backup / restore drill helpers.

This module is intentionally small and filesystem-only.  It does not pretend to
be a cloud backup service; it proves that a production operator can generate a
verifiable package for important local config paths, then run a no-write restore
dry-run that catches missing files or checksum drift before any overwrite.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, BinaryIO, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from kun.core.object_store import ObjectRef, get_object_store

MANIFEST_VERSION = "kun.backup_restore.v1"
PAYLOAD_PREFIX = "payload"
DEFAULT_EXCLUDED_NAMES = {
    ".env",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


class BackupFileEntry(BaseModel):
    """One file recorded in a backup manifest."""

    model_config = ConfigDict(extra="forbid")

    path: str
    size_bytes: int
    sha256: str
    mtime_ns: int


class BackupManifest(BaseModel):
    """Manifest for one local backup drill package."""

    model_config = ConfigDict(extra="forbid")

    version: str = MANIFEST_VERSION
    created_at: str
    repo_root: str
    archive_path: str
    archive_sha256: str
    allowed_roots: list[str] = Field(default_factory=list)
    file_count: int
    total_bytes: int
    files: list[BackupFileEntry] = Field(default_factory=list)


class RestoreDryRunReport(BaseModel):
    """No-write restore validation report."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "warn", "block"]
    manifest_path: str
    archive_path: str
    archive_exists: bool
    archive_sha256_ok: bool = False
    file_count: int = 0
    missing_from_archive: list[str] = Field(default_factory=list)
    sha256_mismatches: list[str] = Field(default_factory=list)
    unsafe_paths: list[str] = Field(default_factory=list)
    would_restore_count: int = 0
    would_overwrite: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ObjectStoreBackupRoundTripReport(BaseModel):
    """Upload/download verification report for a backup package.

    This is still a drill: it proves the configured object store can accept the
    package, return the exact bytes, and pass the same no-write restore check.
    It does not claim that production DB/S3 retention policies are complete.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "warn", "block"]
    manifest_path: str
    archive_path: str
    object_prefix: str
    archive_object_uri: str | None = None
    manifest_object_uri: str | None = None
    archive_download_sha256_ok: bool = False
    manifest_download_sha256_ok: bool = False
    restore_status: Literal["pass", "warn", "block"] | None = None
    restore_report: RestoreDryRunReport | None = None
    notes: list[str] = Field(default_factory=list)


class BackupDrillFreshnessReport(BaseModel):
    """Freshness check for the latest local backup drill manifest."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "warn", "block"]
    backup_dir: str
    latest_manifest_path: str | None = None
    latest_archive_path: str | None = None
    latest_created_at: str | None = None
    age_hours: float | None = None
    max_age_hours: float
    archive_exists: bool = False
    notes: list[str] = Field(default_factory=list)


class BackupObjectStore(Protocol):
    """Small protocol so unit tests can use an in-memory fake store."""

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> ObjectRef: ...

    async def get_bytes(self, ref: ObjectRef | str) -> bytes: ...


def create_backup_package(
    *,
    source_paths: list[Path],
    output_dir: Path,
    repo_root: Path | None = None,
    allowed_roots: list[Path] | None = None,
    package_name: str | None = None,
) -> BackupManifest:
    """Create a tar.gz package and external JSON manifest.

    Every source must live under one of ``allowed_roots``.  Paths are recorded
    relative to ``repo_root`` so packages are portable across machines.
    """

    root = (repo_root or Path.cwd()).resolve()
    allowed = [_resolve_under_root(item, root) for item in (allowed_roots or source_paths)]
    output_dir.mkdir(parents=True, exist_ok=True)
    files = _collect_files(source_paths=source_paths, repo_root=root, allowed_roots=allowed)
    if not files:
        raise ValueError("backup source paths did not contain any files")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base_name = package_name or f"kun-backup-drill-{stamp}"
    archive_path = (output_dir / f"{base_name}.tar.gz").resolve()
    manifest_path = (output_dir / f"{base_name}.manifest.json").resolve()

    entries = [_entry_for(path, root) for path in files]
    with tarfile.open(archive_path, "w:gz") as tar:
        for path, entry in zip(files, entries, strict=True):
            tar.add(path, arcname=f"{PAYLOAD_PREFIX}/{entry.path}", recursive=False)

    archive_sha = sha256_file(archive_path)
    manifest = BackupManifest(
        created_at=datetime.now(UTC).isoformat(),
        repo_root=str(root),
        archive_path=str(archive_path),
        archive_sha256=archive_sha,
        allowed_roots=[_relative_posix(item, root) for item in allowed],
        file_count=len(entries),
        total_bytes=sum(item.size_bytes for item in entries),
        files=entries,
    )
    manifest_path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return manifest


async def object_store_roundtrip_drill(
    *,
    manifest_path: Path,
    restore_root: Path,
    object_prefix: str = "backup-drills",
    scratch_dir: Path | None = None,
    object_store: BackupObjectStore | None = None,
) -> ObjectStoreBackupRoundTripReport:
    """Upload a backup package to object storage, download it, then dry-run restore.

    The downloaded manifest is rewritten only in the local scratch directory so
    ``restore_dry_run`` validates the downloaded archive, not the original local
    archive. No restore files are written.
    """

    manifest = load_manifest(manifest_path)
    archive_path = Path(manifest.archive_path)
    notes: list[str] = []
    if not await asyncio.to_thread(archive_path.exists):
        return ObjectStoreBackupRoundTripReport(
            status="block",
            manifest_path=str(manifest_path),
            archive_path=str(archive_path),
            object_prefix=_normalize_object_prefix(object_prefix),
            notes=["archive path from manifest does not exist"],
        )

    store = object_store or get_object_store()
    prefix = _normalize_object_prefix(object_prefix)
    archive_key = f"{prefix}/{archive_path.name}"
    manifest_key = f"{prefix}/{manifest_path.name}"
    archive_bytes = await asyncio.to_thread(archive_path.read_bytes)
    manifest_bytes = await asyncio.to_thread(manifest_path.read_bytes)

    archive_ref = await store.put_bytes(
        archive_key,
        archive_bytes,
        content_type="application/gzip",
    )
    manifest_ref = await store.put_bytes(
        manifest_key,
        manifest_bytes,
        content_type="application/json",
    )

    downloaded_archive = await store.get_bytes(archive_ref)
    downloaded_manifest = await store.get_bytes(manifest_ref)
    archive_ok = hashlib.sha256(downloaded_archive).hexdigest() == manifest.archive_sha256
    manifest_ok = (
        hashlib.sha256(downloaded_manifest).hexdigest()
        == hashlib.sha256(manifest_bytes).hexdigest()
    )
    if not archive_ok:
        notes.append("downloaded archive sha256 does not match manifest")
    if not manifest_ok:
        notes.append("downloaded manifest sha256 does not match uploaded manifest")

    scratch = (scratch_dir or (manifest_path.parent / ".object-store-roundtrip")).resolve()
    await asyncio.to_thread(scratch.mkdir, parents=True, exist_ok=True)
    downloaded_archive_path = scratch / archive_path.name
    downloaded_manifest_path = scratch / manifest_path.name
    await asyncio.to_thread(downloaded_archive_path.write_bytes, downloaded_archive)

    parsed_manifest = BackupManifest.model_validate_json(downloaded_manifest.decode("utf-8"))
    parsed_manifest = parsed_manifest.model_copy(
        update={"archive_path": str(downloaded_archive_path)}
    )
    await asyncio.to_thread(
        downloaded_manifest_path.write_text,
        parsed_manifest.model_dump_json(indent=2),
        encoding="utf-8",
    )

    restore_report = await asyncio.to_thread(
        restore_dry_run,
        manifest_path=downloaded_manifest_path,
        restore_root=restore_root,
    )
    if restore_report.status == "block":
        notes.append("downloaded package failed restore dry-run")
    elif restore_report.status == "warn":
        notes.append("downloaded package restore dry-run passed with overwrite warnings")

    status: Literal["pass", "warn", "block"]
    if not archive_ok or not manifest_ok or restore_report.status == "block":
        status = "block"
    elif restore_report.status == "warn":
        status = "warn"
    else:
        status = "pass"
    return ObjectStoreBackupRoundTripReport(
        status=status,
        manifest_path=str(manifest_path),
        archive_path=str(archive_path),
        object_prefix=prefix,
        archive_object_uri=archive_ref.uri,
        manifest_object_uri=manifest_ref.uri,
        archive_download_sha256_ok=archive_ok,
        manifest_download_sha256_ok=manifest_ok,
        restore_status=restore_report.status,
        restore_report=restore_report,
        notes=notes,
    )


def load_manifest(manifest_path: Path) -> BackupManifest:
    """Load and validate a manifest JSON file."""

    return BackupManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))


def check_backup_drill_freshness(
    *,
    backup_dir: Path,
    max_age_hours: float = 168.0,
    require_recent: bool = False,
    now: datetime | None = None,
) -> BackupDrillFreshnessReport:
    """Check whether a recent backup drill manifest exists.

    This does not replace ``restore_dry_run``.  It is a cheap release/readiness
    guard that catches the common fake-safe state: scripts exist, but nobody has
    actually produced a recent manifest and archive.
    """

    root = backup_dir.resolve()
    severity_when_missing: Literal["warn", "block"] = "block" if require_recent else "warn"
    if not root.exists():
        return BackupDrillFreshnessReport(
            status=severity_when_missing,
            backup_dir=str(root),
            max_age_hours=max_age_hours,
            notes=["backup drill directory does not exist"],
        )

    manifests = sorted(root.glob("*.manifest.json"))
    if not manifests:
        return BackupDrillFreshnessReport(
            status=severity_when_missing,
            backup_dir=str(root),
            max_age_hours=max_age_hours,
            notes=["no backup drill manifest found"],
        )

    latest: tuple[datetime, Path, BackupManifest] | None = None
    invalid: list[str] = []
    for path in manifests:
        try:
            manifest = load_manifest(path)
            created = _parse_manifest_created_at(manifest.created_at)
        except Exception as exc:  # pragma: no cover - defensive against corrupt local files
            invalid.append(f"{path.name}: {exc!r}")
            continue
        if latest is None or created > latest[0]:
            latest = (created, path, manifest)

    if latest is None:
        return BackupDrillFreshnessReport(
            status="block" if require_recent else "warn",
            backup_dir=str(root),
            max_age_hours=max_age_hours,
            notes=["backup drill manifests exist but none could be parsed", *invalid[:5]],
        )

    current = now or datetime.now(UTC)
    created, manifest_path, manifest = latest
    age_hours = max(0.0, (current - created).total_seconds() / 3600.0)
    archive_path = Path(manifest.archive_path)
    archive_exists = archive_path.exists()
    stale = age_hours > max_age_hours
    notes: list[str] = []
    if invalid:
        notes.append("ignored invalid manifests: " + "; ".join(invalid[:3]))
    if stale:
        notes.append(f"latest backup drill is stale: {age_hours:.1f}h old")
    if not archive_exists:
        notes.append("latest backup drill archive is missing")

    status: Literal["pass", "warn", "block"] = (
        ("block" if require_recent else "warn") if not archive_exists or stale else "pass"
    )
    return BackupDrillFreshnessReport(
        status=status,
        backup_dir=str(root),
        latest_manifest_path=str(manifest_path),
        latest_archive_path=str(archive_path),
        latest_created_at=manifest.created_at,
        age_hours=round(age_hours, 3),
        max_age_hours=max_age_hours,
        archive_exists=archive_exists,
        notes=notes,
    )


def restore_dry_run(
    *,
    manifest_path: Path,
    restore_root: Path,
) -> RestoreDryRunReport:
    """Validate a backup package without writing restored files."""

    manifest = load_manifest(manifest_path)
    archive_path = Path(manifest.archive_path)
    notes: list[str] = []
    manifest_shape_errors: list[str] = []
    if manifest.version != MANIFEST_VERSION:
        manifest_shape_errors.append(f"unexpected manifest version: {manifest.version}")
    if manifest.file_count != len(manifest.files):
        manifest_shape_errors.append(
            f"manifest file_count={manifest.file_count} but files={len(manifest.files)}"
        )
    if not archive_path.exists():
        return RestoreDryRunReport(
            status="block",
            manifest_path=str(manifest_path),
            archive_path=str(archive_path),
            archive_exists=False,
            file_count=manifest.file_count,
            missing_from_archive=[item.path for item in manifest.files],
            notes=[*notes, *manifest_shape_errors],
        )

    archive_sha256_ok = sha256_file(archive_path) == manifest.archive_sha256
    if not archive_sha256_ok:
        notes.append("archive sha256 does not match manifest")

    restore_base = restore_root.resolve()
    missing: list[str] = []
    mismatches: list[str] = []
    unsafe: list[str] = []
    would_overwrite: list[str] = []

    expected_members = {f"{PAYLOAD_PREFIX}/{entry.path}" for entry in manifest.files}
    extra_members: list[str] = []
    non_file_members: list[str] = []
    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()
        names = {member.name for member in members}
        for member in members:
            if member.name not in expected_members:
                extra_members.append(member.name)
            if member.name in expected_members and not member.isfile():
                non_file_members.append(member.name)
        for entry in manifest.files:
            member_name = f"{PAYLOAD_PREFIX}/{entry.path}"
            target = (restore_base / entry.path).resolve()
            if not _is_relative_to(target, restore_base):
                unsafe.append(entry.path)
                continue
            if target.exists():
                would_overwrite.append(entry.path)
            if member_name not in names:
                missing.append(entry.path)
                continue
            member = tar.getmember(member_name)
            extracted = tar.extractfile(member)
            if extracted is None:
                missing.append(entry.path)
                continue
            if sha256_stream(extracted) != entry.sha256:
                mismatches.append(entry.path)

    if manifest_shape_errors:
        notes.extend(manifest_shape_errors)
    if extra_members:
        notes.append("archive contains unexpected members: " + ", ".join(extra_members[:8]))
    if non_file_members:
        notes.append(
            "archive contains non-file payload members: " + ", ".join(non_file_members[:8])
        )

    blockers = bool(
        missing
        or mismatches
        or unsafe
        or extra_members
        or non_file_members
        or manifest_shape_errors
        or not archive_sha256_ok
    )
    status: Literal["pass", "warn", "block"]
    if blockers:
        status = "block"
    elif would_overwrite:
        status = "warn"
        notes.append("dry-run found existing target paths; no files were overwritten")
    else:
        status = "pass"
    return RestoreDryRunReport(
        status=status,
        manifest_path=str(manifest_path),
        archive_path=str(archive_path),
        archive_exists=True,
        archive_sha256_ok=archive_sha256_ok,
        file_count=manifest.file_count,
        missing_from_archive=missing,
        sha256_mismatches=mismatches,
        unsafe_paths=unsafe,
        would_restore_count=manifest.file_count - len(missing) - len(unsafe),
        would_overwrite=would_overwrite,
        notes=notes,
    )


def _parse_manifest_created_at(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def default_backup_sources(repo_root: Path) -> list[Path]:
    """Small default set of local files that matter for an ops drill."""

    return [
        repo_root / ".env.example",
        repo_root / "alembic",
        repo_root / "kun" / "infra",
        repo_root / "docs" / "ops" / "runbook.md",
        repo_root / "scripts" / "backup_postgres.sh",
        repo_root / "scripts" / "restore_postgres_smoke.sh",
    ]


def default_allowed_roots(repo_root: Path) -> list[Path]:
    """Whitelist matching ``default_backup_sources``."""

    return [
        repo_root / ".env.example",
        repo_root / "alembic",
        repo_root / "kun" / "infra",
        repo_root / "docs" / "ops",
        repo_root / "scripts",
    ]


def sha256_file(path: Path) -> str:
    """Return sha256 hex digest for a file."""

    with path.open("rb") as handle:
        return sha256_stream(handle)


def sha256_stream(stream: BinaryIO | IO[bytes] | io.BufferedIOBase) -> str:
    """Return sha256 hex digest for a readable binary stream."""

    digest = hashlib.sha256()
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _collect_files(
    *,
    source_paths: list[Path],
    repo_root: Path,
    allowed_roots: list[Path],
) -> list[Path]:
    collected: list[Path] = []
    seen: set[Path] = set()
    for raw in source_paths:
        source = _resolve_under_root(raw, repo_root)
        if not any(_is_relative_to(source, allowed) for allowed in allowed_roots):
            raise ValueError(f"backup source is outside allowed roots: {source}")
        for path in _iter_source_files(source):
            if path in seen:
                continue
            if not any(_is_relative_to(path, allowed) for allowed in allowed_roots):
                raise ValueError(f"backup file is outside allowed roots: {path}")
            seen.add(path)
            collected.append(path)
    return sorted(collected, key=lambda item: _relative_posix(item, repo_root))


def _iter_source_files(source: Path) -> list[Path]:
    if source.is_file():
        if _is_excluded(source):
            return []
        return [source]
    if not source.exists():
        raise FileNotFoundError(source)
    if not source.is_dir():
        return []
    files: list[Path] = []
    for path in source.rglob("*"):
        if path.is_dir() or _is_excluded(path):
            continue
        if any(part in DEFAULT_EXCLUDED_NAMES for part in path.parts):
            continue
        files.append(path.resolve())
    return files


def _entry_for(path: Path, repo_root: Path) -> BackupFileEntry:
    stat = path.stat()
    return BackupFileEntry(
        path=_relative_posix(path, repo_root),
        size_bytes=stat.st_size,
        sha256=sha256_file(path),
        mtime_ns=stat.st_mtime_ns,
    )


def _resolve_under_root(path: Path, repo_root: Path) -> Path:
    resolved = path.resolve()
    if not _is_relative_to(resolved, repo_root):
        raise ValueError(f"path is outside repo root: {resolved}")
    return resolved


def _relative_posix(path: Path, repo_root: Path) -> str:
    return path.resolve().relative_to(repo_root).as_posix()


def _is_excluded(path: Path) -> bool:
    return path.name in DEFAULT_EXCLUDED_NAMES


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _normalize_object_prefix(prefix: str) -> str:
    cleaned = prefix.strip().strip("/")
    if not cleaned:
        return "backup-drills"
    parts = cleaned.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("object prefix must not contain empty, '.' or '..' segments")
    return cleaned


__all__ = [
    "BackupDrillFreshnessReport",
    "BackupFileEntry",
    "BackupManifest",
    "ObjectStoreBackupRoundTripReport",
    "RestoreDryRunReport",
    "check_backup_drill_freshness",
    "create_backup_package",
    "default_allowed_roots",
    "default_backup_sources",
    "load_manifest",
    "object_store_roundtrip_drill",
    "restore_dry_run",
    "sha256_file",
]
