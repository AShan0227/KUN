"""idle-batch scheduler tests."""

import shlex
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kun.engineering.idle_batch import (
    ABDecisionRollupStep,
    CompilerSyncSourcesStep,
    ConsistencyTestStep,
    ExternalEmergentScanStep,
    HealthReportStep,
    IdleBatchStep,
    IncidentLessonDistillStep,
    KnowledgeConflictStep,
    MethodologyDistillStep,
    QiIdleReplayStep,
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

    def qi_problem_signals(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            {
                "signal_id": "qps_runtime_1",
                "tenant_id": tenant_id,
                "category": "runtime",
                "severity": "warning",
                "summary": "mission task stalled during resume",
                "source": "nuo.system_health",
                "task_type": "mission.product_ops",
                "evidence": {"runtime_status": "running"},
            }
        ]

    def completed_task_history(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            {
                "history_id": "hist-cost-1",
                "task_type": "marketing.ad",
                "summary": "ad copy task cost exceeded estimate but passed",
                "outcome": "completed",
                "risk": "medium",
                "verification_status": "passed",
                "cost_usd": 0.42,
            }
        ]

    def external_scan_items(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": tenant_id,
                "task_type": "coding",
                "source_kind": "internal_history",
                "url": "kun://history/coding-review",
                "snippet": "先写失败测试，再做最小修复，最后跑 targeted + affected tests。",
                "estimated_outcome_delta": 0.22,
                "estimated_cost_delta": -0.03,
            }
        ]


@pytest.fixture(autouse=True)
def _reset_data_source():
    from kun.context.storage import reset_store
    from kun.core.emergent_solution import reset_library

    reset_idle_batch_data_source()
    reset_store()
    reset_library()
    yield
    reset_idle_batch_data_source()
    reset_store()
    reset_library()


@pytest.mark.unit
def test_default_steps_registered():
    steps = list_steps()
    assert "health_report" in steps
    assert "task_replay" in steps
    assert "route_rule_mining" in steps
    assert "qi_idle_replay" in steps
    assert "compiler_sync_sources" in steps
    assert "external_emergent_scan" in steps


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
    from kun.context.storage import get_store

    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await MethodologyDistillStep().run("t-1")

    assert summary["new_rules"] == 2
    assert "高风险任务先跑验证" in summary["rules"]
    assert len(summary["asset_ids"]) == 2
    assets = await get_store().list(tenant_id="t-1", asset_kind="methodology")
    assert len(assets) == 2
    assert {asset.l2_summary for asset in assets} >= {"高风险任务先跑验证", "先读 brief 再动手"}


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
async def test_health_report_step_collects_nuo_report_and_emits_event(monkeypatch) -> None:
    from kun.core.state_ledger import get_state_ledger, reset_state_ledger
    from kun.engineering.nuo_system_health import SystemHealthFinding, SystemHealthReport

    reset_state_ledger()
    events = []

    async def fake_collect_system_health_report(*, tenant_id: str) -> SystemHealthReport:
        return SystemHealthReport(
            tenant_id=tenant_id,
            total_tasks=3,
            outbox_lag=2,
            pending_approvals=1,
            findings=[
                SystemHealthFinding(
                    finding_id="f-1",
                    severity="warn",
                    subsystem="events",
                    title="outbox lag",
                    detail="lag",
                    suggested_action="restart worker",
                )
            ],
        )

    @asynccontextmanager
    async def fake_session_scope(**_kwargs: object) -> AsyncIterator[object]:
        yield object()

    async def fake_emit(_session: object, event: object) -> None:
        events.append(event)

    persisted_signals = []

    async def fake_persist_problem_signals(signals: list[object]) -> int:
        persisted_signals.extend(signals)
        return len(signals)

    monkeypatch.setattr(
        "kun.engineering.nuo_system_health.collect_system_health_report",
        fake_collect_system_health_report,
    )
    monkeypatch.setattr("kun.core.db.session_scope", fake_session_scope)
    monkeypatch.setattr("kun.core.events.emit", fake_emit)
    monkeypatch.setattr(
        "kun.qi.problem_queue.persist_problem_signals", fake_persist_problem_signals
    )

    summary = await HealthReportStep().run("t-1")

    assert summary["total_tasks"] == 3
    assert summary["events_outbox_lag"] == 2
    assert summary["worst_severity"] == "warn"
    assert summary["qi_problem_signals"] == 1
    assert summary["persisted_qi_problem_signals"] == 1
    assert len(persisted_signals) == 1
    assert len(events) == 1
    assert getattr(events[0], "event_type") == "nuo.health_report.generated"
    assert getattr(events[0], "payload")["top_findings"][0]["finding_id"] == "f-1"
    ledger_entry = get_state_ledger().snapshot("system:nuo:t-1")
    assert ledger_entry is not None
    assert ledger_entry.status == "paused"
    assert ledger_entry.current_risk == "medium"
    assert "f-1" in ledger_entry.alert_flags
    reset_state_ledger()


@pytest.mark.unit
def test_nuo_health_findings_include_secret_audit_items() -> None:
    from kun.engineering.nuo_system_health import _findings
    from kun.ops.secret_audit import SecretAuditItem

    findings = _findings(
        outbox_lag=0,
        pending_approvals=0,
        stale_runtime_count=0,
        active_resource_conflicts=0,
        delivery_issues=[],
        secret_audit_items=[
            SecretAuditItem(
                item_id="auth.no_valid_secret",
                area="auth",
                severity="blocker",
                title="没有可用认证密钥",
                detail="缺少 KUN_AUTH_SECRET",
                suggested_action="配置随机密钥",
            )
        ],
        world_handlers=[],
    )

    assert findings[0].finding_id == "secret:auth.no_valid_secret"
    assert findings[0].severity == "error"
    assert findings[0].subsystem == "secret_audit.auth"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_route_rule_mining_step_surfaces_best_model_pattern() -> None:
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await RouteRuleMiningStep().run("t-1")

    assert summary["new_patterns"] == 1
    assert summary["patterns"][0]["recommended_model"] == "strong"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_qi_idle_replay_step_generates_review_only_candidates(monkeypatch) -> None:
    from kun.context.storage import get_store
    from kun.qi.problem_queue import get_qi_problem_queue, reset_qi_problem_queue

    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    reset_qi_problem_queue()
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await QiIdleReplayStep().run("t-1")

    assert summary["signals"] == 1
    assert summary["completed_task_histories"] == 1
    assert summary["candidates"] == 2
    assert summary["production_action"] is False
    assert summary["persisted_review_signals"] == 2
    assert summary["persisted_strategy_pack_draft_assets"] == 2
    assert len(summary["strategy_pack_draft_asset_ids"]) == 2
    assert summary["evaluation_pool"]["evaluated"] == 2
    assert summary["evaluation_pool"]["promotion_allowed"] is False
    assert all(item["promotion_allowed"] is False for item in summary["evaluation_pool"]["records"])
    assert len(summary["strategy_pack_drafts"]) == 2
    assert all(item["production_action"] is False for item in summary["strategy_pack_drafts"])
    assert all(item["requires_human_review"] is True for item in summary["strategy_pack_drafts"])
    assert {item["status"] for item in summary["strategy_pack_drafts"]} <= {
        "draft",
        "needs_strong_review",
    }
    assert all(item["production_action"] is False for item in summary["top_candidates"])
    queued = get_qi_problem_queue().list("t-1", limit=10)
    assert len(queued) == 2
    assert all(signal.source == "qi.idle_replay.candidate" for signal in queued)
    assert all("strategy_pack_draft" in signal.evidence for signal in queued)
    assert all(
        signal.evidence["strategy_pack_draft"]["production_action"] is False for signal in queued
    )
    draft_assets = await get_store().list(tenant_id="t-1", asset_kind="methodology")
    assert len(draft_assets) == 2
    assert all(
        asset.l1_metadata["source"] == "qi.idle_replay.strategy_pack_draft"
        for asset in draft_assets
    )
    assert all(asset.l1_metadata["production_action"] is False for asset in draft_assets)
    assert all(asset.l1_metadata["requires_human_review"] is True for asset in draft_assets)
    assert all(
        asset.l1_metadata["decision_ticket"]["decision_point"] == "qi_experiment"
        for asset in draft_assets
    )
    assert all(
        asset.l1_metadata["decision_ticket"]["status"] == "needs_review" for asset in draft_assets
    )
    assert all("review_only" in asset.tags for asset in draft_assets)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_qi_idle_replay_step_uses_configured_local_model_command(monkeypatch) -> None:
    from kun.qi.problem_queue import reset_qi_problem_queue

    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    reset_qi_problem_queue()
    set_idle_batch_data_source(_FakeIdleBatchDataSource())
    model_script = (
        "import json, sys; "
        "json.load(sys.stdin); "
        "print(json.dumps({'score': 0.73, 'notes': ['idle_local_vote']}))"
    )
    monkeypatch.setenv(
        "KUN_QI_LOCAL_REPLAY_EVALUATOR_CMD",
        shlex.join([sys.executable, "-c", model_script]),
    )

    summary = await QiIdleReplayStep().run("t-1")

    assert summary["evaluation_engine"] == "local_model"
    assert summary["evaluation_pool"]["evaluated"] == 2
    assert all(
        record["evaluator_kind"] == "local_model"
        for record in summary["evaluation_pool"]["records"]
    )
    assert all(
        record["promotion_allowed"] is False for record in summary["evaluation_pool"]["records"]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compiler_sync_sources_step_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("KUN_COMPILER_SYNC_SOURCE_FILES", raising=False)

    summary = await CompilerSyncSourcesStep().run("t-1")

    assert summary["skipped"] is True
    assert summary["synced"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compiler_sync_sources_step_runs_configured_sources(
    monkeypatch,
    tmp_path,
) -> None:
    from kun.context.storage import get_store

    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        """
        {
          "tenant_id": "ignored",
          "items": [
            {"id": "inline-note", "type": "text", "value": "KUN compiler idle sync"}
          ]
        }
        """,
        encoding="utf-8",
    )
    source = tmp_path / "source.json"
    source.write_text(
        f"""
        {{
          "source_id": "idle-docs",
          "tenant_id": "ignored",
          "manifest_path": "{manifest.name}",
          "enabled": true
        }}
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("KUN_COMPILER_SYNC_SOURCE_FILES", str(source))
    monkeypatch.setenv("KUN_COMPILER_SYNC_CONFIG_ROOT", str(tmp_path))

    summary = await CompilerSyncSourcesStep().run("t-1")

    assert summary["skipped"] is False
    assert summary["synced"] == 1
    assert summary["errors"] == 0
    assets = await get_store().list(tenant_id="t-1", asset_kind="knowledge")
    assert len(assets) == 1
    assert assets[0].l1_metadata["material_metadata"]["compiler_sync_source_id"] == "idle-docs"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_emergent_scan_step_is_opt_in() -> None:
    summary = await ExternalEmergentScanStep().run("t-1")

    assert summary["skipped"] is True
    assert summary["candidates_added"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_emergent_scan_step_adds_reviewed_candidates() -> None:
    from kun.core.emergent_solution import get_library

    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await ExternalEmergentScanStep().run("t-1")

    assert summary["skipped"] is False
    assert summary["input_rows"] == 1
    assert summary["sources_queried"] == 1
    assert summary["candidates_added"] == 1
    candidates = get_library().list_for_task_type("coding")
    assert len(candidates) == 1
    assert candidates[0].discovered_by == "external_scan"
    assert candidates[0].source.kind == "internal_history"
    assert candidates[0].description.startswith("先写失败测试")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_emergent_scan_step_reads_opt_in_source_file(
    monkeypatch,
    tmp_path,
) -> None:
    from kun.core.emergent_solution import get_library

    source = tmp_path / "external_scan.json"
    source.write_text(
        """
        {
          "tenant_id": "t-file",
          "items": [
            {
              "task_type": "marketing.ad",
              "source_kind": "competitor_changelog",
              "url": "https://example.com/changelog",
              "snippet": "短视频广告先做强 hook，再压缩到 3 个可测版本。",
              "estimated_outcome_delta": 0.18,
              "estimated_cost_delta": 0.01
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("KUN_EXTERNAL_SCAN_SOURCE_FILES", str(source))
    monkeypatch.setenv("KUN_EXTERNAL_SCAN_CONFIG_ROOT", str(tmp_path))

    summary = await ExternalEmergentScanStep().run("t-file")

    assert summary["skipped"] is False
    assert summary["candidates_added"] == 1
    candidates = get_library().list_for_task_type("marketing.ad")
    assert len(candidates) == 1
    assert candidates[0].source.kind == "competitor_changelog"


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
