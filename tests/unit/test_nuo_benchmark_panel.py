"""傩 benchmark 面板测试。"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from kun.api.nuo.benchmark_panel import (
    BenchmarkRunRequest,
    clear_benchmark_state,
    get_benchmark_result,
    list_benchmark_agents,
    register_agent,
    start_benchmark_run,
)


@pytest.fixture(autouse=True)
def _clean_state() -> None:
    clear_benchmark_state()
    yield
    clear_benchmark_state()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_panel_runs_registered_agent() -> None:
    async def agent(prompt: str) -> str:
        if "hello" in prompt:
            return "hello"
        if "Python function" in prompt:
            return "def add(a, b):\n    return a + b\n"
        if "tenant" in prompt:
            return "tenant isolation"
        if "budget" in prompt:
            return "budget cost"
        return '{"ok":true}'

    register_agent("external_agent:test", agent)

    agents_before = await list_benchmark_agents()
    assert agents_before[0].agent_ref == "external_agent:test"
    assert agents_before[0].latest_run_id is None

    run = await start_benchmark_run(BenchmarkRunRequest(agent_ref="external_agent:test"))
    fetched = await get_benchmark_result(run.run_id)
    agents_after = await list_benchmark_agents()

    assert fetched.run_id == run.run_id
    assert run.summary["success_rate"] == 1.0
    assert len(run.results) == 5
    assert agents_after[0].latest_run_id == run.run_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_panel_rejects_unknown_agent() -> None:
    with pytest.raises(HTTPException) as exc:
        await start_benchmark_run(BenchmarkRunRequest(agent_ref="missing"))

    assert exc.value.status_code == 404
