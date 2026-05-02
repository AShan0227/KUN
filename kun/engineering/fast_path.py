"""FastPath — 决策跳过快速路径 (V2.1 §17.4a, 速度核心机制).

核心: 大部分任务走快速路径直接出结果, 不走完整 StrategyMatcher.decide() 决策链.

6 触发条件 + 4 安全护栏 + 反馈写回, 保证速度铁律 (≤500ms 出结果) +
不为快牺牲安全.

V2.1.2 §17.4a 完整设计:
- cache_hit / template_match / history_reuse / fixed_flow / skill_direct / chitchat
- pre-check: risk 关键词 / 用户信任度 / 跨租户 / 预算
- 反馈仍写: capability_card stats / surprise_score
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)


FastPathHit = Literal[
    "cache_hit",  # task_results 表缓存命中
    "template_match",  # task_type 模板命中
    "history_reuse",  # 用户最近 7 天同类任务复用
    "fixed_flow",  # task_type 标记 deterministic
    "skill_direct",  # 用户明确"调 X skill 处理 Y"
    "chitchat",  # 闲聊判定
]


# 高风险关键词 (1ms 扫描 → 强制退快速路径)
HIGH_RISK_KEYWORDS = [
    "删除",
    "删库",
    "drop ",
    "delete ",
    "truncate",
    "rm -rf",
    "deploy",
    "部署",
    "上线",
    "release",
    "支付",
    "transfer",
    "withdraw",
    "扣款",
    "发邮件",
    "send email",
    "broadcast",
    "跨租户",
    "cross-tenant",
    "cross_tenant",
]

# 闲聊指示词 (短 prompt + 命中 → chitchat)
CHITCHAT_HINTS = [
    "你好",
    "hello",
    "hi",
    "在吗",
    "?",
    "？",
    "thanks",
    "谢谢",
    "ok",
    "好的",
]


@dataclass
class FastPathDecision:
    """快速路径决策结果."""

    is_fast: bool
    hit: FastPathHit | None = None
    reason: str = ""
    pre_check_violations: list[str] = field(default_factory=list)
    response_payload: dict[str, Any] | None = None
    decided_in_ms: int = 0


class FastPathRouter:
    """快速路径路由器.

    用法:
        router = FastPathRouter(
            cache_lookup=lambda fp: ...,
            template_lookup=lambda task_type: ...,
            history_lookup=lambda user_id, task_type: ...,
            deterministic_types=("tools.echo", "tools.curl"),
            user_trust_lookup=lambda user_id: int(user_task_count),
        )
        decision = router.try_fast(task_meta, user_meta)
        if decision.is_fast:
            return decision.response_payload  # 直接返
        else:
            # fallback 到完整 StrategyMatcher.decide()
            ...
    """

    def __init__(
        self,
        *,
        cache_lookup: Any = None,  # Callable[[fingerprint], dict | None]
        template_lookup: Any = None,  # Callable[[task_type], dict | None]
        history_lookup: Any = None,  # Callable[[user_id, task_type], dict | None]
        deterministic_types: tuple[str, ...] = (),
        user_trust_lookup: Any = None,  # Callable[[user_id], int]
        chitchat_max_chars: int = 30,
        new_user_task_threshold: int = 10,
    ) -> None:
        self._cache_lookup = cache_lookup
        self._template_lookup = template_lookup
        self._history_lookup = history_lookup
        self._deterministic_types = set(deterministic_types)
        self._user_trust_lookup = user_trust_lookup
        self._chitchat_max_chars = chitchat_max_chars
        self._new_user_task_threshold = new_user_task_threshold

    def try_fast(
        self,
        task_meta: dict[str, Any],
        user_meta: dict[str, Any] | None = None,
    ) -> FastPathDecision:
        """尝试走快速路径. 返回 FastPathDecision.

        is_fast=True → response_payload 可直接返用户.
        is_fast=False → 走完整决策链 (pre_check_violations 标记原因).
        """
        import time

        start_ns = time.perf_counter_ns()
        user_meta = user_meta or {}

        # ---- 4 个 pre-check 安全护栏 (各 < 5ms) ----

        violations = []

        # ① risk_level pre-check: 高风险关键词扫描
        prompt = str(task_meta.get("user_message", task_meta.get("prompt", "")))
        prompt_lower = prompt.lower()
        for kw in HIGH_RISK_KEYWORDS:
            if kw in prompt_lower:
                violations.append(f"high_risk_keyword:{kw}")
                break

        # ② 用户信任度 pre-check (新用户不走快速路径)
        if self._user_trust_lookup is not None:
            user_id = user_meta.get("user_id", "")
            try:
                task_count = self._user_trust_lookup(user_id) if user_id else 0
            except Exception:
                task_count = 0
            if task_count < self._new_user_task_threshold:
                violations.append("new_user_collecting_capability_data")

        # ③ 租户权限 pre-check
        if task_meta.get("crosses_tenant"):
            violations.append("crosses_tenant_requires_full_decision")

        # ④ 预算 pre-check (任务预估成本超用户阈值)
        est_cost = float(task_meta.get("estimated_cost_usd", 0.0))
        approval_threshold = float(user_meta.get("approval_threshold_money", 1e9))
        if est_cost > approval_threshold:
            violations.append(f"estimated_cost_{est_cost}>{approval_threshold}")

        if violations:
            elapsed = (time.perf_counter_ns() - start_ns) // 1_000_000
            return FastPathDecision(
                is_fast=False,
                pre_check_violations=violations,
                decided_in_ms=int(elapsed),
                reason=f"pre-check failed: {violations[0]}",
            )

        # ---- 6 触发条件 (按速度优先级排) ----

        # 1. 缓存命中 (< 50ms)
        fingerprint = task_meta.get("fingerprint")
        if fingerprint and self._cache_lookup is not None:
            try:
                cached = self._cache_lookup(fingerprint)
            except Exception:
                cached = None
            if cached:
                return self._make_fast(start_ns, "cache_hit", f"fingerprint={fingerprint}", cached)

        # 2. 模板命中
        task_type = task_meta.get("task_type", "")
        if task_type and self._template_lookup is not None:
            try:
                template = self._template_lookup(task_type)
            except Exception:
                template = None
            if template:
                return self._make_fast(
                    start_ns, "template_match", f"task_type={task_type}", template
                )

        # 3. 历史复用 (用户最近 7 天)
        user_id = user_meta.get("user_id", "")
        if user_id and task_type and self._history_lookup is not None:
            try:
                hist = self._history_lookup(user_id, task_type)
            except Exception:
                hist = None
            if hist:
                return self._make_fast(
                    start_ns, "history_reuse", f"user={user_id} type={task_type}", hist
                )

        # 4. 固定流程任务
        if task_type in self._deterministic_types:
            return self._make_fast(
                start_ns,
                "fixed_flow",
                f"deterministic task_type={task_type}",
                {"flow": task_type, "deterministic": True},
            )

        # 5. Skill 直跑 (用户明确指定)
        explicit_skill = task_meta.get("explicit_skill_id")
        if explicit_skill:
            return self._make_fast(
                start_ns,
                "skill_direct",
                f"user explicitly requested skill={explicit_skill}",
                {"skill_id": explicit_skill, "direct_dispatch": True},
            )

        # 6. 闲聊判定
        if len(prompt) <= self._chitchat_max_chars and any(
            h in prompt_lower for h in CHITCHAT_HINTS
        ):
            return self._make_fast(
                start_ns,
                "chitchat",
                f"短 prompt={len(prompt)} 含闲聊词",
                {"is_chitchat": True, "model_tier": "cheap"},
            )

        # 都没命中, 走完整决策
        elapsed = (time.perf_counter_ns() - start_ns) // 1_000_000
        return FastPathDecision(
            is_fast=False,
            decided_in_ms=int(elapsed),
            reason="no fast-path trigger matched",
        )

    @staticmethod
    def _make_fast(
        start_ns: int,
        hit: FastPathHit,
        reason: str,
        payload: dict[str, Any],
    ) -> FastPathDecision:
        import time

        elapsed = (time.perf_counter_ns() - start_ns) // 1_000_000
        return FastPathDecision(
            is_fast=True,
            hit=hit,
            reason=reason,
            response_payload=payload,
            decided_in_ms=int(elapsed),
        )


# ---- helpers ----


def detect_chitchat(prompt: str, max_chars: int = 30) -> bool:
    """单独可用的闲聊判定."""
    if len(prompt) > max_chars:
        return False
    p = prompt.lower()
    return any(h in p for h in CHITCHAT_HINTS)


def has_high_risk_keyword(prompt: str) -> tuple[bool, str | None]:
    """高风险关键词扫描. 返 (命中?, 关键词)."""
    p = prompt.lower()
    for kw in HIGH_RISK_KEYWORDS:
        if kw in p:
            return True, kw
    return False, None


__all__ = [
    "CHITCHAT_HINTS",
    "HIGH_RISK_KEYWORDS",
    "FastPathDecision",
    "FastPathHit",
    "FastPathRouter",
    "detect_chitchat",
    "has_high_risk_keyword",
]
