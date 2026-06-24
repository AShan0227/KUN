"""Audit F111: every integration test file carries an integration/e2e marker.

Without the marker, ``pytest -m "not integration and not e2e"`` still selects
DB/Docker-dependent tests, so running a pure offline unit subset was impossible.
This static guard (reads source only, no DB) keeps the marker discipline so the
isolation cannot silently rot again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_INTEGRATION_DIR = Path(__file__).resolve().parents[1] / "integration"
_EXCLUDE = {"__init__.py", "conftest.py"}

_TEST_FILES = sorted(
    p for p in _INTEGRATION_DIR.glob("test_*.py") if p.name not in _EXCLUDE
)


@pytest.mark.unit
def test_integration_dir_is_nonempty() -> None:
    # Guards against the glob silently matching nothing (which would make the
    # marker check below vacuously pass).
    assert _TEST_FILES, f"no integration test files found under {_INTEGRATION_DIR}"


@pytest.mark.unit
@pytest.mark.parametrize("path", _TEST_FILES, ids=lambda p: p.name)
def test_integration_file_has_marker(path: Path) -> None:
    src = path.read_text(encoding="utf-8")
    has_marker = (
        "pytestmark = pytest.mark.integration" in src
        or "pytestmark = pytest.mark.e2e" in src
        or "pytest.mark.integration" in src
        or "pytest.mark.e2e" in src
    )
    assert has_marker, (
        f"{path.name} has no integration/e2e marker — add "
        "`pytestmark = pytest.mark.integration` so `-m 'not integration and not "
        "e2e'` can isolate the offline unit subset (F111)."
    )
