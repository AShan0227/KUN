from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest
from kun.cli import app
from kun.core.object_store import ObjectRef
from kun.ops.backup_restore import (
    create_backup_package,
    load_manifest,
    object_store_roundtrip_drill,
    restore_dry_run,
    sha256_file,
)
from typer.testing import CliRunner


class InMemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_bytes(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
    ) -> ObjectRef:
        del content_type
        self.objects[key] = data
        return ObjectRef(
            uri=f"s3://test-bucket/{key}", bucket="test-bucket", key=key, size_bytes=len(data)
        )

    async def get_bytes(self, ref: ObjectRef | str) -> bytes:
        parsed = ObjectRef.from_uri(ref) if isinstance(ref, str) else ref
        return self.objects[parsed.key]


def test_backup_package_writes_manifest_and_archive(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    allowed = repo / "config"
    allowed.mkdir()
    (allowed / "app.yaml").write_text("setting: ok\n", encoding="utf-8")

    manifest = create_backup_package(
        source_paths=[allowed],
        output_dir=tmp_path / "backups",
        repo_root=repo,
        allowed_roots=[allowed],
        package_name="smoke",
    )

    manifest_path = tmp_path / "backups" / "smoke.manifest.json"
    archive_path = tmp_path / "backups" / "smoke.tar.gz"
    reloaded = load_manifest(manifest_path)

    assert archive_path.exists()
    assert manifest.file_count == 1
    assert reloaded.files[0].path == "config/app.yaml"
    assert reloaded.allowed_roots == ["config"]
    assert len(reloaded.files[0].sha256) == 64
    assert reloaded.archive_sha256 == manifest.archive_sha256


def test_backup_rejects_source_outside_allowed_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    allowed = repo / "config"
    allowed.mkdir()
    disallowed = repo / "secrets"
    disallowed.mkdir()
    (disallowed / "token.txt").write_text("secret\n", encoding="utf-8")

    with pytest.raises(ValueError, match="outside allowed roots"):
        create_backup_package(
            source_paths=[disallowed],
            output_dir=tmp_path / "backups",
            repo_root=repo,
            allowed_roots=[allowed],
        )


def test_restore_dry_run_passes_and_reports_overwrite_risk(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config"
    config.mkdir()
    (config / "app.yaml").write_text("setting: ok\n", encoding="utf-8")
    create_backup_package(
        source_paths=[config],
        output_dir=tmp_path / "backups",
        repo_root=repo,
        allowed_roots=[config],
        package_name="overwrite",
    )
    restore_root = tmp_path / "restore"
    (restore_root / "config").mkdir(parents=True)
    (restore_root / "config" / "app.yaml").write_text("old\n", encoding="utf-8")

    report = restore_dry_run(
        manifest_path=tmp_path / "backups" / "overwrite.manifest.json",
        restore_root=restore_root,
    )

    assert report.status == "warn"
    assert report.archive_sha256_ok is True
    assert report.missing_from_archive == []
    assert report.would_overwrite == ["config/app.yaml"]


def test_restore_dry_run_finds_missing_archive_entry(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config"
    config.mkdir()
    (config / "a.txt").write_text("a\n", encoding="utf-8")
    (config / "b.txt").write_text("b\n", encoding="utf-8")
    create_backup_package(
        source_paths=[config],
        output_dir=tmp_path / "backups",
        repo_root=repo,
        allowed_roots=[config],
        package_name="missing",
    )

    original = tmp_path / "backups" / "missing.tar.gz"
    broken = tmp_path / "backups" / "broken.tar.gz"
    with tarfile.open(original, "r:gz") as src, tarfile.open(broken, "w:gz") as dst:
        for member in src.getmembers():
            if member.name.endswith("b.txt"):
                continue
            extracted = src.extractfile(member)
            if extracted is not None:
                dst.addfile(member, extracted)

    manifest_path = tmp_path / "backups" / "missing.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["archive_path"] = str(broken)
    payload["archive_sha256"] = "0" * 64
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    report = restore_dry_run(manifest_path=manifest_path, restore_root=tmp_path / "restore")

    assert report.status == "block"
    assert report.archive_sha256_ok is False
    assert report.missing_from_archive == ["config/b.txt"]


def test_restore_dry_run_blocks_unexpected_manifest_version(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config"
    config.mkdir()
    (config / "app.yaml").write_text("setting: ok\n", encoding="utf-8")
    create_backup_package(
        source_paths=[config],
        output_dir=tmp_path / "backups",
        repo_root=repo,
        allowed_roots=[config],
        package_name="versioned",
    )
    manifest_path = tmp_path / "backups" / "versioned.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["version"] = "kun.backup_restore.v999"
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    report = restore_dry_run(manifest_path=manifest_path, restore_root=tmp_path / "restore")

    assert report.status == "block"
    assert any("unexpected manifest version" in item for item in report.notes)


def test_restore_dry_run_blocks_extra_archive_members_even_when_sha_matches(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config"
    config.mkdir()
    (config / "app.yaml").write_text("setting: ok\n", encoding="utf-8")
    create_backup_package(
        source_paths=[config],
        output_dir=tmp_path / "backups",
        repo_root=repo,
        allowed_roots=[config],
        package_name="extra-member",
    )
    archive_path = tmp_path / "backups" / "extra-member.tar.gz"
    original_members: list[tuple[tarfile.TarInfo, bytes]] = []
    with tarfile.open(archive_path, "r:gz") as src:
        for member in src.getmembers():
            extracted = src.extractfile(member)
            original_members.append((member, extracted.read() if extracted is not None else b""))
    with tarfile.open(archive_path, "w:gz") as tar:
        for member, data in original_members:
            tar.addfile(member, io.BytesIO(data))
        data = b"not in manifest\n"
        info = tarfile.TarInfo("payload/extra.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    manifest_path = tmp_path / "backups" / "extra-member.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["archive_sha256"] = sha256_file(archive_path)
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    report = restore_dry_run(manifest_path=manifest_path, restore_root=tmp_path / "restore")

    assert report.archive_sha256_ok is True
    assert report.status == "block"
    assert any("unexpected members" in item for item in report.notes)


def test_ops_backup_drill_cli_create_and_restore_dry_run(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config"
    config.mkdir()
    (config / "app.yaml").write_text("setting: ok\n", encoding="utf-8")
    backup_dir = tmp_path / "backups"
    runner = CliRunner()

    create = runner.invoke(
        app,
        [
            "ops",
            "backup-drill-create",
            "--repo-root",
            str(repo),
            "--output-dir",
            str(backup_dir),
            "--source",
            str(config),
            "--json",
        ],
    )

    assert create.exit_code == 0
    payload = json.loads(create.output)
    manifest_path = backup_dir / Path(payload["archive_path"]).name.replace(
        ".tar.gz",
        ".manifest.json",
    )
    assert manifest_path.exists()

    restore = runner.invoke(
        app,
        [
            "ops",
            "backup-drill-restore-dry-run",
            str(manifest_path),
            "--restore-root",
            str(tmp_path / "restore"),
            "--json",
        ],
    )

    assert restore.exit_code == 0
    assert json.loads(restore.output)["status"] == "pass"


@pytest.mark.asyncio
async def test_object_store_roundtrip_drill_uploads_downloads_and_restores(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config"
    config.mkdir()
    (config / "app.yaml").write_text("setting: ok\n", encoding="utf-8")
    create_backup_package(
        source_paths=[config],
        output_dir=tmp_path / "backups",
        repo_root=repo,
        allowed_roots=[config],
        package_name="object-store",
    )
    store = InMemoryObjectStore()

    report = await object_store_roundtrip_drill(
        manifest_path=tmp_path / "backups" / "object-store.manifest.json",
        restore_root=tmp_path / "restore",
        object_prefix="tenant-a/drills",
        scratch_dir=tmp_path / "scratch",
        object_store=store,
    )

    assert report.status == "pass"
    assert report.archive_download_sha256_ok is True
    assert report.manifest_download_sha256_ok is True
    assert report.restore_status == "pass"
    assert report.archive_object_uri == "s3://test-bucket/tenant-a/drills/object-store.tar.gz"
    assert (
        report.manifest_object_uri == "s3://test-bucket/tenant-a/drills/object-store.manifest.json"
    )


@pytest.mark.asyncio
async def test_object_store_roundtrip_rejects_unsafe_prefix(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config"
    config.mkdir()
    (config / "app.yaml").write_text("setting: ok\n", encoding="utf-8")
    create_backup_package(
        source_paths=[config],
        output_dir=tmp_path / "backups",
        repo_root=repo,
        allowed_roots=[config],
        package_name="unsafe-prefix",
    )

    with pytest.raises(ValueError, match="object prefix"):
        await object_store_roundtrip_drill(
            manifest_path=tmp_path / "backups" / "unsafe-prefix.manifest.json",
            restore_root=tmp_path / "restore",
            object_prefix="../bad",
            object_store=InMemoryObjectStore(),
        )
