"""SoulFile interruption frequency tests."""

from __future__ import annotations

import pytest
from kun.datamodel.soul_file import SoulFile
from pydantic import ValidationError


def test_interruption_frequency_defaults_to_ask_every_five_steps() -> None:
    soul = SoulFile(user_id="u-1")

    assert soul.interruption_frequency == "ask_every_n"
    assert soul.ask_every_n_steps == 5
    assert soul.should_interrupt_at_step(4) is False
    assert soul.should_interrupt_at_step(5) is True


def test_full_auto_never_interrupts() -> None:
    soul = SoulFile(user_id="u-1", interruption_frequency="full_auto")

    assert soul.should_interrupt_at_step(1) is False
    assert soul.should_interrupt_at_step(100) is False


def test_manual_review_interrupts_every_step() -> None:
    soul = SoulFile(user_id="u-1", interruption_frequency="manual_review")

    assert soul.should_interrupt_at_step(1) is True
    assert soul.should_interrupt_at_step(2) is True


def test_ask_every_n_uses_configured_step_interval() -> None:
    soul = SoulFile(user_id="u-1", interruption_frequency="ask_every_n", ask_every_n_steps=3)

    assert soul.should_interrupt_at_step(1) is False
    assert soul.should_interrupt_at_step(3) is True
    assert soul.should_interrupt_at_step(6) is True


def test_ask_every_n_steps_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        SoulFile(user_id="u-1", ask_every_n_steps=0)
