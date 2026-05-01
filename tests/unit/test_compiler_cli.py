from __future__ import annotations

import asyncio
import json

from kun.cli import app
from kun.context.assets import LayeredAsset
from kun.context.storage import InMemoryAssetStore, reset_store, set_store
from typer.testing import CliRunner


def test_compiler_compile_text_outputs_canonical_material_json() -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "compiler",
            "compile-text",
            "# KUN\n\nCompiler CLI",
            "--tenant",
            "tenant-cli",
            "--source-uri",
            "inline:brief.md",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "compiled"
    assert payload["kind"] == "markdown"
    assert payload["tenant_id"] == "tenant-cli"
    assert payload["source"]["uri"] == "inline:brief.md"
    assert payload["l2"].startswith("# KUN")
    assert payload["compiler_profile"]["name"] == "kun-v5-lightweight"


def test_compiler_compile_url_is_placeholder_by_default() -> None:
    runner = CliRunner()

    result = runner.invoke(
        app,
        [
            "compiler",
            "compile-url",
            "https://example.com/report.html",
            "--tenant",
            "tenant-cli",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "placeholder"
    assert payload["kind"] == "unsupported"
    assert payload["source"]["type"] == "url"
    assert payload["risk"]["reason"] == "url_fetch_not_enabled"


def test_compiler_compile_path_can_select_markitdown_backend(tmp_path) -> None:
    runner = CliRunner()
    root = tmp_path / "docs"
    root.mkdir()
    source = root / "deck.pptx"
    source.write_bytes(b"fake office file")

    result = runner.invoke(
        app,
        [
            "compiler",
            "compile-path",
            "deck.pptx",
            "--tenant",
            "tenant-cli",
            "--allowed-root",
            str(root),
            "--backend",
            "markitdown",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["status"] == "unsupported"
    assert payload["risk"]["reason"] == "markitdown_backend_not_enabled"
    assert payload["metadata"]["backend_status"]["name"] == "markitdown"


def test_compiler_ingest_text_stores_in_asset_store() -> None:
    runner = CliRunner()
    store = InMemoryAssetStore()
    set_store(store)
    try:
        result = runner.invoke(
            app,
            [
                "compiler",
                "ingest-text",
                "KUN compiler ingestion CLI",
                "--tenant",
                "tenant-cli",
                "--source-uri",
                "inline:note.txt",
                "--layer",
                "L2_project",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["status"] == "stored"
        assert payload["asset_id"]
        assert payload["summary"] == "KUN compiler ingestion CLI"
        assert payload["stored"] is True
        assert payload["material_status"] == "compiled"

        stored = asyncio.run(store.get(payload["asset_id"], tenant_id="tenant-cli"))
        assert stored is not None
        assert stored.asset_kind == "knowledge"
        assert stored.layer == "L2_project"
        assert stored.l2_summary == "KUN compiler ingestion CLI"
    finally:
        reset_store()


def test_compiler_ingest_url_does_not_store_placeholder_by_default() -> None:
    runner = CliRunner()
    store = InMemoryAssetStore()
    set_store(store)
    try:
        result = runner.invoke(
            app,
            [
                "compiler",
                "ingest-url",
                "https://example.com/report.html",
                "--tenant",
                "tenant-cli",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["stored"] is False
        assert payload["status"] == "material_status_placeholder"
        assert payload["source_uri"] == "https://example.com/report.html"
    finally:
        reset_store()


def test_compiler_ingest_path_markitdown_backend_does_not_store_when_disabled(tmp_path) -> None:
    runner = CliRunner()
    root = tmp_path / "docs"
    root.mkdir()
    source = root / "brief.docx"
    source.write_bytes(b"fake office file")
    store = InMemoryAssetStore()
    set_store(store)
    try:
        result = runner.invoke(
            app,
            [
                "compiler",
                "ingest-path",
                "brief.docx",
                "--tenant",
                "tenant-cli",
                "--allowed-root",
                str(root),
                "--backend",
                "markitdown",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["stored"] is False
        assert payload["status"] == "material_status_unsupported"
        assert payload["material_status"] == "unsupported"
        assert payload["compiler_profile"] == "kun-v5-lightweight"
    finally:
        reset_store()


def test_compiler_ingest_manifest_stores_supported_items(tmp_path) -> None:
    runner = CliRunner()
    root = tmp_path / "docs"
    root.mkdir()
    (root / "note.md").write_text("# KUN\n\nManifest", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "tenant_id": "tenant-cli",
                "allowed_root": str(root),
                "layer": "L2_project",
                "items": [
                    {"id": "inline", "type": "text", "value": "inline manifest item"},
                    {"id": "path", "type": "path", "value": "note.md"},
                    {"id": "url", "type": "url", "value": "https://example.com/report.html"},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = InMemoryAssetStore()
    set_store(store)
    try:
        result = runner.invoke(app, ["compiler", "ingest-manifest", str(manifest)])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["total"] == 3
        assert payload["stored"] == 2
        assert payload["skipped"] == 1
        assert payload["results"][2]["reason"] == "material_status_placeholder"
        stored = asyncio.run(store.list(tenant_id="tenant-cli"))
        assert len(stored) == 2
    finally:
        reset_store()


def test_compiler_ingest_manifest_fail_on_error_exits_nonzero(tmp_path) -> None:
    runner = CliRunner()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "tenant_id": "tenant-cli",
                "items": [{"id": "path", "type": "path", "value": "missing.md"}],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["compiler", "ingest-manifest", str(manifest), "--fail-on-error"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["errors"] == 1


def test_compiler_sync_source_runs_manifest_config(tmp_path) -> None:
    runner = CliRunner()
    config_root = tmp_path / "sync"
    docs_root = tmp_path / "docs"
    config_root.mkdir()
    docs_root.mkdir()
    (docs_root / "note.md").write_text("# KUN\n\nSync CLI", encoding="utf-8")
    manifest = config_root / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "tenant_id": "ignored",
                "items": [
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
                "source_id": "docs-sync-cli",
                "tenant_id": "tenant-cli",
                "type": "manifest_file",
                "manifest_path": "manifest.json",
                "allowed_root": str(docs_root),
            }
        ),
        encoding="utf-8",
    )
    store = InMemoryAssetStore()
    set_store(store)
    try:
        result = runner.invoke(app, ["compiler", "sync-source", str(source)])

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["status"] == "synced"
        assert payload["source_id"] == "docs-sync-cli"
        assert payload["batch_report"]["stored"] == 1
        stored = asyncio.run(store.list(tenant_id="tenant-cli"))
        assert len(stored) == 1
    finally:
        reset_store()


def test_compiler_recompile_candidates_apply_marks_original() -> None:
    runner = CliRunner()
    store = InMemoryAssetStore()
    original = LayeredAsset.build(
        "knowledge",
        "tenant-cli",
        metadata={
            "source": {"type": "inline", "uri": "inline:old"},
            "compiler_recompile_recommended": True,
            "compiler_recompile_reason": "low quality",
        },
        summary="KUN stale compiler summary",
        tags=["compiler_recompile_recommended"],
    )
    asyncio.run(store.put(original))
    set_store(store)
    try:
        result = runner.invoke(
            app,
            [
                "compiler",
                "recompile-candidates",
                "--tenant",
                "tenant-cli",
                "--allow-inline-summary",
                "--apply",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["dry_run"] is False
        assert payload["stored"] == 1
        new_asset_id = payload["results"][0]["new_asset_id"]
        assert new_asset_id
        old = asyncio.run(store.get(original.asset_id, tenant_id="tenant-cli"))
        new = asyncio.run(store.get(new_asset_id, tenant_id="tenant-cli"))
        assert old is not None
        assert old.l1_metadata["compiler_recompile_applied"] is True
        assert old.l1_metadata["soft_forgotten"] is True
        assert new is not None
        assert new.l1_metadata["recompiled_from_asset_id"] == original.asset_id
    finally:
        reset_store()


def test_context_merge_duplicates_apply_marks_duplicate() -> None:
    runner = CliRunner()
    store = InMemoryAssetStore()
    canonical = LayeredAsset.build("memory", "tenant-cli", summary="same")
    duplicate = LayeredAsset.build(
        "memory",
        "tenant-cli",
        metadata={
            "duplicate_candidate": True,
            "duplicate_of": canonical.asset_id,
        },
        summary="same",
        tags=["duplicate_candidate"],
    )
    asyncio.run(store.put(canonical))
    asyncio.run(store.put(duplicate))
    set_store(store)
    try:
        result = runner.invoke(
            app,
            [
                "context",
                "merge-duplicates",
                "--tenant",
                "tenant-cli",
                "--apply",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["dry_run"] is False
        assert payload["merged"] == 1
        after_duplicate = asyncio.run(store.get(duplicate.asset_id, tenant_id="tenant-cli"))
        after_canonical = asyncio.run(store.get(canonical.asset_id, tenant_id="tenant-cli"))
        assert after_duplicate is not None
        assert after_duplicate.l1_metadata["duplicate_merge_applied"] is True
        assert after_duplicate.l1_metadata["soft_forgotten"] is True
        assert after_canonical is not None
        assert after_canonical.l1_metadata["merged_duplicate_count"] == 1
    finally:
        reset_store()


def test_context_maintenance_run_apply_can_merge_duplicates() -> None:
    runner = CliRunner()
    store = InMemoryAssetStore()
    canonical = LayeredAsset.build("memory", "tenant-cli", summary="same")
    duplicate = LayeredAsset.build("memory", "tenant-cli", summary="same")
    asyncio.run(store.put(canonical))
    asyncio.run(store.put(duplicate))
    set_store(store)
    try:
        result = runner.invoke(
            app,
            [
                "context",
                "maintenance-run",
                "--tenant",
                "tenant-cli",
                "--apply",
                "--merge-duplicates",
            ],
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["dry_run"] is False
        assert payload["duplicate_candidates"] == 1
        assert payload["duplicate_merged"] == 1
        after_duplicate = asyncio.run(store.get(duplicate.asset_id, tenant_id="tenant-cli"))
        assert after_duplicate is not None
        assert after_duplicate.l1_metadata["duplicate_merge_applied"] is True
    finally:
        reset_store()
