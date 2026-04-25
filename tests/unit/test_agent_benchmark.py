"""外部 agent benchmark 测试。"""

from __future__ import annotations

import pytest
from kun.engineering.agent_benchmark import (
    BenchmarkTask,
    aggregate_results,
    run_benchmark,
    sample_benchmark_tasks,
    score_output,
)


@pytest.mark.unit
def test_score_output_exact_match() -> None:
    task = BenchmarkTask(
        task_id="t1",
        task_type="exact",
        prompt="return hello",
        expected_kind="exact_match",
        expected="hello",
    )

    assert score_output(output="hello", task=task) == 1.0
    assert score_output(output="hello!", task=task) == 0.0


@pytest.mark.unit
def test_score_output_code_compile() -> None:
    task = BenchmarkTask(
        task_id="t2",
        task_type="code",
        prompt="return code",
        expected_kind="code_compile",
        expected=None,
    )

    assert score_output(output="def add(a, b):\n    return a + b\n", task=task) == 1.0
    assert score_output(output="def broken(:\n", task=task) == 0.0


@pytest.mark.unit
def test_score_output_rubric_signals() -> None:
    task = BenchmarkTask(
        task_id="t3",
        task_type="review",
        prompt="review tenant isolation",
        expected_kind="rubric_score",
        expected={"signals": ["tenant", "isolation"]},
    )

    assert score_output(output="tenant isolation is required", task=task) == 1.0
    assert score_output(output="tenant only", task=task) == 0.5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_benchmark_scores_five_tasks() -> None:
    responses = {
        "Return exactly: hello": "hello",
        "Return a Python function named add(a, b).": "def add(a, b):\n    return a + b\n",
        "Explain why tenant isolation matters.": "tenant isolation prevents data leaks",
        "Explain why budget tracking matters.": "budget and cost tracking prevent runaway spend",
        'Return exactly: {"ok":true}': '{"ok":true}',
    }

    async def agent(prompt: str) -> str:
        return responses[prompt]

    results = await run_benchmark(
        agent_invoke=agent,
        tasks=sample_benchmark_tasks(),
        agent_ref="external_agent:test",
    )
    summary = aggregate_results(results)

    assert len(results) == 5
    assert all(result.success for result in results)
    assert summary["success_rate"] == 1.0
    assert summary["avg_score"] == 1.0
    assert summary["cost_usd"] > 0.0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_benchmark_accepts_custom_cost_estimator() -> None:
    task = BenchmarkTask(
        task_id="t1",
        task_type="exact",
        prompt="return hello",
        expected_kind="exact_match",
        expected="hello",
    )

    async def agent(_prompt: str) -> str:
        return "hello"

    results = await run_benchmark(
        agent_invoke=agent,
        tasks=[task],
        cost_estimator=lambda _task, _response: 0.42,
    )

    assert results[0].cost_usd == 0.42


@pytest.mark.unit
@pytest.mark.asyncio
async def test_run_benchmark_default_cost_estimator_uses_prompt_and_response_size() -> None:
    task = BenchmarkTask(
        task_id="t1",
        task_type="exact",
        prompt="abcdefgh",
        expected_kind="exact_match",
        expected="abcdefgh",
    )

    async def agent(_prompt: str) -> str:
        return "abcdefgh"

    results = await run_benchmark(agent_invoke=agent, tasks=[task])

    assert results[0].cost_usd == pytest.approx(0.000012)
