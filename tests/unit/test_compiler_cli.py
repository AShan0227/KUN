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
