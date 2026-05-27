"""小红书 Browser Adapter — Playwright creator panel 操作 (ADR-026, L6.D-内容分发).

后台: https://creator.xiaohongshu.com/

实现思路:
  - 登录: 扫码 + cookie injection (生产 KUN auth subsystem 管)
  - 笔记 = 标题 + 正文 + 图片 OR 视频 + 标签/话题/地点
  - 发布有"风控审核" — submit 后状态 pending, 不立即可见

⚠️ 不引 Playwright 真依赖 — page_factory 注入式, 单测用 fake.
   真要上 prod 时, 行业接入工程师必须:
     1. 提供已登录的 page_factory (注入 xiaohongshu.com session cookie)
     2. 应对 anti-bot challenge (slider captcha / rotating challenge)
     3. 用 Playwright Inspector 录制最新 selectors (小红书 UI 改版频率高)
     4. 实现"图片上传" — 真生产要走 page.set_input_files()

ADR-025 提示:
  - 小红书算法对"标题字符 + 标签 + 话题"敏感, payload 应包含这些字段
  - 视频 vs 图文走不同的创作流 (URL/selector 不同) — adapter 内分流
  - 默认存草稿 — 与 WeChat MP 同 conservative pattern
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

log = get_logger("kun.interface.automation.xiaohongshu.browser")


# operation → URL path (相对 https://creator.xiaohongshu.com)
_OPERATION_PATHS: dict[str, str] = {
    "publish_note": "/publish/publish",  # 图文 + 视频 共享入口
    "list_notes": "/creator/notemanage",
    "get_note_stats": "/data/note?noteId={note_id}",
}


# DOM selectors — 占位; 真值需 Playwright Inspector 录制
# 小红书前端用 react + 大量 css-in-js, selectors 易变, 应当走 capability 自愈
_SELECTORS: dict[str, dict[str, str]] = {
    "publish_note": {
        # 创作类型切换: 图文 / 视频 (默认图文)
        "tab_image_text": '[data-tab="image-text"]',
        "tab_video": '[data-tab="video"]',
        # 上传 (图片或视频) — 真生产用 page.set_input_files
        "upload_input": 'input[type="file"][accept*="image"]',
        "upload_input_video": 'input[type="file"][accept*="video"]',
        # 表单字段
        "title_input": 'input[placeholder*="标题"]',
        "content_textarea": 'div[contenteditable="true"][data-placeholder*="正文"]',
        "topic_input": 'input[placeholder*="话题"]',
        "location_input": 'input[placeholder*="地点"]',
        # 按钮
        "save_draft_btn": 'button.draft-btn',
        "publish_btn": 'button.publish-btn',
        # 反馈
        "success_indicator": '.publish-success',
        "captcha_modal": '.captcha-challenge',
    },
    "list_notes": {
        "page_loaded": ".note-management-table",
        "note_rows": ".note-item",
    },
    "get_note_stats": {
        "page_loaded": ".note-stat-panel",
        "view_count": '[data-stat="view"]',
        "like_count": '[data-stat="like"]',
        "favorite_count": '[data-stat="favorite"]',
        "comment_count": '[data-stat="comment"]',
    },
}


class XiaohongshuBrowserAdapter(BrowserAdapter):
    """小红书创作者后台 browser adapter."""

    platform = "xiaohongshu"
    supported_operations: ClassVar[set[str]] = set(_OPERATION_PATHS.keys())

    def __init__(
        self,
        *,
        base_url: str = "https://creator.xiaohongshu.com",
        page_factory: PageFactory | None = None,
        default_timeout_sec: float = 90.0,  # 上传 + 风控审核慢
    ) -> None:
        super().__init__(
            page_factory=page_factory, default_timeout_sec=default_timeout_sec
        )
        self._base_url = base_url.rstrip("/")

    def _build_url(self, op: str, payload: dict[str, Any]) -> str:
        path = _OPERATION_PATHS[op]
        for key, value in payload.items():
            placeholder = "{" + key + "}"
            if placeholder in path:
                path = path.replace(placeholder, str(value))
        return f"{self._base_url}{path}"

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

        page_loaded_sel = selectors.get("page_loaded") or selectors.get(
            "success_indicator"
        )
        if page_loaded_sel:
            try:
                await page.wait_for_selector(
                    page_loaded_sel, timeout=action.timeout_sec * 1000
                )
            except Exception as e:
                log.warning(
                    "xiaohongshu_browser.page_not_loaded",
                    operation=op,
                    error=str(e),
                )

        if op == "publish_note":
            return await self._publish_note(action, page, selectors)

        if op == "list_notes":
            return ActionResult(
                action_id=action.action_id,
                status="ok",
                kind_used="browser",
                result_payload={
                    "navigated_to": url,
                    "page_loaded": page_loaded_sel is not None,
                },
                latency_ms=0.0,
                rationale="xiaohongshu browser list_notes navigated",
            )

        if op == "get_note_stats":
            return ActionResult(
                action_id=action.action_id,
                status="ok",
                kind_used="browser",
                result_payload={
                    "navigated_to": url,
                    "note_id": action.payload.get("note_id", ""),
                },
                latency_ms=0.0,
                rationale="xiaohongshu browser get_note_stats navigated",
            )

        return ActionResult(
            action_id=action.action_id,
            status="failed",
            kind_used="browser",
            result_payload={},
            latency_ms=0.0,
            error=f"operation handler not implemented: {op}",
        )

    async def _publish_note(
        self,
        action: Action,
        page: BrowserPage,
        selectors: dict[str, str],
    ) -> ActionResult:
        """填充笔记表单 → 存草稿 (默认) 或发布.

        Payload 字段:
          - title:       必填, 笔记标题 (≤20 字符建议)
          - content:     必填, 正文
          - note_type:   选填, 'image_text' (默认) | 'video'
          - topics:      选填, list[str], 话题 (e.g. ["#KUN", "#AgentOS"])
          - location:    选填, 地点
          - publish_now: 选填, 默认 False (只存草稿)

        默认存草稿 (同 WeChat MP conservative pattern): 小红书审核机制不可逆,
        给人工二次确认机会.
        """
        payload = action.payload
        title = payload.get("title", "")
        content = payload.get("content", "")
        note_type = payload.get("note_type", "image_text")
        topics = payload.get("topics", []) or []
        location = payload.get("location", "")
        publish_now = bool(payload.get("publish_now", False))

        if not title or not content:
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used="browser",
                result_payload={},
                latency_ms=0.0,
                error="title and content are required",
            )
        if note_type not in ("image_text", "video"):
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used="browser",
                result_payload={},
                latency_ms=0.0,
                error=f"invalid note_type: {note_type!r} (must be image_text or video)",
            )

        try:
            # 1. 切类型 tab
            tab_sel = (
                selectors.get("tab_video")
                if note_type == "video"
                else selectors.get("tab_image_text")
            )
            if tab_sel:
                await page.click(tab_sel)
            # 2. 标题
            if selectors.get("title_input"):
                await page.fill(selectors["title_input"], title)
            # 3. 正文 (contenteditable)
            if selectors.get("content_textarea"):
                await page.fill(selectors["content_textarea"], content)
            # 4. 话题 (依次填入)
            if topics and selectors.get("topic_input"):
                for topic in topics:
                    await page.fill(selectors["topic_input"], topic)
            # 5. 地点
            if location and selectors.get("location_input"):
                await page.fill(selectors["location_input"], location)
            # 6. 存草稿 / 发布
            target_btn = (
                selectors.get("publish_btn")
                if publish_now
                else selectors.get("save_draft_btn")
            )
            if target_btn:
                await page.click(target_btn)
            # 7. 等成功 indicator
            if selectors.get("success_indicator"):
                await page.wait_for_selector(
                    selectors["success_indicator"],
                    timeout=action.timeout_sec * 1000,
                )
            return ActionResult(
                action_id=action.action_id,
                status="ok",
                kind_used="browser",
                result_payload={
                    "title": title,
                    "note_type": note_type,
                    "published": publish_now,
                    "draft_saved": not publish_now,
                    "topics_applied": list(topics),
                },
                latency_ms=0.0,
                rationale=(
                    "xiaohongshu publish_now submitted (审核 pending)"
                    if publish_now
                    else "xiaohongshu draft saved (publish_now=False)"
                ),
            )
        except Exception as e:
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used="browser",
                result_payload={"title": title, "note_type": note_type},
                latency_ms=0.0,
                error=f"publish_note flow failed: {e}",
            )

    async def health_check(self) -> bool:
        """探活: 能创建 page + goto creator panel."""
        if self._page_factory is None:
            return False
        try:
            page = await self._page_factory()
            await page.goto(f"{self._base_url}/")
            await page.close()
            return True
        except Exception as e:
            log.warning("xiaohongshu_browser.health_check_failed", error=str(e))
            return False


__all__ = ["XiaohongshuBrowserAdapter"]
