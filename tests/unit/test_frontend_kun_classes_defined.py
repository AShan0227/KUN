"""Audit F067: every kun-* class used in the frontend is defined in globals.css.

The kun-* component classes (kun-nav-link, kun-surface, ...) were referenced
across the app but had zero definitions, so those surfaces rendered unstyled.
This static guard (reads files only, no Node/build needed) asserts the set of
kun-* classes referenced in templates is a subset of those defined in
globals.css, so the gap cannot silently reopen.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
_SRC = _FRONTEND / "src"
_GLOBALS = _SRC / "app" / "globals.css"

pytestmark = pytest.mark.unit


def _used_classes() -> set[str]:
    used: set[str] = set()
    for path in _SRC.rglob("*.tsx"):
        for m in re.findall(r"kun-[a-z0-9-]+", path.read_text(encoding="utf-8")):
            used.add(m)
    return used


def _defined_classes() -> set[str]:
    css = _GLOBALS.read_text(encoding="utf-8")
    # `.kun-foo {` selectors defined in globals.css
    return set(re.findall(r"\.(kun-[a-z0-9-]+)\s*\{", css))


@pytest.mark.unit
def test_globals_css_exists_and_defines_kun_classes() -> None:
    assert _GLOBALS.is_file(), f"missing {_GLOBALS}"
    assert _defined_classes(), "globals.css defines no kun-* classes (F067 regressed)"


@pytest.mark.unit
def test_every_used_kun_class_is_defined() -> None:
    used = _used_classes()
    assert used, "no kun-* classes found in frontend templates — check the glob"
    missing = sorted(used - _defined_classes())
    assert not missing, (
        f"these kun-* classes are used in templates but undefined in globals.css "
        f"(would render unstyled — F067): {missing}"
    )
