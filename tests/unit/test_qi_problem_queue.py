from __future__ import annotations

from types import SimpleNamespace

import pytest
from kun.qi.cron_jobs import _pick_explore_prompt
from kun.qi.problem_queue import (
    QiProblemSignal,
    SqlQiProblemQueue,
    _upsert_problem_signal_stmt,
    get_configured_qi_problem_queue,
    get_qi_problem_queue,
    prompt_for_problem,
    reset_qi_problem_queue,
)
from sqlalchemy.dialects import postgresql


def setup_function(_function: object) -> None:
    reset_qi_problem_queue()


def teardown_function(_function: object) -> None:
    reset_qi_problem_queue()


def test_problem_queue_dedupes_and_prioritizes() -> None:
    queue = get_qi_problem_queue()
    low = QiProblemSignal.build(
        tenant_id="u-test",
        category="runtime",
        severity="info",
        summary="普通提示",
        source="test",
    )
    high = QiProblemSignal.build(
        tenant_id="u-test",
        category="world_gateway",
        severity="critical",
        summary="邮件 handler 缺补偿",
        source="test",
    )
    queue.enqueue_many([low, high, high])

    assert len(queue.list("u-test")) == 2
    assert queue.pick("u-test") == high


def test_problem_queue_treats_nuo_warn_as_warning() -> None:
    queue = get_qi_problem_queue()
    info = QiProblemSignal.build(
        tenant_id="u-test",
        category="runtime",
        severity="info",
        summary="普通状态",
        source="test",
    )
    warn = QiProblemSignal.build(
        tenant_id="u-test",
        category="world_gateway",
        severity="warn",
        summary="NUO 发现 handler 风险",
        source="nuo.system_health",
    )
    queue.enqueue_many([info, warn])

    assert queue.pick("u-test") == warn


def test_prompt_for_problem_is_actionable() -> None:
    signal = QiProblemSignal.build(
        tenant_id="u-test",
        category="delivery",
        severity="warning",
        summary="交付状态声明和实际动作不一致",
        source="nuo",
    )
    prompt = prompt_for_problem(signal)
    assert "真实系统问题" in prompt
    assert "可验证" in prompt
    assert "交付状态声明" in prompt


def test_sql_problem_queue_upsert_dedupes_by_tenant_signal() -> None:
    signal = QiProblemSignal.build(
        tenant_id="u-test",
        category="world_gateway",
        severity="critical",
        summary="WorldGateway handler 连续失败",
        source="test",
    )

    stmt = _upsert_problem_signal_stmt(signal, signal.created_at)
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    sql = str(stmt.compile(dialect=dialect))

    assert "INSERT INTO qi_problem_signals" in sql
    assert "ON CONFLICT (tenant_id, signal_id) DO UPDATE" in sql
    assert "occurrence_count = (qi_problem_signals.occurrence_count + " in sql
    assert "status = " in sql


@pytest.mark.asyncio
async def test_collect_problem_signals_persists_sampled_findings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kun.engineering.nuo_system_health import SystemHealthFinding, SystemHealthReport
    from kun.qi import problem_queue

    async def fake_collect_system_health_report(*, tenant_id: str) -> SystemHealthReport:
        return SystemHealthReport(
            tenant_id=tenant_id,
            findings=[
                SystemHealthFinding(
                    finding_id="f-1",
                    severity="warn",
                    subsystem="world_gateway",
                    title="WorldGateway handler 缺补偿",
                    detail="真实外发 handler 没有补偿演练",
                    suggested_action="补补偿策略",
                )
            ],
        )

    persisted: list[QiProblemSignal] = []

    async def fake_persist(signals: list[QiProblemSignal]) -> int:
        persisted.extend(signals)
        return len(signals)

    monkeypatch.setattr(
        "kun.engineering.nuo_system_health.collect_system_health_report",
        fake_collect_system_health_report,
    )
    monkeypatch.setattr(problem_queue, "persist_problem_signals", fake_persist)

    signals = await problem_queue.collect_problem_signals("u-test")

    assert len(signals) == 1
    assert persisted == signals
    assert signals[0].category == "world_gateway"
    assert signals[0].summary == "WorldGateway handler 缺补偿"


@pytest.mark.asyncio
async def test_qi_prompt_prefers_real_problem_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    queue = get_qi_problem_queue()
    queue.enqueue(
        QiProblemSignal.build(
            tenant_id="u-test",
            category="world_gateway",
            severity="critical",
            summary="WorldGateway handler 连续失败",
            source="test",
        )
    )
    app = SimpleNamespace(state=SimpleNamespace(qi_problem_queue=queue))

    async def _no_collect(_tenant_id: str) -> list[QiProblemSignal]:
        return []

    monkeypatch.setattr("kun.qi.problem_queue.collect_problem_signals", _no_collect)
    prompt = await _pick_explore_prompt(app=app, tenant_id="u-test")
    assert "WorldGateway handler 连续失败" in prompt


def test_sql_problem_queue_is_explicitly_async() -> None:
    queue = SqlQiProblemQueue()
    assert hasattr(queue.enqueue_many, "__call__")


def test_configured_problem_queue_uses_sql_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_QI_PROBLEM_QUEUE_DB_ENABLED", "1")

    assert isinstance(get_configured_qi_problem_queue(), SqlQiProblemQueue)


@pytest.mark.asyncio
async def test_qi_prompt_can_consume_async_problem_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncQueue:
        def __init__(self) -> None:
            self.signals: list[QiProblemSignal] = []

        async def enqueue_many(self, signals: list[QiProblemSignal]) -> int:
            self.signals.extend(signals)
            return len(signals)

        async def pick(self, tenant_id: str) -> QiProblemSignal | None:
            return self.signals[0] if self.signals and tenant_id == "u-test" else None

    signal = QiProblemSignal.build(
        tenant_id="u-test",
        category="runtime",
        severity="error",
        summary="SQL 队列里的真实问题",
        source="test",
    )
    queue = FakeAsyncQueue()
    app = SimpleNamespace(state=SimpleNamespace(qi_problem_queue=queue))

    async def _collect(_tenant_id: str) -> list[QiProblemSignal]:
        return [signal]

    monkeypatch.setattr("kun.qi.problem_queue.collect_problem_signals", _collect)

    prompt = await _pick_explore_prompt(app=app, tenant_id="u-test")

    assert "SQL 队列里的真实问题" in prompt
