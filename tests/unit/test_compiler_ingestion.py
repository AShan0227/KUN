"""Compiler → Context AssetStore bridge tests."""

import pytest
from kun.compiler import (
    CompilerIngestor,
    LightweightMaterialCompiler,
    compile_protocol_asset,
    compile_skill_markdown_asset,
    compile_task_ref_asset,
    material_to_layered_asset,
)
from kun.context.assets import AssetLayer
from kun.context.storage import InMemoryAssetStore
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.qi.protocol import (
    Protocol,
    ProtocolExecution,
    ProtocolSkillStep,
    ProtocolTrigger,
    ProtocolVerificationSpec,
)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_text_stores_compiled_material_as_knowledge_asset() -> None:
    store = InMemoryAssetStore()
    ingestor = CompilerIngestor(store=store)

    result = await ingestor.ingest_text(
        "# KUN\n\nCompiler layer",
        tenant_id="tenant-compiler",
        source_uri="brief.md",
        declared_kind="markdown",
        layer=AssetLayer.L2_PROJECT,
    )

    assert result.stored is True
    assert result.asset_id == result.material.asset_id
    stored = await store.get(result.asset_id or "", tenant_id="tenant-compiler")
    assert stored is not None
    assert stored.asset_kind == "knowledge"
    assert stored.layer == AssetLayer.L2_PROJECT
    assert stored.l1_metadata["kind"] == "markdown"
    assert stored.l1_metadata["compiler_profile"]["name"] == "kun-v5-lightweight"
    assert stored.l2_summary.startswith("# KUN")
    assert "compiler" in stored.tags


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_bytes_stores_pdf_material_without_text_flattening() -> None:
    raw = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"
    store = InMemoryAssetStore()
    ingestor = CompilerIngestor(store=store)

    result = await ingestor.ingest_bytes(
        raw,
        tenant_id="tenant-compiler",
        source_uri="attachment:brief.pdf",
        mime_type="application/pdf",
        layer=AssetLayer.L2_PROJECT,
        metadata={"source": "chat_attachment"},
    )

    assert result.stored is True
    assert result.material.kind == "pdf"
    assert result.material.source.type == "bytes"
    stored = await store.get(result.asset_id or "", tenant_id="tenant-compiler")
    assert stored is not None
    assert stored.l1_metadata["kind"] == "pdf"
    assert stored.l1_metadata["source"]["type"] == "bytes"
    assert stored.l1_metadata["material_metadata"]["source"] == "chat_attachment"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_rejected_path_is_not_stored(tmp_path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    store = InMemoryAssetStore()
    ingestor = CompilerIngestor(store=store)

    result = await ingestor.ingest_path(
        outside,
        tenant_id="tenant-compiler",
        allowed_root=root,
    )

    assert result.stored is False
    assert result.reason == "material_status_rejected"
    assert await store.list(tenant_id="tenant-compiler") == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ingest_url_stores_allowlisted_fetched_material() -> None:
    async def fetcher(_url: str, _max_bytes: int) -> tuple[str, bytes]:
        return "text/html", b"<h1>KUN URL</h1><p>compiled from web</p>"

    store = InMemoryAssetStore()
    compiler = LightweightMaterialCompiler(
        url_fetch_enabled=True,
        allowed_url_hosts={"docs.example.com"},
        url_fetcher=fetcher,
    )
    ingestor = CompilerIngestor(compiler=compiler, store=store)

    result = await ingestor.ingest_url(
        "https://docs.example.com/report.html",
        tenant_id="tenant-compiler",
        layer=AssetLayer.L2_PROJECT,
    )

    assert result.stored is True
    stored = await store.get(result.asset_id or "", tenant_id="tenant-compiler")
    assert stored is not None
    assert stored.l1_metadata["source"]["type"] == "url"
    assert stored.l1_metadata["material_metadata"]["url_fetch_enabled"] is True
    assert stored.layer == AssetLayer.L2_PROJECT
    assert "compiled from web" in (stored.l2_summary or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_material_to_layered_asset_skips_unsupported_url() -> None:
    material = await LightweightMaterialCompiler().compile_url(
        "https://example.com/data",
        tenant_id="tenant-compiler",
    )

    assert material.status == "placeholder"
    assert material_to_layered_asset(material) is None


@pytest.mark.unit
def test_compile_skill_markdown_asset() -> None:
    asset = compile_skill_markdown_asset(
        "# Code Reviewer\n\nUse when reviewing Python patches.\n\n## Workflow\nRun tests first.",
        tenant_id="tenant-compiler",
        skill_id="code-reviewer",
        source_uri="skills/code-reviewer/SKILL.md",
    )

    assert asset.asset_kind == "skill"
    assert asset.l1_metadata["compiled_kind"] == "skill"
    assert asset.l1_metadata["skill_id"] == "code-reviewer"
    assert asset.l1_metadata["title"] == "Code Reviewer"
    assert "compiled_internal" in asset.tags
    assert "code" in asset.tags
    assert "Use when reviewing" in (asset.l2_summary or "")


@pytest.mark.unit
def test_compile_task_ref_asset() -> None:
    owner = Owner(tenant_id="tenant-compiler", user_id="u-1")
    task_ref = TaskRef(
        meta=TaskMeta(
            fingerprint=TaskMeta.compute_fingerprint("修复测试", owner),
            task_type="coding.python",
            risk_level="medium",
            complexity_score=0.55,
            estimated_cost_usd=0.2,
            owner=owner,
            success_criteria_short="pytest 通过",
        ),
        spec=TaskSpec(
            goal_detail="修复 pytest 失败并回归",
            success_metrics=["pytest pass"],
            required_skills=["coding-pytest"],
        ),
    )

    asset = compile_task_ref_asset(task_ref)

    assert asset.asset_kind == "task"
    assert asset.tenant_id == "tenant-compiler"
    assert asset.l1_metadata["compiled_kind"] == "task"
    assert asset.l1_metadata["task_type"] == "coding.python"
    assert "pytest 通过" in (asset.l2_summary or "")
    assert "coding.python" in asset.tags


@pytest.mark.unit
def test_compile_protocol_asset() -> None:
    protocol = Protocol(
        tenant_id="tenant-compiler",
        protocol_id="coding.python.fastapi",
        version="1.0.0",
        status="stable",
        trigger=ProtocolTrigger(task_type_pattern="coding.python.*"),
        execution=ProtocolExecution(mode="MAX", max_steps=7, expected_cost_usd=0.3),
        skill_chain=[ProtocolSkillStep(skill="reader"), ProtocolSkillStep(skill="python_exec")],
        verification=[ProtocolVerificationSpec(kind="pytest_pass", required=True)],
    )

    asset = compile_protocol_asset(protocol)

    assert asset.asset_kind == "methodology"
    assert asset.tenant_id == "tenant-compiler"
    assert asset.l1_metadata["compiled_kind"] == "protocol"
    assert asset.l1_metadata["protocol_id"] == "coding.python.fastapi"
    assert asset.l1_metadata["execution_mode"] == "MAX"
    assert asset.l1_metadata["skill_chain"] == ["reader", "python_exec"]
    assert "protocol" in asset.tags
    assert "pytest_pass" in (asset.l2_summary or "")
