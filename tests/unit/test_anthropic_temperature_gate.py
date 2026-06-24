"""Which models accept `temperature` (audit F122).

Fable 5 / Opus 4.7 / 4.8 (+ Mythos) removed sampling params and 400 if temperature
is sent. The old check only excluded opus-4-7, so opus-4-8 / fable-5 would 400.
"""

from __future__ import annotations

import pytest
from kun.interface.llm.anthropic_provider import _accepts_temperature


@pytest.mark.unit
@pytest.mark.parametrize(
    "model_id",
    [
        "claude-opus-4-7",
        "claude-opus-4-8",
        "claude-fable-5",
        "claude-mythos-5",
    ],
)
def test_temperature_rejected_for_newer_models(model_id: str) -> None:
    assert _accepts_temperature(model_id) is False


@pytest.mark.unit
@pytest.mark.parametrize(
    "model_id",
    [
        "claude-sonnet-4-6",
        "claude-haiku-4-5",
        "claude-haiku-4-5-20251001",
        "claude-opus-4-6",
    ],
)
def test_temperature_accepted_for_older_models(model_id: str) -> None:
    assert _accepts_temperature(model_id) is True
