"""Shopify API Adapter — Shopify Admin REST API (ADR-026).

Auth: OAuth Access Token (shop-specific) — 注入 http_caller 时带上.
Endpoint: https://<shop>.myshopify.com/admin/api/2024-10/

设计原则:
  - 3 个 baseline operation 验证 pattern, 行业接入再扩
  - 不引 shopify-python-api SDK — 用通用 http_caller (与 KUN auth flow 解耦)
  - operation → endpoint 映射 dict, 加新 operation 只改 _OPERATION_MAP
"""

from __future__ import annotations

from typing import Any, ClassVar

from kun.core.logging import get_logger
from kun.interface.automation.api_base import APIAdapter, HttpCaller
from kun.interface.automation.base import Action, ActionResult

log = get_logger("kun.interface.automation.shopify.api")


# operation → (HTTP method, endpoint template, payload mapper)
# endpoint template 含 {var} 用 action.payload 字段填.
_OPERATION_MAP: dict[str, tuple[str, str]] = {
    "create_product": ("POST", "/admin/api/2024-10/products.json"),
    "list_orders": ("GET", "/admin/api/2024-10/orders.json"),
    "get_product": ("GET", "/admin/api/2024-10/products/{product_id}.json"),
}


def _build_create_product_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Shopify 要求 wrap 在 {"product": {...}}."""
    return {
        "product": {
            "title": payload.get("title", ""),
            "body_html": payload.get("body_html", ""),
            "vendor": payload.get("vendor", ""),
            "product_type": payload.get("product_type", ""),
            "tags": payload.get("tags", ""),
            "variants": payload.get("variants", []),
        }
    }


class ShopifyAPIAdapter(APIAdapter):
    """Shopify Admin REST API 适配器."""

    platform = "shopify"
    supported_operations: ClassVar[set[str]] = set(_OPERATION_MAP.keys())

    def __init__(
        self,
        *,
        shop_domain: str = "demo-shop.myshopify.com",
        access_token: str | None = None,
        http_caller: HttpCaller | None = None,
        default_timeout_sec: float = 30.0,
    ) -> None:
        super().__init__(
            http_caller=http_caller, default_timeout_sec=default_timeout_sec
        )
        self._shop_domain = shop_domain
        self._access_token = access_token

    def _build_url(self, endpoint_template: str, payload: dict[str, Any]) -> str:
        endpoint = endpoint_template
        # 简单 {var} 替换 (避免引 jinja2 等重型库)
        for key, value in payload.items():
            placeholder = "{" + key + "}"
            if placeholder in endpoint:
                endpoint = endpoint.replace(placeholder, str(value))
        return f"https://{self._shop_domain}{endpoint}"

    async def _do_execute(
        self, action: Action, *, http_caller: HttpCaller
    ) -> ActionResult:
        op = action.operation
        if op not in _OPERATION_MAP:
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used="api",
                result_payload={},
                latency_ms=0.0,
                error=f"unsupported operation: {op}",
            )

        method, endpoint_template = _OPERATION_MAP[op]
        url = self._build_url(endpoint_template, action.payload)

        # POST/PUT 时 wrap body; GET 不带 body
        body: dict[str, Any] | None = None
        if method == "POST" and op == "create_product":
            body = _build_create_product_body(action.payload)
        elif method == "POST":
            body = action.payload

        try:
            resp = await http_caller(method, url, body)
        except Exception as e:
            log.warning(
                "shopify_api.http_failed",
                operation=op,
                error=str(e),
            )
            return ActionResult(
                action_id=action.action_id,
                status="failed",
                kind_used="api",
                result_payload={},
                latency_ms=0.0,
                error=str(e),
            )

        # 解析 Shopify 标准响应
        status_code = resp.get("_status_code", 200) if isinstance(resp, dict) else 200
        if isinstance(status_code, int) and status_code >= 400:
            action_status = "failed"
            if status_code == 401 or status_code == 403:
                action_status = "auth_required"
            elif status_code == 429:
                action_status = "rate_limited"
            return ActionResult(
                action_id=action.action_id,
                status=action_status,
                kind_used="api",
                result_payload=resp,
                latency_ms=0.0,
                error=resp.get("errors") if isinstance(resp, dict) else str(resp),
                rationale=f"shopify api {op} returned {status_code}",
            )

        return ActionResult(
            action_id=action.action_id,
            status="ok",
            kind_used="api",
            result_payload=resp,
            latency_ms=0.0,
            rationale=f"shopify api {op} succeeded",
        )

    async def health_check(self) -> bool:
        """探活: 调 /admin/api/2024-10/shop.json 看是否 200."""
        if self._http_caller is None or self._access_token is None:
            return False
        try:
            resp = await self._http_caller(
                "GET",
                f"https://{self._shop_domain}/admin/api/2024-10/shop.json",
                None,
            )
            status = resp.get("_status_code", 200) if isinstance(resp, dict) else 200
            return status < 400
        except Exception:
            return False


__all__ = ["ShopifyAPIAdapter"]
