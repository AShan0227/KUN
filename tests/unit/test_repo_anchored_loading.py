"""Skills + watchtower rules load from a non-repo-root cwd (audit F152).

Both defaulted to cwd-relative paths ("skills" / "rules"), so launching from any
other directory silently loaded nothing. They now fall back to the repo-root-
anchored path.
"""

from __future__ import annotations

import pytest
from kun.skills.loader import load_skills_from_dir
from kun.watchtower.engine import load_rules


@pytest.mark.unit
def test_skills_load_from_foreign_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # a cwd with no ./skills
    registry = load_skills_from_dir()  # default "skills"
    assert len(registry) >= 1, "skills should load via repo-root fallback"


@pytest.mark.unit
def test_rules_load_from_foreign_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)  # a cwd with no ./rules
    rules = load_rules()  # default "rules"
    assert len(rules) >= 1, "watchtower rules should load via repo-root fallback"


@pytest.mark.unit
def test_explicit_existing_root_still_wins(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # An explicitly-passed, existing (empty) dir must be honored, not overridden.
    (tmp_path / "SKILL_PLACEHOLDER").write_text("noop", encoding="utf-8")
    registry = load_skills_from_dir(tmp_path)
    assert len(registry) == 0  # no SKILL.md here → empty, not the repo's skills
