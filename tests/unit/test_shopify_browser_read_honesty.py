"""Shopify browser read ops don't fake success on a failed page load (audit F149).

list_orders / get_product used to return status="ok" purely from navigation, even
when the page-loaded selector never appeared — inflating AdapterRouter health. Now
a confirmed load failure returns "failed", and a successful nav is marked
navigation_only (base stub extracts no real data).
"""

from __future__ import annotations

import pytest
from kun.interface.automation import make_action
from kun.interface.automation.shopify import ShopifyBrowserAdapter


class _LoadFailsPage:
    def __init__(self) -> None:
        self.closed = False

    async def goto(self, url: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        return None

    async def fill(self, selector: str, value: str) -> None:
        return None

    async def click(self, selector: str) -> None:
        return None

    async def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        raise TimeoutError(f"selector never appeared: {selector}")

    async def screenshot(self, *, path: str | None = None) -> bytes:
        return b"png"

    async def close(self) -> None:
        self.closed = True


async def _failing_factory() -> _LoadFailsPage:
    return _LoadFailsPage()


class _LoadOkPage(_LoadFailsPage):
    async def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        return None  # page loaded


async def _ok_factory() -> _LoadOkPage:
    return _LoadOkPage()


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op,payload", [("list_orders", {}), ("get_product", {"product_id": "789"})]
)
async def test_read_op_fails_when_page_not_loaded(op: str, payload: dict) -> None:
    adapter = ShopifyBrowserAdapter(page_factory=_failing_factory)
    result = await adapter.execute(
        make_action(target_platform="shopify", operation=op, payload=payload)
    )
    assert result.status == "failed"
    assert "page load not confirmed" in (result.error or "")


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op,payload", [("list_orders", {}), ("get_product", {"product_id": "789"})]
)
async def test_read_op_marks_navigation_only_on_success(op: str, payload: dict) -> None:
    adapter = ShopifyBrowserAdapter(page_factory=_ok_factory)
    result = await adapter.execute(
        make_action(target_platform="shopify", operation=op, payload=payload)
    )
    assert result.status == "ok"
    assert result.result_payload.get("navigation_only") is True
    assert result.result_payload.get("page_loaded") is True
