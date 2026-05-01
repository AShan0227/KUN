from __future__ import annotations

import pytest
from kun.compiler import (
    CompilerIntakeRequest,
    LightweightMaterialCompiler,
    build_compiler_review_package,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intake_review_compiles_safe_path_into_asset(tmp_path) -> None:
    root = tmp_path / "safe"
    root.mkdir()
    source = root / "brief.md"
    source.write_text("# KUN\n\nThis is a useful project brief for the compiler.", encoding="utf-8")

    package = await build_compiler_review_package(
        CompilerIntakeRequest(
            tenant_id="tenant-compiler",
            source_type="path",
            value="brief.md",
            allowed_root=str(root),
        )
    )

    assert package.decision == "compiled_to_asset"
    assert package.store_allowed is True
    assert package.needs_human_review is False
    assert package.material is not None
    assert package.material.status == "compiled"
    assert package.asset is not None
    assert package.asset.asset_kind == "knowledge"
    assert package.backend.status == "available"
    assert package.as_review_ticket()["asset_id"] == package.asset.asset_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intake_review_blocks_path_without_allowed_root() -> None:
    package = await build_compiler_review_package(
        CompilerIntakeRequest(
            tenant_id="tenant-compiler",
            source_type="path",
            value="/tmp/customer-secret.md",
        )
    )

    assert package.decision == "blocked"
    assert package.material is None
    assert package.asset is None
    assert package.risk_level == "high"
    assert "path_allowed_root_required" in package.risk_flags
    assert package.needs_human_review is True
    assert package.store_allowed is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intake_review_does_not_process_path_traversal(tmp_path) -> None:
    root = tmp_path / "safe"
    root.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")

    package = await build_compiler_review_package(
        CompilerIntakeRequest(
            tenant_id="tenant-compiler",
            source_type="path",
            value="../secret.md",
            allowed_root=str(root),
        )
    )

    assert package.decision == "blocked"
    assert package.material is not None
    assert package.material.status == "rejected"
    assert package.asset is None
    assert "path_traversal" in package.risk_flags
    assert "fix the source boundary before retrying" in package.next_actions


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intake_review_holds_low_quality_raw_text() -> None:
    package = await build_compiler_review_package(
        CompilerIntakeRequest(
            tenant_id="tenant-compiler",
            source_type="raw_text",
            value="ok",
        )
    )

    assert package.decision == "compiled_hold_for_review"
    assert package.material is not None
    assert package.material.status == "compiled"
    assert package.asset is None
    assert package.quality.level == "low"
    assert "too_short" in package.quality.flags
    assert package.needs_human_review is True
    assert package.needs_recompile is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intake_review_reports_markitdown_backend_unavailable(tmp_path) -> None:
    root = tmp_path / "docs"
    root.mkdir()
    source = root / "deck.pptx"
    source.write_bytes(b"fake office file")

    package = await build_compiler_review_package(
        CompilerIntakeRequest(
            tenant_id="tenant-compiler",
            source_type="path",
            value="deck.pptx",
            allowed_root=str(root),
        )
    )

    assert package.suggested_backend == "markitdown"
    assert package.decision == "backend_unavailable"
    assert package.backend.name == "markitdown"
    assert package.backend.status == "disabled"
    assert "MarkItDown backend is not enabled" in package.backend.reason
    assert package.material is not None
    assert package.material.status == "unsupported"
    assert package.asset is None
    assert package.needs_recompile is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intake_review_url_is_manual_until_fetch_policy_allows_it() -> None:
    package = await build_compiler_review_package(
        CompilerIntakeRequest(
            tenant_id="tenant-compiler",
            source_type="url",
            value="https://example.com/report.html",
        )
    )

    assert package.suggested_backend == "manual"
    assert package.decision == "backend_unavailable"
    assert package.backend.status == "unavailable"
    assert package.material is not None
    assert package.material.status == "placeholder"
    assert package.asset is None
    assert (
        "enable URL fetch with host allowlist or capture the content manually"
        in package.next_actions
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intake_review_url_can_compile_when_policy_and_fetcher_allow_it() -> None:
    async def fetcher(_url: str, _max_bytes: int) -> tuple[str, bytes]:
        return "text/markdown", b"# Report\n\nCompiled through an allowlisted URL policy."

    compiler = LightweightMaterialCompiler(
        url_fetch_enabled=True,
        allowed_url_hosts={"docs.example.com"},
        url_fetcher=fetcher,
    )

    package = await build_compiler_review_package(
        CompilerIntakeRequest(
            tenant_id="tenant-compiler",
            source_type="url",
            value="https://docs.example.com/report.md",
        ),
        compiler=compiler,
    )

    assert package.decision == "compiled_to_asset"
    assert package.backend.name == "plain"
    assert package.backend.status == "available"
    assert package.material is not None
    assert package.material.status == "compiled"
    assert package.asset is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_intake_review_requires_raw_bytes_for_bytes_input() -> None:
    package = await build_compiler_review_package(
        CompilerIntakeRequest(
            tenant_id="tenant-compiler",
            source_type="bytes",
            value="attachment:missing.pdf",
            mime_type="application/pdf",
        )
    )

    assert package.decision == "blocked"
    assert package.material is None
    assert package.asset is None
    assert "raw_bytes_required" in package.reasons
