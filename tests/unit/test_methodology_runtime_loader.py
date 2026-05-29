"""Unit tests for V7 §12 RSI runtime methodology loader/selector.

Covers V7.PHASE-X.G.RSI-CLOSED-LOOP read-side. Three pieces:
  1. load_methodologies() — yaml parse + status filter + error tolerance
  2. MethodologyRuntimeSelector.select_for() — keyword overlap scoring
  3. render_for_system_prompt() — orchestrator-ready string output
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kun.engineering.methodology_runtime_loader import (
    MethodologyEntry,
    MethodologyRuntimeSelector,
    TaskContext,
    load_methodologies,
    render_for_system_prompt,
)

YAML_PROD_A = """\
topic: production-loop-validation
title: real PG e2e chain proves wiring
description: |
  Chain multiple real-PG e2e tests to prove production loop closure.
trigger:
  - condition: new database-backed subsystem needs validation
action:
  - write one ultimate e2e test that walks all subsystems
  - use real writer/reader/emitter for each piece
  - assert row counts after the run
applicability:
  - V7 Phase X.B / X.C wiring proofs
  - acceptance gates
confidence: high
"""

YAML_PROD_B = """\
topic: collaboration
title: human in loop via collab ticket
description: |
  Use V7 §11 CollaborationTicket for irreversible actions, not bool flags.
trigger:
  - condition: capability transitions to production
  - condition: external action requires human approval
action:
  - open ticket type=approval with deadline + fallback_policy
  - wait for response OR fall back on deadline
  - pass ticket_id to downstream service for audit trail
applicability:
  - V7 §12.2 production flip
  - V7 §11.2 external action gate
confidence: high
"""

YAML_NON_PROD = """\
topic: experimental
title: unstable candidate not yet promoted
description: still in replay
lifecycle_stage: replay
trigger:
  - condition: experimental capability
action:
  - test only, do not use in production
"""

YAML_MALFORMED = """\
topic: malformed
title: this yaml is broken
description:
trigger:
  - [unbalanced
"""


@pytest.fixture
def seeds_dir(tmp_path: Path) -> Path:
    d = tmp_path / "methodologies"
    d.mkdir()
    (d / "production_a.yaml").write_text(YAML_PROD_A, encoding="utf-8")
    (d / "production_b.yaml").write_text(YAML_PROD_B, encoding="utf-8")
    (d / "non_production.yaml").write_text(YAML_NON_PROD, encoding="utf-8")
    (d / "malformed.yaml").write_text(YAML_MALFORMED, encoding="utf-8")
    return d


# ============================================================
# load_methodologies
# ============================================================


def test_load_methodologies_returns_production_only_by_default(seeds_dir: Path) -> None:
    entries = load_methodologies(seeds_dir)
    titles = {e.title for e in entries}
    assert "real PG e2e chain proves wiring" in titles
    assert "human in loop via collab ticket" in titles
    # Non-production seed must NOT leak into runtime context
    assert "unstable candidate not yet promoted" not in titles


def test_load_methodologies_status_whitelist_can_include_replay(
    seeds_dir: Path,
) -> None:
    entries = load_methodologies(
        seeds_dir, status_whitelist=("production", "replay")
    )
    titles = {e.title for e in entries}
    assert "unstable candidate not yet promoted" in titles


def test_load_methodologies_skips_malformed_yaml_without_raising(
    seeds_dir: Path,
) -> None:
    entries = load_methodologies(seeds_dir)
    # Malformed file is skipped, others return successfully
    assert len(entries) == 2


def test_load_methodologies_missing_dir_returns_empty(tmp_path: Path) -> None:
    entries = load_methodologies(tmp_path / "nonexistent")
    assert entries == []


def test_load_methodologies_normalizes_trigger_action_lists(
    seeds_dir: Path,
) -> None:
    entries = load_methodologies(seeds_dir)
    by_title = {e.title: e for e in entries}
    a = by_title["real PG e2e chain proves wiring"]
    # trigger yaml shape is [{condition: '...'}] — flattened to list[str]
    assert any("database-backed" in t for t in a.triggers)
    # action yaml shape is [str, str, str]
    assert "write one ultimate e2e test that walks all subsystems" in a.actions


# ============================================================
# MethodologyRuntimeSelector.select_for
# ============================================================


def test_selector_returns_empty_when_query_has_no_overlap(seeds_dir: Path) -> None:
    selector = MethodologyRuntimeSelector(load_methodologies(seeds_dir))
    ctx = TaskContext(
        task_type="quantum.physics.computation",
        goal_keywords=["entanglement", "qubit"],
        goal_statement="design a quantum compiler",
    )
    assert selector.select_for(ctx) == []


def test_selector_picks_production_loop_seed_for_e2e_query(
    seeds_dir: Path,
) -> None:
    selector = MethodologyRuntimeSelector(load_methodologies(seeds_dir))
    ctx = TaskContext(
        task_type="testing.acceptance.production",
        goal_keywords=["e2e", "real", "PG", "wiring"],
        goal_statement="prove production wiring closure via real PG e2e test",
    )
    chosen = selector.select_for(ctx, top_k=2)
    assert len(chosen) >= 1
    assert chosen[0].title == "real PG e2e chain proves wiring"
    assert chosen[0].score > 0


def test_selector_picks_collab_seed_for_human_in_loop_query(
    seeds_dir: Path,
) -> None:
    selector = MethodologyRuntimeSelector(load_methodologies(seeds_dir))
    ctx = TaskContext(
        task_type="governance.collaboration",
        goal_keywords=["human", "approval", "ticket", "production"],
        goal_statement="gate production flip via collaboration ticket",
    )
    chosen = selector.select_for(ctx, top_k=1)
    assert len(chosen) == 1
    assert chosen[0].title == "human in loop via collab ticket"


def test_selector_top_k_caps_results(seeds_dir: Path) -> None:
    selector = MethodologyRuntimeSelector(load_methodologies(seeds_dir))
    ctx = TaskContext(
        task_type="governance.production",
        goal_statement="production wiring + real PG + ticket gate",
    )
    chosen = selector.select_for(ctx, top_k=1)
    assert len(chosen) == 1


def test_selector_min_score_drops_weakly_relevant(seeds_dir: Path) -> None:
    entries = load_methodologies(seeds_dir)
    selector = MethodologyRuntimeSelector(entries, min_score=0.95)
    ctx = TaskContext(
        task_type="testing",
        goal_statement="real production wiring",
    )
    # min_score=0.95 is unattainable for short queries — nothing returned
    assert selector.select_for(ctx) == []


# ============================================================
# render_for_system_prompt
# ============================================================


def test_render_for_system_prompt_emits_empty_when_no_entries() -> None:
    assert render_for_system_prompt([]) == ""


def test_render_for_system_prompt_contains_titles_and_actions(
    seeds_dir: Path,
) -> None:
    entries = load_methodologies(seeds_dir)
    selector = MethodologyRuntimeSelector(entries)
    chosen = selector.select_for(
        TaskContext(
            task_type="governance.production",
            goal_statement="production wiring + real PG + ticket gate + human approval",
        ),
        top_k=2,
    )
    out = render_for_system_prompt(chosen)
    assert "RSI 进化产物" in out
    assert "real PG e2e chain proves wiring" in out or "human in loop" in out
    # Should include at least one "行动:" prefix
    assert "行动:" in out


def test_render_for_system_prompt_truncates_very_long_descriptions() -> None:
    very_long_desc = "x" * 1000
    entry = MethodologyEntry(
        file_path="/fake",
        topic="t",
        title="long-desc-methodology",
        description=very_long_desc,
        actions=["a" * 1000],
        score=0.5,
    )
    out = render_for_system_prompt([entry])
    # No single rendered line should exceed reasonable bound
    lines = out.splitlines()
    assert all(len(line) <= 300 for line in lines), (
        f"some line is too long: {max(len(line) for line in lines)}"
    )
