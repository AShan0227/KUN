"""APIAdapter base — HTTP-based adapter scaffold (ADR-026).

行业接入时, 继承 APIAdapter, 实现 _do_execute 调具体 SaaS API.
通用部分 (timeout / retry / auth / health_check) 由 base 处理.
"""

from __future__ import annotations

import asyncio
import time
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from kun.core.ids import new_id
from kun.core.logging import get_logger
from kun.interface.automation.base import (
    Action,
    ActionResult,
    AutomationAdapter,
)

log = get_logger("kun.interface.automation.api_base")


HttpCaller = Callable[[str, str, dict[str, Any] | None], Awaitable[dict[str, Any]]]
"""HTTP caller signature: (method, url, body) → response dict.

production 接 httpx; 测试用 fake."""


class APIAdapter(AutomationAdapter):
    """官方 API 适配器 base. 子类实现 _do_execute."""

    kind = "api"
    platform: str  # 子类设
    supported_operations: set[str]  # 子类设

    def __init__(
        self,
        *,
        http_caller: HttpCaller | None = None,
        default_timeout_sec: float = 30.0,
    ) -> None:
        self._http_caller = http_caller
        self._default_timeout = default_timeout_sec

    @abstractmethod
    async def _do_execute(self, action: Action, *, http_caller: HttpCaller) -> ActionResult:
        """子类实现具体 API 调用逻辑.

        http_caller 由 base 注入 (生产 httpx, 测试 fake).
        """
        raise NotImplementedError

    async def execute(self, action: Action) -> ActionResult:
        """通用 execute — 调子类 _do_execute + timeout + retry + timing + error wrapping.

        Audit F094: honor the Action contract the base docstring promised.
        - ``timeout_sec`` bounds each attempt via ``asyncio.wait_for``.
        - ``max_retries`` is the max number of ATTEMPTS (default 1 → single attempt,
          i.e. unchanged behavior). We retry ONLY on timeout/exception (transient),
          never on a returned status="failed" ActionResult (a real business failure
          must surface immediately, not be re-run).

        Caveat: a retry re-invokes the external SaaS call, which is only safe for
        idempotent operations. Default max_retries=1 means callers opt in explicitly.
        """
        if self._http_caller is None:
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used=self.kind,
                result_payload={},
                latency_ms=0.0,
                error="no http_caller injected; production needs httpx",
            )
        attempts = max(1, action.max_retries)
        last_error = "unknown"
        for attempt in range(attempts):
            started = time.perf_counter()
            try:
                result = await asyncio.wait_for(
                    self._do_execute(action, http_caller=self._http_caller),
                    timeout=action.timeout_sec,
                )
            except TimeoutError:
                last_error = f"timeout after {action.timeout_sec}s"
                log.warning(
                    "api_adapter.execute_timeout",
                    platform=self.platform,
                    operation=action.operation,
                    attempt=attempt + 1,
                    timeout_sec=action.timeout_sec,
                )
                continue
            except Exception as e:
                last_error = str(e)
                log.warning(
                    "api_adapter.execute_failed",
                    platform=self.platform,
                    operation=action.operation,
                    attempt=attempt + 1,
                    error=str(e),
                )
                continue
            # Success path — a returned ActionResult (even status="failed") is a
            # definitive business outcome and is NOT retried.
            if result.latency_ms == 0.0:
                from dataclasses import replace

                result = replace(result, latency_ms=(time.perf_counter() - started) * 1000)
            return result
        # All attempts exhausted on transient failures.
        return ActionResult(
            action_id=action.action_id,
            status="failed",
            kind_used=self.kind,
            result_payload={},
            latency_ms=0.0,
            error=last_error,
        )

    async def health_check(self) -> bool:
        """默认: 任何子类没 override 时返回 True. 子类应实现真探活."""
        return True


def make_action(
    *,
    target_platform: str,
    operation: str,
    payload: dict[str, Any],
    tenant_id: str = "default",
    **kwargs: Any,
) -> Action:
    """便利函数: 构造 Action 不用手动给 action_id."""
    return Action(
        action_id=new_id("action") if "action_id" not in kwargs else kwargs.pop("action_id"),
        target_platform=target_platform,
        operation=operation,
        payload=payload,
        tenant_id=tenant_id,
        **kwargs,
    )


__all__ = ["APIAdapter", "HttpCaller", "make_action"]
