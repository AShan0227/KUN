"""Wire 37: install_runtime 装上 Wire 35/36 (hermes rethink + verification)."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from kun.api.runtime import install_runtime
from kun.engineering.execution_protocol import (
    StructuredStepGenerator,
    ThoughtActionConsistency,
)
from kun.engineering.verification_runner import VerificationRunner
from kun.watchtower.engine import RuleEngine
from kun.watchtower.rules import GuardRule, RuleTrigger
from starlette.datastructures import State


def _empty_engine() -> RuleEngine:
    return RuleEngine(
        [GuardRule(id="x", kind="guard", trigger=RuleTrigger(event_type="*", when="True"))]
    )


def _fresh_app():
    return SimpleNamespace(state=State())


# ---- Wire 35: hermes consistency_checker + max_rethinks ----


def test_install_runtime_hermes_default_has_consistency_checker() -> None:
    """默认 KUN_HERMES_ENABLED=1 → generator 装了 consistency_checker."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_HERMES_ENABLED", None)
        os.environ.pop("KUN_HERMES_CONSISTENCY_THRESHOLD", None)
        os.environ.pop("KUN_HERMES_MAX_RETHINKS", None)

        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())

    gen = app.state.structured_step_generator
    assert isinstance(gen, StructuredStepGenerator)
    assert gen._consistency is not None
    assert isinstance(gen._consistency, ThoughtActionConsistency)
    assert gen._consistency.consistency_threshold == 0.5  # default
    assert gen._max_rethinks == 2  # default


def test_install_runtime_hermes_disabled() -> None:
    """KUN_HERMES_ENABLED=0 → generator None."""
    with patch.dict(os.environ, {"KUN_HERMES_ENABLED": "0"}):
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())

    assert app.state.structured_step_generator is None


def test_install_runtime_hermes_custom_threshold_and_rethinks() -> None:
    """env 控制 threshold + max_rethinks."""
    with patch.dict(
        os.environ,
        {
            "KUN_HERMES_ENABLED": "1",
            "KUN_HERMES_CONSISTENCY_THRESHOLD": "0.7",
            "KUN_HERMES_MAX_RETHINKS": "4",
        },
    ):
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())

    gen = app.state.structured_step_generator
    assert gen._consistency.consistency_threshold == 0.7
    assert gen._max_rethinks == 4


# ---- Wire 36: VerificationRunner ----


def test_install_runtime_verification_default_enabled() -> None:
    """默认 KUN_VERIFICATION_ENABLED=1 → VerificationRunner 装上."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_VERIFICATION_ENABLED", None)
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())

    assert isinstance(app.state.verification_runner, VerificationRunner)
    # orchestrator 拿到 runner
    assert app.state.orchestrator.verification_runner is app.state.verification_runner


def test_install_runtime_verification_disabled() -> None:
    """KUN_VERIFICATION_ENABLED=0 → runner None."""
    with patch.dict(os.environ, {"KUN_VERIFICATION_ENABLED": "0"}):
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())

    assert app.state.verification_runner is None
    assert app.state.orchestrator.verification_runner is None
