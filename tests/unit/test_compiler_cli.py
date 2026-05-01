from __future__ import annotations

import asyncio
import json

from kun.cli import app
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
