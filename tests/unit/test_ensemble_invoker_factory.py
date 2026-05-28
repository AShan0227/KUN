"""V7 Phase X.B.MF-2 — ensemble_invoker_factory unit tests.

Tests the factory logic in isolation (no LongTaskOrchestrator coupling).
Wiring proof — i.e. orchestrator真 imports + calls the factory — is in
tests/integration/test_long_task_uses_ensemble_when_enabled.py.
"""

from __future__ import annotations

import pytest
from kun.integration.ensemble_invoker_factory import (
    _has_cross_family_pair,
    _parse_tiers_csv,
    _resolve_consensus_strategy,
    _select_providers,
    _truthy,
    build_ensemble_invoker_from_settings,
)
from kun.interface.llm.ensemble import ConsensusStrategy
from kun.interface.llm.router import LLMRouter
from kun.interface.llm.stub_provider import StubProvider


def _make_router(
    *,
    top_provider: StubProvider | None = None,
    strong_provider: StubProvider | None = None,
    coding_provider: StubProvider | None = None,
    cheap_provider: StubProvider | None = None,
) -> LLMRouter:
    providers: dict[str, StubProvider] = {}
    if top_provider:
        providers["top"] = top_provider  # type: ignore[index]
    if strong_provider:
        providers["strong"] = strong_provider  # type: ignore[index]
    if coding_provider:
        providers["coding"] = coding_provider  # type: ignore[index]
    if cheap_provider:
        providers["cheap"] = cheap_provider  # type: ignore[index]
    return LLMRouter(providers=providers)  # type: ignore[arg-type]


# ============================================================
# Small helpers
# ============================================================


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("", False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_truthy_parsing(value: str, expected: bool) -> None:
    assert _truthy(value) is expected


def test_parse_tiers_csv_strips_and_drops_blank() -> None:
    assert _parse_tiers_csv("top, cheap , ") == ["top", "cheap"]
    assert _parse_tiers_csv("") == []
    assert _parse_tiers_csv("  ,  ") == []


def test_resolve_consensus_strategy_defaults_to_majority() -> None:
    assert _resolve_consensus_strategy(None) == ConsensusStrategy.MAJORITY_VOTE
    assert _resolve_consensus_strategy("") == ConsensusStrategy.MAJORITY_VOTE


def test_resolve_consensus_strategy_known_values() -> None:
    assert (
        _resolve_consensus_strategy("majority_vote")
        == ConsensusStrategy.MAJORITY_VOTE
    )
    assert _resolve_consensus_strategy("weighted") == ConsensusStrategy.WEIGHTED
    assert (
        _resolve_consensus_strategy("pick_best_by_metric")
        == ConsensusStrategy.PICK_BEST_BY_METRIC
    )


def test_resolve_consensus_strategy_unknown_logs_and_falls_back() -> None:
    assert (
        _resolve_consensus_strategy("BOGUS")
        == ConsensusStrategy.MAJORITY_VOTE
    )


# ============================================================
# _select_providers
# ============================================================


def test_select_providers_picks_named_tiers() -> None:
    p_top = StubProvider(model_id="claude-opus-4-7", tier="top")
    p_cheap = StubProvider(model_id="gpt-5.5", tier="cheap")
    router = _make_router(top_provider=p_top, cheap_provider=p_cheap)
    selected = _select_providers(router, ["top", "cheap"])
    assert len(selected) == 2
    assert {p.model_id for p in selected} == {"claude-opus-4-7", "gpt-5.5"}


def test_select_providers_skips_unknown_tier() -> None:
    p_top = StubProvider(model_id="claude-opus-4-7", tier="top")
    router = _make_router(top_provider=p_top)
    selected = _select_providers(router, ["top", "nonexistent"])
    assert len(selected) == 1


def test_select_providers_dedupes_same_provider_across_tiers() -> None:
    """Two tiers pointing at the same provider instance → dedupe to 1."""
    shared = StubProvider(model_id="claude-opus-4-7", tier="top")
    router = _make_router(top_provider=shared, strong_provider=shared)
    selected = _select_providers(router, ["top", "strong"])
    assert len(selected) == 1


# ============================================================
# _has_cross_family_pair
# ============================================================


def test_cross_family_pair_detected() -> None:
    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="gpt-5.5", tier="cheap")
    assert _has_cross_family_pair([p1, p2]) is True


def test_cross_family_pair_same_family_returns_false() -> None:
    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="claude-haiku-4-5", tier="cheap")
    assert _has_cross_family_pair([p1, p2]) is False


# ============================================================
# build_ensemble_invoker_from_settings — env-var orchestration
# ============================================================


def test_factory_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KUN_V7_ENSEMBLE_ENABLED", raising=False)
    router = _make_router(
        top_provider=StubProvider(model_id="claude-opus-4-7", tier="top"),
        cheap_provider=StubProvider(model_id="gpt-5.5", tier="cheap"),
    )
    assert build_ensemble_invoker_from_settings(router=router) is None


def test_factory_returns_none_when_too_few_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_ENSEMBLE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_TIERS", "top")  # 1 tier only
    router = _make_router(
        top_provider=StubProvider(model_id="claude-opus-4-7", tier="top")
    )
    assert build_ensemble_invoker_from_settings(router=router) is None


def test_factory_returns_none_when_no_cross_family_pair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All providers same family (e.g. all anthropic) → factory bails out.

    Better to fall back to single-LLM than crash at runtime.
    """
    monkeypatch.setenv("KUN_V7_ENSEMBLE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_TIERS", "top,cheap")
    router = _make_router(
        top_provider=StubProvider(model_id="claude-opus-4-7", tier="top"),
        cheap_provider=StubProvider(model_id="claude-haiku-4-5", tier="cheap"),
    )
    assert build_ensemble_invoker_from_settings(router=router) is None


def test_factory_returns_invoker_when_cross_family_2_tiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Happy path — cross-family pair from router, env on → invoker returned."""
    monkeypatch.setenv("KUN_V7_ENSEMBLE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_TIERS", "top,cheap")
    router = _make_router(
        top_provider=StubProvider(model_id="claude-opus-4-7", tier="top"),
        cheap_provider=StubProvider(model_id="gpt-5.5", tier="cheap"),
    )
    invoker = build_ensemble_invoker_from_settings(router=router)
    assert invoker is not None
    assert callable(invoker)


async def test_factory_invoker_actually_runs_cross_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The returned invoker, when called, runs the ensemble path."""
    monkeypatch.setenv("KUN_V7_ENSEMBLE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_TIERS", "top,cheap")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_STRATEGY", "majority_vote")
    router = _make_router(
        top_provider=StubProvider(model_id="claude-opus-4-7", tier="top"),
        cheap_provider=StubProvider(model_id="gpt-5.5", tier="cheap"),
    )
    invoker = build_ensemble_invoker_from_settings(router=router)
    assert invoker is not None
    step = await invoker([{"role": "user", "content": "hello ensemble"}])
    # Stub providers always succeed, step should have content
    assert step.content
    assert isinstance(step.usage_tokens, int)


def test_factory_respects_strategy_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KUN_V7_ENSEMBLE_STRATEGY=weighted should not error out factory build."""
    monkeypatch.setenv("KUN_V7_ENSEMBLE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_TIERS", "top,cheap")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_STRATEGY", "weighted")
    router = _make_router(
        top_provider=StubProvider(model_id="claude-opus-4-7", tier="top"),
        cheap_provider=StubProvider(model_id="gpt-5.5", tier="cheap"),
    )
    invoker = build_ensemble_invoker_from_settings(router=router)
    assert invoker is not None


# ============================================================
# Production wiring grep guard (regression防)
# ============================================================


def test_production_orchestrator_imports_factory() -> None:
    """The exact production construction site must import this factory.

    Without this import line, MF-2 wiring is broken — and the V7 §11.4
    ensemble would once again be orphan code.
    """
    from pathlib import Path

    orch_source = (
        Path(__file__).resolve().parent.parent.parent
        / "kun"
        / "engineering"
        / "orchestrator.py"
    ).read_text(encoding="utf-8")
    assert (
        "from kun.integration.ensemble_invoker_factory import" in orch_source
    ), (
        "kun/engineering/orchestrator.py does NOT import the ensemble factory — "
        "X.B.MF-2 wiring regressed"
    )
    assert "build_ensemble_invoker_from_settings(" in orch_source, (
        "Factory function imported but NOT called — wiring still broken"
    )
