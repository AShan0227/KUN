"""API runtime wiring tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from kun.api.runtime import (
    get_code_capability,
    get_mission_resume_worker,
    get_orchestrator,
    get_pending_task_resume_worker,
    install_runtime,
)
from kun.engineering.mission_worker import MissionOrchestratorRunner, MissionResumeWorker
from kun.engineering.pending_task_resume import PendingTaskResumeWorker
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
    worker = get_mission_resume_worker(app)
    assert isinstance(worker, MissionResumeWorker)
    assert isinstance(worker.runner, MissionOrchestratorRunner)
    pending_worker = get_pending_task_resume_worker(app)
    assert isinstance(pending_worker, PendingTaskResumeWorker)
    assert pending_worker.orchestrator is orchestrator
    assert get_code_capability(app).executor.workspace_root.exists()


def test_get_orchestrator_fails_before_lifespan_install() -> None:
    app = SimpleNamespace(state=State())

    with pytest.raises(RuntimeError, match="runtime has not been initialized"):
        get_orchestrator(app)


def test_install_runtime_uses_code_capability_workspace_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = SimpleNamespace(state=State())
    monkeypatch.setenv("KUN_CODE_CAPABILITY_WORKSPACE_ROOT", str(tmp_path))

    install_runtime(app, rule_engine=RuleEngine([]))

    capability = get_code_capability(app)
    assert capability.reader.root == tmp_path.resolve()
    assert capability.reviewer.workspace_root == tmp_path.resolve()
