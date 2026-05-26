"""治理层 L5 (ADR-020) — RSI 闭环 + RCDH + 数据脊柱 + 自指限制 + Resource Quota.

包含:
  - rsi_loop.py          — RSI 10 步闭环编排 (ADR-024)
  - rcdh.py              — 强制诊断层级检查器 (ADR-021)
  - evidence_ledger.py   — 任务证据账本 (ADR-024)
  - promotion_queue.py   — capability 晋级队列 (ADR-024)
  - diagnosis_scope.py   — 范围圈定工具 (ADR-021, ≤5 模块强制约束)
  - self_referential.py  — 自指限制 source of truth (ADR-024, L3.5)
  - resource_quota.py    — token + experiment + dedup 工程化限流 (L4.5)

这些模块在 L1.2 (拆 orchestrator) + L1.3 (alembic 0011) 阶段获得真实现.
"""

from kun.governance.exploration_penalty import (
    ExplorationPenalty,
    PenaltyCheckResult,
    candidate_signature_key,
)
from kun.governance.priority_channel import (
    PriorityClassification,
    PriorityTier,
    classify_request_priority,
    is_high_priority_channel,
    prioritize_requests,
    split_by_tier,
)
from kun.governance.resource_quota import (
    QuotaCheckResult,
    ResourceQuota,
)
from kun.governance.self_referential import (
    SELF_REFERENTIAL_PREFIXES,
    is_self_referential,
)

__all__ = [
    "SELF_REFERENTIAL_PREFIXES",
    "ExplorationPenalty",
    "PenaltyCheckResult",
    "PriorityClassification",
    "PriorityTier",
    "QuotaCheckResult",
    "ResourceQuota",
    "candidate_signature_key",
    "classify_request_priority",
    "is_high_priority_channel",
    "is_self_referential",
    "prioritize_requests",
    "split_by_tier",
]
