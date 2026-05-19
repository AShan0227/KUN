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
from kun.core.tenancy import TenantContext, tenant_scope


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop_writeback(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr("kun.api.nuo.benchmark_panel.record_benchmark_result", noop_writeback)
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
async def test_benchmark_panel_isolates_agents_and_runs_by_tenant() -> None:
    async def agent(prompt: str) -> str:
        if "hello" in prompt:
            return "hello"
        return prompt

    with tenant_scope(TenantContext(tenant_id="tenant-a")):
        register_agent("external_agent:same", agent)
        run_a = await start_benchmark_run(
            BenchmarkRunRequest(
                agent_ref="external_agent:same",
                tasks=[
                    {
                        "task_id": "exact-hello",
                        "task_type": "exact_match",
                        "prompt": "Return exactly: hello",
                        "expected_kind": "exact_match",
                        "expected": "hello",
                    }
                ],
            )
        )
        agents_a = await list_benchmark_agents()

    with tenant_scope(TenantContext(tenant_id="tenant-b")):
        register_agent("external_agent:same", agent)
        agents_b_before = await list_benchmark_agents()
        with pytest.raises(HTTPException) as exc:
            await get_benchmark_result(run_a.run_id)
        run_b = await start_benchmark_run(
            BenchmarkRunRequest(
                agent_ref="external_agent:same",
                tasks=[
                    {
                        "task_id": "exact-hello",
                        "task_type": "exact_match",
                        "prompt": "Return exactly: hello",
                        "expected_kind": "exact_match",
                        "expected": "hello",
                    }
                ],
            )
        )
        agents_b_after = await list_benchmark_agents()

    assert agents_a[0].latest_run_id == run_a.run_id
    assert agents_b_before[0].latest_run_id is None
    assert exc.value.status_code == 404
    assert agents_b_after[0].latest_run_id == run_b.run_id


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_panel_writes_capability_result(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def writeback(**kwargs: object) -> None:
        calls.append(kwargs)

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

    monkeypatch.setattr("kun.api.nuo.benchmark_panel.record_benchmark_result", writeback)
    register_agent("external_agent:test", agent)

    run = await start_benchmark_run(BenchmarkRunRequest(agent_ref="external_agent:test"))

    assert len(calls) == 1
    assert calls[0]["agent_ref"] == "external_agent:test"
    assert calls[0]["tenant_id"] == run.tenant_id
    results = calls[0]["results"]
    assert isinstance(results, list)
    assert len(results) == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_benchmark_panel_rejects_unknown_agent() -> None:
    with pytest.raises(HTTPException) as exc:
        await start_benchmark_run(BenchmarkRunRequest(agent_ref="missing"))

    assert exc.value.status_code == 404
