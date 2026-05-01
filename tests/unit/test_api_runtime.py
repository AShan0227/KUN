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
    get_scheduler_background_job,
    install_runtime,
    schedule_cron_job_via_lane,
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


@pytest.mark.asyncio
async def test_cron_background_job_runs_through_multi_lane_scheduler() -> None:
    app = SimpleNamespace(state=State())
    install_runtime(app, rule_engine=RuleEngine([]))
    seen: list[str] = []

    async def callback() -> dict[str, str]:
        seen.append("ran")
        return {"ok": "yes"}

    wrapped = schedule_cron_job_via_lane(
        app,
        name="unit_idle_job",
        lane="nuo",
        callback=callback,
        tenant_id="tenant",
    )

    assert get_scheduler_background_job(app, "unit_idle_job") is callback
    await wrapped()

    assert seen == ["ran"]
    dashboard = app.state.multi_task_scheduler.dashboard()
    assert dashboard.total == 1
    assert dashboard.done == 1
    assert "nuo" in dashboard.lane_limits
