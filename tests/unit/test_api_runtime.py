"""API runtime wiring tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from kun.api.runtime import get_mission_resume_worker, get_orchestrator, install_runtime
from kun.engineering.mission_worker import MissionResumeWorker
from kun.watchtower.engine import RuleEngine
from kun.watchtower.rules import GuardRule, RuleTrigger
from starlette.datastructures import State


def test_install_runtime_reuses_loaded_rule_engine() -> None:
    app = SimpleNamespace(state=State())
    rule = GuardRule(
        id="always_fire",
        kind="guard",
        trigger=RuleTrigger(event_type="*", when="True"),
    )
    rule_engine = RuleEngine([rule])

    orchestrator = install_runtime(app, rule_engine=rule_engine)

    assert app.state.rule_engine is rule_engine
    assert app.state.orchestrator is orchestrator
    assert get_orchestrator(app) is orchestrator
    assert orchestrator.rule_engine is rule_engine
    assert orchestrator.rule_engine.rules == [rule]
    assert isinstance(get_mission_resume_worker(app), MissionResumeWorker)


def test_get_orchestrator_fails_before_lifespan_install() -> None:
    app = SimpleNamespace(state=State())

    with pytest.raises(RuntimeError, match="runtime has not been initialized"):
        get_orchestrator(app)
