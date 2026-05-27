"""电商行业 Shopify golden tasks (ADR-011 §冷启动校准 / ADR-026 §业务能力卡).

6 个标准任务覆盖电商 Shopify 基线能力. 行业上线时 KUN 跑这 6 个 task
→ 通过率 ≥ 0.85 视为该平台能力卡冷启动 OK.

任务设计原则:
  - 覆盖 3 baseline op (create_product / list_orders / get_product)
    各 2 种场景 (happy path + edge case)
  - 用 ShopifyAPIAdapter 或 ShopifyBrowserAdapter 跑 (取决 Router)
  - golden_output 不强求 ID 精确 (动态生成), 用 keys_present + status_ok
    metric 验证
"""

from __future__ import annotations

from kun.evaluation.industry_suite import GoldenTask, IndustryEvalSuite


def build_shopify_golden_tasks() -> list[GoldenTask]:
    """6 个 Shopify baseline golden task."""
    return [
        # ---- create_product (2 tasks) ----
        GoldenTask(
            task_id="shopify-create-product-basic",
            industry="电商",
            target_platform="shopify",
            operation="create_product",
            description="创建一个基础产品 (title + vendor)",
            golden_input={
                "target_platform": "shopify",
                "operation": "create_product",
                "payload": {
                    "title": "Test Mug",
                    "vendor": "ACME Studio",
                    "product_type": "Drinkware",
                },
            },
            golden_output={
                # API 响应 wrap 在 product 键里
                "product": {"title": "Test Mug"},
            },
            metric_name="keys_present",
            tags=["happy_path", "create"],
        ),
        GoldenTask(
            task_id="shopify-create-product-with-variants",
            industry="电商",
            target_platform="shopify",
            operation="create_product",
            description="创建带 variants 的产品",
            golden_input={
                "target_platform": "shopify",
                "operation": "create_product",
                "payload": {
                    "title": "Test T-Shirt",
                    "vendor": "Fashion Inc",
                    "product_type": "Apparel",
                    "variants": [
                        {"option1": "Small", "price": "19.99"},
                        {"option1": "Medium", "price": "19.99"},
                    ],
                },
            },
            golden_output={
                "product": {"title": "Test T-Shirt"},
            },
            metric_name="keys_present",
            tags=["happy_path", "create", "variants"],
        ),
        # ---- list_orders (2 tasks) ----
        GoldenTask(
            task_id="shopify-list-orders-default",
            industry="电商",
            target_platform="shopify",
            operation="list_orders",
            description="列出所有订单 (默认参数)",
            golden_input={
                "target_platform": "shopify",
                "operation": "list_orders",
                "payload": {},
            },
            golden_output={"orders": []},
            metric_name="keys_present",
            tags=["happy_path", "read"],
        ),
        GoldenTask(
            task_id="shopify-list-orders-open-only",
            industry="电商",
            target_platform="shopify",
            operation="list_orders",
            description="只列 open 状态订单",
            golden_input={
                "target_platform": "shopify",
                "operation": "list_orders",
                "payload": {"status": "open"},
            },
            golden_output={"orders": []},
            metric_name="keys_present",
            tags=["happy_path", "read", "filtered"],
        ),
        # ---- get_product (2 tasks) ----
        GoldenTask(
            task_id="shopify-get-product-by-id",
            industry="电商",
            target_platform="shopify",
            operation="get_product",
            description="根据 product_id 查产品详情",
            golden_input={
                "target_platform": "shopify",
                "operation": "get_product",
                "payload": {"product_id": "12345"},
            },
            golden_output={"product": {}},
            metric_name="keys_present",
            tags=["happy_path", "read"],
        ),
        GoldenTask(
            task_id="shopify-get-product-missing-id",
            industry="电商",
            target_platform="shopify",
            operation="get_product",
            description="缺 product_id payload → adapter 应处理 (返回 ok 或 failed 均可, 不应崩溃)",
            golden_input={
                "target_platform": "shopify",
                "operation": "get_product",
                "payload": {},  # 故意缺 product_id
            },
            golden_output={},  # 不强约束 — adapter 行为可接受
            metric_name="exact_key_match",  # golden 空 → 任何输出都 1.0
            tags=["edge_case", "missing_payload"],
        ),
    ]


def build_shopify_eval_suite() -> IndustryEvalSuite:
    """打包电商 Shopify 评测集."""
    return IndustryEvalSuite(
        industry="电商",
        tasks=build_shopify_golden_tasks(),
    )


__all__ = [
    "build_shopify_eval_suite",
    "build_shopify_golden_tasks",
]
