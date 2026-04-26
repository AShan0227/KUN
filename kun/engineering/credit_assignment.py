"""Credit Assignment — 信用分配 + 稀疏奖励 shaping (V2.2 §25 / Wire 14).

解决 KUN 长任务 (≥10 step) 的两个 RL 经典问题:
1. 稀疏奖励: 现在只有 task done 才有 outcome, 中间 step 没信号
2. 信用模糊: task pass 时 outcome 平摊到所有 step / 资源 → 真起作用的没被强化

三件套:
- StepCredit: 每 step 记录 resources_used + immediate_reward + credit_share
- DenseRewardSignals: ValueGate ΔV / multi_judge consensus / 边际收益 / code execute pass
- TaskCreditReport: task done 后, 反思关键路径, 给关键 step 的资源 boost credit

跟 V2.2 §17 (capability_card) + §3.2 (ImportanceScorer) + §19.4 (ValueGate) 联动:
- step 完时填 immediate_reward → ValueGate 看
- task done 时反思 → 改 record_outcome 从均摊 → credit-weighted
- 资源的 contribution_score 累计 → ImportanceScorer.score 加 第 6 维 (§25.3.4)

设计原则:
- 不强制接 orchestrator (留接口给后续 wire)
- StepCredit / TaskCreditReport 独立数据模型, 跟 capability_card 通过 reduce 函数集成
- 容错: 反思 LLM 调用失败 → 退化到均摊 (老行为)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# 资源类型 (跟 EntityType 对齐, 加 memory)
ResourceKind = str  # "memory" / "skill" / "model" / "role_template" / "tool"


class StepCredit(BaseModel):
    """每 step 的信用记录."""

    step_id: int
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    finished_at: datetime | None = None

    # step 用的资源 (按 kind 分组)
    # e.g. {"memory": ["mem-1", "mem-2"], "skill": ["coding-pytest"], "model": ["claude-opus"]}
    resources_used: dict[ResourceKind, list[str]] = Field(default_factory=dict)

    # immediate reward (dense intermediate, 不等 task done)
    # 累计 ValueGate ΔV / multi_judge ΔConsensus / marginal ROI 等信号
    immediate_reward: float = 0.0

    # 信用份额 (sum to 1.0, 反思后填), key = "kind:id"
    # e.g. {"memory:mem-1": 0.4, "skill:coding-pytest": 0.4, "model:claude-opus": 0.2}
    credit_share: dict[str, float] = Field(default_factory=dict)

    # 反思后判定 (task done 时由 RetrospectiveReflector 填)
    is_critical_path: bool = False

    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskCreditReport(BaseModel):
    """一个 task 完成后, 整体信用分配报告."""

    task_id: str
    task_outcome: str  # pass / partial / fail
    total_immediate_reward: float
    step_credits: list[StepCredit]
    critical_path_step_ids: list[int] = Field(default_factory=list)
    reflection_summary: str = ""
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class CreditAssignment:
    """信用分配引擎.

    用法:
        ca = CreditAssignment()
        # 每 step 完时
        ca.record_step(step_id=1, resources={"memory": ["m1"], "model": ["claude"]},
                       immediate_reward=0.15)
        # task done 时
        report = await ca.finalize_task(task_id="tk-1", outcome="pass",
                                        reflector=my_reflector)
        # 喂回 capability_writeback
        await ca.distribute_outcome_to_capability(report)

    Args:
        critical_boost_factor: 关键路径上的资源 credit boost (默认 1.5x)
        immediate_reward_floor: dense reward 下限 (避免负反馈反推)
    """

    def __init__(
        self,
        *,
        critical_boost_factor: float = 1.5,
        immediate_reward_floor: float = 0.0,
    ) -> None:
        if critical_boost_factor < 1.0:
            raise ValueError("critical_boost_factor must be >= 1.0")
        self.critical_boost_factor = critical_boost_factor
        self.immediate_reward_floor = immediate_reward_floor
        # task_id → list[StepCredit]
        self._step_credits: dict[str, list[StepCredit]] = {}

    def record_step(
        self,
        task_id: str,
        step_id: int,
        resources: dict[ResourceKind, list[str]],
        immediate_reward: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> StepCredit:
        """每 step 完时调一次."""
        credit = StepCredit(
            step_id=step_id,
            finished_at=datetime.now(UTC),
            resources_used=dict(resources),
            immediate_reward=max(self.immediate_reward_floor, float(immediate_reward)),
            credit_share=self._equal_share(resources),
            metadata=metadata or {},
        )
        self._step_credits.setdefault(task_id, []).append(credit)
        return credit

    @staticmethod
    def _equal_share(resources: dict[ResourceKind, list[str]]) -> dict[str, float]:
        """初始 credit_share: 该 step 用的所有资源平摊."""
        flat: list[str] = []
        for kind, ids in resources.items():
            for rid in ids:
                flat.append(f"{kind}:{rid}")
        if not flat:
            return {}
        share = 1.0 / len(flat)
        return dict.fromkeys(flat, share)

    async def finalize_task(
        self,
        task_id: str,
        outcome: str,
        *,
        reflector: Any | None = None,
    ) -> TaskCreditReport:
        """task done 时调. 跑 RetrospectiveReflector 找关键路径, boost credit.

        Args:
            outcome: "pass" / "partial" / "fail"
            reflector: callable[[task_id, step_credits, outcome], Awaitable[list[int]]]
                返关键路径 step_ids. None → 退化到均摊 (老行为)
        """
        step_credits = self._step_credits.get(task_id, [])
        critical_ids: list[int] = []

        if reflector is not None and step_credits:
            try:
                critical_ids = await reflector(task_id, step_credits, outcome)
            except Exception:
                logger.exception("credit_assignment.reflector failed (falling back to equal share)")

        # boost critical step 的 credit_share
        for step in step_credits:
            if step.step_id in critical_ids:
                step.is_critical_path = True
                step.credit_share = {
                    k: v * self.critical_boost_factor for k, v in step.credit_share.items()
                }

        total_reward = sum(s.immediate_reward for s in step_credits)
        return TaskCreditReport(
            task_id=task_id,
            task_outcome=outcome,
            total_immediate_reward=total_reward,
            step_credits=step_credits,
            critical_path_step_ids=critical_ids,
        )

    def aggregate_resource_credits(self, report: TaskCreditReport) -> dict[str, float]:
        """把 task 的 step credits 汇总到资源级 credit.

        Returns:
            {"memory:m1": 0.6, "skill:coding-pytest": 1.2, ...}
            (boost 后可 > 1.0, 不归一)
        """
        agg: dict[str, float] = {}
        for step in report.step_credits:
            for resource_key, share in step.credit_share.items():
                # 用 share × step 的 immediate_reward 加权 (没 reward 时给基线 0.5)
                base = step.immediate_reward if step.immediate_reward > 0 else 0.5
                agg[resource_key] = agg.get(resource_key, 0.0) + share * base
        return agg

    def reset_task(self, task_id: str) -> None:
        """task 完成后清理 (避免 _step_credits 内存持续增长)."""
        self._step_credits.pop(task_id, None)


# ============================================================================
# RetrospectiveReflector — 反思关键路径
# ============================================================================


async def heuristic_reflector(
    task_id: str,
    step_credits: list[StepCredit],
    outcome: str,
) -> list[int]:
    """启发式反思 (不调 LLM, 测试用).

    规则:
    - immediate_reward > 平均的 step → 关键
    - immediate_reward = 0 但是 final step → 关键 (兜底任务总要 commit)
    """
    if not step_credits:
        return []
    rewards = [s.immediate_reward for s in step_credits]
    avg = sum(rewards) / len(rewards) if rewards else 0
    critical: list[int] = []
    for s in step_credits:
        if s.immediate_reward > avg:
            critical.append(s.step_id)
    if not critical and step_credits:
        # 兜底: 至少最后一步
        critical.append(step_credits[-1].step_id)
    return critical


async def llm_reflector_factory(
    llm_invoker: Any,
) -> Any:
    """生产 reflector: 调 cheap LLM 反思关键路径.

    Args:
        llm_invoker: async fn(prompt) → str (返 LLM 输出)

    Returns:
        async fn(task_id, step_credits, outcome) → list[int]
    """

    async def _reflect(task_id: str, step_credits: list[StepCredit], outcome: str) -> list[int]:
        if not step_credits:
            return []
        steps_summary = "\n".join(
            f"  step {s.step_id}: resources={s.resources_used}, reward={s.immediate_reward:.2f}"
            for s in step_credits
        )
        prompt = (
            f"任务 {task_id} 结果: {outcome}\n"
            f"步骤摘要:\n{steps_summary}\n\n"
            "请判断哪些 step 是关键路径 (没它任务就失败). "
            '只返 JSON, 格式: {"critical_step_ids": [1, 3, 5]}'
        )
        try:
            response = await llm_invoker(prompt)
            import json

            data = json.loads(response)
            ids = data.get("critical_step_ids", [])
            return [int(i) for i in ids if isinstance(i, int | str)]
        except Exception:
            logger.exception("llm_reflector parse failed (returning empty)")
            return []

    return _reflect


class ContributionTracker:
    """全局资源贡献跟踪 (V2.2 §25.3.4 wire).

    累计每个资源 (memory:m1 / skill:s2 / model:m3) 的历史贡献度.
    给 ImportanceScorer.score_with_contribution_boost 用.

    contribution_score = 0.5 × (K/N) + 0.5 × (M/N)
    - N: 总被用次数
    - K: 在 pass 任务里被用的次数
    - M: 在 critical_path 里的次数

    没历史 → 0.0 (新资源不加分).
    """

    def __init__(self) -> None:
        # resource_key (e.g. "memory:m1") → (N, K, M)
        self._stats: dict[str, tuple[int, int, int]] = {}

    def update_from_report(self, report: TaskCreditReport) -> None:
        """task done 后, 用 TaskCreditReport 更新统计."""
        is_pass = report.task_outcome in ("pass", "partial")
        for step in report.step_credits:
            for resource_key in step.credit_share:
                n, k, m = self._stats.get(resource_key, (0, 0, 0))
                n += 1
                if is_pass:
                    k += 1
                if step.is_critical_path:
                    m += 1
                self._stats[resource_key] = (n, k, m)

    def contribution_score(self, asset_id: str, kind: str = "memory") -> float:
        """查 contribution score [0..1]. asset_id 可裸 id, 自动加 kind 前缀."""
        key = asset_id if ":" in asset_id else f"{kind}:{asset_id}"
        n, k, m = self._stats.get(key, (0, 0, 0))
        if n == 0:
            return 0.0
        return 0.5 * (k / n) + 0.5 * (m / n)

    def reset(self) -> None:
        self._stats.clear()


_tracker: ContributionTracker | None = None


def get_contribution_tracker() -> ContributionTracker:
    """singleton getter."""
    global _tracker
    if _tracker is None:
        _tracker = ContributionTracker()
    return _tracker


def reset_contribution_tracker() -> None:
    global _tracker
    _tracker = None


__all__ = [
    "ContributionTracker",
    "CreditAssignment",
    "StepCredit",
    "TaskCreditReport",
    "get_contribution_tracker",
    "heuristic_reflector",
    "llm_reflector_factory",
    "reset_contribution_tracker",
]
