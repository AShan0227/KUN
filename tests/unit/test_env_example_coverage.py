"""Audit F119: .env.example documents every Settings field and stays valid.

The example env file had drifted ~50 vars behind what the code actually reads
(the whole V7 feature-flag family + the auth trio were undocumented), so a new
deployer copying it could not know auth must be enabled in production. These
tests keep .env.example honest: every typed Settings field must appear, the
security-critical auth section must be flagged production-required, and every
non-comment line must be a valid KEY=VALUE pair.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kun.core.config import Settings

_ENV_EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


@pytest.mark.unit
def test_every_settings_field_has_a_documented_env_key() -> None:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    present = set(re.findall(r"KUN_[A-Z0-9_]+", text))
    missing = [
        f"KUN_{name.upper()}"
        for name in Settings.model_fields
        if f"KUN_{name.upper()}" not in present
    ]
    assert not missing, (
        f".env.example is missing keys for these Settings fields: {missing}. "
        "Add a (commented) example line so deployers can discover them (F119)."
    )


@pytest.mark.unit
def test_auth_section_flags_production_required() -> None:
    text = _ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "KUN_AUTH_ENABLED" in text, ".env.example must document KUN_AUTH_ENABLED"
    assert "生产必" in text, (
        "the auth section must visibly flag that auth is production-required "
        "(F119/F007a) so a copy-paste deploy is not silently insecure"
    )


@pytest.mark.unit
def test_every_uncommented_line_is_valid_key_value() -> None:
    bad: list[str] = []
    for i, raw in enumerate(_ENV_EXAMPLE.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Any uppercase env name = value (KUN_* plus legit provider/OTEL vars).
        if not re.match(r"^[A-Z][A-Z0-9_]*=", line):
            bad.append(f"L{i}: {raw!r}")
    assert not bad, f".env.example has malformed (non KEY=value) lines: {bad}"
