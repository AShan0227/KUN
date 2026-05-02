from __future__ import annotations

import pytest
from kun.compiler import CompilerRecompiler, LightweightMaterialCompiler
from kun.context.assets import LayeredAsset
from kun.context.storage import InMemoryAssetStore


def _candidate_asset(
    source: dict[str, str], *, tenant_id: str = "tenant-recompile"
) -> LayeredAsset:
    return LayeredAsset.build(
        "knowledge",
        tenant_id,
        metadata={
            "source": source,
            "compiler": "kun.compiler.lightweight",
            "compiler_recompile_recommended": True,
            "compiler_recompile_reason": "compiler_quality_score below threshold",
            "compiler_quality_score": 0.4,
        },
        summary="PDF document; text extraction unavailable",
        tags=["compiler", "compiler_recompile_recommended"],
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recompiler_dry_run_plans_path_candidate(tmp_path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    note = root / "note.md"
    note.write_text("# KUN\n\nbetter source", encoding="utf-8")
    store = InMemoryAssetStore()
    original = _candidate_asset({"type": "path", "uri": str(note)})
    await store.put(original)

    report = await CompilerRecompiler(store=store).recompile_candidates(
        tenant_id="tenant-recompile",
        allowed_roots=[root],
        dry_run=True,
    )

    assert report.candidates == 1
    assert report.planned == 1
    assert report.stored == 0
    assert report.results[0].status == "planned"
    assert report.results[0].new_asset_id
    assert len(await store.list(tenant_id="tenant-recompile")) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recompiler_apply_stores_new_asset_and_marks_original(tmp_path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    note = root / "note.md"
    note.write_text("# KUN\n\nbetter source", encoding="utf-8")
    store = InMemoryAssetStore()
    original = _candidate_asset({"type": "path", "uri": str(note)})
    await store.put(original)

    report = await CompilerRecompiler(store=store).recompile_candidates(
        tenant_id="tenant-recompile",
        allowed_roots=[root],
        dry_run=False,
    )

    assert report.stored == 1
    result = report.results[0]
    assert result.status == "stored"
    assert result.new_asset_id is not None
    stored = await store.list(tenant_id="tenant-recompile")
    assert len(stored) == 2
    old = await store.get(original.asset_id, tenant_id="tenant-recompile")
    new = await store.get(result.new_asset_id, tenant_id="tenant-recompile")
    assert old is not None
    assert old.l1_metadata["compiler_recompile_applied"] is True
    assert old.l1_metadata["compiler_recompile_recommended"] is False
    assert old.l1_metadata["compiler_recompiled_to_asset_id"] == result.new_asset_id
    assert old.l1_metadata["soft_forgotten"] is True
    assert "compiler_recompile_recommended" not in old.tags
    assert new is not None
    assert new.asset_id != original.asset_id
    assert new.l1_metadata["recompiled_from_asset_id"] == original.asset_id
    assert "compiler_recompiled" in new.tags


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recompiler_skips_path_without_matching_allowed_root(tmp_path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    store = InMemoryAssetStore()
    await store.put(_candidate_asset({"type": "path", "uri": str(outside)}))

    report = await CompilerRecompiler(store=store).recompile_candidates(
        tenant_id="tenant-recompile",
        allowed_roots=[root],
        dry_run=False,
    )

    assert report.skipped == 1
    assert report.results[0].reason == "path_source_requires_matching_allowed_root"
    assert len(await store.list(tenant_id="tenant-recompile")) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recompiler_url_still_obeys_compiler_allowlist() -> None:
    store = InMemoryAssetStore()
    await store.put(_candidate_asset({"type": "url", "uri": "https://example.com/report.html"}))

    report = await CompilerRecompiler(store=store).recompile_candidates(
        tenant_id="tenant-recompile",
        dry_run=False,
    )

    assert report.skipped == 1
    assert report.results[0].reason == "material_status_placeholder"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recompiler_url_can_store_when_fetch_is_allowlisted() -> None:
    async def fetcher(_url: str, _max_bytes: int) -> tuple[str, bytes]:
        return "text/html", b"<h1>Fresh docs</h1>"

    store = InMemoryAssetStore()
    await store.put(_candidate_asset({"type": "url", "uri": "https://docs.example.com/a.html"}))
    compiler = LightweightMaterialCompiler(
        url_fetch_enabled=True,
        allowed_url_hosts={"docs.example.com"},
        url_fetcher=fetcher,
    )

    report = await CompilerRecompiler(compiler=compiler, store=store).recompile_candidates(
        tenant_id="tenant-recompile",
        dry_run=False,
    )

    assert report.stored == 1
    result = report.results[0]
    assert result.new_asset_id
    new = await store.get(result.new_asset_id, tenant_id="tenant-recompile")
    assert new is not None
    assert new.l2_summary == "Fresh docs"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_recompiler_inline_requires_explicit_summary_fallback() -> None:
    store = InMemoryAssetStore()
    await store.put(_candidate_asset({"type": "inline", "uri": "inline:old"}))

    skipped = await CompilerRecompiler(store=store).recompile_candidates(
        tenant_id="tenant-recompile",
        dry_run=False,
    )
    applied = await CompilerRecompiler(store=store).recompile_candidates(
        tenant_id="tenant-recompile",
        dry_run=False,
        allow_inline_summary=True,
    )

    assert skipped.results[0].reason == "source_type_inline_not_reconstructable"
    assert applied.stored == 1
