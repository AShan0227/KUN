from __future__ import annotations

import json

import pytest
from kun.compiler import (
    CompilerIngestor,
    CompilerSyncRunner,
)
from kun.compiler.batch import CompilerBatchIngestor
from kun.context.storage import InMemoryAssetStore


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_source_ingests_manifest_file_under_config_root(tmp_path) -> None:
    config_root = tmp_path / "sync"
    docs_root = tmp_path / "docs"
    config_root.mkdir()
    docs_root.mkdir()
    (docs_root / "note.md").write_text("# KUN\n\nSync source", encoding="utf-8")
    manifest = config_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "tenant_id": "ignored-by-source",
                "items": [
                    {"id": "inline", "type": "text", "value": "inline sync note"},
                    {"id": "path", "type": "path", "value": "note.md"},
                ],
            }
        ),
        encoding="utf-8",
    )
    source = config_root / "source.json"
    source.write_text(
        json.dumps(
            {
                "source_id": "docs-sync",
                "tenant_id": "tenant-sync",
                "type": "manifest_file",
                "manifest_path": "manifest.json",
                "allowed_root": str(docs_root),
                "layer": "L2_project",
                "metadata": {"project": "v5"},
            }
        ),
        encoding="utf-8",
    )
    store = InMemoryAssetStore()
    runner = CompilerSyncRunner(
        batch_ingestor=CompilerBatchIngestor(
            ingestor=CompilerIngestor(store=store),
        )
    )

    report = await runner.sync_source_file(source)

    assert report.status == "synced"
    assert report.batch_report is not None
    assert report.batch_report.stored == 2
    stored = await store.list(tenant_id="tenant-sync")
    assert len(stored) == 2
    assert stored[0].l1_metadata["material_metadata"]["compiler_sync_source_id"] == "docs-sync"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_source_disabled_skips_without_reading_manifest(tmp_path) -> None:
    config_root = tmp_path / "sync"
    config_root.mkdir()
    source = config_root / "source.json"
    source.write_text(
        json.dumps(
            {
                "source_id": "disabled",
                "tenant_id": "tenant-sync",
                "type": "manifest_file",
                "manifest_path": "missing.json",
                "enabled": False,
            }
        ),
        encoding="utf-8",
    )

    report = await CompilerSyncRunner().sync_source_file(source)

    assert report.status == "skipped_disabled"
    assert report.reason == "sync_source_disabled"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sync_source_rejects_manifest_outside_config_root(tmp_path) -> None:
    config_root = tmp_path / "sync"
    outside = tmp_path / "outside"
    config_root.mkdir()
    outside.mkdir()
    manifest = outside / "manifest.json"
    manifest.write_text(
        json.dumps({"tenant_id": "tenant-sync", "items": []}),
        encoding="utf-8",
    )
    source = config_root / "source.json"
    source.write_text(
        json.dumps(
            {
                "source_id": "bad",
                "tenant_id": "tenant-sync",
                "type": "manifest_file",
                "manifest_path": "../outside/manifest.json",
            }
        ),
        encoding="utf-8",
    )

    report = await CompilerSyncRunner().sync_source_file(source)

    assert report.status == "error"
    assert "outside config_root" in report.reason
