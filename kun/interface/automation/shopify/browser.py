"""Shopify Browser Adapter — Playwright admin panel 操作 (ADR-026).

Browser fallback path. 当 API 不可用 / 操作没 API 时走这条.

实现思路:
  - admin panel: https://<shop>.myshopify.com/admin
  - 登录: OAuth + cookie session (生产由 KUN auth subsystem 管, 注入 cookie)
  - 各 operation 走相应页面 → fill form → submit → parse result

⚠️ 不引 Playwright 真依赖 — page_factory 注入式, 单测用 fake.
真要上 prod 时, 行业接入工程师补 page_factory 实装即可.
"""

from __future__ import annotations

from typing import Any, ClassVar

from kun.core.logging import get_logger
from kun.interface.automation.base import Action, ActionResult
from kun.interface.automation.browser_base import (
    BrowserAdapter,
    BrowserPage,
    PageFactory,
)

log = get_logger("kun.interface.automation.shopify.browser")


# operation → URL path + 操作描述
_OPERATION_PATHS: dict[str, str] = {
    "create_product": "/admin/products/new",
    "list_orders": "/admin/orders",
    "get_product": "/admin/products/{product_id}",
}


# DOM selector — 真实场景需要 Playwright Inspector 录制 + 长期维护
# 这里是占位 selector, 行业上线时填真值
_SELECTORS: dict[str, dict[str, str]] = {
    "create_product": {
        "title_input": 'input[name="product[title]"]',
        "body_input": 'textarea[name="product[body_html]"]',
        "save_btn": 'button[type="submit"][value="save"]',
        "success_indicator": '[data-saved="true"]',
    },
    "list_orders": {
        "order_rows": "tr[data-order-id]",
        "page_loaded": "h1.admin-page-title",
    },
    "get_product": {
        "product_title": "h1.product-title",
        "page_loaded": '[data-product-id]',
    },
}


class ShopifyBrowserAdapter(BrowserAdapter):
    """Shopify admin panel browser adapter."""

    platform = "shopify"
    supported_operations: ClassVar[set[str]] = set(_OPERATION_PATHS.keys())

    def __init__(
        self,
        *,
        shop_domain: str = "demo-shop.myshopify.com",
        page_factory: PageFactory | None = None,
        default_timeout_sec: float = 30.0,
    ) -> None:
        super().__init__(
            page_factory=page_factory, default_timeout_sec=default_timeout_sec
        )
        self._shop_domain = shop_domain

    def _build_url(self, op: str, payload: dict[str, Any]) -> str:
        path = _OPERATION_PATHS[op]
        for key, value in payload.items():
            placeholder = "{" + key + "}"
            if placeholder in path:
                path = path.replace(placeholder, str(value))
        return f"https://{self._shop_domain}{path}"

    async def _do_execute(
        self, action: Action, *, page: BrowserPage
    ) -> ActionResult:
        op = action.operation
        if op not in _OPERATION_PATHS:
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used="browser",
                result_payload={},
                latency_ms=0.0,
                error=f"unsupported operation: {op}",
            )

        url = self._build_url(op, action.payload)
        selectors = _SELECTORS.get(op, {})

        try:
            await page.goto(url, timeout=action.timeout_sec * 1000)
        except Exception as e:
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used="browser",
                result_payload={},
                latency_ms=0.0,
                error=f"goto failed: {e}",
            )

        # 等页面 ready (loaded indicator)
        page_loaded_sel = selectors.get("page_loaded") or selectors.get("success_indicator")
        if page_loaded_sel:
            try:
                await page.wait_for_selector(
                    page_loaded_sel, timeout=action.timeout_sec * 1000
                )
            except Exception as e:
                log.warning(
                    "shopify_browser.page_not_loaded",
                    operation=op,
                    error=str(e),
                )

        # operation-specific action
        if op == "create_product":
            try:
                title = action.payload.get("title", "")
                body = action.payload.get("body_html", "")
                if selectors.get("title_input"):
                    await page.fill(selectors["title_input"], title)
                if selectors.get("body_input") and body:
                    await page.fill(selectors["body_input"], body)
                if selectors.get("save_btn"):
                    await page.click(selectors["save_btn"])
                # 等成功 indicator
                if selectors.get("success_indicator"):
                    await page.wait_for_selector(
                        selectors["success_indicator"],
                        timeout=action.timeout_sec * 1000,
                    )
                return ActionResult(
                    action_id=action.action_id,
                    status="ok",
                    kind_used="browser",
                    result_payload={"created": True, "title": title},
                    latency_ms=0.0,
                    rationale="shopify browser create_product succeeded",
                )
            except Exception as e:
                return ActionResult(
                    action_id=action.action_id,
                    status="failed",
                    kind_used="browser",
                    result_payload={},
                    latency_ms=0.0,
                    error=f"create_product flow failed: {e}",
                )

        elif op == "list_orders":
            # 仅 navigate + 探活, 真 list 需要 page.locator(...).all() 在子类扩展
            return ActionResult(
                action_id=action.action_id,
                status="ok",
                kind_used="browser",
                result_payload={"navigated_to": url, "page_loaded": page_loaded_sel is not None},
                latency_ms=0.0,
                rationale="shopify browser list_orders navigated",
            )

        elif op == "get_product":
            return ActionResult(
                action_id=action.action_id,
                status="ok",
                kind_used="browser",
                result_payload={
                    "navigated_to": url,
                    "product_id": action.payload.get("product_id", ""),
                },
                latency_ms=0.0,
                rationale="shopify browser get_product navigated",
            )

        return ActionResult(
            action_id=action.action_id,
            status="failed",
            kind_used="browser",
            result_payload={},
            latency_ms=0.0,
            error=f"operation handler not implemented: {op}",
        )

    async def health_check(self) -> bool:
        """探活: 能创建 page + goto admin login 即可."""
        if self._page_factory is None:
            return False
        try:
            page = await self._page_factory()
            await page.goto(f"https://{self._shop_domain}/admin/login")
            await page.close()
            return True
        except Exception as e:
            log.warning("shopify_browser.health_check_failed", error=str(e))
            return False


__all__ = ["ShopifyBrowserAdapter"]
