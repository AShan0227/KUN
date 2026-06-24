"""L6.D-内容分发 — 微信公众号 BrowserAdapter 单测."""

from __future__ import annotations

import pytest
from kun.interface.automation import (
    AdapterRegistry,
    AdapterRouter,
    make_action,
)
from kun.interface.automation.wechat_mp import WeChatMPBrowserAdapter

# ---- Fakes ----


class FakePage:
    """Minimal BrowserPage fake recording all actions."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.actions: list[tuple[str, ...]] = []
        self.closed = False
        self._fail_on = fail_on  # action name to raise on

    async def goto(self, url: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        if self._fail_on == "goto":
            raise RuntimeError("network error")
        self.actions.append(("goto", url))

    async def fill(self, selector: str, value: str) -> None:
        if self._fail_on == "fill":
            raise RuntimeError("element not found")
        self.actions.append(("fill", selector, value))

    async def click(self, selector: str) -> None:
        if self._fail_on == "click":
            raise RuntimeError("click intercepted")
        self.actions.append(("click", selector))

    async def wait_for_selector(
        self, selector: str, *, timeout: float | None = None  # noqa: ASYNC109
    ) -> None:
        if self._fail_on == "wait":
            raise TimeoutError("selector not found")
        self.actions.append(("wait", selector))

    async def screenshot(self, *, path: str | None = None) -> bytes:
        return b"png"

    async def close(self) -> None:
        self.closed = True


async def fake_page_factory() -> FakePage:
    return FakePage()


# ---- supported_operations / platform ----


def test_supported_operations() -> None:
    b = WeChatMPBrowserAdapter()
    assert "publish_article" in b.supported_operations
    assert "list_articles" in b.supported_operations
    assert "get_article_stats" in b.supported_operations
    assert b.platform == "wechat_mp"


# ---- publish_article ----


@pytest.mark.asyncio
async def test_publish_article_saves_draft_by_default() -> None:
    """publish_now 不传 → 默认存草稿 (微信群发次数有限制, ADR-025 conservative)."""
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = WeChatMPBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="wechat_mp",
            operation="publish_article",
            payload={
                "title": "KUN 内测公告",
                "content_html": "<p>测试内容</p>",
                "author": "小鲲",
                "digest": "一句话摘要",
                # publish_now 不传 → 默认 False
            },
        )
    )
    assert result.status == "ok"
    assert result.result_payload["draft_saved"] is True
    assert result.result_payload["published"] is False
    page = pages[0]
    # 应该 fill title + content + author + digest
    fills = [a for a in page.actions if a[0] == "fill"]
    assert any(a[2] == "KUN 内测公告" for a in fills)
    assert any(a[2] == "<p>测试内容</p>" for a in fills)
    assert any(a[2] == "小鲲" for a in fills)
    # 应该 click save_draft 而非 publish_now
    clicks = [a for a in page.actions if a[0] == "click"]
    assert any("js_save" in a[1] for a in clicks)
    assert not any("js_send" in a[1] for a in clicks)
    assert page.closed is True


@pytest.mark.asyncio
async def test_publish_article_publish_now_true_clicks_send() -> None:
    """publish_now=True → 点群发按钮."""
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = WeChatMPBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="wechat_mp",
            operation="publish_article",
            payload={
                "title": "正式发布",
                "content_html": "<p>正文</p>",
                "publish_now": True,
            },
        )
    )
    assert result.status == "ok"
    assert result.result_payload["published"] is True
    assert result.result_payload["draft_saved"] is False
    page = pages[0]
    clicks = [a for a in page.actions if a[0] == "click"]
    assert any("js_send" in a[1] for a in clicks)


@pytest.mark.asyncio
async def test_publish_article_missing_title_fails() -> None:
    b = WeChatMPBrowserAdapter(page_factory=fake_page_factory)
    result = await b.execute(
        make_action(
            target_platform="wechat_mp",
            operation="publish_article",
            payload={"content_html": "<p>x</p>"},  # 缺 title
        )
    )
    assert result.status == "failed"
    assert "title and content_html" in (result.error or "")


@pytest.mark.asyncio
async def test_publish_article_missing_content_fails() -> None:
    b = WeChatMPBrowserAdapter(page_factory=fake_page_factory)
    result = await b.execute(
        make_action(
            target_platform="wechat_mp",
            operation="publish_article",
            payload={"title": "no content"},  # 缺 content_html
        )
    )
    assert result.status == "failed"
    assert "content_html" in (result.error or "")


@pytest.mark.asyncio
async def test_publish_article_digest_truncated_to_120_chars() -> None:
    """微信摘要上限 120 字, 应该自动截断."""
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = WeChatMPBrowserAdapter(page_factory=factory)
    long_digest = "x" * 200
    await b.execute(
        make_action(
            target_platform="wechat_mp",
            operation="publish_article",
            payload={
                "title": "title",
                "content_html": "<p>c</p>",
                "digest": long_digest,
            },
        )
    )
    page = pages[0]
    digest_fills = [
        a for a in page.actions if a[0] == "fill" and "digest" in a[1]
    ]
    # 应该被截到 120
    assert len(digest_fills) == 1
    assert len(digest_fills[0][2]) == 120


@pytest.mark.asyncio
async def test_publish_article_fill_failure_returns_failed() -> None:
    async def factory() -> FakePage:
        return FakePage(fail_on="fill")

    b = WeChatMPBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="wechat_mp",
            operation="publish_article",
            payload={"title": "x", "content_html": "<p>c</p>"},
        )
    )
    assert result.status == "failed"
    assert "publish_article flow failed" in (result.error or "")


# ---- list_articles ----


@pytest.mark.asyncio
async def test_list_articles_navigates() -> None:
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = WeChatMPBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="wechat_mp",
            operation="list_articles",
            payload={},
        )
    )
    assert result.status == "ok"
    assert "appmsg" in result.result_payload["navigated_to"]
    assert pages[0].closed is True


# ---- get_article_stats ----


@pytest.mark.asyncio
async def test_get_article_stats_substitutes_id() -> None:
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = WeChatMPBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="wechat_mp",
            operation="get_article_stats",
            payload={"article_id": "abc_xyz_123"},
        )
    )
    assert result.status == "ok"
    assert "abc_xyz_123" in result.result_payload["navigated_to"]
    assert result.result_payload["article_id"] == "abc_xyz_123"


# ---- error paths ----


@pytest.mark.asyncio
async def test_unsupported_operation_returns_failed() -> None:
    b = WeChatMPBrowserAdapter(page_factory=fake_page_factory)
    result = await b.execute(
        make_action(
            target_platform="wechat_mp",
            operation="this_does_not_exist",
            payload={},
        )
    )
    assert result.status == "failed"
    assert "unsupported operation" in (result.error or "")


@pytest.mark.asyncio
async def test_goto_failure_returns_failed() -> None:
    async def factory() -> FakePage:
        return FakePage(fail_on="goto")

    b = WeChatMPBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="wechat_mp",
            operation="list_articles",
            payload={},
        )
    )
    assert result.status == "failed"
    assert "goto failed" in (result.error or "")


# ---- health_check ----


@pytest.mark.asyncio
async def test_health_check_with_factory() -> None:
    b = WeChatMPBrowserAdapter(page_factory=fake_page_factory)
    assert await b.health_check() is True


@pytest.mark.asyncio
async def test_health_check_no_factory() -> None:
    b = WeChatMPBrowserAdapter(page_factory=None)
    assert await b.health_check() is False


# ---- Router integration ----


@pytest.mark.asyncio
async def test_router_registers_and_dispatches_wechat_mp() -> None:
    reg = AdapterRegistry()
    b = WeChatMPBrowserAdapter(page_factory=fake_page_factory)
    reg.register(b)
    router = AdapterRouter(reg)
    decision = await router.route_and_execute(
        make_action(
            target_platform="wechat_mp",
            operation="list_articles",
            payload={},
        )
    )
    assert decision.result is not None
    assert decision.result.status == "ok"
    assert decision.result.kind_used == "browser"
    assert decision.fallback_used is False
