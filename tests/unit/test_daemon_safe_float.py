"""_safe_float keeps malformed gate-evidence JSON from killing the daemon (audit F073).

Gate evidence files are written by the business workspace and aren't validated, so
a non-numeric value used to crash float() inside the tick. _safe_float coerces and
falls back to a conservative default instead of raising.
"""

from __future__ import annotations

import pytest
from kun.control_plane.daemon import _safe_float


@pytest.mark.unit
@pytest.mark.parametrize(
    "value,default,expected",
    [
        (0.95, 0.0, 0.95),
        ("0.8", 0.0, 0.8),
        (1, 0.0, 1.0),
        ("n/a", 0.0, 0.0),  # non-numeric → default
        (None, 0.95, 0.95),
        ({"score": 1}, 0.0, 0.0),  # dict → default, no crash
        ([], 1.0, 1.0),
        ("", 0.5, 0.5),
    ],
)
def test_safe_float_coerces_or_defaults(value, default, expected) -> None:
    assert _safe_float(value, default=default) == expected


@pytest.mark.unit
def test_safe_float_never_raises_on_garbage() -> None:
    # The whole point of F073: malformed evidence must not propagate an exception
    # out of the daemon tick.
    for junk in ("abc", None, {}, [], object(), float("nan")):
        _safe_float(junk, default=0.0)  # must not raise
