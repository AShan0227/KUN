"""Shopify platform adapters (L6.D / ADR-026).

第一个真实 platform adapter — 作 4 行业 pattern 的样板. 国内电商
(淘宝 / 京东 / 拼多多 / 小红书电商) 复用同一架构, 仅换 selector + endpoint.

包含:
  - api.py:     ShopifyAPIAdapter — Shopify Admin REST API (OAuth bearer)
  - browser.py: ShopifyBrowserAdapter — Playwright admin panel 操作

3 个 baseline operations (足够验证 pattern):
  - create_product: POST /admin/api/2024-10/products.json
  - list_orders:    GET /admin/api/2024-10/orders.json?status=any
  - get_product:    GET /admin/api/2024-10/products/{id}.json
"""

from kun.interface.automation.shopify.api import ShopifyAPIAdapter
from kun.interface.automation.shopify.browser import ShopifyBrowserAdapter

__all__ = ["ShopifyAPIAdapter", "ShopifyBrowserAdapter"]
