"""C15 project constitution tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.project_constitution import router
from kun.datamodel.project_constitution import (
    ConstitutionLoader,
    ConstitutionRule,
    InMemoryProjectConstitutionStore,
    ProjectConstitution,
    reset_constitution_store,
)


def _constitution() -> ProjectConstitution:
    return ProjectConstitution(
        project_id="proj-1",
        tenant_id="tenant-1",
        updated_by="user-1",
        rules=[
            ConstitutionRule(
                rule_id="no-alpha",
                kind="forbidden_word",
                pattern="alpha",
                severity="block",
                description="avoid internal codename",
            ),
            ConstitutionRule(
                rule_id="must-brand",
                kind="required_word",
                pattern="KUN",
                severity="warn",
            ),
        ],
    )


@pytest.mark.unit
def test_rule_rejects_empty_fields() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        ConstitutionRule(rule_id="", kind="tone", pattern="calm")
    with pytest.raises(ValueError, match="must not be empty"):
        ConstitutionRule(rule_id="r1", kind="tone", pattern="")


@pytest.mark.unit
def test_rule_rejects_invalid_regex() -> None:
    with pytest.raises(Exception):  # pydantic wraps re.error
        ConstitutionRule(rule_id="bad", kind="forbidden_word", pattern="[", is_regex=True)


@pytest.mark.unit
def test_evaluate_text_flags_forbidden_and_missing_required() -> None:
    loader = ConstitutionLoader(InMemoryProjectConstitutionStore())

    violations = loader.evaluate_text(_constitution(), "alpha launch plan")

    assert {v.rule_id for v in violations} == {"no-alpha", "must-brand"}
    assert any(v.severity == "block" for v in violations)


@pytest.mark.unit
def test_evaluate_text_passes_when_rules_satisfied() -> None:
    loader = ConstitutionLoader(InMemoryProjectConstitutionStore())

    assert loader.evaluate_text(_constitution(), "KUN launch plan") == []


@pytest.mark.unit
def test_evaluate_tools_blocks_denied_tool() -> None:
    constitution = ProjectConstitution(
        project_id="proj",
        tenant_id="tenant",
        updated_by="u",
        rules=[
            ConstitutionRule(
                rule_id="no-shell", kind="blocked_tool", pattern="shell", severity="block"
            )
        ],
    )

    violations = ConstitutionLoader().evaluate_tools(constitution, ["shell.exec"])

    assert len(violations) == 1
    assert violations[0].rule_id == "no-shell"


@pytest.mark.unit
def test_evaluate_tools_enforces_allowlist() -> None:
    constitution = ProjectConstitution(
        project_id="proj",
        tenant_id="tenant",
        updated_by="u",
        rules=[ConstitutionRule(rule_id="allow-read", kind="allowed_tool", pattern="read")],
    )

    violations = ConstitutionLoader().evaluate_tools(constitution, ["read_file", "shell.exec"])

    assert [v.evidence for v in violations] == ["shell.exec"]


@pytest.mark.unit
def test_render_to_system_prompt_is_compact_and_actionable() -> None:
    prompt = ConstitutionLoader().render_to_system_prompt(_constitution())

    assert "Project constitution for proj-1" in prompt
    assert "[BLOCK] forbidden_word: alpha" in prompt
    assert "[WARN] required_word: KUN" in prompt


@pytest.mark.unit
def test_load_from_file_missing_returns_none(tmp_path: Path) -> None:
    assert ConstitutionLoader().load_from_file(tmp_path) is None


@pytest.mark.unit
def test_load_from_file_parses_frontmatter(tmp_path: Path) -> None:
    config_dir = tmp_path / ".kun"
    config_dir.mkdir()
    (config_dir / "constitution.md").write_text(
        """---
project_id: proj-file
tenant_id: tenant-file
updated_by: user-file
version: 2
rules:
  - rule_id: tone-calm
    kind: tone
    pattern: calm
    severity: warn
    description: Keep tone calm.
---

# Project constitution
""",
        encoding="utf-8",
    )

    constitution = ConstitutionLoader().load_from_file(tmp_path)

    assert constitution is not None
    assert constitution.project_id == "proj-file"
    assert constitution.version == 2
    assert constitution.rules[0].description == "Keep tone calm."


@pytest.mark.unit
def test_load_from_file_rejects_missing_frontmatter(tmp_path: Path) -> None:
    config_dir = tmp_path / ".kun"
    config_dir.mkdir()
    (config_dir / "constitution.md").write_text("plain markdown", encoding="utf-8")

    with pytest.raises(ValueError, match="missing frontmatter"):
        ConstitutionLoader().load_from_file(tmp_path)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_store_upsert_versions_and_tenant_boundary() -> None:
    store = InMemoryProjectConstitutionStore()
    first = await store.put(_constitution())
    second = await store.put(_constitution())

    assert first.version == 1
    assert second.version == 2
    assert await store.get(tenant_id="other", project_id="proj-1") is None
    assert len(await store.list(tenant_id="tenant-1")) == 1


@pytest.mark.unit
def test_constitution_api_crud() -> None:
    reset_constitution_store()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    headers = {"X-Tenant-Id": "tenant-api"}

    create = client.put(
        "/api/projects/proj-api/constitution",
        headers=headers,
        json={
            "updated_by": "user-api",
            "rules": [
                {
                    "rule_id": "no-beta",
                    "kind": "forbidden_word",
                    "pattern": "beta",
                    "severity": "block",
                    "description": "No beta phrasing.",
                }
            ],
        },
    )
    assert create.status_code == 200
    assert create.json()["version"] == 1

    get = client.get("/api/projects/proj-api/constitution", headers=headers)
    assert get.status_code == 200
    assert get.json()["rules"][0]["rule_id"] == "no-beta"

    prompt = client.get("/api/projects/proj-api/constitution/prompt", headers=headers)
    assert "forbidden_word: beta" in prompt.json()["prompt"]

    listed = client.get("/api/projects", headers=headers)
    assert listed.json()["items"][0]["project_id"] == "proj-api"

    deleted = client.delete("/api/projects/proj-api/constitution", headers=headers)
    assert deleted.status_code == 204
    assert client.get("/api/projects/proj-api/constitution", headers=headers).status_code == 404
