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

超时规则 (L5.2 实装, 防 dormant feature):
  - capability 在某个 state 停留 > stale_threshold_days → 标 stale
  - capability 超过 promotion_deadline → state="expired" + 写
    strategy_search_request 重审 (triggered_by="promotion_timeout")
  - Strategist 判断"过期 / 污染 / 低价值" → 合并 / 降级 / 删除 / 重排队
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, Field

from kun.core.ids import new_id
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
    "awaiting_human_review",
]


DEFAULT_STALE_THRESHOLD_DAYS = 7
"""capability 在同一 state 停留超过此天数 → 标 stale (写 search_request)."""


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
    last_state_change_at: datetime | None = None


@dataclass(frozen=True)
class TimeoutCheckResult:
    """单次超时扫描结果."""

    capability_id: str
    promotion_state: str
    expired: bool  # 超过 promotion_deadline
    stale: bool  # 同 state 停留过久
    days_in_state: float
    days_until_deadline: float
    recommended_action: str  # advance / re-evaluate / mark_expired / keep


CapabilityReader = Callable[[], Awaitable[list[dict[str, Any]]]]
"""读所有 active (state ∉ {enabled, rolled_back, expired}) 的 capability."""

CapabilityStateWriter = Callable[[str, dict[str, Any]], Awaitable[None]]
"""更新 capability state — (capability_id, payload_with_changes)."""

SearchRequestEmitter = Callable[[dict[str, Any]], Awaitable[None]]
"""写 strategy_search_request — 复用 Supervisor 同签名."""


def evaluate_capability_timeout(
    capability: dict[str, Any],
    *,
    now: datetime | None = None,
    stale_threshold_days: int = DEFAULT_STALE_THRESHOLD_DAYS,
) -> TimeoutCheckResult:
    """对单 capability 计算超时状态. Pure 函数, 不写库."""
    now = now or datetime.now(UTC)
    capability_id = capability.get("capability_id", "unknown")
    state = capability.get("promotion_state", "merged")

    last_change = capability.get("last_state_change_at") or capability.get(
        "promotion_started_at"
    )
    deadline = capability.get("promotion_deadline")

    if isinstance(last_change, str):
        last_change = _parse_iso(last_change)
    if isinstance(deadline, str):
        deadline = _parse_iso(deadline)

    days_in_state = 0.0
    if isinstance(last_change, datetime):
        if last_change.tzinfo is None:
            last_change = last_change.replace(tzinfo=UTC)
        days_in_state = (now - last_change).total_seconds() / 86400.0

    days_until_deadline = 0.0
    if isinstance(deadline, datetime):
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
        days_until_deadline = (deadline - now).total_seconds() / 86400.0

    expired = isinstance(deadline, datetime) and now >= deadline
    stale = days_in_state >= stale_threshold_days

    if expired:
        action = "mark_expired"
    elif stale and state not in ("ready", "enabled"):
        action = "re_evaluate"
    elif state == "ready":
        action = "advance"  # ready → 等 Gate enable, 推 reminder
    else:
        action = "keep"

    return TimeoutCheckResult(
        capability_id=str(capability_id),
        promotion_state=state,
        expired=expired,
        stale=stale,
        days_in_state=days_in_state,
        days_until_deadline=days_until_deadline,
        recommended_action=action,
    )


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    except (ValueError, TypeError):
        return None


@dataclass
class PromotionTimeoutSweeper:
    """周期扫 active capabilities → 标 expired / 写 reaudit search_request.

    Idempotent: 同 capability 重复扫不会重复 emit (写库前先检查 state 是否变化).
    """

    capability_reader: CapabilityReader
    state_writer: CapabilityStateWriter | None = None
    search_emitter: SearchRequestEmitter | None = None
    stale_threshold_days: int = DEFAULT_STALE_THRESHOLD_DAYS
    last_scan_at: datetime | None = field(default=None)

    async def sweep(self) -> dict[str, Any]:
        """单次扫描. Returns 汇总报告."""
        now = datetime.now(UTC)
        try:
            capabilities = await self.capability_reader()
        except Exception as e:
            log.warning("promotion_queue.reader_failed", error=str(e))
            return {"scanned": 0, "expired": 0, "stale": 0, "error": str(e)}

        results: list[TimeoutCheckResult] = []
        expired_count = 0
        stale_count = 0
        emitted_search_requests = 0

        for cap in capabilities:
            result = evaluate_capability_timeout(
                cap, now=now, stale_threshold_days=self.stale_threshold_days
            )
            results.append(result)

            if result.expired:
                expired_count += 1
                # 写 capability state → expired
                if self.state_writer is not None:
                    try:
                        await self.state_writer(
                            result.capability_id,
                            {
                                "promotion_state": "expired",
                                "last_state_change_at": now,
                            },
                        )
                    except Exception as e:
                        log.warning(
                            "promotion_queue.state_writer_failed",
                            capability_id=result.capability_id,
                            error=str(e),
                        )
                # 写 search_request 让 Strategist 重审
                if self.search_emitter is not None:
                    try:
                        await self.search_emitter(
                            _build_reaudit_search_request(cap, result, now)
                        )
                        emitted_search_requests += 1
                    except Exception as e:
                        log.warning(
                            "promotion_queue.search_emit_failed",
                            capability_id=result.capability_id,
                            error=str(e),
                        )
            elif result.stale and self.search_emitter is not None:
                stale_count += 1
                # 仅推 reaudit search_request, 不动 state (允许继续 try)
                try:
                    await self.search_emitter(
                        _build_reaudit_search_request(cap, result, now)
                    )
                    emitted_search_requests += 1
                except Exception as e:
                    log.warning(
                        "promotion_queue.search_emit_failed",
                        capability_id=result.capability_id,
                        error=str(e),
                    )

        self.last_scan_at = now
        log.info(
            "promotion_queue.sweep_done",
            scanned=len(capabilities),
            expired=expired_count,
            stale=stale_count,
            search_requests=emitted_search_requests,
        )
        return {
            "scanned": len(capabilities),
            "expired": expired_count,
            "stale": stale_count,
            "search_requests_emitted": emitted_search_requests,
            "results": results,
        }


def _build_reaudit_search_request(
    capability: dict[str, Any],
    result: TimeoutCheckResult,
    now: datetime,
) -> dict[str, Any]:
    """构造 promotion_timeout reaudit 用的 strategy_search_request."""
    tenant_id = capability.get("tenant_id", "default")
    target_module = capability.get("target_module", "unknown")
    return {
        "request_id": new_id("strategy_search"),
        "tenant_id": tenant_id,
        "triggered_by": "promotion_timeout",
        "target_module": target_module,
        "anomaly_kind": "promotion_stale" if not result.expired else "promotion_expired",
        "evidence": [
            {
                "type": "promotion_timeout",
                "capability_id": result.capability_id,
                "promotion_state": result.promotion_state,
                "days_in_state": round(result.days_in_state, 2),
                "days_until_deadline": round(result.days_until_deadline, 2),
                "recommended_action": result.recommended_action,
                "change_summary": capability.get("change_summary", ""),
            }
        ],
        "priority": "high" if result.expired else "medium",
        "dedup_key": f"{tenant_id}:promotion_timeout:{result.capability_id}",
        "status": "open",
        "created_at": now,
        "severity": "strong" if result.expired else "mid",
        "escalation_path": ["role", "task", "gate"] if result.expired else ["role", "task"],
        "is_self_referential": False,
        "repeat_count": 1,
    }


async def advance(capability_id: str) -> PromotionState:
    """推进一档晋级 (按 replay → shadow → canary → ready → enabled).

    实装留给上层服务接 DB writer; 当前仅 stub log.
    """
    log.info("promotion_queue.advance", capability_id=capability_id)
    return "in_replay"


__all__ = [
    "DEFAULT_STALE_THRESHOLD_DAYS",
    "CapabilityReader",
    "CapabilityStateWriter",
    "PromotionState",
    "PromotionTimeoutSweeper",
    "RuntimeCapability",
    "SearchRequestEmitter",
    "TimeoutCheckResult",
    "advance",
    "evaluate_capability_timeout",
]
