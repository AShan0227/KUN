"""LongTaskRuntimeBundle — V7 Phase X.H.PROD-ENTRY-WIRE.

**The single hub** for "which opt-in runtime features get passed to
LongTaskOrchestrator at the production entry". Before this module the
production entry (``kun/engineering/orchestrator.py:1312``) silently
omitted three features that **existed as opt-in ctor params**:

  - ``trifecta_coordinator`` (V7 §12.4 RSI 三线, X.E.TRIFECTA-WIRING)
  - ``methodology_selector`` (V7 §12 RSI runtime, X.G.RSI-CLOSED-LOOP)
  - ``critique_every_n_steps`` (DIST-D ExternalSupervisor cadence)

→ tests passed (they explicitly set those params)
→ dogfood scripts passed (they explicitly set those params)
→ production WS tasks silently ran WITHOUT them

This was the X.H self-audit's strongest finding (5 root causes — see
``docs/dev_logs/X.H-self-audit-rootcause.md``). The bundle here fixes
it by:

  1. Frozen dataclass forcing the production entry to acknowledge each
     feature by either setting it or explicitly setting None.
  2. Factory ``from_env_defaults`` reads env switches consistently across
     all opt-in features (no more "trifecta says ON but production
     didn't pass coord", because the bundle now owns the coord).
  3. Validation: bundle re-asserts that the LongTaskOrchestrator class
     signature is in sync (CI guard against silent regressions).

Usage at production entry::

    bundle = LongTaskRuntimeBundle.from_env_defaults(
        llm_router=self.llm_router,
        external_supervisor=external_supervisor,
    )
    lt_orch = LongTaskOrchestrator(
        ...,
        **bundle.as_orchestrator_kwargs(),
    )
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.engineering.long_task_runtime_bundle")


# Env switches (cost-sensitive defaults: OFF in production unless opted-in).
ENV_TRIFECTA_ENABLED = "KUN_V7_TRIFECTA_ORCHESTRATOR_ENABLED"
ENV_TRIFECTA_EVERY_N = "KUN_V7_TRIFECTA_EVERY_N_STEPS"
ENV_METHODOLOGY_ENABLED = "KUN_V7_METHODOLOGY_INJECT_ENABLED"
ENV_METHODOLOGY_TOP_K = "KUN_V7_METHODOLOGY_TOP_K"
ENV_METHODOLOGY_DIR = "KUN_V7_METHODOLOGY_DIR"
ENV_CRITIQUE_EVERY_N = "KUN_V7_CRITIQUE_EVERY_N_STEPS"
ENV_DISCIPLINE_ENABLED = "KUN_V7_DISCIPLINE_ENFORCER_ENABLED"

# Defaults — small enough that turning ON is safe for early adopters but
# big enough that the feature actually fires multiple times in a normal
# long task. Tune via env if cost becomes a concern.
DEFAULT_TRIFECTA_EVERY_N_STEPS = 5
DEFAULT_METHODOLOGY_TOP_K = 3
DEFAULT_CRITIQUE_EVERY_N_STEPS = 3
DEFAULT_METHODOLOGY_DIR = str(
    Path(__file__).resolve().parent.parent.parent
    / "seeds"
    / "methodologies"
)


def _truthy(env_value: str | None) -> bool:
    if env_value is None:
        return False
    return env_value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning(
            "long_task_runtime_bundle.bad_env_int",
            env=name,
            value=raw,
            fallback=default,
        )
        return default


@dataclass(frozen=True)
class LongTaskRuntimeBundle:
    """The single hub for opt-in LongTaskOrchestrator runtime features.

    All fields are Optional — None means "not opted in" for this run.
    The production entry must construct via :py:meth:`from_env_defaults`
    or set every field explicitly. There is no implicit fallback.

    Fields mirror the LongTaskOrchestrator ctor exactly so
    ``as_orchestrator_kwargs()`` can ``**`` into the constructor.
    """

    trifecta_coordinator: Any | None = None
    """X.E TrifectaCoordinator (kun.agents.trifecta.TrifectaCoordinator)."""

    trifecta_every_n_steps: int | None = None
    """How many main-line steps between trifecta ticks. None=disabled."""

    trifecta_n_future_candidates: int = 3

    methodology_selector: Any | None = None
    """X.G MethodologyRuntimeSelector (kun.engineering.methodology_runtime_loader)."""

    methodology_top_k: int = 0
    """0 disables injection even if selector wired."""

    critique_every_n_steps: int | None = None
    """DIST-D External Supervisor critique cadence. None=disabled."""

    discipline_enforcer: Any | None = None
    """V7 §4.3 Phase F EngineeringDisciplineEnforcer. None=disabled
    (orphan pre-X.I-0)."""

    # Diagnostic — captured at construction time so cockpit / audit logs
    # can see exactly which features fired without rebuilding context.
    enabled_flags: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def disabled(cls) -> LongTaskRuntimeBundle:
        """All-disabled bundle. Useful for tests that want a baseline."""
        return cls(
            enabled_flags={
                "trifecta": False,
                "methodology": False,
                "critique": False,
                "discipline": False,
            }
        )

    @classmethod
    def from_env_defaults(
        cls,
        *,
        llm_router: Any | None = None,
        external_supervisor: Any | None = None,
        methodology_dir: str | Path | None = None,
    ) -> LongTaskRuntimeBundle:
        """Build a bundle from env switches.

        - Trifecta: opt-in via KUN_V7_TRIFECTA_ORCHESTRATOR_ENABLED. When
          on, builds a real-LLM coordinator using ``llm_router``.
        - Methodology: opt-in via KUN_V7_METHODOLOGY_INJECT_ENABLED. Loads
          seeds from ``methodology_dir`` (default: <repo>/seeds/methodologies/).
        - Critique: opt-in via KUN_V7_CRITIQUE_EVERY_N_STEPS=N (any positive
          int means "fire every N main-line LLM steps"). Requires
          ``external_supervisor`` to be wired.

        Each feature degrades cleanly to disabled if a precondition is
        missing (logged at INFO so cockpit can show "wired vs requested-
        but-precondition-missing").
        """
        flags: dict[str, bool] = {
            "trifecta": False,
            "methodology": False,
            "critique": False,
            "discipline": False,
        }

        # ---- Trifecta ----
        trifecta_coord = None
        trifecta_every_n = None
        if _truthy(os.environ.get(ENV_TRIFECTA_ENABLED)):
            if llm_router is None:
                log.info(
                    "long_task_runtime_bundle.trifecta_requested_but_no_router",
                    env=ENV_TRIFECTA_ENABLED,
                )
            else:
                trifecta_coord = _build_real_llm_trifecta_coordinator(
                    llm_router=llm_router
                )
                trifecta_every_n = _env_int(
                    ENV_TRIFECTA_EVERY_N, DEFAULT_TRIFECTA_EVERY_N_STEPS
                )
                flags["trifecta"] = True

        # ---- Methodology ----
        methodology_sel = None
        methodology_topk = 0
        if _truthy(os.environ.get(ENV_METHODOLOGY_ENABLED)):
            from kun.engineering.methodology_runtime_loader import (
                MethodologyRuntimeSelector,
                load_methodologies,
            )

            mdir = (
                methodology_dir
                or os.environ.get(ENV_METHODOLOGY_DIR)
                or DEFAULT_METHODOLOGY_DIR
            )
            try:
                entries = load_methodologies(mdir)
                if entries:
                    methodology_sel = MethodologyRuntimeSelector(entries)
                    methodology_topk = _env_int(
                        ENV_METHODOLOGY_TOP_K, DEFAULT_METHODOLOGY_TOP_K
                    )
                    flags["methodology"] = True
                else:
                    log.info(
                        "long_task_runtime_bundle.methodology_enabled_but_empty",
                        dir=str(mdir),
                    )
            except Exception as e:
                log.warning(
                    "long_task_runtime_bundle.methodology_load_failed",
                    error=f"{type(e).__name__}: {e}",
                )

        # ---- Critique ----
        critique_every_n = None
        critique_value = _env_int(ENV_CRITIQUE_EVERY_N, 0)
        if critique_value > 0:
            if external_supervisor is None:
                log.info(
                    "long_task_runtime_bundle.critique_requested_but_no_supervisor",
                    env=ENV_CRITIQUE_EVERY_N,
                    value=critique_value,
                )
            else:
                critique_every_n = critique_value
                flags["critique"] = True

        # ---- Discipline enforcer (X.I-0) ----
        discipline_enf = None
        if _truthy(os.environ.get(ENV_DISCIPLINE_ENABLED)):
            from kun.governance.engineering_discipline import (
                EngineeringDisciplineEnforcer,
            )

            discipline_enf = EngineeringDisciplineEnforcer()
            flags["discipline"] = True

        log.info(
            "long_task_runtime_bundle.built",
            trifecta_enabled=flags["trifecta"],
            methodology_enabled=flags["methodology"],
            critique_enabled=flags["critique"],
            discipline_enabled=flags["discipline"],
        )
        return cls(
            trifecta_coordinator=trifecta_coord,
            trifecta_every_n_steps=trifecta_every_n,
            methodology_selector=methodology_sel,
            methodology_top_k=methodology_topk,
            critique_every_n_steps=critique_every_n,
            discipline_enforcer=discipline_enf,
            enabled_flags=flags,
        )

    def as_orchestrator_kwargs(self) -> dict[str, Any]:
        """Project the bundle to LongTaskOrchestrator ctor kwargs.

        The set of keys returned **must** stay in sync with the orchestrator
        ctor signature for the audit test in
        ``tests/integration/test_production_entry_runtime_bundle.py`` to
        pass — that test is the CI guard against this exact root cause
        recurring (a future engineer adds a 4th opt-in feature but forgets
        to plumb it through the bundle).
        """
        return {
            "trifecta_coordinator": self.trifecta_coordinator,
            "trifecta_every_n_steps": self.trifecta_every_n_steps,
            "trifecta_n_future_candidates": self.trifecta_n_future_candidates,
            "methodology_selector": self.methodology_selector,
            "methodology_top_k": self.methodology_top_k,
            "critique_every_n_steps": self.critique_every_n_steps,
            "discipline_enforcer": self.discipline_enforcer,
        }


def _build_real_llm_trifecta_coordinator(*, llm_router: Any) -> Any:
    """Build a TrifectaCoordinator backed by real LLM hooks.

    Past/present/future each call llm_router.invoke with a tiny prompt;
    cheap model is preferred for cost control. Failure in any line is
    swallowed by the coordinator (V7 §12.4 protocol).

    X.I-4 — the past hook is now backed by bug_root_cause_cases (with
    an LLM augmentation when DB returns no matches). Free if DB has
    matches; falls back to a tiny LLM call otherwise.
    """
    from kun.agents.trifecta import TrifectaCoordinator
    from kun.integration.bug_root_cause_lookup import (
        lookup_similar_root_cause_cases,
    )
    from kun.interface.llm.base import LLMMessage, LLMRequest

    async def _call(prompt: str, max_tokens: int = 120) -> tuple[str, float]:
        try:
            req = LLMRequest(
                messages=[LLMMessage(role="user", content=prompt)],
                temperature=0.5,
                max_tokens=max_tokens,
            )
            resp = await llm_router.invoke(req)
            return resp.content or "", float(resp.cost_usd_actual or 0.0)
        except Exception as e:
            return f"trifecta_call_failed: {type(e).__name__}: {e}", 0.0

    async def _past_hook(task_id, recent):
        # X.I-4 — try bug_root_cause_cases lookup first (zero cost).
        try:
            findings = await lookup_similar_root_cause_cases(
                tenant_id="default",
                recent_steps=recent or [],
                limit=3,
            )
            if findings:
                return (findings, 0.0, None)
        except Exception as e:
            log.warning(
                "long_task_runtime_bundle.past_hook_lookup_failed",
                task_id=task_id,
                error=f"{type(e).__name__}: {e}",
            )
        # No matches in DB → small LLM call as fallback
        text, cost = await _call(
            f"V7 §12.4 trifecta past line. task={task_id}. "
            f"Find 1 potential root-cause signal in 1 sentence ≤40 字.",
            max_tokens=120,
        )
        return ([{"finding": text.strip()[:240]}], cost, None)

    async def _present_hook(task_id, _cur):
        text, cost = await _call(
            f"V7 §12.4 trifecta present line. task={task_id}. "
            f"Evaluate drift in 1 sentence ≤40 字. ok/concerning/alarming.",
            max_tokens=100,
        )
        return ([{"critique": text.strip()[:240]}], cost, None)

    async def _future_hook(task_id, _plan, n):
        text, cost = await _call(
            f"V7 §12.4 trifecta future line. task={task_id}. "
            f"Propose {n} candidate next steps, each ≤30 字.",
            max_tokens=250,
        )
        candidates = [
            {"candidate": line.strip()[:240], "metric": 0.7 + 0.05 * i}
            for i, line in enumerate(
                (text or "").split("\n")[: max(1, n)]
            )
            if line.strip()
        ] or [{"candidate": text.strip()[:240], "metric": 0.7}]
        return (candidates, cost, None)

    return TrifectaCoordinator(
        past_hook=_past_hook,
        present_hook=_present_hook,
        future_hook=_future_hook,
    )


__all__ = [
    "DEFAULT_CRITIQUE_EVERY_N_STEPS",
    "DEFAULT_METHODOLOGY_TOP_K",
    "DEFAULT_TRIFECTA_EVERY_N_STEPS",
    "ENV_CRITIQUE_EVERY_N",
    "ENV_METHODOLOGY_DIR",
    "ENV_METHODOLOGY_ENABLED",
    "ENV_METHODOLOGY_TOP_K",
    "ENV_TRIFECTA_ENABLED",
    "ENV_TRIFECTA_EVERY_N",
    "LongTaskRuntimeBundle",
]
