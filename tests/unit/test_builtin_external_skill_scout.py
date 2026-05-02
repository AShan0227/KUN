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
async def test_external_skill_scout_builtin_can_plan_offline_sources_and_candidates() -> None:
    autoload_builtins()

    result = await dispatch(
        "external-skill-scout",
        {
            "task_need": {
                "task_type": "coding.review",
                "summary": "Need safer TypeScript pull request review behavior.",
            },
            "source_registry": [
                {
                    "source_kind": "github_repo",
                    "repo": "example/review-skills",
                    "name": "Review source",
                    "description": "TypeScript code review skill templates.",
                    "license": "MIT",
                    "commit_sha": "abc123",
                    "pushed_at": "2026-03-01T00:00:00Z",
                    "skills": [
                        {
                            "name": "TypeScript reviewer",
                            "description": "Review TypeScript pull requests.",
                            "files": [{"path": "SKILL.md", "content": "Review diffs."}],
                        }
                    ],
                }
            ],
            "candidates": [
                {
                    "repo": "example/local-review",
                    "name": "Local review checklist",
                    "description": "Review TypeScript code diffs.",
                    "license": "Apache-2.0",
                    "commit_sha": "def456",
                    "files": [{"path": "SKILL.md", "content": "Review code."}],
                }
            ],
        },
    )

    assert result.ok is True
    assert result.metadata["offline_only"] is True
    assert result.metadata["auto_fetch_allowed"] is False
    assert result.metadata["auto_install_allowed"] is False
    assert result.output["offline_only"] is True
    assert result.output["source_reviews"][0]["source_name"] == "Review source"
    assert result.output["source_reviews"][0]["scorecard"]["license_score"] == 1.0
    assert result.output["candidate_reviews"]
    assert result.output["recommended_next_actions"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_skill_scout_builtin_rejects_empty_need() -> None:
    autoload_builtins()

    result = await dispatch("external-skill-scout", {})

    assert result.ok is False
    assert "task_need" in (result.error or "")
    assert result.metadata["production_action"] is False
