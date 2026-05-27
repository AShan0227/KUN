"""L6.D-内容分发 — 小红书 BrowserAdapter 单测."""

from __future__ import annotations

import pytest
from kun.interface.automation import (
    AdapterRegistry,
    AdapterRouter,
    make_action,
)
from kun.interface.automation.xiaohongshu import XiaohongshuBrowserAdapter

# ---- Fakes ----


class FakePage:
    """Minimal BrowserPage fake recording all actions."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self.actions: list[tuple[str, ...]] = []
        self.closed = False
        self._fail_on = fail_on

    async def goto(self, url: str, *, timeout: float | None = None) -> None:  # noqa: ASYNC109
        if self._fail_on == "goto":
            raise RuntimeError("anti-bot challenge: slider")
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
    b = XiaohongshuBrowserAdapter()
    assert "publish_note" in b.supported_operations
    assert "list_notes" in b.supported_operations
    assert "get_note_stats" in b.supported_operations
    assert b.platform == "xiaohongshu"


# ---- publish_note ----


@pytest.mark.asyncio
async def test_publish_note_saves_draft_by_default() -> None:
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = XiaohongshuBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="publish_note",
            payload={
                "title": "KUN AgentOS 体验",
                "content": "今天试用了 KUN, 体验很流畅...",
                "topics": ["#KUN", "#AgentOS"],
                "location": "北京",
                # publish_now 默认 False
            },
        )
    )
    assert result.status == "ok"
    assert result.result_payload["draft_saved"] is True
    assert result.result_payload["published"] is False
    assert result.result_payload["note_type"] == "image_text"
    assert result.result_payload["topics_applied"] == ["#KUN", "#AgentOS"]
    page = pages[0]
    fills = [a for a in page.actions if a[0] == "fill"]
    # title + content + 2 topics + location = 5 fill
    assert len(fills) >= 4  # 至少 title + content + 1 topic + location
    # 应该点 save_draft 不是 publish
    clicks = [a for a in page.actions if a[0] == "click"]
    assert any("draft-btn" in a[1] for a in clicks)
    assert not any("publish-btn" in a[1] for a in clicks if "draft" not in a[1])
    assert page.closed is True


@pytest.mark.asyncio
async def test_publish_note_publish_now_clicks_publish() -> None:
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = XiaohongshuBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="publish_note",
            payload={
                "title": "Real publish",
                "content": "正式发布",
                "publish_now": True,
            },
        )
    )
    assert result.status == "ok"
    assert result.result_payload["published"] is True
    assert result.result_payload["draft_saved"] is False
    page = pages[0]
    clicks = [a for a in page.actions if a[0] == "click"]
    # publish-btn (而非 draft-btn) 被点
    publish_clicks = [a for a in clicks if "publish-btn" in a[1]]
    assert len(publish_clicks) >= 1


@pytest.mark.asyncio
async def test_publish_note_video_switches_tab() -> None:
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = XiaohongshuBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="publish_note",
            payload={
                "title": "Video note",
                "content": "视频笔记内容",
                "note_type": "video",
            },
        )
    )
    assert result.status == "ok"
    assert result.result_payload["note_type"] == "video"
    page = pages[0]
    clicks = [a for a in page.actions if a[0] == "click"]
    # 应该 click video tab
    assert any("video" in a[1] for a in clicks)


@pytest.mark.asyncio
async def test_publish_note_invalid_note_type_fails() -> None:
    b = XiaohongshuBrowserAdapter(page_factory=fake_page_factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="publish_note",
            payload={
                "title": "x",
                "content": "y",
                "note_type": "livestream",  # 不支持
            },
        )
    )
    assert result.status == "failed"
    assert "invalid note_type" in (result.error or "")


@pytest.mark.asyncio
async def test_publish_note_missing_title_fails() -> None:
    b = XiaohongshuBrowserAdapter(page_factory=fake_page_factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="publish_note",
            payload={"content": "no title"},
        )
    )
    assert result.status == "failed"
    assert "title and content" in (result.error or "")


@pytest.mark.asyncio
async def test_publish_note_missing_content_fails() -> None:
    b = XiaohongshuBrowserAdapter(page_factory=fake_page_factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="publish_note",
            payload={"title": "no content"},
        )
    )
    assert result.status == "failed"
    assert "content" in (result.error or "")


@pytest.mark.asyncio
async def test_publish_note_fill_failure_returns_failed() -> None:
    async def factory() -> FakePage:
        return FakePage(fail_on="fill")

    b = XiaohongshuBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="publish_note",
            payload={"title": "x", "content": "y"},
        )
    )
    assert result.status == "failed"
    assert "publish_note flow failed" in (result.error or "")
    # 错误 payload 应保留 title 以利诊断
    assert result.result_payload.get("title") == "x"


@pytest.mark.asyncio
async def test_publish_note_empty_topics_handled() -> None:
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = XiaohongshuBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="publish_note",
            payload={
                "title": "No topics",
                "content": "content",
                "topics": [],  # 显式空 list
            },
        )
    )
    assert result.status == "ok"
    assert result.result_payload["topics_applied"] == []


# ---- list_notes ----


@pytest.mark.asyncio
async def test_list_notes_navigates() -> None:
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = XiaohongshuBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="list_notes",
            payload={},
        )
    )
    assert result.status == "ok"
    assert "notemanage" in result.result_payload["navigated_to"]
    assert pages[0].closed is True


# ---- get_note_stats ----


@pytest.mark.asyncio
async def test_get_note_stats_substitutes_id() -> None:
    pages: list[FakePage] = []

    async def factory() -> FakePage:
        p = FakePage()
        pages.append(p)
        return p

    b = XiaohongshuBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="get_note_stats",
            payload={"note_id": "65abcdef"},
        )
    )
    assert result.status == "ok"
    assert "65abcdef" in result.result_payload["navigated_to"]
    assert result.result_payload["note_id"] == "65abcdef"


# ---- error paths ----


@pytest.mark.asyncio
async def test_unsupported_operation_returns_failed() -> None:
    b = XiaohongshuBrowserAdapter(page_factory=fake_page_factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="livestream_start",
            payload={},
        )
    )
    assert result.status == "failed"
    assert "unsupported operation" in (result.error or "")


@pytest.mark.asyncio
async def test_goto_failure_returns_failed() -> None:
    """例如 anti-bot challenge 触发 → goto 抛 → return failed."""
    async def factory() -> FakePage:
        return FakePage(fail_on="goto")

    b = XiaohongshuBrowserAdapter(page_factory=factory)
    result = await b.execute(
        make_action(
            target_platform="xiaohongshu",
            operation="list_notes",
            payload={},
        )
    )
    assert result.status == "failed"
    assert "goto failed" in (result.error or "")
    # 反爬挑战体现在错误信息里
    assert "anti-bot" in (result.error or "") or "slider" in (result.error or "")


# ---- health_check ----


@pytest.mark.asyncio
async def test_health_check_with_factory() -> None:
    b = XiaohongshuBrowserAdapter(page_factory=fake_page_factory)
    assert await b.health_check() is True


@pytest.mark.asyncio
async def test_health_check_no_factory() -> None:
    b = XiaohongshuBrowserAdapter(page_factory=None)
    assert await b.health_check() is False


# ---- Router integration ----


@pytest.mark.asyncio
async def test_router_registers_and_dispatches_xiaohongshu() -> None:
    reg = AdapterRegistry()
    b = XiaohongshuBrowserAdapter(page_factory=fake_page_factory)
    reg.register(b)
    router = AdapterRouter(reg)
    decision = await router.route_and_execute(
        make_action(
            target_platform="xiaohongshu",
            operation="list_notes",
            payload={},
        )
    )
    assert decision.result is not None
    assert decision.result.status == "ok"
    assert decision.result.kind_used == "browser"
    assert decision.fallback_used is False


@pytest.mark.asyncio
async def test_default_timeout_longer_than_shopify() -> None:
    """小红书 timeout 应比 Shopify 长 (上传 + 风控审核慢)."""
    b = XiaohongshuBrowserAdapter()
    # 默认 90s, 比 Shopify 30s 长 (反映实际平台特性)
    assert b._default_timeout >= 60.0
