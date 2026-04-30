from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest
from kun.ops.backup_restore import (
    create_backup_package,
    load_manifest,
    restore_dry_run,
)


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
