"""行业评测集框架 (L6 / ADR-011 / ADR-026).

每个行业 (电商 / 投放 / 内容分发 / CRM) 选定后, 用 IndustryEvalSuite
建该行业的标准任务集 + 能力卡冷启动校准.

每个 GoldenTask = (target_platform, operation, golden_input, golden_output,
acceptance_metric). 跑完一次 KUN.execute(task), 用 acceptance_metric 评
结果是否达标. 全 suite 跑完产 EvalReport, 喂 capability_card 冷启动.

industry-agnostic — 4 行业共用同一 framework, 各自填具体 task.
"""

from kun.evaluation.ecommerce_shopify import (
    build_shopify_eval_suite,
    build_shopify_golden_tasks,
)
from kun.evaluation.industry_suite import (
    AcceptanceMetric,
    EvalReport,
    EvalResult,
    GoldenTask,
    IndustryEvalSuite,
    builtin_acceptance_metrics,
)

__all__ = [
    "AcceptanceMetric",
    "EvalReport",
    "EvalResult",
    "GoldenTask",
    "IndustryEvalSuite",
    "build_shopify_eval_suite",
    "build_shopify_golden_tasks",
    "builtin_acceptance_metrics",
]
