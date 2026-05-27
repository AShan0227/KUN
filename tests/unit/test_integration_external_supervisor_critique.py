"""Unit tests for kun.integration.external_supervisor_critique.

The critique prompt renderer is the integration-layer template that the
LongTaskOrchestrator hands to ``ExternalSupervisorService.analyze_observation``
on its every-K-step cross-check. We verify the prompt carries the anchor
block, the right number of recent steps, the JSON output spec, and that
truncation / validation hold.
"""

from __future__ import annotations

import pytest
from kun.integration.external_supervisor_critique import (
    CRITIQUE_SYSTEM_PROMPT_TEMPLATE,
    MAX_CHARS_PER_STEP,
    MAX_STEPS_IN_PROMPT,
    render_critique_prompt,
)

# --------------------------------------------------------------------------- #
# Static template assertions                                                  #
# --------------------------------------------------------------------------- #


def test_template_contains_critical_sections() -> None:
    """Template carries the four immutable sections the supervisor needs."""
    assert "EXTERNAL CRITIC" in CRITIQUE_SYSTEM_PROMPT_TEMPLATE
    assert "GOAL ANCHOR" in CRITIQUE_SYSTEM_PROMPT_TEMPLATE
    assert "LAST {n_steps} STEPS" in CRITIQUE_SYSTEM_PROMPT_TEMPLATE
    # JSON output spec — the supervisor parses JSON-first
    assert '"verdict"' in CRITIQUE_SYSTEM_PROMPT_TEMPLATE
    assert '"drift_signals"' in CRITIQUE_SYSTEM_PROMPT_TEMPLATE
    assert '"self_aggrandizement_detected"' in CRITIQUE_SYSTEM_PROMPT_TEMPLATE
    assert '"rationale"' in CRITIQUE_SYSTEM_PROMPT_TEMPLATE
    assert '"recommended_action"' in CRITIQUE_SYSTEM_PROMPT_TEMPLATE


# --------------------------------------------------------------------------- #
# Rendering behaviour                                                         #
# --------------------------------------------------------------------------- #


def test_render_includes_anchor_block_with_goal_and_criteria() -> None:
    """Anchor's goal_statement + success_criteria + invariants appear verbatim."""
    anchor = {
        "goal_statement": "ship OAuth2 PKCE",
        "success_criteria": ["login works", "refresh works"],
        "out_of_scope": ["payment"],
        "invariants": ["never log passwords"],
    }
    out = render_critique_prompt(
        anchor=anchor,
        last_steps=[{"step_idx": 1, "summary": "drafted schema"}],
    )

    assert "Goal: ship OAuth2 PKCE" in out
    assert "login works" in out
    assert "refresh works" in out
    assert "payment" in out
    assert "never log passwords" in out


def test_render_includes_last_steps_in_order() -> None:
    """Steps render as `[step N] <summary>` lines, in input order."""
    anchor = {"goal_statement": "g"}
    steps = [
        {"step_idx": 1, "summary": "looked up docs"},
        {"step_idx": 2, "summary": "wrote test"},
        {"step_idx": 3, "summary": "ran test"},
    ]
    out = render_critique_prompt(anchor=anchor, last_steps=steps)

    assert "[step 1] looked up docs" in out
    assert "[step 2] wrote test" in out
    assert "[step 3] ran test" in out
    # Order: step 1 appears before step 3 in the rendered text
    assert out.index("[step 1]") < out.index("[step 3]")
    # n_steps placeholder populated
    assert "LAST 3 STEPS" in out


def test_render_includes_json_output_spec() -> None:
    """Rendered prompt instructs the LLM to return strict JSON."""
    anchor = {"goal_statement": "g"}
    out = render_critique_prompt(
        anchor=anchor,
        last_steps=[{"step_idx": 1, "summary": "x"}],
    )

    # All five JSON keys appear
    assert '"verdict"' in out
    assert '"drift_signals"' in out
    assert '"self_aggrandizement_detected"' in out
    assert '"rationale"' in out
    assert '"recommended_action"' in out
    # Plus the "only JSON" instruction
    assert "只输出 JSON" in out


def test_render_truncates_to_max_steps() -> None:
    """When > max_steps_in_prompt steps are given, the front is dropped."""
    anchor = {"goal_statement": "g"}
    # 15 steps, default max is 10
    steps = [{"step_idx": i, "summary": f"summary {i}"} for i in range(1, 16)]
    out = render_critique_prompt(anchor=anchor, last_steps=steps)

    # The most recent steps (6..15) survive; the front (1..5) is dropped
    assert "[step 15] summary 15" in out
    assert "[step 6] summary 6" in out
    assert "[step 1]" not in out
    assert "[step 5]" not in out
    # n_steps rendered = MAX_STEPS_IN_PROMPT (not 15)
    assert f"LAST {MAX_STEPS_IN_PROMPT} STEPS" in out


def test_render_respects_custom_max_steps_in_prompt() -> None:
    """Caller can shrink the look-back window via max_steps_in_prompt."""
    anchor = {"goal_statement": "g"}
    steps = [{"step_idx": i, "summary": f"s{i}"} for i in range(1, 6)]
    out = render_critique_prompt(
        anchor=anchor, last_steps=steps, max_steps_in_prompt=2
    )

    # Only the last two survive
    assert "[step 4]" in out
    assert "[step 5]" in out
    assert "[step 3]" not in out
    assert "LAST 2 STEPS" in out


def test_render_truncates_oversized_per_step_summary() -> None:
    """Each step's summary is capped at MAX_CHARS_PER_STEP characters."""
    anchor = {"goal_statement": "g"}
    huge = "x" * (MAX_CHARS_PER_STEP + 500)
    out = render_critique_prompt(
        anchor=anchor, last_steps=[{"step_idx": 1, "summary": huge}]
    )

    # We rendered a truncated version with "..." marker
    assert "..." in out
    # The verbatim oversized payload did not appear
    assert huge not in out


def test_render_rejects_empty_anchor() -> None:
    """Empty anchor → ValueError (critique with nothing to compare against)."""
    with pytest.raises(ValueError, match="anchor"):
        render_critique_prompt(
            anchor={}, last_steps=[{"step_idx": 1, "summary": "x"}]
        )


def test_render_rejects_none_anchor() -> None:
    """None anchor → ValueError. Mirrors empty-dict case for safety."""
    with pytest.raises(ValueError, match="anchor"):
        render_critique_prompt(
            anchor=None,  # type: ignore[arg-type]
            last_steps=[{"step_idx": 1, "summary": "x"}],
        )


def test_render_rejects_invalid_max_steps() -> None:
    """max_steps_in_prompt < 1 is rejected."""
    anchor = {"goal_statement": "g"}
    with pytest.raises(ValueError, match="max_steps_in_prompt"):
        render_critique_prompt(
            anchor=anchor,
            last_steps=[{"step_idx": 1, "summary": "x"}],
            max_steps_in_prompt=0,
        )


def test_render_handles_empty_last_steps() -> None:
    """No recent steps → still renders, with a placeholder ('no recent steps')."""
    anchor = {"goal_statement": "g"}
    out = render_critique_prompt(anchor=anchor, last_steps=[])

    assert "(no recent steps)" in out
    assert "LAST 0 STEPS" in out


def test_render_uses_placeholder_for_blank_summary() -> None:
    """A step with an empty summary still renders, with a placeholder."""
    anchor = {"goal_statement": "g"}
    out = render_critique_prompt(
        anchor=anchor, last_steps=[{"step_idx": 42, "summary": ""}]
    )

    assert "[step 42]" in out
    assert "(no summary recorded)" in out


def test_render_anchor_with_only_goal_statement() -> None:
    """Minimal anchor (only goal_statement) is sufficient — no list sections."""
    anchor = {"goal_statement": "fix the bug"}
    out = render_critique_prompt(
        anchor=anchor, last_steps=[{"step_idx": 1, "summary": "did stuff"}]
    )

    assert "Goal: fix the bug" in out
    # No "Success criteria:" header rendered when the list is absent
    assert "Success criteria:" not in out
    assert "Invariants:" not in out
