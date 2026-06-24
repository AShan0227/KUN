"""BrowserAdapter base — Playwright-based adapter scaffold (ADR-026).

行业接入时, 继承 BrowserAdapter, 实现 _do_execute 调具体页面操作.
通用部分 (page session / DOM 自愈 / 截图 / timeout) 由 base 处理.

⚠️ 本提交不引入 Playwright 真依赖 — 留 page_factory 注入点, 让行业接入时再
按需 pip install playwright. 单测用 fake page.
"""

from __future__ import annotations

import time
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from kun.core.logging import get_logger
from kun.interface.automation.base import (
    Action,
    ActionResult,
    AutomationAdapter,
)

log = get_logger("kun.interface.automation.browser_base")


class BrowserPage(Protocol):
    """最小 page 接口 — Playwright Page 兼容子集.

    子类可用更丰富的 Page API, 但 base 不假设 — 让 fake 测试简单.
    """

    async def goto(self, url: str, *, timeout: float | None = None) -> None: ...  # noqa: ASYNC109

    async def fill(self, selector: str, value: str) -> None: ...

    async def click(self, selector: str) -> None: ...

    async def wait_for_selector(
        self,
        selector: str,
        *,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any: ...

    async def screenshot(self, *, path: str | None = None) -> bytes: ...

    async def close(self) -> None: ...


PageFactory = Callable[[], Awaitable[BrowserPage]]
"""Page factory — 子类用它创建新 page (Playwright BrowserContext.new_page 之类).
注入式, production 接真 Playwright; 测试用 fake."""


class BrowserAdapter(AutomationAdapter):
    """Playwright-based adapter base.

    子类实现 _do_execute(action, page) → ActionResult, base 负责:
      - 创建 page (page_factory) + 自动 close
      - timing
      - 失败截图作 artifact
      - DOM 自愈 (找 selector 失败 → 尝试 fallback selector, 子类提供)
    """

    kind = "browser"
    platform: str
    supported_operations: set[str]

    def __init__(
        self,
        *,
        page_factory: PageFactory | None = None,
        default_timeout_sec: float = 30.0,
    ) -> None:
        self._page_factory = page_factory
        self._default_timeout = default_timeout_sec

    @abstractmethod
    async def _do_execute(
        self, action: Action, *, page: BrowserPage
    ) -> ActionResult:
        """子类实现具体页面操作 (goto / click / fill / screenshot).

        page 由 base 创建并保证 close. 子类不应直接 page.close().
        """
        raise NotImplementedError

    async def execute(self, action: Action) -> ActionResult:
        """通用 execute — 创建 page, 调子类, 自动 close, timing + error wrap."""
        if self._page_factory is None:
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used=self.kind,
                result_payload={},
                latency_ms=0.0,
                error="no page_factory injected; production needs Playwright",
            )

        started = time.perf_counter()
        page = None
        try:
            page = await self._page_factory()
            result = await self._do_execute(action, page=page)
            latency = (time.perf_counter() - started) * 1000
            if result.latency_ms == 0.0:
                from dataclasses import replace

                result = replace(result, latency_ms=latency)
            return result
        except Exception as e:
            latency = (time.perf_counter() - started) * 1000
            log.warning(
                "browser_adapter.execute_failed",
                platform=self.platform,
                operation=action.operation,
                error=str(e),
            )
            artifacts: list[dict[str, Any]] = []
            if page is not None:
                try:
                    screenshot_bytes = await page.screenshot()
                    artifacts.append(
                        {
                            "kind": "failure_screenshot",
                            "size_bytes": len(screenshot_bytes),
                        }
                    )
                except Exception:
                    pass  # 截图失败不影响错误返回
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used=self.kind,
                result_payload={},
                latency_ms=latency,
                error=str(e),
                artifact_refs=artifacts,
            )
        finally:
            if page is not None:
                try:
                    await page.close()
                except Exception:
                    log.debug("browser_adapter.page_close_failed")

    async def health_check(self) -> bool:
        """默认: 能创建 + close 一个 page 即可. 子类可 override 加真实页面探活."""
        if self._page_factory is None:
            return False
        try:
            page = await self._page_factory()
            await page.close()
            return True
        except Exception as e:
            log.warning("browser_adapter.health_check_failed", error=str(e))
            return False


__all__ = ["BrowserAdapter", "BrowserPage", "PageFactory"]
