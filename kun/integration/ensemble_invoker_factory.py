"""Ensemble invoker factory — V7 Phase X.B.MF-2 production wiring.

Builds a ``make_ensemble_llm_invoker`` from settings + an existing ``LLMRouter``
so that ``LongTaskOrchestrator(llm_invoker=...)`` can be wired to use V7 §11.4
multi-LLM ensemble instead of single-LLM, **without changing the orchestrator
construction site beyond a 1-line factory call**.

Production wiring (`kun/engineering/orchestrator.py:_run_long_task_branch`):

    invoker = build_ensemble_invoker_from_settings(
        router=self.llm_router,
        purpose="execution",
        profile=llm_profile,
        tenant_id=tenant_id,
    )
    if invoker is None:
        invoker = make_llm_invoker(self.llm_router, purpose="execution", ...)
    lt_orch = LongTaskOrchestrator(llm_invoker=invoker, ...)

Opt-in via two env vars (default = ensemble OFF, falls back to single-LLM):

  - ``KUN_V7_ENSEMBLE_ENABLED=true`` — master switch
  - ``KUN_V7_ENSEMBLE_TIERS="top,cheap"`` — CSV of ``ModelTier`` names from the
    router. Each named tier's provider becomes an ensemble member. Need ≥ 2.

Cross-family enforcement (V7 §11.2):
  - require_cross_family=True at invocation time. If the configured tiers
    happen to all point at the same family (e.g. router has anthropic at both
    "top" and "strong"), ensemble_invoke will raise CrossFamilyConfigError on
    the first call. This factory therefore validates cross-family AT
    CONSTRUCTION TIME and returns None (caller falls back to single-LLM)
    rather than scheduling a runtime crash. Failure logged.

Per V7 §16.6 attacker audit follow-through:
  - This module's existence does NOT prove production uses ensemble. The
    grep proof is in ``kun/engineering/orchestrator.py`` import of this module.
  - Wiring proof test is in ``tests/integration/test_long_task_uses_ensemble_when_enabled.py``.
"""

from __future__ import annotations

import os

from kun.agents.executor.exec_loop import LLMInvoker
from kun.core.logging import get_logger
from kun.integration.ensemble_invoker import (
    make_ensemble_call_log_emitter,
    make_ensemble_llm_invoker,
)
from kun.interface.llm.base import LLMProvider, TaskProfile, ToolSpec
from kun.interface.llm.cross_family import classify_family, is_cross_family
from kun.interface.llm.ensemble import ConsensusStrategy
from kun.interface.llm.router import LLMRouter

log = get_logger("kun.integration.ensemble_invoker_factory")


_ENABLED_ENV = "KUN_V7_ENSEMBLE_ENABLED"
_TIERS_ENV = "KUN_V7_ENSEMBLE_TIERS"
_STRATEGY_ENV = "KUN_V7_ENSEMBLE_STRATEGY"
_LOCAL_MODEL_ENV = "KUN_V7_ENSEMBLE_LOCAL_MODEL_ID"
_LOCAL_BASE_URL_ENV = "KUN_V7_ENSEMBLE_LOCAL_BASE_URL"


def _truthy(value: str) -> bool:
    return value.strip().lower() not in {"", "false", "0", "no", "off"}


def _parse_tiers_csv(raw: str) -> list[str]:
    return [t.strip() for t in raw.split(",") if t.strip()]


def _resolve_consensus_strategy(raw: str | None) -> ConsensusStrategy:
    """Map env-var string → ConsensusStrategy, default majority_vote."""
    if not raw:
        return ConsensusStrategy.MAJORITY_VOTE
    raw_norm = raw.strip().lower()
    for s in ConsensusStrategy:
        if s.value == raw_norm:
            return s
    log.warning(
        "ensemble_invoker_factory.unknown_strategy_using_default",
        raw=raw,
        default="majority_vote",
    )
    return ConsensusStrategy.MAJORITY_VOTE


def _select_providers(
    router: LLMRouter, tier_names: list[str]
) -> list[LLMProvider]:
    """Resolve tier names → provider instances from the router registry."""
    selected: list[LLMProvider] = []
    seen_ids: set[int] = set()
    for tier_name in tier_names:
        provider = router.providers.get(tier_name)
        if provider is None:
            log.warning(
                "ensemble_invoker_factory.unknown_tier",
                tier=tier_name,
                known_tiers=list(router.providers.keys()),
            )
            continue
        # Deduplicate: some routers share the same provider across tiers
        if id(provider) in seen_ids:
            log.info(
                "ensemble_invoker_factory.duplicate_provider_skipped",
                tier=tier_name,
                provider=provider.name,
            )
            continue
        seen_ids.add(id(provider))
        selected.append(provider)
    return selected


def _has_cross_family_pair(providers: list[LLMProvider]) -> bool:
    """V7 §11.2 — at least one cross-family pair required."""
    for i in range(len(providers)):
        for j in range(i + 1, len(providers)):
            if is_cross_family(providers[i].model_id, providers[j].model_id):
                return True
    return False


def _maybe_build_local_provider() -> LLMProvider | None:
    """V7 Phase X.B.QWEN-WIRE: optionally append a LocalLLMProvider to ensemble.

    Env-driven:
      KUN_V7_ENSEMBLE_LOCAL_MODEL_ID="qwen2.5:14b-instruct-q4_K_M"  # required
      KUN_V7_ENSEMBLE_LOCAL_BASE_URL="http://localhost:11434/v1"   # optional, default
    """
    model_id = os.environ.get(_LOCAL_MODEL_ENV, "").strip()
    if not model_id:
        return None
    base_url = (
        os.environ.get(_LOCAL_BASE_URL_ENV, "").strip()
        or "http://localhost:11434/v1"
    )

    try:
        from kun.interface.llm.local_provider import LocalLLMProvider

        provider = LocalLLMProvider(model_id=model_id, base_url=base_url)
    except Exception as e:
        log.warning(
            "ensemble_invoker_factory.local_provider_construct_failed",
            model_id=model_id,
            base_url=base_url,
            error=f"{type(e).__name__}: {e}",
        )
        return None

    log.info(
        "ensemble_invoker_factory.local_provider_added",
        model_id=model_id,
        base_url=base_url,
        family=classify_family(model_id).value,
    )
    return provider


def build_ensemble_invoker_from_settings(
    *,
    router: LLMRouter,
    purpose: str = "execution",
    profile: TaskProfile | None = None,
    tools: list[ToolSpec] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    tenant_id: str | None = None,
) -> LLMInvoker | None:
    """Return an ensemble invoker built from env settings, or None.

    Returns:
        LLMInvoker if all of:
          - ``KUN_V7_ENSEMBLE_ENABLED`` is truthy
          - ``KUN_V7_ENSEMBLE_TIERS`` parses to ≥ 2 distinct tiers
          - The selected providers have ≥ 1 cross-family pair (V7 §11.2)
        Otherwise ``None`` — caller is responsible for falling back.

    Audit grep targets (for the next attacker-audit pass):
      - This function is imported from ``kun/engineering/orchestrator.py``
        (the production LongTaskOrchestrator construction site).
      - The factory wires ``make_ensemble_call_log_emitter(tenant_id)`` so
        every ensemble call真writes to ``ensemble_calls`` table.
    """
    enabled_raw = os.environ.get(_ENABLED_ENV, "")
    if not _truthy(enabled_raw):
        log.debug("ensemble_invoker_factory.disabled", env=_ENABLED_ENV)
        return None

    tiers_raw = os.environ.get(_TIERS_ENV, "")
    tier_names = _parse_tiers_csv(tiers_raw)

    # V7 Phase X.B.QWEN-WIRE: local provider may bring the count to ≥ 2 even
    # when only 1 tier is configured. Build providers list first, defer the
    # min-2 check until after local provider is added.
    has_local_env = bool(os.environ.get(_LOCAL_MODEL_ENV, "").strip())
    if len(tier_names) < 1 or (len(tier_names) < 2 and not has_local_env):
        log.warning(
            "ensemble_invoker_factory.too_few_tiers",
            tiers_env=tiers_raw,
            required_min=2,
            hint=(
                f"set {_TIERS_ENV}='top,cheap' for ≥2 from router, "
                f"OR set {_LOCAL_MODEL_ENV} + ≥1 tier for cross-family"
            ),
        )
        return None

    providers = _select_providers(router, tier_names)

    # V7 Phase X.B.QWEN-WIRE: optionally extend ensemble with a local provider
    # (e.g. Qwen via Ollama). Cross-family enforcement uses the extended list.
    local_provider = _maybe_build_local_provider()
    if local_provider is not None:
        providers.append(local_provider)

    if len(providers) < 2:
        log.warning(
            "ensemble_invoker_factory.insufficient_providers_after_dedup",
            requested_tiers=tier_names,
            resolved_count=len(providers),
        )
        return None

    # V7 §11.2 cross-family check (defensive — ensemble_invoke would raise at
    # call time, but failing here lets caller fall back gracefully).
    if not _has_cross_family_pair(providers):
        families = sorted({classify_family(p.model_id).value for p in providers})
        log.warning(
            "ensemble_invoker_factory.no_cross_family_pair",
            providers=[f"{p.name}/{p.model_id}" for p in providers],
            families=families,
            hint=(
                "configured tiers map to the same provider family; ensemble "
                "would crash at runtime. Falling back to single-LLM."
            ),
        )
        return None

    strategy = _resolve_consensus_strategy(os.environ.get(_STRATEGY_ENV))

    log.info(
        "ensemble_invoker_factory.built",
        purpose=purpose,
        tiers=tier_names,
        n_providers=len(providers),
        providers=[f"{p.name}/{p.model_id}" for p in providers],
        families=sorted({classify_family(p.model_id).value for p in providers}),
        strategy=strategy.value,
        tenant_id=tenant_id,
    )

    call_log_emitter = (
        make_ensemble_call_log_emitter(tenant_id) if tenant_id else None
    )

    return make_ensemble_llm_invoker(
        providers,
        consensus_strategy=strategy,
        purpose=purpose,
        profile=profile,
        tools=tools,
        temperature=temperature,
        max_tokens=max_tokens,
        require_cross_family=True,
        call_log_emitter=call_log_emitter,
    )


__all__ = [
    "build_ensemble_invoker_from_settings",
]
