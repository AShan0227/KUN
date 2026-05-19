"""Capability writeback math — tests the pure apply_outcome logic, no DB.

We don't import the module-level function that hits the DB; instead we
use CapabilityCard + _apply_outcome via its public shape (record_outcome
itself needs session_scope which we'd have to mock heavily).

Test hits the internal helper via explicit import.
"""

from __future__ import annotations

import pytest
from kun.datamodel.capability import CapabilityCard, EntityRef
from kun.engineering.agent_benchmark import AgentBenchmarkResult
from kun.engineering.capability_writeback import (
    TaskOutcome,
    _apply_outcome,
    _select_card_for_update,
    record_benchmark_result,
    record_judge_verdict,
    record_outcome,
)
from kun.engineering.multi_judge import JudgeBallot, JuryVerdict
from sqlalchemy.dialects import postgresql


def _empty_card() -> CapabilityCard:
    return CapabilityCard(
        entity_ref=EntityRef(entity_type="role_template", entity_id="rt-test"),
        capabilities=[],
    )


@pytest.mark.unit
def test_apply_outcome_creates_capability_on_first_write():
    card = _empty_card()
    _apply_outcome(
        card,
        TaskOutcome(
            entity_type="role_template",
            entity_id="rt-test",
            task_type="coding.python.basic",
            outcome="pass",
            cost_usd=0.05,
            duration_sec=10,
        ),
    )
    cap = card.find("coding.python.basic")
    assert cap is not None
    assert cap.stats.total_invocations == 1
    assert cap.stats.success_count == 1
    assert cap.stats.success_rate == 1.0
    assert cap.stats.avg_cost_usd == 0.05


@pytest.mark.unit
def test_apply_outcome_running_average():
    card = _empty_card()
    for outcome, cost, dur in [("pass", 0.10, 10), ("fail", 0.20, 20), ("pass", 0.30, 30)]:
        _apply_outcome(
            card,
            TaskOutcome(
                entity_type="role_template",
                entity_id="rt-test",
                task_type="coding.python.basic",
                outcome=outcome,  # type: ignore[arg-type]
                cost_usd=cost,
                duration_sec=dur,
            ),
        )
    cap = card.find("coding.python.basic")
    assert cap.stats.total_invocations == 3
    assert cap.stats.success_count == 2
    assert cap.stats.failure_count == 1
    assert abs(cap.stats.success_rate - 2 / 3) < 1e-9
    assert abs(cap.stats.avg_cost_usd - 0.20) < 1e-9
    assert cap.stats.duration_p95 == 30  # p95/p99 are max in walking skeleton


@pytest.mark.unit
def test_apply_outcome_records_failure_mode():
    card = _empty_card()
    _apply_outcome(
        card,
        TaskOutcome(
            entity_type="role_template",
            entity_id="rt-test",
            task_type="coding.python.basic",
            outcome="fail",
            cost_usd=0.01,
            duration_sec=5,
            failure_name="test_timeout",
            failure_root_cause="external db slow",
        ),
    )
    cap = card.find("coding.python.basic")
    assert len(cap.failure_modes) == 1
    assert cap.failure_modes[0].name == "test_timeout"
    assert cap.failure_modes[0].frequency == 1


@pytest.mark.unit
def test_apply_outcome_surprise_rate_ema():
    card = _empty_card()
    # A run of non-surprise followed by a burst of surprise
    for s in [0.1, 0.1, 0.1, 0.8, 0.8, 0.8]:
        _apply_outcome(
            card,
            TaskOutcome(
                entity_type="role_template",
                entity_id="rt-test",
                task_type="coding.python.basic",
                outcome="pass",
                cost_usd=0.01,
                duration_sec=1,
                surprise_score=s,
            ),
        )
    cap = card.find("coding.python.basic")
    # surprise_rate after 6 events with an EMA alpha=1/20 starting at 0 should
    # be > 0 but small (we hit 3 surprise events)
    assert 0.0 < cap.quality.surprise_rate < 0.5


@pytest.mark.unit
def test_writeback_select_locks_existing_card() -> None:
    sql = str(
        _select_card_for_update(
            tenant_id="u-sylvan",
            entity_type="role_template",
            entity_id="rt-test",
        ).compile(dialect=postgresql.dialect())
    )

    assert "FOR UPDATE" in sql


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_outcome_sets_explicit_rls_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str | None] = {}

    class _FakeSession:
        pass

    class _FakeScope:
        async def __aenter__(self) -> _FakeSession:
            return _FakeSession()

        async def __aexit__(self, *_exc: object) -> None:
            return None

    def fake_session_scope(*, tenant_id: str | None = None, **_kwargs: object) -> _FakeScope:
        seen["tenant_id"] = tenant_id
        return _FakeScope()

    async def fake_record_in_txn(
        _session: _FakeSession,
        _tenant_id: str,
        _outcome: TaskOutcome,
    ) -> None:
        return None

    monkeypatch.setattr(
        "kun.engineering.capability_writeback.session_scope",
        fake_session_scope,
    )
    monkeypatch.setattr(
        "kun.engineering.capability_writeback._record_outcome_in_txn",
        fake_record_in_txn,
    )

    await record_outcome(
        "u-explicit",
        TaskOutcome(
            entity_type="role_template",
            entity_id="rt-test",
            task_type="coding.python.basic",
            outcome="pass",
            cost_usd=0.01,
            duration_sec=1,
        ),
    )

    assert seen == {"tenant_id": "u-explicit"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_judge_verdict_writes_each_judge_ballot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, TaskOutcome]] = []

    async def fake_record_outcome(tenant_id: str, outcome: TaskOutcome) -> None:
        seen.append((tenant_id, outcome))

    monkeypatch.setattr(
        "kun.engineering.capability_writeback.record_outcome",
        fake_record_outcome,
    )
    verdict = JuryVerdict(
        pass_=True,
        avg_score=0.7,
        spread=0.2,
        rationale="2/3 judges pass",
        ballots=[
            JudgeBallot(
                judge_id="judge_a",
                pass_=True,
                score=0.9,
                reason="good",
                cost_usd_actual=0.01,
                latency_ms=100.0,
            ),
            JudgeBallot(
                judge_id="judge_b",
                pass_=False,
                score=0.4,
                reason="weak",
                cost_usd_actual=0.02,
                latency_ms=200.0,
            ),
            JudgeBallot(
                judge_id="judge_c",
                pass_=True,
                score=0.8,
                reason="ok",
                cost_usd_actual=0.03,
                latency_ms=300.0,
            ),
        ],
    )

    for ballot in verdict.ballots:
        await record_judge_verdict(
            judge_id=ballot.judge_id,
            task_type="coding.python",
            verdict=verdict,
            tenant_id="u-judge",
        )

    assert len(seen) == 3
    assert {outcome.entity_id for _, outcome in seen} == {"judge_a", "judge_b", "judge_c"}
    assert {outcome.task_type for _, outcome in seen} == {"judge.coding.python"}
    assert seen[0][0] == "u-judge"
    failed = next(outcome for _, outcome in seen if outcome.entity_id == "judge_b")
    assert failed.outcome == "fail"
    assert failed.failure_name == "judge_rejected"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_record_benchmark_result_writes_aggregated_agent_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, TaskOutcome]] = []

    async def fake_record_outcome(tenant_id: str, outcome: TaskOutcome) -> None:
        seen.append((tenant_id, outcome))

    monkeypatch.setattr(
        "kun.engineering.capability_writeback.record_outcome",
        fake_record_outcome,
    )

    await record_benchmark_result(
        agent_ref="external_agent:test",
        tenant_id="u-bench",
        results=[
            AgentBenchmarkResult(
                agent_ref="external_agent:test",
                task_id="t1",
                success=True,
                score=1.0,
                cost_usd=0.03,
                duration_sec=1.0,
            ),
            AgentBenchmarkResult(
                agent_ref="external_agent:test",
                task_id="t2",
                success=False,
                score=0.5,
                cost_usd=0.01,
                duration_sec=2.0,
            ),
        ],
    )

    assert len(seen) == 1
    tenant_id, outcome = seen[0]
    assert tenant_id == "u-bench"
    assert outcome.entity_type == "external_agent"
    assert outcome.entity_id == "external_agent:test"
    assert outcome.task_type == "benchmark"
    assert outcome.outcome == "partial"
    assert outcome.cost_usd == pytest.approx(0.04)
    assert outcome.duration_sec == pytest.approx(3.0)
    assert outcome.rubric_score == pytest.approx(3.75)
