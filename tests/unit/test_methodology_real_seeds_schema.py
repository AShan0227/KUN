"""Real seed methodologies must parse their dict-valued bodies (audit F034).

The shipped seeds in seeds/methodologies/ use mapping-valued `trigger` / `action`
/ `applicability` (e.g. action: {required_sections: [...], ...}). The loader's
_listify previously returned [] for a top-level mapping, dropping actions for
~28/33 promoted methodologies — gutting the RSI read side. These guard that the
structured bodies are recovered.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kun.engineering.methodology_runtime_loader import _listify, load_methodologies

_SEEDS = Path(__file__).resolve().parents[2] / "seeds" / "methodologies"


@pytest.mark.unit
def test_listify_flattens_nested_mapping() -> None:
    raw = {
        "required_sections": ["状态", "背景"],
        "dependency_ordering": {"description": "order rule", "rule": "1. graph"},
        "signals_from": [{"human directive": "写 ADR"}],
    }
    out = _listify(raw)
    assert "状态" in out
    assert "order rule" in out
    assert "1. graph" in out
    assert "写 ADR" in out


@pytest.mark.unit
def test_listify_preserves_old_shapes() -> None:
    assert _listify("hello") == ["hello"]
    assert _listify(["a", "b"]) == ["a", "b"]
    assert _listify([{"condition": "x"}]) == ["x"]
    assert _listify(None) == []


@pytest.mark.unit
def test_real_seed_dict_valued_action_not_dropped() -> None:
    if not _SEEDS.is_dir():
        pytest.skip("seeds/methodologies not present")
    by_topic = {e.topic: e for e in load_methodologies(_SEEDS)}
    adr = by_topic.get("adr_authoring")
    assert adr is not None, "adr_authoring seed not loaded"
    # action: is a mapping in the YAML — must flatten, not drop.
    assert adr.actions, "actions empty — dict-valued action body was dropped"
    assert adr.triggers, "triggers empty — dict-valued trigger body was dropped"
    assert adr.applicability, "applicability empty — dict body was dropped"


@pytest.mark.unit
def test_most_real_seeds_have_actions() -> None:
    if not _SEEDS.is_dir():
        pytest.skip("seeds/methodologies not present")
    entries = load_methodologies(_SEEDS)
    assert len(entries) >= 20
    with_actions = [e for e in entries if e.actions]
    # Before the fix only the few list-valued seeds had actions; now the
    # mapping-valued majority do too.
    assert len(with_actions) >= int(0.9 * len(entries)), (
        f"only {len(with_actions)}/{len(entries)} seeds have actions"
    )
