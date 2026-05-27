"""Automation Adapter base — Action / ActionResult / Protocol (ADR-026).

KUN 操作外部 SaaS 的统一抽象. 不绑死具体平台 — 4 行业 (电商/投放/内容分发/CRM)
通过 Registry 注册各自的 adapter, Router 决定走 API 还是 Browser.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

AutomationKind = Literal["api", "browser"]
"""adapter 的执行方式 — API (官方接口) 或 Browser (Playwright 模拟)."""


ActionStatus = Literal["ok", "failed", "retry", "auth_required", "captcha", "rate_limited"]


@dataclass(frozen=True)
class Action:
    """单次外部操作请求.

    target_platform: shopify / meta_ads / wechat_mp / salesforce / ...
    operation:       上架商品 / 查订单 / 创建广告 / 发布文章 / ...
    payload:         operation 所需参数 (industry-specific)
    """

    action_id: str
    target_platform: str
    operation: str
    payload: dict[str, Any]
    tenant_id: str = "default"
    requested_kind: AutomationKind | None = None
    """显式要求 API 或 Browser (None 让 Router 决定)"""
    timeout_sec: float = 30.0
    max_retries: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True)
class ActionResult:
    """单次外部操作结果."""

    action_id: str
    status: ActionStatus
    kind_used: AutomationKind  # 实际走的 API 还是 Browser
    result_payload: dict[str, Any]
    latency_ms: float
    rationale: str = ""
    """成功 reason / 失败 reason / fallback reason"""
    error: str | None = None
    artifact_refs: list[dict[str, Any]] = field(default_factory=list)
    """截图 / 网络 trace / API response dump 的引用"""
    cost_usd: float = 0.0
    """API call cost (browser 通常 0); 用于 capability_card.cost dim"""


class AutomationAdapter(Protocol):
    """单 platform / 单 operation 的执行适配器.

    实现:
      - kind: api / browser
      - platform: shopify / meta_ads / wechat_mp / salesforce / ...
      - supported_operations: 该 adapter 能干啥操作集合
      - execute(action) → ActionResult
      - health_check() → bool (Router 周期性 ping 决定是否还可用)
    """

    kind: AutomationKind
    platform: str
    supported_operations: set[str]

    async def execute(self, action: Action) -> ActionResult:
        """执行 action, 返回结果."""
        ...

    async def health_check(self) -> bool:
        """快速探活. False → Router 短期 cooldown 不调."""
        ...


__all__ = [
    "Action",
    "ActionResult",
    "ActionStatus",
    "AutomationAdapter",
    "AutomationKind",
]
