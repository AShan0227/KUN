"""微信公众号 Browser Adapter — Playwright admin panel 操作 (ADR-026, L6.D-内容分发).

后台: https://mp.weixin.qq.com/

实现思路:
  - 登录: 扫码 + cookie injection (生产 KUN auth subsystem 管)
  - 各 operation 通过页面表单 + 点击完成
  - 解析结果: 等待 success indicator 出现, 或抓 url 变化

⚠️ 不引 Playwright 真依赖 — page_factory 注入式, 单测用 fake.
   真要上 prod 时, 行业接入工程师必须:
     1. 提供已登录的 page_factory (注入 wx.qq.com session cookie)
     2. 用 Playwright Inspector 录制最新 selectors (微信 UI 经常改)
     3. 实现重试 + CAPTCHA 处理 (微信偶尔弹验证码)

ADR-025 提示:
  - 微信发布有"群发次数上限" — 订阅号 1次/日, 服务号 4次/月
  - publish_article 应该 idempotent / 可保存草稿不直接群发, 由 caller 决定 publish_now
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

log = get_logger("kun.interface.automation.wechat_mp.browser")


# operation → URL path (相对 https://mp.weixin.qq.com)
_OPERATION_PATHS: dict[str, str] = {
    "publish_article": "/cgi-bin/appmsg?t=media/appmsg_edit_v2",
    "list_articles": "/cgi-bin/appmsg",
    "get_article_stats": "/cgi-bin/readtemplate?t=appmsgstat&id={article_id}",
}


# DOM selectors — 占位; 真值需 Playwright Inspector 录制后维护
# 微信 UI 变更频繁, selectors 应当走 capability_card 自愈
_SELECTORS: dict[str, dict[str, str]] = {
    "publish_article": {
        "title_input": 'input[name="title"]',
        "author_input": 'input[name="author"]',
        # 微信富文本编辑器在 iframe 内, 真生产用 frame_locator
        "content_iframe": "iframe.rich_edit_iframe",
        "content_body": "body[contenteditable=true]",
        "digest_input": 'textarea[name="digest"]',
        "save_draft_btn": '.js_save',
        "publish_now_btn": '.js_send',
        "success_indicator": '.weui-toast.weui-toast_success',
    },
    "list_articles": {
        "page_loaded": ".weui-desktop-mass-appmsg__list",
        "article_rows": ".weui-desktop-mass-appmsg-item",
    },
    "get_article_stats": {
        "page_loaded": ".appmsg-stat-page",
        "read_count": '[data-key="read_num"]',
        "like_count": '[data-key="like_num"]',
        "share_count": '[data-key="share_num"]',
    },
}


class WeChatMPBrowserAdapter(BrowserAdapter):
    """微信公众号后台 browser adapter."""

    platform = "wechat_mp"
    supported_operations: ClassVar[set[str]] = set(_OPERATION_PATHS.keys())

    def __init__(
        self,
        *,
        base_url: str = "https://mp.weixin.qq.com",
        page_factory: PageFactory | None = None,
        default_timeout_sec: float = 60.0,  # 微信加载慢, 默认更长
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

        # 等页面 ready
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
                    "wechat_mp_browser.page_not_loaded",
                    operation=op,
                    error=str(e),
                )

        # ---- operation-specific ----

        if op == "publish_article":
            return await self._publish_article(action, page, selectors)

        if op == "list_articles":
            return ActionResult(
                action_id=action.action_id,
                status="ok",
                kind_used="browser",
                result_payload={
                    "navigated_to": url,
                    "page_loaded": page_loaded_sel is not None,
                },
                latency_ms=0.0,
                rationale="wechat_mp browser list_articles navigated",
            )

        if op == "get_article_stats":
            return ActionResult(
                action_id=action.action_id,
                status="ok",
                kind_used="browser",
                result_payload={
                    "navigated_to": url,
                    "article_id": action.payload.get("article_id", ""),
                },
                latency_ms=0.0,
                rationale="wechat_mp browser get_article_stats navigated",
            )

        return ActionResult(
            action_id=action.action_id,
            status="failed",
            kind_used="browser",
            result_payload={},
            latency_ms=0.0,
            error=f"operation handler not implemented: {op}",
        )

    async def _publish_article(
        self,
        action: Action,
        page: BrowserPage,
        selectors: dict[str, str],
    ) -> ActionResult:
        """填充图文消息表单 → 保存草稿 (默认) 或群发 (when publish_now=True).

        Payload 字段:
          - title:         必填, 文章标题
          - content_html:  必填, 文章正文 (HTML)
          - author:        选填, 作者
          - digest:        选填, 摘要 (≤120 字符)
          - publish_now:   选填, 默认 False (只存草稿). True 时点群发.

        默认存草稿: 微信群发次数有限制 + 不可撤回, caller 不显式 publish_now
        就只入草稿, 给人工二次确认机会. (ADR-025 conservative pattern.)
        """
        payload = action.payload
        title = payload.get("title", "")
        content_html = payload.get("content_html", "")
        author = payload.get("author", "")
        digest = payload.get("digest", "")
        publish_now = bool(payload.get("publish_now", False))

        if not title or not content_html:
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used="browser",
                result_payload={},
                latency_ms=0.0,
                error="title and content_html are required",
            )

        try:
            # 1. 标题
            if selectors.get("title_input"):
                await page.fill(selectors["title_input"], title)
            # 2. 作者 (选填)
            if author and selectors.get("author_input"):
                await page.fill(selectors["author_input"], author)
            # 3. 摘要 (选填) — 截 120 字符 (微信上限)
            if digest and selectors.get("digest_input"):
                await page.fill(selectors["digest_input"], digest[:120])
            # 4. 正文 (富文本) — 真生产用 frame_locator + page.evaluate, 这里简化
            #    占位: 假设 page 支持向 contenteditable body fill
            if selectors.get("content_body"):
                await page.fill(selectors["content_body"], content_html)
            # 5. 保存草稿 / 群发
            target_btn = (
                selectors.get("publish_now_btn")
                if publish_now
                else selectors.get("save_draft_btn")
            )
            if target_btn:
                await page.click(target_btn)
            # 6. 等成功 toast
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
                    "published": publish_now,
                    "draft_saved": not publish_now,
                },
                latency_ms=0.0,
                rationale=(
                    "wechat_mp publish_now succeeded"
                    if publish_now
                    else "wechat_mp draft saved (publish_now=False)"
                ),
            )
        except Exception as e:
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used="browser",
                result_payload={},
                latency_ms=0.0,
                error=f"publish_article flow failed: {e}",
            )

    async def health_check(self) -> bool:
        """探活: 能创建 page + goto MP login 即可."""
        if self._page_factory is None:
            return False
        try:
            page = await self._page_factory()
            await page.goto(f"{self._base_url}/")
            await page.close()
            return True
        except Exception as e:
            log.warning("wechat_mp_browser.health_check_failed", error=str(e))
            return False


__all__ = ["WeChatMPBrowserAdapter"]
