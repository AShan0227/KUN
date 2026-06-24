"""Anthropic stop_reason → finish_reason mapping (audit F124).

The old mapping folded refusal / model_context_window_exceeded into "stop", so a
declined or context-truncated response looked like a successful completion.
"""

from __future__ import annotations

import pytest
from kun.interface.llm.anthropic_provider import _map_finish_reason


@pytest.mark.unit
@pytest.mark.parametrize(
    "stop_reason,has_tools,expected",
    [
        ("end_turn", False, "stop"),
        ("stop_sequence", False, "stop"),
        ("max_tokens", False, "length"),
        ("model_context_window_exceeded", False, "length"),
        ("refusal", False, "error"),
        ("end_turn", True, "tool_use"),  # tool calls win
        ("max_tokens", True, "tool_use"),
        (None, False, "stop"),
    ],
)
def test_map_finish_reason(stop_reason, has_tools, expected) -> None:
    assert _map_finish_reason(stop_reason, has_tool_calls=has_tools) == expected


@pytest.mark.unit
def test_refusal_is_not_masked_as_success() -> None:
    # The whole point of F124: a refusal must NOT report as "stop".
    assert _map_finish_reason("refusal", has_tool_calls=False) == "error"
    assert _map_finish_reason("refusal", has_tool_calls=False) != "stop"
