"""5 类傩诊断 fix handler 实装 (V2.1 §10.6 / T59 + T60 / M3.2 提前).

每个 handler 真做 in-memory side effect + 写 audit log.
真"修复"逻辑 (调外部子系统) 留给 M5 接入对应基础设施 (cache GC / router force_fallback /
network throttle / TokenMeter purge), 现在每类 handler:
- 真改 in-memory state (clearing / setting flag / appending throttle list)
- audit 记录到全局 _FIX_AUDIT_LOG (M4 持久化)
- 返 FixOutcome 给 DiagnoseRunner.run() 出报告
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from kun.security.diagnose_runner import (
    DiagnoseFinding,
    DiagnoseRunner,
    FixOutcome,
    FixPlan,
    ManagerCategory,
)

logger = logging.getLogger(__name__)


# 全局 audit log (M4 持久化到 fix_audit 表).
_FIX_AUDIT_LOG: list[dict[str, Any]] = []

# in-memory side-effect stores (真做事的地方, 给 router/orchestrator 后续 check).
_THROTTLED_USERS: set[str] = set()
_FORCE_FALLBACK_TASKS: set[str] = set()
_PURGED_USER_TOKEN_DETAILS: set[str] = set()
_CACHE_TTL_BOOSTS: dict[str, int] = {}  # cache_key → boosted_ttl_sec
_CLEANED_CONTEXT_TIERS: set[str] = set()  # tier_name (short_term / long_term)


def get_fix_audit_log() -> list[dict[str, Any]]:
    return list(_FIX_AUDIT_LOG)


def reset_fix_state() -> None:
    """测试用: 清所有 in-memory state + audit log."""
    _FIX_AUDIT_LOG.clear()
    _THROTTLED_USERS.clear()
    _FORCE_FALLBACK_TASKS.clear()
    _PURGED_USER_TOKEN_DETAILS.clear()
    _CACHE_TTL_BOOSTS.clear()
    _CLEANED_CONTEXT_TIERS.clear()


def is_user_throttled(user_id: str) -> bool:
    """router / chat_handler 检查 user 是否被 network_guard 限流."""
    return user_id in _THROTTLED_USERS


def is_task_forced_fallback(task_id: str) -> bool:
    """router 检查任务是否需要强制走 fallback tier."""
    return task_id in _FORCE_FALLBACK_TASKS


def get_cache_ttl_boost(cache_key: str) -> int | None:
    """cache layer 检查 key 是否有 TTL boost."""
    return _CACHE_TTL_BOOSTS.get(cache_key)


def _audit(
    category: str,
    plan: FixPlan,
    finding: DiagnoseFinding,
    action: str,
    success: bool,
    notes: str = "",
) -> None:
    _FIX_AUDIT_LOG.append(
        {
            "ts": datetime.now(UTC).isoformat(),
            "category": category,
            "plan_id": plan.plan_id,
            "finding_id": finding.finding_id,
            "subsystem": finding.subsystem,
            "action": action,
            "success": success,
            "notes": notes,
        }
    )


# ============================================================================
# 5 类 Handler
# ============================================================================


async def clean_handler(plan: FixPlan, finding: DiagnoseFinding) -> FixOutcome:
    """clean: Context tier 短期/长期到期 → 触发清理 (V1 §10.5 #1)."""
    cleaned_tiers: list[str] = []
    if "短期" in finding.description or "short" in finding.description.lower():
        _CLEANED_CONTEXT_TIERS.add("short_term")
        cleaned_tiers.append("short_term")
    if "长期" in finding.description or "long" in finding.description.lower():
        _CLEANED_CONTEXT_TIERS.add("long_term")
        cleaned_tiers.append("long_term")
    # 默认: 没指明 → 清短期
    if not cleaned_tiers:
        _CLEANED_CONTEXT_TIERS.add("short_term")
        cleaned_tiers.append("short_term")

    notes = f"cleaned context tiers: {', '.join(cleaned_tiers)}"
    _audit("clean", plan, finding, f"clean_context:{','.join(cleaned_tiers)}", True, notes)
    return FixOutcome(plan_id=plan.plan_id, success=True, verified=True, notes=notes)


async def accelerate_handler(plan: FixPlan, finding: DiagnoseFinding) -> FixOutcome:
    """accelerate: 缓存命中率低/p95 高 → 升 cache TTL (V1 §10.5 #2)."""
    cache_key = f"finding:{finding.finding_id}"
    boosted_ttl = 900  # 15 min (默认 5 min)
    _CACHE_TTL_BOOSTS[cache_key] = boosted_ttl

    notes = f"boosted cache TTL for {cache_key} → {boosted_ttl}s"
    _audit("accelerate", plan, finding, f"boost_ttl:{boosted_ttl}", True, notes)
    return FixOutcome(plan_id=plan.plan_id, success=True, verified=False, notes=notes)


async def failover_handler(plan: FixPlan, finding: DiagnoseFinding) -> FixOutcome:
    """failover: provider 失败 → 标记任务下次走 fallback tier (V1 §10.5 #3)."""
    # finding.description 里如果带 task_id 提取出来; 否则用 finding_id 作占位
    task_id = finding.finding_id  # M5 接 task_ref 后用真 task_id
    _FORCE_FALLBACK_TASKS.add(task_id)

    notes = f"task {task_id} marked to force fallback tier on next retry"
    _audit("failover", plan, finding, "force_fallback", True, notes)
    return FixOutcome(plan_id=plan.plan_id, success=True, verified=False, notes=notes)


async def network_guard_handler(plan: FixPlan, finding: DiagnoseFinding) -> FixOutcome:
    """network_guard: 异常调用模式 → user throttle (V1 §10.5 #4)."""
    user_id = "unknown"
    # 简单解析 finding.description 找 user_id (M5 接结构化 finding.affected_user_id)
    parts = finding.description.split()
    for p in parts:
        if p.startswith("u-"):
            user_id = p
            break
    if user_id == "unknown":
        user_id = finding.finding_id  # fallback to finding id
    _THROTTLED_USERS.add(user_id)

    notes = f"user {user_id} added to throttle list"
    _audit("network_guard", plan, finding, f"throttle_user:{user_id}", True, notes)
    return FixOutcome(plan_id=plan.plan_id, success=True, verified=False, notes=notes)


async def privacy_handler(plan: FixPlan, finding: DiagnoseFinding) -> FixOutcome:
    """privacy: 数据足迹超阈值 → 清临时缓存 (V1 §10.5 #5)."""
    # 找 user_id (同 network_guard)
    user_id = "unknown"
    for p in finding.description.split():
        if p.startswith("u-"):
            user_id = p
            break
    if user_id == "unknown":
        user_id = finding.finding_id
    _PURGED_USER_TOKEN_DETAILS.add(user_id)

    notes = f"purged temp token detail logs for user {user_id}"
    _audit("privacy", plan, finding, f"purge_user:{user_id}", True, notes)
    return FixOutcome(plan_id=plan.plan_id, success=True, verified=True, notes=notes)


# ============================================================================
# 注册到 DiagnoseRunner
# ============================================================================

_HANDLER_MAP: dict[ManagerCategory, Any] = {
    "clean": clean_handler,
    "accelerate": accelerate_handler,
    "failover": failover_handler,
    "network_guard": network_guard_handler,
    "privacy": privacy_handler,
}


def register_default_fix_handlers(runner: DiagnoseRunner) -> None:
    """install_runtime 调用一次, 把 5 类 default handler 装进 runner."""
    for category, handler in _HANDLER_MAP.items():
        runner.register_fix_handler(category, handler)


__all__ = [
    "accelerate_handler",
    "clean_handler",
    "failover_handler",
    "get_cache_ttl_boost",
    "get_fix_audit_log",
    "is_task_forced_fallback",
    "is_user_throttled",
    "network_guard_handler",
    "privacy_handler",
    "register_default_fix_handlers",
    "reset_fix_state",
]
