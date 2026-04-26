"""TaskBoundaryGuard — 任务边界守护 (V2.2 §28, OffTopicEval 启发).

OffTopicEval (Lambda, ICLR 2026) 揭示: 即使 LLM 被给了明确角色和边界, 它
"几乎每次都回答不该回答的问题". KUN 用工程化方式对治 — 加一层 boundary
check, 在 intent 之后 / planner 之前算"task 是否在 agent role scope 内".

不在 → reject + 反问用户:
  "我是 [营销文案 agent], 这个 [bug 修复] 任务不在我擅长范围.
   您要继续吗? 我可以转给 [coding agent] 或者您自己处理."

跟 KUN 已有架构区别:
- PlanOnlyGate: 防"高危操作" (destructive), 不防 off-topic
- SoulFile.professional_role: 描述用户角色, 跟 agent role 不直接关联
- watchtower: 规则触发, 没"任务级 in-scope" 检测
- ValueGate: 步级 ROI, 不是任务级

V2.2 §28 补这个空缺.

设计原则:
- 不强制依赖 RoleTemplate model (向后兼容, 用 dict scope_config)
- 启发式 + LLM judge 二合 (跟 ThoughtActionConsistency 模式一致)
- strict_mode=True → reject; False → 警告但放行 (调用方决定)
- 容错: 没 scope_config → 默认中性 (in_scope=0.7), 不阻塞
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class BoundaryDecision(BaseModel):
    """边界检测结果."""

    in_scope: bool
    boundary_score: float = Field(ge=0.0, le=1.0)
    reason: str  # whitelist_match / blacklist_hit / llm_judge / no_scope_defined / threshold
    suggested_redirect: str = ""  # 推荐转给的 role_id
    matched_pattern: str = ""  # 命中的 pattern (whitelist / blacklist)


class ScopeConfig(BaseModel):
    """Agent role scope 配置. RoleTemplate 没字段时用这个."""

    role_id: str = ""
    role_name: str = ""
    allowed_task_types: list[str] = Field(
        default_factory=list
    )  # 白名单, e.g. ["marketing.copywriting", "marketing.ad"]
    forbidden_task_types: list[str] = Field(default_factory=list)  # 黑名单, e.g. ["coding.*"]
    boundary_strict_mode: bool = True  # True → 低 score 就 reject; False → 警告但放行
    out_of_scope_redirect: str = ""  # 建议转给的 role_id


class TaskBoundaryGuard:
    """V2.2 §28 — 任务边界守护.

    用法:
        scope = ScopeConfig(
            role_id="marketing-agent",
            allowed_task_types=["marketing.*"],
            forbidden_task_types=["coding.*"],
        )
        guard = TaskBoundaryGuard()
        decision = await guard.check(task_meta={"task_type": "coding.python"}, scope=scope)
        if not decision.in_scope and scope.boundary_strict_mode:
            raise BoundaryViolationError(decision.reason)

    Args:
        threshold: in_scope 阈值 (默认 0.4, 低于此 → in_scope=False)
        llm_judge: 可选 callable[(task_meta, scope), Awaitable[float]] 算 0..1 分
    """

    def __init__(
        self,
        *,
        threshold: float = 0.4,
        llm_judge: Any = None,
    ) -> None:
        if not 0 <= threshold <= 1:
            raise ValueError("threshold must be in [0, 1]")
        self.threshold = threshold
        self._llm_judge = llm_judge
        # 监控统计
        self._stats = {
            "checks_total": 0,
            "in_scope_count": 0,
            "out_of_scope_count": 0,
            "no_scope_defined_count": 0,
        }

    async def check(
        self,
        task_meta: dict[str, Any],
        scope: ScopeConfig | None = None,
    ) -> BoundaryDecision:
        """主入口. 算 task 是否在 scope 内."""
        self._stats["checks_total"] += 1

        # 没 scope → 默认中性放行
        if scope is None or (not scope.allowed_task_types and not scope.forbidden_task_types):
            self._stats["no_scope_defined_count"] += 1
            return BoundaryDecision(
                in_scope=True,
                boundary_score=0.7,  # 中性
                reason="no_scope_defined",
            )

        task_type = str(task_meta.get("task_type", ""))

        # 1. 黑名单优先 (硬拒)
        for pattern in scope.forbidden_task_types:
            if self._matches(pattern, task_type):
                self._stats["out_of_scope_count"] += 1
                return BoundaryDecision(
                    in_scope=False,
                    boundary_score=0.0,
                    reason="blacklist_hit",
                    matched_pattern=pattern,
                    suggested_redirect=scope.out_of_scope_redirect,
                )

        # 2. 白名单命中 → 直接 in_scope
        for pattern in scope.allowed_task_types:
            if self._matches(pattern, task_type):
                self._stats["in_scope_count"] += 1
                return BoundaryDecision(
                    in_scope=True,
                    boundary_score=1.0,
                    reason="whitelist_match",
                    matched_pattern=pattern,
                )

        # 3. 都没命中 → LLM judge 兜底
        if self._llm_judge is not None:
            try:
                score = float(await self._llm_judge(task_meta, scope))
                score = max(0.0, min(1.0, score))
                in_scope = score >= self.threshold
                if in_scope:
                    self._stats["in_scope_count"] += 1
                else:
                    self._stats["out_of_scope_count"] += 1
                return BoundaryDecision(
                    in_scope=in_scope,
                    boundary_score=score,
                    reason="llm_judge",
                    suggested_redirect="" if in_scope else scope.out_of_scope_redirect,
                )
            except Exception:
                logger.exception("TaskBoundaryGuard llm_judge failed")

        # 4. 没 LLM judge → 启发式: task_type 跟 role_name 词共现
        score = self._heuristic_overlap(task_type, scope)
        in_scope = score >= self.threshold
        if in_scope:
            self._stats["in_scope_count"] += 1
        else:
            self._stats["out_of_scope_count"] += 1
        return BoundaryDecision(
            in_scope=in_scope,
            boundary_score=score,
            reason="heuristic_overlap",
            suggested_redirect="" if in_scope else scope.out_of_scope_redirect,
        )

    @staticmethod
    def _matches(pattern: str, task_type: str) -> bool:
        """简单模式匹配: pattern.endswith('.*') → 前缀匹配; 否则 exact."""
        if pattern.endswith(".*"):
            prefix = pattern[:-2]
            return task_type == prefix or task_type.startswith(prefix + ".")
        return pattern == task_type

    @staticmethod
    def _heuristic_overlap(task_type: str, scope: ScopeConfig) -> float:
        """task_type vs role_name 的词共现 (启发式)."""
        if not task_type:
            return 0.5  # 没信息中性
        # 把 task_type "coding.python.fastapi" 拆成 ["coding", "python", "fastapi"]
        task_terms = set(task_type.lower().replace("_", ".").split("."))
        role_terms = set(scope.role_name.lower().replace("_", " ").split())
        if not task_terms or not role_terms:
            return 0.5
        overlap = len(task_terms & role_terms)
        if overlap == 0:
            return 0.2  # 词完全不重叠 → off-topic 嫌疑高
        if overlap == 1:
            return 0.5  # 一个词重叠 → 中性
        return 0.8  # 多个词重叠 → 大概率 in-scope

    def get_stats(self) -> dict[str, int]:
        return dict(self._stats)


__all__ = [
    "BoundaryDecision",
    "ScopeConfig",
    "TaskBoundaryGuard",
]
