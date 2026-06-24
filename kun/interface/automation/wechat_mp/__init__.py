"""微信公众号 (WeChat Official Account) automation (L6.D-内容分发).

微信公众号特点 (相对 Shopify):
  - 无稳定官方写 API (订阅号 API 受限严; 服务号要审核)
  - 全 Browser path 强制 (除非企业有特许 OpenAPI 接入)
  - 登录: 扫码 + cookie session 持久化 (生产由 KUN auth + cookie injection 管)
  - 反爬: DOM 选择器易变 — 需 Playwright Inspector 长期维护

Operations:
  - publish_article: 发布图文消息 (草稿 → 群发)
  - list_articles: 列已发布文章
  - get_article_stats: 查文章数据 (阅读 / 点赞 / 在看)
"""

from kun.interface.automation.wechat_mp.browser import WeChatMPBrowserAdapter

__all__ = ["WeChatMPBrowserAdapter"]
