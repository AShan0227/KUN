"""CreditAssignment 单测 (V2.2 §25 / Wire 14)."""

from __future__ import annotations

from typing import Any

import pytest
from kun.engineering.credit_assignment import (
    CodeChangeCreditInput,
    CreditAssignment,
    ResourceCreditDelta,
    StepCredit,
    build_code_change_credit_report,
    contribution_score_from_counts,
    get_contribution_tracker,
    heuristic_reflector,
    hydrate_contribution_tracker_from_db,
    make_resource_key,
    persist_code_change_credit_report,
    persist_resource_credit_report,
    reset_contribution_tracker,
    resource_credit_summaries_from_rows,
    split_resource_key,
    summarize_resource_credit_deltas,
)
from sqlalchemy.dialects import postgresql

# ---- record_step ----


def test_record_step_creates_initial_credit() -> None:
    ca = CreditAssignment()
    credit = ca.record_step(
        task_id="tk-1",
        step_id=1,
        resources={"memory": ["m1"], "model": ["claude"]},
        immediate_reward=0.10,
    )
    assert credit.step_id == 1
    assert credit.immediate_reward == 0.10
    # 平摊: 2 资源 → 各 0.5
    assert abs(credit.credit_share["memory:m1"] - 0.5) < 1e-9
    assert abs(credit.credit_share["model:claude"] - 0.5) < 1e-9


def test_record_step_preserves_prefixed_context_resource_keys() -> None:
    """Context assets may already carry kind:id; do not double-prefix them."""

    ca = CreditAssignment()
    credit = ca.record_step(
        task_id="tk-context",
        step_id=1,
        resources={
            "memory": ["memory:m1", "knowledge:k1", "skill:s1"],
            "model": ["gpt-5.5"],
        },
        immediate_reward=0.4,
    )

    assert "memory:m1" in credit.credit_share
    assert "knowledge:k1" in credit.credit_share
    assert "skill:s1" in credit.credit_share
    assert "memory:knowledge:k1" not in credit.credit_share
    assert "memory:skill:s1" not in credit.credit_share


def test_record_step_immediate_reward_floor() -> None:
    """负 reward 被 floor 到 0 (避免负反馈反推)."""
    ca = CreditAssignment(immediate_reward_floor=0.0)
    credit = ca.record_step(
        task_id="tk-1",
        step_id=1,
        resources={"model": ["m"]},
        immediate_reward=-0.5,
    )
    assert credit.immediate_reward == 0.0


def test_record_step_empty_resources() -> None:
    ca = CreditAssignment()
    credit = ca.record_step(task_id="tk-1", step_id=1, resources={})
    assert credit.credit_share == {}


def test_add_resources_to_step_updates_credit_share_for_late_decisions() -> None:
    ca = CreditAssignment()
    credit = ca.record_step(
        task_id="tk-1",
        step_id=1,
        resources={"model": ["gpt-5.5"]},
    )
    assert credit.credit_share == {"model:gpt-5.5": 1.0}

    updated = ca.add_resources_to_step(
        "tk-1",
        1,
        {"decision_ticket": ["dt-1"], "anti_gaming_detected": ["fake_completion"]},
    )

    assert updated is not None
    assert updated.resources_used["decision_ticket"] == ["dt-1"]
    assert updated.resources_used["anti_gaming_detected"] == ["fake_completion"]
    assert abs(updated.credit_share["model:gpt-5.5"] - (1 / 3)) < 1e-9
    assert abs(updated.credit_share["decision_ticket:dt-1"] - (1 / 3)) < 1e-9


def test_record_step_can_credit_reusable_decision_strategy_keys() -> None:
    ca = CreditAssignment()
    credit = ca.record_step(
        task_id="tk-policy",
        step_id=1,
        resources={
            "decision_ticket": ["dt-memory-1"],
            "decision_point": ["memory_policy_selected"],
            "decision_action": ["memory_policy_selected__targeted_meta_decision_methodology"],
            "memory_policy": ["targeted_meta_decision_methodology"],
            "memory_policy_depth": ["targeted"],
            "memory_policy_layer": ["meta_decision", "methodology"],
        },
        immediate_reward=0.6,
    )

    assert "decision_ticket:dt-memory-1" in credit.credit_share
    assert "decision_point:memory_policy_selected" in credit.credit_share
    assert (
        "decision_action:memory_policy_selected__targeted_meta_decision_methodology"
        in credit.credit_share
    )
    assert "memory_policy:targeted_meta_decision_methodology" in credit.credit_share
    assert "memory_policy_depth:targeted" in credit.credit_share
    assert "memory_policy_layer:meta_decision" in credit.credit_share


# ---- finalize_task + reflector ----


@pytest.mark.asyncio
async def test_finalize_with_heuristic_reflector_marks_critical() -> None:
    """高 reward step 应该被 heuristic reflector 标 critical."""
    ca = CreditAssignment(critical_boost_factor=2.0)
    ca.record_step("tk-1", 1, {"model": ["a"]}, immediate_reward=0.1)
    ca.record_step("tk-1", 2, {"model": ["b"]}, immediate_reward=0.5)  # 高
    ca.record_step("tk-1", 3, {"model": ["c"]}, immediate_reward=0.1)
    report = await ca.finalize_task("tk-1", "pass", reflector=heuristic_reflector)

    assert report.task_outcome == "pass"
    assert 2 in report.critical_path_step_ids
    # 确认 step 2 的 credit 被 boost
    step_2 = next(s for s in report.step_credits if s.step_id == 2)
    assert step_2.is_critical_path is True
    assert step_2.credit_share["model:b"] == 2.0  # 1.0 × 2.0 boost


@pytest.mark.asyncio
async def test_finalize_no_reflector_falls_back_to_equal_share() -> None:
    """没 reflector → critical 列表为空, credit 不 boost."""
    ca = CreditAssignment()
    ca.record_step("tk-2", 1, {"skill": ["s1"]}, immediate_reward=0.1)
    ca.record_step("tk-2", 2, {"skill": ["s2"]}, immediate_reward=0.1)
    report = await ca.finalize_task("tk-2", "pass", reflector=None)
    assert report.critical_path_step_ids == []


@pytest.mark.asyncio
async def test_finalize_reflector_exception_safe() -> None:
    """reflector 抛异常 → log + 退化, 不破坏主流程."""

    async def bad_reflector(task_id, steps, outcome):
        raise RuntimeError("boom")

    ca = CreditAssignment()
    ca.record_step("tk-3", 1, {"model": ["x"]}, immediate_reward=0.2)
    report = await ca.finalize_task("tk-3", "pass", reflector=bad_reflector)
    assert report.critical_path_step_ids == []


@pytest.mark.asyncio
async def test_finalize_no_steps_returns_empty_report() -> None:
    ca = CreditAssignment()
    report = await ca.finalize_task("tk-empty", "fail")
    assert report.step_credits == []
    assert report.total_immediate_reward == 0.0


# ---- aggregate_resource_credits ----


@pytest.mark.asyncio
async def test_aggregate_resource_credits_sums_share_x_reward() -> None:
    ca = CreditAssignment(critical_boost_factor=1.5)
    ca.record_step("tk-4", 1, {"model": ["claude"]}, immediate_reward=0.5)
    ca.record_step("tk-4", 2, {"model": ["claude"]}, immediate_reward=0.3)
    report = await ca.finalize_task("tk-4", "pass", reflector=heuristic_reflector)
    agg = ca.aggregate_resource_credits(report)
    # claude 用了 2 步 → share=1.0 each, reward 0.5/0.3
    # 关键步 reward × boost (1.5 step 1) — heuristic 标 step 1 (高于平均 0.4)
    assert "model:claude" in agg
    assert agg["model:claude"] > 0


@pytest.mark.asyncio
async def test_aggregate_zero_reward_uses_baseline() -> None:
    """immediate_reward=0 时用 baseline 0.5 算 credit (避免全部 0)."""
    ca = CreditAssignment()
    ca.record_step("tk-5", 1, {"skill": ["s1"]}, immediate_reward=0.0)
    report = await ca.finalize_task("tk-5", "pass")
    agg = ca.aggregate_resource_credits(report)
    assert agg["skill:s1"] > 0  # 用 baseline


@pytest.mark.asyncio
async def test_aggregate_resource_deltas_counts_success_and_critical_path() -> None:
    ca = CreditAssignment(critical_boost_factor=2.0)
    ca.record_step("tk-delta", 1, {"memory": ["m1"], "model": ["gpt-5.5"]}, immediate_reward=0.8)
    ca.record_step("tk-delta", 2, {"memory": ["m1"]}, immediate_reward=0.1)
    report = await ca.finalize_task("tk-delta", "pass", reflector=heuristic_reflector)

    deltas = ca.aggregate_resource_deltas(report)

    assert set(deltas) == {"memory:m1", "model:gpt-5.5"}
    assert deltas["memory:m1"].used_count == 2
    assert deltas["memory:m1"].pass_count == 2
    assert deltas["memory:m1"].critical_count == 1
    assert deltas["memory:m1"].credit_total > 0
    assert deltas["model:gpt-5.5"].resource_kind == "model"
    assert deltas["model:gpt-5.5"].resource_id == "gpt-5.5"


def test_contribution_tracker_updates_from_deltas() -> None:
    from kun.engineering.credit_assignment import ContributionTracker

    tracker = ContributionTracker()
    tracker.update_from_deltas(
        {
            "memory:m1": ResourceCreditDelta(
                resource_key="memory:m1",
                resource_kind="memory",
                resource_id="m1",
                used_count=2,
                pass_count=2,
                critical_count=1,
                credit_total=1.0,
            )
        }
    )
    assert tracker.contribution_score("m1", "memory") == 0.75


def test_contribution_tracker_is_tenant_scoped_for_same_resource_key() -> None:
    from kun.engineering.credit_assignment import ContributionTracker

    tracker = ContributionTracker()
    tracker.seed_counts(
        "skill:writer",
        used_count=4,
        pass_count=4,
        critical_count=4,
        tenant_id="tenant-a",
    )
    tracker.seed_counts(
        "skill:writer",
        used_count=4,
        pass_count=0,
        critical_count=0,
        tenant_id="tenant-b",
    )

    assert tracker.contribution_score("writer", "skill", tenant_id="tenant-a") == 1.0
    assert tracker.contribution_score("writer", "skill", tenant_id="tenant-b") == 0.0
    assert tracker.contribution_score("writer", "skill", tenant_id="tenant-c") == 0.0


def test_resource_credit_summaries_are_human_readable() -> None:
    class Row:
        resource_key = "skill:writer"
        resource_kind = "skill"
        resource_id = "writer"
        used_count = 4
        pass_count = 3
        critical_count = 2
        credit_total = 2.34567
        last_seen_at = None

    summaries = resource_credit_summaries_from_rows([Row()])

    assert summaries[0].resource_key == "skill:writer"
    assert summaries[0].contribution_score == 0.625
    assert summaries[0].credit_total == 2.3457


def test_summarize_resource_credit_deltas_groups_by_kind() -> None:
    summaries = summarize_resource_credit_deltas(
        {
            "memory:m1": ResourceCreditDelta(
                resource_key="memory:m1",
                resource_kind="memory",
                resource_id="m1",
                used_count=2,
                pass_count=2,
                critical_count=1,
                credit_total=0.8,
            ),
            "memory:m2": ResourceCreditDelta(
                resource_key="memory:m2",
                resource_kind="memory",
                resource_id="m2",
                used_count=1,
                pass_count=0,
                critical_count=0,
                credit_total=0.1,
            ),
            "skill:writer": ResourceCreditDelta(
                resource_key="skill:writer",
                resource_kind="skill",
                resource_id="writer",
                used_count=1,
                pass_count=1,
                critical_count=1,
                credit_total=1.2,
            ),
        },
        top_n_per_kind=1,
    )

    by_kind = {summary.resource_kind: summary for summary in summaries}
    assert by_kind["memory"].resource_count == 2
    assert by_kind["memory"].used_count == 3
    assert by_kind["memory"].pass_count == 2
    assert by_kind["memory"].critical_count == 1
    assert by_kind["memory"].top_resource_keys == ["memory:m1"]
    assert by_kind["skill"].contribution_score == 1.0
    assert by_kind["skill"].top_resource_keys == ["skill:writer"]


def test_build_code_change_credit_report_marks_successful_change_as_critical() -> None:
    report = build_code_change_credit_report(
        CodeChangeCreditInput(
            task_id="task-code",
            path="kun/example.py",
            mode="dry_run",
            phase="done",
            ok=True,
            checks_passed=True,
            review_ok=True,
            bytes_changed=42,
        )
    )

    assert report.task_id == "task-code"
    assert report.task_outcome == "pass"
    assert report.critical_path_step_ids == [1]
    step = report.step_credits[0]
    assert step.is_critical_path is True
    assert step.immediate_reward > 0.8
    assert step.resources_used["code_capability"] == [
        "workflow_propose_change",
        "mode_dry_run",
        "phase_done",
        "ext_py",
        "checks_passed",
        "dry_run",
        "review_passed",
    ]
    assert "code_capability:workflow_propose_change" in step.credit_share


def test_build_code_change_credit_report_keeps_failure_as_negative_evidence() -> None:
    report = build_code_change_credit_report(
        CodeChangeCreditInput(
            task_id="task-code-fail",
            path="../bad.sh",
            mode="apply",
            phase="resolve",
            ok=False,
            checks_passed=False,
            review_ok=False,
            lint_failed_count=1,
            test_failed_count=2,
        )
    )

    assert report.task_outcome == "fail"
    assert report.critical_path_step_ids == []
    step = report.step_credits[0]
    assert step.immediate_reward == 0.10
    assert "review_failed" in step.resources_used["code_capability"]
    assert "lint_failed" in step.resources_used["code_capability"]
    assert "test_failed" in step.resources_used["code_capability"]


@pytest.mark.asyncio
async def test_persist_code_change_credit_report_builds_code_capability_upsert() -> None:
    class FakeSession:
        sql = ""

        async def execute(self, stmt: Any) -> None:
            self.sql = str(
                stmt.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )

    session = FakeSession()
    deltas = await persist_code_change_credit_report(
        session,  # type: ignore[arg-type]
        tenant_id="u-sylvan",
        credit=CodeChangeCreditInput(
            task_id="task-code-sql",
            path="service.ts",
            mode="apply",
            phase="done",
            ok=True,
            applied=True,
            checks_passed=True,
            review_ok=True,
            bytes_changed=100,
        ),
    )

    assert "code_capability:workflow_propose_change" in deltas
    assert deltas["code_capability:workflow_propose_change"].pass_count == 1
    assert "resource_credit_stats" in session.sql
    assert "code_capability" in session.sql


def test_resource_key_helpers_and_score_clamp() -> None:
    assert split_resource_key("memory:m1") == ("memory", "m1")
    assert split_resource_key("legacy-id") == ("memory", "legacy-id")
    assert make_resource_key("skill", "s1") == "skill:s1"
    assert make_resource_key("skill", "skill:s1") == "skill:s1"
    assert contribution_score_from_counts(used_count=0, pass_count=10, critical_count=10) == 0.0
    assert contribution_score_from_counts(used_count=2, pass_count=9, critical_count=1) == 0.75


@pytest.mark.asyncio
async def test_persist_resource_credit_report_builds_atomic_upsert() -> None:
    class FakeSession:
        sql = ""

        async def execute(self, stmt: Any) -> None:
            self.sql = str(
                stmt.compile(
                    dialect=postgresql.dialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )

    ca = CreditAssignment()
    ca.record_step("tk-sql", 1, {"memory": ["m1"]}, immediate_reward=0.7)
    report = await ca.finalize_task("tk-sql", "pass", reflector=heuristic_reflector)
    session = FakeSession()

    deltas = await persist_resource_credit_report(session, tenant_id="u-sylvan", report=report)  # type: ignore[arg-type]

    assert "memory:m1" in deltas
    assert "ON CONFLICT" in session.sql
    assert "resource_credit_stats" in session.sql
    assert "used_count" in session.sql


@pytest.mark.asyncio
async def test_hydrate_contribution_tracker_from_db_seeds_hot_cache() -> None:
    class Row:
        resource_key = "strategy_pack:education"
        used_count = 3
        pass_count = 3
        critical_count = 3

    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[Row]:
            return [Row()]

    class FakeSession:
        async def execute(self, _stmt: Any) -> Result:
            return Result()

    reset_contribution_tracker()
    try:
        count = await hydrate_contribution_tracker_from_db(
            FakeSession(),  # type: ignore[arg-type]
            tenant_id="u-sylvan",
            resource_kinds=["strategy_pack"],
            min_interval_sec=0,
        )
        assert count == 1
        assert (
            get_contribution_tracker().contribution_score(
                "education",
                "strategy_pack",
                tenant_id="u-sylvan",
            )
            == 1.0
        )
    finally:
        reset_contribution_tracker()


# ---- reset_task ----


def test_reset_task_clears_state() -> None:
    ca = CreditAssignment()
    ca.record_step("tk-6", 1, {"model": ["x"]})
    assert "tk-6" in ca._step_credits
    ca.reset_task("tk-6")
    assert "tk-6" not in ca._step_credits


# ---- 校验 ----


def test_invalid_critical_boost_raises() -> None:
    with pytest.raises(ValueError):
        CreditAssignment(critical_boost_factor=0.5)


# ---- heuristic_reflector ----


@pytest.mark.asyncio
async def test_heuristic_reflector_picks_above_average() -> None:
    steps = [
        StepCredit(step_id=1, immediate_reward=0.1),
        StepCredit(step_id=2, immediate_reward=0.5),
        StepCredit(step_id=3, immediate_reward=0.2),
    ]
    critical = await heuristic_reflector("tk-x", steps, "pass")
    assert 2 in critical


@pytest.mark.asyncio
async def test_heuristic_reflector_falls_back_to_last_step() -> None:
    """全 reward 0 → 兜底返最后一步."""
    steps = [
        StepCredit(step_id=1, immediate_reward=0.0),
        StepCredit(step_id=5, immediate_reward=0.0),
    ]
    critical = await heuristic_reflector("tk-x", steps, "pass")
    assert critical == [5]
