"""Automation 适配器层 (L6 / ADR-026) — KUN 操作他人 SaaS 的接入层.

与 kun/interface/adapters/ 区分:
  - adapters/ 是 **输出翻译** (KUN 内部 → A2A / email / REST / markdown)
  - automation/ 是 **动作执行** (KUN 操作 Shopify / Meta Ads / 微信公众号 / Salesforce)

Browser-first hybrid 架构:
  - BrowserAdapter (主路径) — Playwright + DOM 自愈, 覆盖度最大化
  - APIAdapter (加速路径) — 官方 API 有就走 API (快 10x), 缺时 fallback browser
  - AdapterRouter — capability_card 驱动选 API 还是 browser
  - AdapterRegistry — 注册 (platform, operation) → adapter

4 个行业 (电商/投放/内容分发/CRM) 共用同一 framework, 行业 specific adapter
plug-in 进 Registry.

  Industry Selected → AdapterRouter.execute(Action(...))
                       ↓
                  capability_card 看 (target, operation) API 成功率
                       ↓
              ┌────────┴────────┐
              ↓                 ↓
        API Adapter        Browser Adapter
        (Shopify / Meta)   (Playwright + DOM 自愈)
"""

from kun.interface.automation.api_base import APIAdapter, HttpCaller, make_action
from kun.interface.automation.base import (
    Action,
    ActionResult,
    AutomationAdapter,
    AutomationKind,
)
from kun.interface.automation.browser_base import (
    BrowserAdapter,
    BrowserPage,
    PageFactory,
)
from kun.interface.automation.registry import AdapterRegistry
from kun.interface.automation.router import (
    AdapterRouter,
    AdapterSelection,
    RouterDecision,
)

__all__ = [
    "APIAdapter",
    "Action",
    "ActionResult",
    "AdapterRegistry",
    "AdapterRouter",
    "AdapterSelection",
    "AutomationAdapter",
    "AutomationKind",
    "BrowserAdapter",
    "BrowserPage",
    "HttpCaller",
    "PageFactory",
    "RouterDecision",
    "make_action",
]
