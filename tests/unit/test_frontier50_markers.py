"""Frontier50 can_run marker matching + configurable workdir (audit F144).

The old code matched ``"ab" in text`` (firing on database/table/label) and
hardcoded one developer's absolute workdir. Now "ab" must be a delimited token
and the workdir is env-configurable.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kun.control_plane.frontier50_external import (
    _default_workdir,
    _mentions_frontier50_or_ab,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "work-qi-ab-round-01",  # the real marker
        "frontier50-r01-test",
        "run the AB round",  # standalone, case-insensitive
        "frontier50 comparator",
    ],
)
def test_matches_real_markers(text: str) -> None:
    assert _mentions_frontier50_or_ab(text) is True


@pytest.mark.unit
@pytest.mark.parametrize(
    "text",
    [
        "database migration work",
        "build a data table",
        "label the fabricated output",
        "about to abandon the cabinet",
        "work-qi-product-build-01",
    ],
)
def test_does_not_match_substrings(text: str) -> None:
    assert _mentions_frontier50_or_ab(text) is False


@pytest.mark.unit
def test_workdir_defaults_and_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUN_FRONTIER50_WORKDIR", raising=False)
    # Fallback path is still a Path (back-compat for the original machine).
    assert isinstance(_default_workdir(), Path)

    monkeypatch.setenv("KUN_FRONTIER50_WORKDIR", "/tmp/kun-ab-workspace")
    assert _default_workdir() == Path("/tmp/kun-ab-workspace")
