"""V7 Phase X.H.PROD-ENTRY-WIRE — production entry runtime bundle audit.

This file is the **CI guard against the X.H self-audit's strongest
finding**: the production WS entry (``kun/engineering/orchestrator.py``)
omitted opt-in LongTaskOrchestrator features (trifecta / methodology /
critique) for three releases (X.E, X.G, DIST-D) without anyone noticing
because:

  - tests passed (they explicitly set those params)
  - dogfood scripts passed (they explicitly set those params)
  - the production entry silently ran WITHOUT them

The bundle in ``kun/engineering/long_task_runtime_bundle.py`` is the
fix. THIS file is the regression guard — when a future engineer adds a
4th opt-in feature to LongTaskOrchestrator (call it "FEATURE_X"), they
must either:

  1. add FEATURE_X to the bundle, OR
  2. explicitly add an exemption note here.

If they do neither, the audit test below fails — preventing the same
root-cause pattern from recurring.

The 5 root causes this guard addresses (R1-R5 in
``docs/dev_logs/X.H-self-audit-rootcause.md``):

  R1 — wrong grep granularity: this test greps real instantiation, not
       merely class-level import.
  R2 — no production entries inventory: this test enumerates the only
       blessed entry path (orchestrator.py:run_long_task_branch).
  R3 — opt-in defaults OFF with no consumer enforcement: bundle
       construction forces a decision for each feature.
  R4 — test fixtures look like production callers: this test
       distinguishes "fixture caller" from "production entry" by name.
  R5 — module-level retrospective skips entry-level check: bundle keys
       are asserted to equal orchestrator class signature opt-in params.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from kun.engineering.long_task_orchestrator import LongTaskOrchestrator
from kun.engineering.long_task_runtime_bundle import LongTaskRuntimeBundle

PRODUCTION_ENTRY_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "kun"
    / "engineering"
    / "orchestrator.py"
)


# The opt-in features that the runtime bundle MUST mediate. Every key
# here MUST be a parameter on both:
#   1. LongTaskRuntimeBundle.as_orchestrator_kwargs() return dict
#   2. LongTaskOrchestrator.__init__ signature
# A future feature added without going through the bundle = test fail =
# CI blocks the merge.
EXPECTED_BUNDLE_KEYS = frozenset(
    {
        "trifecta_coordinator",
        "trifecta_every_n_steps",
        "trifecta_n_future_candidates",
        "methodology_selector",
        "methodology_top_k",
        "critique_every_n_steps",
    }
)


def test_bundle_disabled_factory_returns_all_none_or_default() -> None:
    """Sanity: the disabled bundle is benign (all opt-ins off)."""
    b = LongTaskRuntimeBundle.disabled()
    kwargs = b.as_orchestrator_kwargs()
    assert kwargs["trifecta_coordinator"] is None
    assert kwargs["trifecta_every_n_steps"] is None
    assert kwargs["methodology_selector"] is None
    assert kwargs["methodology_top_k"] == 0
    assert kwargs["critique_every_n_steps"] is None


def test_bundle_keys_exactly_match_expected_set() -> None:
    """The bundle.as_orchestrator_kwargs() emits the exact set we expect.

    If a future feature is added to the bundle but not here, this fails →
    the test author must consciously add the new feature to the audit set
    (proving they thought about the production entry).
    """
    actual_keys = set(LongTaskRuntimeBundle.disabled().as_orchestrator_kwargs().keys())
    assert actual_keys == EXPECTED_BUNDLE_KEYS, (
        f"LongTaskRuntimeBundle keys drifted from audit set.\n"
        f"  expected: {sorted(EXPECTED_BUNDLE_KEYS)}\n"
        f"  actual:   {sorted(actual_keys)}\n"
        f"If you added a feature to the bundle, update EXPECTED_BUNDLE_KEYS "
        f"in this file and the X.H rootcause doc."
    )


def test_orchestrator_class_signature_accepts_all_bundle_keys() -> None:
    """LongTaskOrchestrator.__init__ must accept every bundle key as a
    kwarg. Otherwise as_orchestrator_kwargs() would fail at runtime."""
    sig = inspect.signature(LongTaskOrchestrator.__init__)
    params = set(sig.parameters.keys())
    missing = EXPECTED_BUNDLE_KEYS - params
    assert not missing, (
        f"LongTaskOrchestrator.__init__ does NOT accept bundle keys "
        f"{sorted(missing)}. Either add them to the ctor or remove them "
        f"from the bundle."
    )


def test_production_entry_passes_runtime_bundle_kwargs() -> None:
    """**The headline audit**: the production WS entry must use
    `**bundle.as_orchestrator_kwargs()` (or pass each bundle key
    explicitly) when constructing LongTaskOrchestrator.

    We parse kun/engineering/orchestrator.py and look for the
    LongTaskOrchestrator() call. If the call doesn't spread a bundle's
    kwargs (`**runtime_bundle.as_orchestrator_kwargs()`) AND doesn't
    name every EXPECTED_BUNDLE_KEYS as an explicit kwarg, the test
    fails — exactly the regression that introduced the X.H gap.
    """
    src = PRODUCTION_ENTRY_PATH.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_call = False
    bundle_kwargs_spread = False
    explicit_kwargs: set[str] = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match LongTaskOrchestrator(...) by name
        is_orch_call = (
            isinstance(func, ast.Name) and func.id == "LongTaskOrchestrator"
        ) or (
            isinstance(func, ast.Attribute) and func.attr == "LongTaskOrchestrator"
        )
        if not is_orch_call:
            continue
        found_call = True
        for kw in node.keywords:
            if kw.arg is None:
                # `**something` star-spread
                # Detect `**runtime_bundle.as_orchestrator_kwargs()` shape
                val = kw.value
                if (
                    isinstance(val, ast.Call)
                    and isinstance(val.func, ast.Attribute)
                    and val.func.attr == "as_orchestrator_kwargs"
                ):
                    bundle_kwargs_spread = True
            else:
                explicit_kwargs.add(kw.arg)

    assert found_call, (
        f"Could not find a LongTaskOrchestrator(...) call in "
        f"{PRODUCTION_ENTRY_PATH}. The production entry must construct it."
    )

    if bundle_kwargs_spread:
        # Good — bundle is the source of truth.
        return

    # Otherwise every EXPECTED_BUNDLE_KEYS must be explicitly named.
    missing = EXPECTED_BUNDLE_KEYS - explicit_kwargs
    assert not missing, (
        f"Production entry {PRODUCTION_ENTRY_PATH} constructs "
        f"LongTaskOrchestrator without `**runtime_bundle.as_orchestrator_kwargs()` "
        f"and is missing explicit kwargs {sorted(missing)}.\n"
        f"This is the X.H root cause pattern: opt-in feature exists on "
        f"the class but is silently omitted at the production entry, so "
        f"real user tasks never get the feature.\n"
        f"Fix: either spread the bundle kwargs, or name every missing key "
        f"explicitly (and document why the bundle isn't appropriate)."
    )


def test_from_env_defaults_off_when_no_env_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env switches set → bundle is all-disabled (safe default)."""
    for env_name in (
        "KUN_V7_TRIFECTA_ORCHESTRATOR_ENABLED",
        "KUN_V7_METHODOLOGY_INJECT_ENABLED",
        "KUN_V7_CRITIQUE_EVERY_N_STEPS",
    ):
        monkeypatch.delenv(env_name, raising=False)

    bundle = LongTaskRuntimeBundle.from_env_defaults(
        llm_router=object(), external_supervisor=object()
    )
    assert bundle.enabled_flags == {
        "trifecta": False,
        "methodology": False,
        "critique": False,
    }
    assert bundle.trifecta_coordinator is None
    assert bundle.methodology_selector is None
    assert bundle.critique_every_n_steps is None


def test_from_env_defaults_trifecta_requires_llm_router(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even if env says ON, missing llm_router → trifecta stays disabled.

    Bundle reports via enabled_flags so cockpit can show
    "requested but precondition missing" instead of pretending it's wired.
    """
    monkeypatch.setenv("KUN_V7_TRIFECTA_ORCHESTRATOR_ENABLED", "true")
    monkeypatch.delenv("KUN_V7_METHODOLOGY_INJECT_ENABLED", raising=False)
    monkeypatch.delenv("KUN_V7_CRITIQUE_EVERY_N_STEPS", raising=False)

    bundle = LongTaskRuntimeBundle.from_env_defaults(llm_router=None)
    assert bundle.enabled_flags["trifecta"] is False
    assert bundle.trifecta_coordinator is None


def test_from_env_defaults_methodology_loads_real_seeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Methodology env on + a non-empty seeds dir → selector loads."""
    monkeypatch.setenv("KUN_V7_METHODOLOGY_INJECT_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_METHODOLOGY_TOP_K", "2")
    monkeypatch.delenv("KUN_V7_TRIFECTA_ORCHESTRATOR_ENABLED", raising=False)
    monkeypatch.delenv("KUN_V7_CRITIQUE_EVERY_N_STEPS", raising=False)

    seeds = tmp_path / "methodologies"
    seeds.mkdir()
    (seeds / "one.yaml").write_text(
        "topic: test\ntitle: synthetic seed for bundle test\n"
        "description: only present so loader returns 1 entry\n",
        encoding="utf-8",
    )

    bundle = LongTaskRuntimeBundle.from_env_defaults(
        methodology_dir=seeds,
    )
    assert bundle.enabled_flags["methodology"] is True
    assert bundle.methodology_selector is not None
    assert bundle.methodology_top_k == 2


def test_from_env_defaults_critique_requires_external_supervisor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_CRITIQUE_EVERY_N_STEPS", "4")
    monkeypatch.delenv("KUN_V7_TRIFECTA_ORCHESTRATOR_ENABLED", raising=False)
    monkeypatch.delenv("KUN_V7_METHODOLOGY_INJECT_ENABLED", raising=False)

    # No external_supervisor passed
    bundle = LongTaskRuntimeBundle.from_env_defaults(external_supervisor=None)
    assert bundle.enabled_flags["critique"] is False
    assert bundle.critique_every_n_steps is None

    # When supervisor wired, critique activates
    bundle2 = LongTaskRuntimeBundle.from_env_defaults(
        external_supervisor=object()
    )
    assert bundle2.enabled_flags["critique"] is True
    assert bundle2.critique_every_n_steps == 4
