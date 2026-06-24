"""APIAdapter honors Action.timeout_sec / max_retries (audit F094).

The base docstring promised base-level timeout + retry, but execute() ran
_do_execute exactly once with no timeout. These assert the contract: timeout
bounds each attempt, retries cover transient failures only, business failures
and the default single-attempt path are not retried.
"""

from __future__ import annotations

import asyncio
from typing import ClassVar

import pytest
from kun.interface.automation.api_base import APIAdapter, HttpCaller, make_action
from kun.interface.automation.base import Action, ActionResult


async def _fake_http(method: str, url: str, body: dict | None) -> dict:  # pragma: no cover
    return {}


class _Adapter(APIAdapter):
    platform = "fake"
    supported_operations: ClassVar[set[str]] = {"op"}

    def __init__(self, behavior, **kw) -> None:
        super().__init__(http_caller=_fake_http, **kw)
        self._behavior = behavior
        self.calls = 0

    async def _do_execute(self, action: Action, *, http_caller: HttpCaller) -> ActionResult:
        self.calls += 1
        return await self._behavior(self, action)


def _ok(_self, action) -> ActionResult:
    async def inner():
        return ActionResult(
            action_id=action.action_id,
            status="ok",
            kind_used="api",
            result_payload={"done": True},
            latency_ms=0.0,
        )

    return inner()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_timeout_is_enforced() -> None:
    async def slow(_self, _action) -> ActionResult:
        await asyncio.sleep(1.0)
        raise AssertionError("should have timed out")

    adapter = _Adapter(slow)
    action = make_action(target_platform="fake", operation="op", payload={}, timeout_sec=0.02)
    result = await adapter.execute(action)
    assert result.status == "failed"
    assert "timeout" in (result.error or "")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_retries_transient_then_succeeds() -> None:
    async def flaky(self, action) -> ActionResult:
        if self.calls < 3:
            raise RuntimeError("transient")
        return await _ok(self, action)

    adapter = _Adapter(flaky)
    action = make_action(target_platform="fake", operation="op", payload={}, max_retries=3)
    result = await adapter.execute(action)
    assert result.status == "ok"
    assert adapter.calls == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_business_failure_is_not_retried() -> None:
    async def biz_fail(self, action) -> ActionResult:
        async def inner():
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used="api",
                result_payload={},
                latency_ms=1.0,
                error="real business failure",
            )

        return await inner()

    adapter = _Adapter(biz_fail)
    action = make_action(target_platform="fake", operation="op", payload={}, max_retries=5)
    result = await adapter.execute(action)
    assert result.status == "failed"
    assert adapter.calls == 1  # NOT retried — definitive outcome


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_is_single_attempt() -> None:
    adapter = _Adapter(_ok)
    action = make_action(target_platform="fake", operation="op", payload={})  # max_retries=1
    result = await adapter.execute(action)
    assert result.status == "ok"
    assert adapter.calls == 1
