"""DOGFOOD-P4 — unit tests for grep-verify skill.

Covers:
  - confirmed verdict when pattern matches (returns parsed file:line:text)
  - refuted verdict when pattern doesn't match (verdict == 'refuted')
  - whitelist enforcement (root must be in allowed dirs)
  - path traversal refusal (`..`)
  - forbidden shell metacharacters in pattern
  - max_matches cap enforced
  - KUN_REPO_ROOT env override works
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from kun.skills.builtin.grep_verify import execute


@pytest.fixture(autouse=True)
def _fake_repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Build a fake repo with whitelist dirs + content to grep against."""
    (tmp_path / "kun" / "agents").mkdir(parents=True)
    (tmp_path / "kun" / "agents" / "x.py").write_text(
        "class TaskPlanner:\n    pass\n\ndef plan_task():\n    pass\n"
    )
    (tmp_path / "kun" / "agents" / "y.py").write_text(
        "from kun.agents.x import TaskPlanner\n\n# also TaskPlanner here\n"
    )
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "secret.py").write_text("class SecretClass:\n    pass\n")
    monkeypatch.setenv("KUN_REPO_ROOT", str(tmp_path))
    return tmp_path


@pytest.mark.unit
def test_confirmed_when_pattern_matches() -> None:
    r = asyncio.run(
        execute({"pattern": r"class TaskPlanner\b", "root": "kun"})
    )
    assert r.ok, r.error
    assert r.output["verdict"] == "confirmed"
    assert r.output["match_count"] >= 1
    # At least one match in x.py
    assert any(m["path"].endswith("x.py") for m in r.output["matches"])


@pytest.mark.unit
def test_refuted_when_no_matches() -> None:
    r = asyncio.run(
        execute({"pattern": r"class DefinitelyMissingClass\b", "root": "kun"})
    )
    assert r.ok, r.error
    assert r.output["verdict"] == "refuted"
    assert r.output["match_count"] == 0
    assert r.output["matches"] == []


@pytest.mark.unit
def test_match_includes_line_number_and_text() -> None:
    r = asyncio.run(execute({"pattern": "TaskPlanner", "root": "kun"}))
    assert r.ok
    for m in r.output["matches"]:
        assert "path" in m
        assert "line_no" in m and isinstance(m["line_no"], int)
        assert "line" in m
        assert "TaskPlanner" in m["line"]


@pytest.mark.unit
def test_root_outside_whitelist_refused() -> None:
    """'private/' is NOT in whitelist."""
    r = asyncio.run(execute({"pattern": "SecretClass", "root": "private"}))
    assert r.ok is False
    assert "not in whitelist" in r.error


@pytest.mark.unit
def test_path_traversal_refused() -> None:
    r = asyncio.run(execute({"pattern": "anything", "root": "kun/../private"}))
    assert r.ok is False
    # Either traversal or whitelist refusal — both valid blocks
    assert "not in whitelist" in r.error or "escapes" in r.error


@pytest.mark.unit
def test_pattern_with_shell_metachars_refused() -> None:
    """Defense in depth: even though we pass via argv (not shell), refuse
    patterns that contain shell-only metacharacters. Catches mistakes where
    LLM might pass a literal shell command thinking it's regex."""
    r = asyncio.run(execute({"pattern": "foo; rm -rf /", "root": "kun"}))
    assert r.ok is False
    assert "forbidden" in r.error


@pytest.mark.unit
def test_empty_pattern_returns_clear_error() -> None:
    r = asyncio.run(execute({"pattern": "", "root": "kun"}))
    assert r.ok is False
    assert "pattern is required" in r.error


@pytest.mark.unit
def test_max_matches_cap_enforced() -> None:
    """Set max_matches small and verify cap + truncated flag."""
    r = asyncio.run(
        execute(
            {
                "pattern": "TaskPlanner",
                "root": "kun",
                "max_matches": 1,
            }
        )
    )
    assert r.ok
    assert len(r.output["matches"]) <= 1
    # If the fixture has 3 occurrences and cap is 1, must be truncated
    if r.output["match_count"] >= 1:
        assert r.output["truncated"] is True


@pytest.mark.unit
def test_claim_field_passes_through() -> None:
    """The optional 'claim' free-text is echoed in output for logging trail."""
    r = asyncio.run(
        execute(
            {
                "pattern": "TaskPlanner",
                "root": "kun",
                "claim": "verifying TaskPlanner is wired",
            }
        )
    )
    assert r.ok
    assert r.output["claim"] == "verifying TaskPlanner is wired"


@pytest.mark.unit
def test_root_default_is_kun() -> None:
    """When root not specified, defaults to 'kun' (the main source tree)."""
    r = asyncio.run(execute({"pattern": "TaskPlanner"}))
    assert r.ok
    assert r.output["verdict"] == "confirmed"
