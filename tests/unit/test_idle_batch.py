"""idle-batch scheduler tests."""

from typing import Any

import pytest
from kun.engineering.idle_batch import (
    ABDecisionRollupStep,
    ConsistencyTestStep,
    HealthReportStep,
    IdleBatchStep,
    IncidentLessonDistillStep,
    KnowledgeConflictStep,
    MethodologyDistillStep,
    RouteRuleMiningStep,
    TaskReplayStep,
    list_steps,
    register_step,
    reset_idle_batch_data_source,
    run_once,
    set_idle_batch_data_source,
)


class _RecordingStep(IdleBatchStep):
    step_id = "test_recorder"

    def __init__(self) -> None:
        self.calls = 0

    async def run(self, tenant_id: str) -> dict[str, Any]:
        self.calls += 1
        return {"tenant": tenant_id, "calls": self.calls}


class _FakeIdleBatchDataSource:
    def recent_tasks(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            {"task_id": "a", "old_score": 0.6, "new_score": 0.9},
            {"task_id": "b", "old_score": 0.8, "new_score": 0.7},
        ]

    def consistency_samples(self, tenant_id: str) -> list[dict[str, Any]]:
        return [{"scores": [0.9, 0.88, 0.87]}, {"scores": [0.2, 0.9, 0.4]}]

    def narratives(self, tenant_id: str) -> list[dict[str, Any]]:
        return [{"rule": "高风险任务先跑验证"}, {"lesson": "先读 brief 再动手"}]

    def memory_claims(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            {"key": "api_port", "value": "8000", "confidence": 0.9},
            {"key": "api_port", "value": "8010", "confidence": 0.4},
        ]

    def experiments(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            {"experiment_id": "exp-good", "control_score": 0.6, "treatment_score": 0.8},
            {"experiment_id": "exp-bad", "guardrail_breached": True},
        ]

    def health_snapshot(self, tenant_id: str) -> dict[str, Any]:
        return {"total_tasks": 8, "events_outbox_lag": 1, "lifetime_cost_usd_equivalent": 2.5}

    def route_logs(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            {"task_type": "coding", "model": "strong", "success": True, "cost_usd": 0.2},
            {"task_type": "coding", "model": "strong", "success": True, "cost_usd": 0.3},
            {"task_type": "coding", "model": "cheap", "success": False, "cost_usd": 0.05},
        ]


@pytest.fixture(autouse=True)
def _reset_data_source():
    reset_idle_batch_data_source()
    yield
    reset_idle_batch_data_source()


@pytest.mark.unit
def test_default_steps_registered():
    steps = list_steps()
    assert "health_report" in steps
    assert "task_replay" in steps
    assert "route_rule_mining" in steps


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_once_enabled_filter():
    recorder = _RecordingStep()
    register_step(recorder)
    reports = await run_once("u-test", enabled={"test_recorder"})
    assert len(reports) == 1
    assert reports[0].step_id == "test_recorder"
    assert reports[0].status == "ok"
    assert recorder.calls == 1
    assert reports[0].summary == {"tenant": "u-test", "calls": 1}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_task_replay_step_computes_win_rate() -> None:
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await TaskReplayStep().run("t-1")

    assert summary["replayed"] == 2
    assert summary["treatment_wins"] == 1
    assert summary["control_wins"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_consistency_step_flags_unstable_samples() -> None:
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await ConsistencyTestStep().run("t-1")

    assert summary["samples"] == 2
    assert summary["unstable"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_methodology_distill_step_extracts_rules() -> None:
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await MethodologyDistillStep().run("t-1")

    assert summary["new_rules"] == 2
    assert "高风险任务先跑验证" in summary["rules"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_knowledge_conflict_step_resolves_by_confidence() -> None:
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await KnowledgeConflictStep().run("t-1")

    assert summary["resolved"] == 1
    assert summary["resolutions"][0]["winner"] == "8000"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_ab_decision_step_rolls_up_promote_and_rollback() -> None:
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await ABDecisionRollupStep().run("t-1")

    assert summary["promoted"] == 1
    assert summary["rolled_back"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_health_report_step_uses_data_source_snapshot() -> None:
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await HealthReportStep().run("t-1")

    assert summary["total_tasks"] == 8
    assert summary["events_outbox_lag"] == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_rule_mining_step_surfaces_best_model_pattern() -> None:
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await RouteRuleMiningStep().run("t-1")

    assert summary["new_patterns"] == 1
    assert summary["patterns"][0]["recommended_model"] == "strong"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_incident_lesson_step_distills_repeat_patterns() -> None:
    from kun.security.incident_response import IncidentEvent, IncidentResponseEngine

    engine = IncidentResponseEngine()
    await engine.handle(
        IncidentEvent(
            incident_id="inc-1",
            severity="L2",
            category="security",
            title="cross tenant",
            affected_tenant_id="t-1",
        )
    )
    await engine.handle(
        IncidentEvent(
            incident_id="inc-2",
            severity="L2",
            category="security",
            title="cross tenant again",
            affected_tenant_id="t-1",
        )
    )

    summary = await IncidentLessonDistillStep(incident_provider=lambda: engine).run("t-1")

    assert summary["incidents"] == 2
    assert any(lesson["lesson_kind"] == "repeat_pattern" for lesson in summary["lessons"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_full_seven_step_idle_batch_run() -> None:
    set_idle_batch_data_source(_FakeIdleBatchDataSource())
    enabled = {
        "task_replay",
        "consistency_test",
        "methodology_distill",
        "knowledge_conflict",
        "ab_decision_roll_up",
        "health_report",
        "route_rule_mining",
    }

    reports = await run_once("t-1", enabled=enabled)

    assert len(reports) == 7
    assert {report.status for report in reports} == {"ok"}
    assert {report.step_id for report in reports} == enabled
