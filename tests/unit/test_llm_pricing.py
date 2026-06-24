"""LLM pricing + cost accounting (audit F024).

The Anthropic pricing table was wrong (Opus 3x high, Haiku 4x low) and cost
omitted cache-write tokens. These guard the corrected rates and the cache-write
charge, since both feed the ADR-008 cost loop / budget kill-switch.
"""

from __future__ import annotations

import pytest
from kun.interface.llm.anthropic_provider import _PRICING, AnthropicProvider
from kun.interface.llm.base import UsageInfo


@pytest.mark.unit
def test_pricing_table_matches_published_rates() -> None:
    # Authoritative rates (USD / 1M tokens) — claude-api skill, 2026-06.
    assert _PRICING["claude-opus-4-8"]["input"] == 5.0
    assert _PRICING["claude-opus-4-8"]["output"] == 25.0
    assert _PRICING["claude-sonnet-4-6"]["input"] == 3.0
    assert _PRICING["claude-haiku-4-5"]["input"] == 1.0
    assert _PRICING["claude-haiku-4-5"]["output"] == 5.0
    assert _PRICING["claude-fable-5"]["input"] == 10.0
    # Opus 4.7 was the 3x-too-high entry — must now match Opus-tier $5/$25.
    assert _PRICING["claude-opus-4-7"]["input"] == 5.0


@pytest.mark.unit
def test_cache_read_is_one_tenth_input() -> None:
    for mid, p in _PRICING.items():
        assert p["cached"] == pytest.approx(p["input"] * 0.1), mid


@pytest.mark.unit
def test_cache_write_is_1_25x_input() -> None:
    for mid, p in _PRICING.items():
        assert p["cache_write"] == pytest.approx(p["input"] * 1.25), mid


@pytest.mark.unit
def test_compute_cost_charges_cache_write() -> None:
    provider = AnthropicProvider(model_id="claude-opus-4-8", tier="top")
    # 1M cache-write tokens at $6.25/M = $6.25; nothing else set.
    usage = UsageInfo(cache_creation_input_tokens=1_000_000)
    assert provider.compute_cost(usage) == pytest.approx(6.25)


@pytest.mark.unit
def test_compute_cost_full_breakdown() -> None:
    provider = AnthropicProvider(model_id="claude-opus-4-8", tier="top")
    usage = UsageInfo(
        input_tokens=1_000_000,  # $5
        output_tokens=1_000_000,  # $25
        cached_input_tokens=1_000_000,  # $0.5 (read)
        cache_creation_input_tokens=1_000_000,  # $6.25 (write)
    )
    assert provider.compute_cost(usage) == pytest.approx(5.0 + 25.0 + 0.5 + 6.25)
