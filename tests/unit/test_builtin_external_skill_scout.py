from __future__ import annotations

import pytest
from kun.skills.dispatcher import autoload_builtins, dispatch, is_registered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_skill_scout_builtin_returns_review_only_plan() -> None:
    autoload_builtins()

    result = await dispatch(
        "external-skill-scout",
        {
            "task_need": {
                "task_type": "coding.review",
                "summary": "Need better TypeScript pull request review behavior.",
                "topics": ["typescript", "code review"],
            }
        },
    )

    assert is_registered("external-skill-scout") is True
    assert result.ok is True
    assert result.metadata["review_only"] is True
    assert result.metadata["auto_fetch_allowed"] is False
    assert result.metadata["auto_install_allowed"] is False
    assert result.metadata["production_action"] is False
    assert result.output["review_only"] is True
    assert result.output["auto_fetch_allowed"] is False
    assert result.output["auto_install_allowed"] is False
    assert result.output["production_action"] is False
    assert "mattpocock/skills" in result.output["recommended_repo_refs"]
    assert result.output["scout_queries"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_skill_scout_builtin_rejects_empty_need() -> None:
    autoload_builtins()

    result = await dispatch("external-skill-scout", {})

    assert result.ok is False
    assert "task_need" in (result.error or "")
    assert result.metadata["production_action"] is False
