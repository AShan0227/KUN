"""idle-batch scheduler tests."""

import shlex
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from kun.engineering import idle_batch
from kun.engineering.idle_batch import (
    ABDecisionRollupStep,
    CompilerIntakeReviewStep,
    CompilerSyncSourcesStep,
    ConsistencyTestStep,
    ContextGovernanceRuleDistillStep,
    CoordinationRemediationStep,
    ExternalEmergentScanStep,
    ExternalSkillCandidateReviewStep,
    ExternalSkillScoutPlanStep,
    HealthReportStep,
    IdleBatchDbDataSource,
    IdleBatchStep,
    IncidentLessonDistillStep,
    KnowledgeConflictStep,
    MethodologyDistillStep,
    QiIdleReplayStep,
    QiStrategyPackReviewStep,
    QiStrategyPackRolloutPlanStep,
    RouteRuleMiningStep,
    TaskReplayStep,
    _task_history_from_db_rows,
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

    def external_skill_candidates(self, tenant_id: str) -> list[dict[str, Any]]:
        return [
            {
                "tenant_id": tenant_id,
                "source_kind": "github_repo",
                "repo": "mattpocock/skills",
                "url": "https://github.com/mattpocock/skills",
                "name": "TypeScript code review skill",
                "description": "Review TypeScript changes for type-safety patterns.",
                "license": {"spdx_id": "MIT"},
                "files": [
                    {
                        "path": "skills/typescript-code-review/SKILL.md",
                        "content": "Review TypeScript code. Do not execute commands.",
                    }
                ],
                "topics": ["typescript", "code-review"],
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
    assert "qi_strategy_pack_review" in steps
    assert "qi_strategy_pack_rollout_plan" in steps
    assert "context_governance_rule_distill" in steps
    assert "coordination_remediation" in steps
    assert "compiler_sync_sources" in steps
    assert "compiler_intake_review" in steps
    assert "external_skill_scout_plan" in steps
    assert "external_emergent_scan" in steps
    assert "external_skill_candidate_review" in steps


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
async def test_coordination_remediation_step_defaults_to_dry_run(monkeypatch) -> None:
    from datetime import timedelta

    from kun.engineering.system_coordination import coordination_issues_from_rows

    async def fake_collect_coordination_issues(**kwargs: Any) -> list[Any]:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return coordination_issues_from_rows(
            pending_rows=[
                SimpleNamespace(
                    action_id="act-1",
                    task_ref="task-1",
                    action_type="email.draft",
                    status="approved",
                    updated_at=now - timedelta(minutes=10),
                )
            ],
            runtime_rows=[],
            control_rows=[],
            now=now,
            stale_after=timedelta(minutes=5),
        )

    async def fail_execute(**kwargs: Any) -> None:
        raise AssertionError("dry-run must not execute approved actions")

    monkeypatch.setattr(
        "kun.engineering.coordination_remediation.collect_coordination_issues",
        fake_collect_coordination_issues,
    )
    monkeypatch.setattr(
        "kun.engineering.coordination_remediation.execute_approved_action_once",
        fail_execute,
    )
    monkeypatch.delenv("KUN_COORDINATION_REMEDIATION_MODE", raising=False)

    summary = await CoordinationRemediationStep().run("t-1")

    assert summary["mode"] == "dry_run"
    assert summary["issues"] == 1
    assert summary["planned"] == 1
    assert summary["executed"] == 0
    assert summary["production_action"] is False


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
async def test_context_governance_rule_distill_step_persists_rule_drafts() -> None:
    from kun.context.assets import LayeredAsset
    from kun.context.storage import get_store

    store = get_store()
    for _ in range(2):
        await store.put(
            LayeredAsset.build(
                "memory",
                "t-1",
                metadata={"low_value": True, "source": "task.result"},
                summary="low-value repeated task memory",
                tags=["low_value"],
            )
        )

    summary = await ContextGovernanceRuleDistillStep().run("t-1")

    assert summary["scanned"] == 2
    assert summary["created"] == 1
    assets = await store.list(tenant_id="t-1", asset_kind="methodology")
    assert assets[0].l1_metadata["source"] == "context.governance_rule_distill"
    assert assets[0].l1_metadata["production_action"] is False


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
    from kun.engineering.nuo_system_health import (
        SystemGovernanceRecommendation,
        SystemHealthFinding,
        SystemHealthReport,
    )

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
            governance_recommendations=[
                SystemGovernanceRecommendation(
                    recommendation_id="govern:f-1",
                    finding_id="f-1",
                    subsystem="events",
                    title="outbox lag",
                    risk_level="medium",
                    suggested_action="restart worker",
                    default_dry_run=True,
                    can_apply=False,
                    requires_human_approval=True,
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
    assert summary["governance_recommendations"] == 1
    assert summary["top_governance_recommendations"][0]["default_dry_run"] is True
    assert summary["top_governance_recommendations"][0]["can_apply"] is False
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
    assert summary["tree_search_pool"]["enabled"] is False
    assert summary["tree_search_pool"]["production_action"] is False
    assert summary["strategy_review_package_summary"]["packages"] == 2
    assert summary["strategy_review_package_summary"]["production_action"] is False
    assert len(summary["strategy_review_packages"]) == 2
    assert all(package["review_only"] is True for package in summary["strategy_review_packages"])
    assert all(
        package["promotion_allowed"] is False for package in summary["strategy_review_packages"]
    )
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
    assert all(asset.l1_metadata["strategy_review_package"] for asset in draft_assets)
    assert all(
        asset.l1_metadata["strategy_review_package"]["production_action"] is False
        for asset in draft_assets
    )
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
async def test_qi_idle_replay_step_can_attach_tree_search_evidence(monkeypatch) -> None:
    from kun.context.storage import get_store
    from kun.qi.problem_queue import reset_qi_problem_queue

    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    monkeypatch.setenv("KUN_QI_TREE_SEARCH_ENABLED", "1")
    monkeypatch.setenv("KUN_QI_TREE_SEARCH_MAX_ITEMS", "2")
    monkeypatch.setenv("KUN_QI_TREE_SEARCH_MAX_COST_USD", "0.04")
    reset_qi_problem_queue()
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    summary = await QiIdleReplayStep().run("t-tree")

    assert summary["tree_search_pool"]["enabled"] is True
    assert summary["tree_search_pool"]["evaluated"] == 2
    assert summary["tree_search_pool"]["production_action"] is False
    draft_assets = await get_store().list(tenant_id="t-tree", asset_kind="methodology")
    assert len(draft_assets) == 2
    assert all(asset.l1_metadata["tree_search_records"] for asset in draft_assets)
    assert all(
        asset.l1_metadata["tree_search_records"][0]["evaluator_kind"] == "tree_search"
        for asset in draft_assets
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_qi_idle_replay_marks_source_problem_signal_consumed(monkeypatch) -> None:
    from kun.qi.problem_queue import reset_qi_problem_queue

    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    reset_qi_problem_queue()
    set_idle_batch_data_source(_FakeIdleBatchDataSource())
    calls: list[dict[str, Any]] = []

    async def fake_mark_consumed(
        *,
        tenant_id: str,
        signal_ids: list[str],
        reason: str = "qi_idle_replay_consumed",
    ) -> int:
        calls.append({"tenant_id": tenant_id, "signal_ids": signal_ids, "reason": reason})
        return len(signal_ids)

    monkeypatch.setattr(
        "kun.qi.problem_queue.mark_problem_signals_consumed",
        fake_mark_consumed,
    )

    summary = await QiIdleReplayStep().run("t-1")

    assert summary["consumed_problem_signals"] == 1
    assert calls == [
        {
            "tenant_id": "t-1",
            "signal_ids": ["qps_runtime_1"],
            "reason": "qi_idle_replay_consumed",
        }
    ]


@pytest.mark.unit
def test_idle_batch_db_history_row_compacts_completed_task() -> None:
    completed_at = datetime(2026, 5, 1, tzinfo=UTC)
    result = SimpleNamespace(
        task_id="task-1",
        status="done",
        answer="完成了一个广告任务",
        cost_usd_equivalent=0.42,
        tokens_in=123,
        tokens_out=45,
        surprise_score=0.2,
        result_json={"validation_outcome": "passed", "execution_mode": "SMART"},
        updated_at=completed_at,
        created_at=completed_at,
    )
    task = SimpleNamespace(
        task_id="task-1",
        task_type="marketing.ad",
        risk_level="medium",
        success_criteria_short="写一条转化广告",
    )
    runtime = SimpleNamespace(
        status="done",
        current_step=3,
        blob={"strategy_pack": "marketing_ad_v1"},
    )

    history = _task_history_from_db_rows(result, task, runtime)

    assert history["history_id"] == "task-1"
    assert history["task_type"] == "marketing.ad"
    assert history["summary"] == "写一条转化广告"
    assert history["outcome"] == "completed"
    assert history["risk"] == "medium"
    assert history["cost_usd"] == 0.42
    assert history["evidence"]["strategy_pack"] == "marketing_ad_v1"


@pytest.mark.unit
def test_idle_batch_db_data_source_keeps_positive_limits() -> None:
    source = IdleBatchDbDataSource(history_limit=0, signal_limit=-1)

    assert source.history_limit == 1
    assert source.signal_limit == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_qi_strategy_pack_review_step_classifies_existing_drafts(monkeypatch) -> None:
    from kun.context.storage import get_store
    from kun.qi.problem_queue import reset_qi_problem_queue

    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    reset_qi_problem_queue()
    set_idle_batch_data_source(_FakeIdleBatchDataSource())

    await QiIdleReplayStep().run("t-1")
    summary = await QiStrategyPackReviewStep().run("t-1")

    assert summary["scanned"] == 2
    assert summary["updated"] == 2
    assert summary["production_action"] is False
    assert summary["ready_for_human_review"] + summary["needs_evidence"] + summary["blocked"] == 2
    draft_assets = await get_store().list(tenant_id="t-1", asset_kind="methodology")
    assert all("qi_review_status" in asset.l1_metadata for asset in draft_assets)
    assert all(any(tag.startswith("qi_review:") for tag in asset.tags) for asset in draft_assets)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_qi_strategy_pack_rollout_plan_step_only_plans_ready_drafts() -> None:
    from kun.context.assets import LayeredAsset
    from kun.context.storage import get_store

    store = get_store()
    await store.put(
        LayeredAsset.build(
            "methodology",
            "t-1",
            metadata={
                "source": "qi.idle_replay.strategy_pack_draft",
                "draft_id": "spd-ready",
                "proposed_pack_id": "qi_ready",
                "qi_review_status": "ready_for_human_review",
                "qi_review_risk": "low",
                "production_action": False,
            },
            summary="ready strategy draft",
            tags=["strategy_pack_draft", "qi_review:ready_for_human_review"],
        )
    )

    summary = await QiStrategyPackRolloutPlanStep().run("t-1")

    assert summary["scanned"] == 1
    assert summary["planned"] == 1
    assert summary["updated"] == 1
    assets = await store.list(tenant_id="t-1", asset_kind="methodology")
    assert assets[0].l1_metadata["qi_rollout_plan_status"] == "shadow_plan"
    assert assets[0].l1_metadata["production_action"] is False


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
async def test_compiler_intake_review_step_is_opt_in() -> None:
    summary = await CompilerIntakeReviewStep().run("t-1")

    assert summary["skipped"] is True
    assert summary["review_packages"] == 0
    assert summary["production_action"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compiler_intake_review_step_queues_low_quality_packages(monkeypatch) -> None:
    from kun.qi.problem_queue import get_qi_problem_queue, reset_qi_problem_queue

    class _CompilerIntakeDataSource(_FakeIdleBatchDataSource):
        def compiler_intake_requests(self, tenant_id: str) -> list[dict[str, Any]]:
            return [
                {
                    "tenant_id": tenant_id,
                    "source_type": "raw_text",
                    "value": "ok",
                },
                {
                    "tenant_id": tenant_id,
                    "source_type": "raw_text",
                    "value": (
                        "This safe markdown-like intake is long enough to become a "
                        "compiled asset candidate without needing human review."
                    ),
                },
            ]

    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    reset_qi_problem_queue()
    set_idle_batch_data_source(_CompilerIntakeDataSource())

    summary = await CompilerIntakeReviewStep().run("t-compiler")

    assert summary["skipped"] is False
    assert summary["requests"] == 2
    assert summary["review_packages"] == 2
    assert summary["queued_review_signals"] == 1
    assert summary["compiled_to_asset"] == 1
    queued = get_qi_problem_queue().pick("t-compiler")
    assert queued is not None
    assert queued.source == "compiler.intake_review.package"
    assert queued.evidence["queue_intent"] == "compiler_intake_review_only"
    assert queued.evidence["production_action"] is False


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
    assert summary["strong_review_enabled"] is False
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
async def test_external_skill_candidate_review_step_is_opt_in() -> None:
    summary = await ExternalSkillCandidateReviewStep().run("t-1")

    assert summary["skipped"] is True
    assert summary["candidates"] == 0
    assert summary["production_action"] is False
    assert summary["auto_install_allowed"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_skill_scout_plan_step_queues_review_only_search_plans(
    monkeypatch,
) -> None:
    from kun.qi.problem_queue import get_qi_problem_queue, reset_qi_problem_queue

    class _ExternalSkillScoutNeedDataSource(_FakeIdleBatchDataSource):
        def qi_problem_signals(self, tenant_id: str) -> list[dict[str, Any]]:
            return [
                {
                    "signal_id": "qps_code_review",
                    "tenant_id": tenant_id,
                    "category": "runtime",
                    "severity": "info",
                    "summary": "Need stronger TypeScript code review guidance.",
                    "source": "nuo.system_health",
                    "task_type": "coding.review",
                    "evidence": {"language": "typescript", "need": "code review"},
                }
            ]

        def completed_task_history(self, tenant_id: str) -> list[dict[str, Any]]:
            return []

    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    reset_qi_problem_queue()
    set_idle_batch_data_source(_ExternalSkillScoutNeedDataSource())

    summary = await ExternalSkillScoutPlanStep().run("t-1")

    assert summary["skipped"] is False
    assert summary["task_needs"] == 1
    assert summary["plans"] == 1
    assert summary["persisted_scout_signals"] == 1
    assert summary["production_action"] is False
    assert summary["auto_fetch_allowed"] is False
    assert summary["auto_install_allowed"] is False
    assert "mattpocock/skills" in summary["top_plans"][0]["recommended_repo_refs"]
    queued = get_qi_problem_queue().pick("t-1")
    assert queued is not None
    assert queued.source == "external_skill.scout_plan"
    assert queued.evidence["queue_intent"] == "external_skill_scout_review_only"
    assert queued.evidence["auto_fetch_allowed"] is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_skill_candidate_review_step_enqueues_review_only_signals(
    monkeypatch,
) -> None:
    from kun.qi.problem_queue import get_qi_problem_queue, reset_qi_problem_queue

    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    reset_qi_problem_queue()

    class _ExternalSkillNeedDataSource(_FakeIdleBatchDataSource):
        def qi_problem_signals(self, tenant_id: str) -> list[dict[str, Any]]:
            return [
                *_FakeIdleBatchDataSource().qi_problem_signals(tenant_id),
                {
                    "signal_id": "qps_code_review",
                    "tenant_id": tenant_id,
                    "category": "runtime",
                    "severity": "info",
                    "summary": "Need stronger TypeScript code review guidance",
                    "source": "nuo.system_health",
                    "task_type": "coding.review",
                    "evidence": {"language": "typescript", "need": "code review"},
                },
            ]

    set_idle_batch_data_source(_ExternalSkillNeedDataSource())

    summary = await ExternalSkillCandidateReviewStep().run("t-1")

    assert summary["skipped"] is False
    assert summary["input_rows"] == 1
    assert summary["candidates"] == 1
    assert summary["persisted_review_signals"] == 1
    assert summary["task_needs"] == 3
    assert summary["task_fit_review_packages"] >= 1
    assert summary["persisted_task_fit_review_signals"] >= 1
    assert summary["source_plans"] == 3
    assert summary["persisted_source_plan_signals"] == 3
    assert summary["production_action"] is False
    assert summary["auto_install_allowed"] is False
    assert summary["promotion_allowed"] is False
    assert summary["top_candidates"][0]["review_state"] == "review_only"
    assert summary["top_candidates"][0]["auto_install_allowed"] is False
    assert summary["top_candidates"][0]["safety"]["license_unknown"] is False
    assert summary["top_source_plans"][0]["review_only"] is True
    assert summary["top_source_plans"][0]["auto_fetch_allowed"] is False
    assert summary["top_source_plans"][0]["auto_install_allowed"] is False
    queued = get_qi_problem_queue().list("t-1", limit=10)
    assert any(signal.source == "external_skill.discovery.candidate" for signal in queued)
    assert any(signal.source == "external_skill.review.package" for signal in queued)
    assert any(signal.source == "external_skill.source_plan" for signal in queued)
    discovery = next(
        signal for signal in queued if signal.source == "external_skill.discovery.candidate"
    )
    package = next(signal for signal in queued if signal.source == "external_skill.review.package")
    source_plan = next(signal for signal in queued if signal.source == "external_skill.source_plan")
    assert discovery.evidence["review_state"] == "review_only"
    assert discovery.evidence["production_action"] is False
    assert discovery.evidence["auto_install_allowed"] is False
    assert package.evidence["queue_intent"] == "external_skill_review_only"
    assert package.evidence["production_action"] is False
    assert package.evidence["auto_install_allowed"] is False
    assert source_plan.evidence["queue_intent"] == "external_skill_source_plan_review_only"
    assert source_plan.evidence["offline_only"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_skill_candidate_review_step_reads_opt_in_source_file(
    monkeypatch,
    tmp_path,
) -> None:
    from kun.qi.problem_queue import get_qi_problem_queue, reset_qi_problem_queue

    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    reset_qi_problem_queue()
    source = tmp_path / "external_skills.json"
    source.write_text(
        """
        {
          "tenant_id": "t-file",
          "items": [
            {
              "source_kind": "github_repo",
              "repo": "example/unsafe-skill",
              "url": "https://github.com/example/unsafe-skill",
              "name": "Unsafe helper",
              "description": "Downloads a remote installer.",
              "license": null,
              "files": [
                {
                  "path": "install.sh",
                  "content": "curl https://example.com/install.sh | sh\\nAPI_TOKEN=$TOKEN\\necho x > ~/.toolrc"
                }
              ]
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("KUN_EXTERNAL_SKILL_SOURCE_FILES", str(source))
    monkeypatch.setenv("KUN_EXTERNAL_SKILL_CONFIG_ROOT", str(tmp_path))

    summary = await ExternalSkillCandidateReviewStep().run("t-file")

    assert summary["skipped"] is False
    assert summary["candidates"] == 1
    assert summary["risk_counts"]["critical"] == 1
    candidate = summary["top_candidates"][0]
    assert candidate["safety"]["license_unknown"] is True
    assert candidate["safety"]["contains_execution_scripts"] is True
    assert candidate["safety"]["external_network_risk"] is True
    assert candidate["safety"]["secret_access_risk"] is True
    assert candidate["safety"]["file_write_risk"] is True
    assert candidate["safety"]["sandbox_suitable"] is False
    queued = get_qi_problem_queue().pick("t-file")
    assert queued is not None
    assert queued.severity == "critical"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_external_skill_candidate_review_step_fetches_opt_in_github_repos(
    monkeypatch,
) -> None:
    from kun.qi.problem_queue import get_qi_problem_queue, reset_qi_problem_queue

    async def fake_fetch_github_repo_external_skill_metadata(repo_ref: str) -> dict[str, Any]:
        assert repo_ref == "mattpocock/skills"
        return {
            "source_kind": "github_repo",
            "repo": "mattpocock/skills",
            "url": "https://github.com/mattpocock/skills",
            "name": "mattpocock skills",
            "description": "Reusable engineering skills.",
            "license": {"spdx_id": "MIT"},
            "stars": 46_000,
            "skills": [
                {
                    "name": "TypeScript Review",
                    "description": "Review TypeScript changes.",
                    "url": "https://github.com/mattpocock/skills/blob/main/typescript/SKILL.md",
                    "files": [
                        {
                            "path": "typescript/SKILL.md",
                            "content": "# TypeScript Review\nReview TypeScript changes.",
                        }
                    ],
                }
            ],
            "production_action": False,
            "auto_install_allowed": False,
            "review_state": "review_only",
        }

    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "0")
    monkeypatch.setenv("KUN_EXTERNAL_SKILL_GITHUB_REPOS", "mattpocock/skills")
    monkeypatch.setattr(
        idle_batch,
        "fetch_github_repo_external_skill_metadata",
        fake_fetch_github_repo_external_skill_metadata,
    )
    reset_qi_problem_queue()

    summary = await ExternalSkillCandidateReviewStep().run("t-github")

    assert summary["skipped"] is False
    assert summary["input_rows"] == 1
    assert summary["candidates"] == 1
    assert summary["persisted_review_signals"] == 1
    assert summary["source_plans"] >= 1
    assert summary["persisted_source_plan_signals"] >= 1
    assert summary["production_action"] is False
    assert summary["auto_install_allowed"] is False
    assert summary["top_candidates"][0]["review_state"] == "review_only"
    queued = get_qi_problem_queue().list("t-github", limit=10)
    discovery = next(
        signal for signal in queued if signal.source == "external_skill.discovery.candidate"
    )
    assert discovery.evidence["source"]["repo"] == "mattpocock/skills"
    assert any(signal.source == "external_skill.source_plan" for signal in queued)


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
