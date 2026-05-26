"""Promotion Queue · capability 晋级队列 (ADR-024).

新候选 capability 合入 main 后默认 runtime_enabled=false, 自动进 promotion queue.
按 replay → shadow → canary → rollback drill → ready 序列推进, 达标才启用.

晋级状态机:
  merged       — 合入 main, runtime_enabled=false
  in_replay    — 历史 replay 中
  in_shadow    — shadow 模式 (新旧并跑, 只记录新)
  in_canary    — canary (1% → 5% → 25% 流量)
  ready        — 达标, 待 Gate 启用
  enabled      — runtime_enabled=true, 真生效
  rolled_back  — 出问题回滚
  expired      — 超时 (N 天) 未晋级 → 重审

超时规则 (防 dormant feature):
  - 候选 > N 天未晋级 → 写 strategy_search_request 重审
  - Strategist 判断 "过期 / 污染 / 低价值" → 合并 / 降级 / 删除 / 重排队

实施路径: L1.3 alembic 0011 建表 + L2 编排逻辑实装. 现在是骨架.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field

from kun.core.logging import get_logger

log = get_logger("kun.governance.promotion_queue")

PromotionState = Literal[
    "merged",
    "in_replay",
    "in_shadow",
    "in_canary",
    "ready",
    "enabled",
    "rolled_back",
    "expired",
]


class RuntimeCapability(BaseModel):
    """候选能力 — 写入 runtime_capabilities 表 (alembic 0011)."""

    capability_id: str
    target_module: str
    change_summary: str
    enabled: bool = False  # 合入 main 后默认 false
    promotion_state: PromotionState = "merged"
    promotion_started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    promotion_deadline: datetime = Field(
        default_factory=lambda: datetime.now(UTC) + timedelta(days=14)
    )
    rollback_on: list[str] = Field(default_factory=list)
    sampling_rate: float = 0.0  # canary 阶段


async def advance(capability_id: str) -> PromotionState:
    """推进一档晋级 (按 replay → shadow → canary → ready → enabled)."""
    # TODO L2: 实装
    log.info("promotion_queue.advance", capability_id=capability_id)
    return "in_replay"


async def check_expired() -> list[RuntimeCapability]:
    """扫超时候选 → 写 strategy_search_request 重审.

    由 idle-batch worker 周期性调用.
    """
    # TODO L2: 实装
    return []
