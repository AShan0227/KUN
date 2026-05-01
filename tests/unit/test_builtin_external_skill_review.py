from __future__ import annotations

import pytest
from kun.skills.dispatcher import autoload_builtins, dispatch, is_registered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_skill_review_builtin_reviews_offline_candidate() -> None:
    autoload_builtins()

    result = await dispatch(
        "external-skill-review",
        {
            "task_need": {
                "task_type": "coding.review",
                "summary": "Need better TypeScript PR review behavior.",
            },
            "candidate": {
                "source_kind": "github_repo",
                "repo": "mattpocock/skills",
                "name": "TypeScript review skill",
                "description": "Code review templates for TypeScript engineers.",
                "topics": ["typescript", "code review"],
                "files": ["SKILL.md", "README.md"],
                "license_id": "MIT",
            },
        },
    )

    assert is_registered("external-skill-review") is True
    assert result.ok is True
    assert result.metadata["review_only"] is True
    assert result.metadata["auto_fetch_allowed"] is False
    assert result.metadata["auto_install_allowed"] is False
    assert result.metadata["production_action"] is False
    assert result.output["review_only"] is True
    assert result.output["production_action"] is False
    assert result.output["candidate_name"] == "TypeScript review skill"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_skill_review_builtin_rejects_missing_candidate() -> None:
    autoload_builtins()

    result = await dispatch(
        "external-skill-review",
        {"task_need": "Need a code review skill"},
    )

    assert result.ok is False
    assert "candidate" in (result.error or "")
    assert result.metadata["production_action"] is False
