from __future__ import annotations

from types import SimpleNamespace

import pytest
from kun.qi.cron_jobs import _pick_explore_prompt
from kun.qi.problem_queue import (
    QiProblemSignal,
    get_qi_problem_queue,
    prompt_for_problem,
    reset_qi_problem_queue,
)


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
