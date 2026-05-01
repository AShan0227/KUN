from __future__ import annotations

from pathlib import Path

import pytest
from kun.compiler import CanonicalAsset, LightweightMaterialCompiler, default_registry


def _required_fields(asset: CanonicalAsset) -> set[str]:
    return set(asset.model_dump())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compile_plain_text() -> None:
    asset = await LightweightMaterialCompiler().compile_text(
        "hello KUN compiler",
        tenant_id="tenant_a",
    )

    assert asset.status == "compiled"
    assert asset.kind == "text"
    assert asset.tenant_id == "tenant_a"
    assert asset.l1 == "hello KUN compiler"
    assert asset.l2 == "hello KUN compiler"
    assert asset.l3_ref is None
    assert asset.tokens_estimate > 0
    assert asset.risk.level == "low"
    assert asset.permissions.store_l2 is True
    assert asset.provenance.input_sha256
    assert asset.compiler_profile.name == "kun-v5-lightweight"
    assert {
        "asset_id",
        "kind",
        "source",
        "tenant_id",
        "l1",
        "l2",
        "l3_ref",
        "tokens_estimate",
        "risk",
        "permissions",
        "provenance",
        "compiler_profile",
        "metadata",
    } <= _required_fields(asset)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compile_markdown() -> None:
    asset = await LightweightMaterialCompiler().compile_text(
        "# Title\n\n- item",
        tenant_id="tenant_a",
    )

    assert asset.status == "compiled"
    assert asset.kind == "markdown"
    assert "# Title" in asset.l2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compile_html_strips_tags() -> None:
    asset = await LightweightMaterialCompiler().compile_text(
        "<html><body><h1>Title</h1><p>Hello <b>KUN</b></p></body></html>",
        tenant_id="tenant_a",
    )

    assert asset.status == "compiled"
    assert asset.kind == "html"
    assert "Title" in asset.l2
    assert "<h1>" not in asset.l2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compile_json_canonicalizes_keys() -> None:
    asset = await LightweightMaterialCompiler().compile_text(
        '{"b": 2, "a": 1}',
        tenant_id="tenant_a",
    )

    assert asset.status == "compiled"
    assert asset.kind == "json"
    assert asset.metadata["json_valid"] is True
    assert asset.l2.splitlines()[1].strip() == '"a": 1,'


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compile_csv_from_source_suffix() -> None:
    asset = await LightweightMaterialCompiler().compile_text(
        "name,score\nkun,5\nnuo,4\n",
        tenant_id="tenant_a",
        source_uri="inline:scores.csv",
    )

    assert asset.status == "compiled"
    assert asset.kind == "csv"
    assert asset.metadata["rows"] == 3
    assert asset.metadata["columns"] == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compile_path_blocks_traversal(tmp_path: Path) -> None:
    root = tmp_path / "safe"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    asset = await LightweightMaterialCompiler().compile_path(
        "../outside.txt",
        tenant_id="tenant_a",
        allowed_root=root,
    )

    assert asset.status == "rejected"
    assert asset.kind == "unsupported"
    assert asset.permissions.read is False
    assert "path_traversal" in asset.risk.flags


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compile_url_is_placeholder_without_fetching() -> None:
    asset = await LightweightMaterialCompiler().compile_url(
        "https://example.com/report.html",
        tenant_id="tenant_a",
    )

    assert asset.status == "placeholder"
    assert asset.kind == "unsupported"
    assert asset.source.type == "url"
    assert asset.risk.reason == "url_fetch_not_implemented"
    assert asset.tokens_estimate == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compile_invalid_url_is_rejected() -> None:
    asset = await LightweightMaterialCompiler().compile_url(
        "file:///etc/passwd",
        tenant_id="tenant_a",
    )

    assert asset.status == "rejected"
    assert asset.kind == "unsupported"
    assert asset.permissions.read is False
    assert "unsupported_url" in asset.risk.flags


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compile_unsupported_binary_path(tmp_path: Path) -> None:
    root = tmp_path / "safe"
    root.mkdir()
    binary = root / "payload.bin"
    binary.write_bytes(b"\x00\x01\x02\x03")

    asset = await LightweightMaterialCompiler().compile_path(
        binary,
        tenant_id="tenant_a",
        allowed_root=root,
    )

    assert asset.status == "unsupported"
    assert asset.kind == "unsupported"
    assert asset.source.detected_kind == "binary_unknown"
    assert asset.permissions.transform is False


@pytest.mark.unit
def test_default_registry_exposes_lightweight_compiler() -> None:
    compiler = default_registry.get()

    assert isinstance(compiler, LightweightMaterialCompiler)
    assert default_registry.names() == ["lightweight"]
