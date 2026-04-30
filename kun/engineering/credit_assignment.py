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
from collections.abc import Iterable
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from kun.core.metrics import resource_credit_update_total
from kun.core.orm import ResourceCreditRow

logger = logging.getLogger(__name__)


# 资源类型 (跟 EntityType 对齐, 加 memory)
ResourceKind = str  # "memory" / "skill" / "model" / "role_template" / "tool"


# V2.2 §25.4a RewardMap (ICLR 2026 启发): step 内部 4 子阶段
StageKind = Literal["perceive", "understand", "reason", "decide"]


class StageReward(BaseModel):
    """V2.2 §25.4a — step 内部子阶段奖励 (RewardMap 启发).

    把"一个 step 一个 reward"拆成 4 个子阶段各自 reward, 让 KUN 知道
    "在哪个子阶段错了" → 学习效率暴涨.
    """

    stage: StageKind
    reward: float = Field(ge=0.0, le=1.0)
    reason: str = ""


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
    # V2.2 §25.4a: 默认从 stage_rewards 平均算; 也可调用方手动设
    immediate_reward: float = 0.0

    # V2.2 §25.4a: 4 子阶段各自 reward (perceive / understand / reason / decide)
    # 留空 → step 没走 RewardMap 模式, immediate_reward 是手动设的标量
    stage_rewards: list[StageReward] = Field(default_factory=list)

    # 信用份额 (sum to 1.0, 反思后填), key = "kind:id"
    credit_share: dict[str, float] = Field(default_factory=dict)

    # 反思后判定 (task done 时由 RetrospectiveReflector 填)
    is_critical_path: bool = False

    metadata: dict[str, Any] = Field(default_factory=dict)

    def compute_stage_aggregated_reward(self) -> float:
        """V2.2 §25.4a: 从 stage_rewards 算平均 immediate_reward.

        如果 stage_rewards 不空 → 用它; 否则保留 self.immediate_reward.
        """
        if not self.stage_rewards:
            return self.immediate_reward
        return sum(s.reward for s in self.stage_rewards) / len(self.stage_rewards)


class TaskCreditReport(BaseModel):
    """一个 task 完成后, 整体信用分配报告."""

    task_id: str
    task_outcome: str  # pass / partial / fail
    total_immediate_reward: float
    step_credits: list[StepCredit]
    critical_path_step_ids: list[int] = Field(default_factory=list)
    reflection_summary: str = ""
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ResourceCreditDelta(BaseModel):
    """一个 task 对某个资源产生的可持久化信用增量."""

    resource_key: str
    resource_kind: str
    resource_id: str
    used_count: int = 0
    pass_count: int = 0
    critical_count: int = 0
    credit_total: float = 0.0


class ResourceCreditSummary(BaseModel):
    """Human/NUO-facing durable resource credit row.

    This turns the raw ``resource_credit_stats`` counters into an inspectable
    scorecard: what resource was used, how often it helped, and whether it sat
    on the critical path.  It is intentionally compact so it can be shown in
    NUO and CLI without exposing all task internals.
    """

    model_config = ConfigDict(extra="forbid")

    resource_key: str
    resource_kind: str
    resource_id: str
    contribution_score: float
    used_count: int
    pass_count: int
    critical_count: int
    credit_total: float
    last_seen_at: datetime | None = None


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
        stage_rewards: list[StageReward] | None = None,
    ) -> StepCredit:
        """每 step 完时调一次.

        Args:
            stage_rewards: V2.2 §25.4a — 可选 4 子阶段 reward (RewardMap 模式).
                          给了的话 immediate_reward 自动算 (覆盖手动 reward).
        """
        if stage_rewards:
            # RewardMap 模式: 用 4 子阶段平均
            computed_reward = sum(s.reward for s in stage_rewards) / len(stage_rewards)
        else:
            computed_reward = float(immediate_reward)
        credit = StepCredit(
            step_id=step_id,
            finished_at=datetime.now(UTC),
            resources_used=dict(resources),
            immediate_reward=max(self.immediate_reward_floor, computed_reward),
            stage_rewards=stage_rewards or [],
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

    def aggregate_resource_deltas(
        self,
        report: TaskCreditReport,
    ) -> dict[str, ResourceCreditDelta]:
        """把 task report 汇总成 DB 可 upsert 的资源信用增量.

        这比 ``aggregate_resource_credits`` 更适合长期 MoE 学习:
        - used_count: 资源被用过几次
        - pass_count: 资源参与过多少 pass/partial 任务
        - critical_count: 资源是否在关键路径上
        - credit_total: 资源拿到的加权信用总分
        """
        is_pass = report.task_outcome in ("pass", "partial")
        deltas: dict[str, ResourceCreditDelta] = {}
        for step in report.step_credits:
            base = step.immediate_reward if step.immediate_reward > 0 else 0.5
            for resource_key, share in step.credit_share.items():
                kind, resource_id = split_resource_key(resource_key)
                delta = deltas.setdefault(
                    resource_key,
                    ResourceCreditDelta(
                        resource_key=resource_key,
                        resource_kind=kind,
                        resource_id=resource_id,
                    ),
                )
                delta.used_count += 1
                if is_pass:
                    delta.pass_count += 1
                if step.is_critical_path:
                    delta.critical_count += 1
                delta.credit_total += max(0.0, share * base)
        return deltas

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
    """Tenant-scoped resource contribution hot cache (V2.2 §25.3.4 wire).

    累计每个租户下每个资源 (memory:m1 / skill:s2 / model:m3) 的历史贡献度.
    给 ImportanceScorer.score_with_contribution_boost 用.

    contribution_score = 0.5 × (K/N) + 0.5 × (M/N)
    - N: 总被用次数
    - K: 在 pass 任务里被用的次数
    - M: 在 critical_path 里的次数

    没历史 → 0.0 (新资源不加分).
    """

    def __init__(self) -> None:
        # (tenant_id, resource_key) → (N, K, M)
        self._stats: dict[tuple[str, str], tuple[int, int, int]] = {}

    @staticmethod
    def _tenant_scope(tenant_id: str | None) -> str:
        return tenant_id or "__global__"

    def update_from_report(self, report: TaskCreditReport, *, tenant_id: str | None = None) -> None:
        """task done 后, 用 TaskCreditReport 更新统计."""
        is_pass = report.task_outcome in ("pass", "partial")
        tenant_scope = self._tenant_scope(tenant_id)
        for step in report.step_credits:
            for resource_key in step.credit_share:
                scoped_key = (tenant_scope, resource_key)
                n, k, m = self._stats.get(scoped_key, (0, 0, 0))
                n += 1
                if is_pass:
                    k += 1
                if step.is_critical_path:
                    m += 1
                self._stats[scoped_key] = (n, k, m)

    def update_from_deltas(
        self,
        deltas: dict[str, ResourceCreditDelta],
        *,
        tenant_id: str | None = None,
    ) -> None:
        """用 DB 同构的 delta 更新进程内热 cache."""
        tenant_scope = self._tenant_scope(tenant_id)
        for resource_key, delta in deltas.items():
            scoped_key = (tenant_scope, resource_key)
            n, k, m = self._stats.get(scoped_key, (0, 0, 0))
            self._stats[scoped_key] = (
                n + delta.used_count,
                k + delta.pass_count,
                m + delta.critical_count,
            )

    def seed_counts(
        self,
        resource_key: str,
        *,
        used_count: int,
        pass_count: int,
        critical_count: int,
        tenant_id: str | None = None,
    ) -> None:
        """从持久化统计灌入热 cache, 不覆盖更高的本地计数."""
        scoped_key = (self._tenant_scope(tenant_id), resource_key)
        n, k, m = self._stats.get(scoped_key, (0, 0, 0))
        self._stats[scoped_key] = (
            max(n, used_count),
            max(k, pass_count),
            max(m, critical_count),
        )

    def contribution_score(
        self,
        asset_id: str,
        kind: str = "memory",
        *,
        tenant_id: str | None = None,
    ) -> float:
        """查 contribution score [0..1]. asset_id 可裸 id, 自动加 kind 前缀."""
        key = asset_id if ":" in asset_id else f"{kind}:{asset_id}"
        n, k, m = self._stats.get((self._tenant_scope(tenant_id), key), (0, 0, 0))
        return contribution_score_from_counts(used_count=n, pass_count=k, critical_count=m)

    def reset(self) -> None:
        self._stats.clear()


def split_resource_key(resource_key: str) -> tuple[str, str]:
    """Split ``kind:id`` with a safe fallback for legacy bare ids."""
    kind, sep, resource_id = resource_key.partition(":")
    if not sep:
        return "memory", resource_key
    return kind or "memory", resource_id or resource_key


def make_resource_key(kind: str, resource_id: str) -> str:
    return resource_id if ":" in resource_id else f"{kind}:{resource_id}"


def contribution_score_from_counts(
    *,
    used_count: int,
    pass_count: int,
    critical_count: int,
) -> float:
    if used_count <= 0:
        return 0.0
    safe_pass = max(0, min(pass_count, used_count))
    safe_critical = max(0, min(critical_count, used_count))
    return 0.5 * (safe_pass / used_count) + 0.5 * (safe_critical / used_count)


async def persist_resource_credit_report(
    session: AsyncSession,
    *,
    tenant_id: str,
    report: TaskCreditReport,
) -> dict[str, ResourceCreditDelta]:
    """Persist task credit deltas with an atomic Postgres upsert."""
    deltas = CreditAssignment().aggregate_resource_deltas(report)
    if not deltas:
        return {}
    now = datetime.now(UTC)
    rows = [
        {
            "tenant_id": tenant_id,
            "resource_key": delta.resource_key,
            "resource_kind": delta.resource_kind,
            "resource_id": delta.resource_id,
            "used_count": delta.used_count,
            "pass_count": delta.pass_count,
            "critical_count": delta.critical_count,
            "credit_total": delta.credit_total,
            "last_seen_at": now,
            "updated_at": now,
        }
        for delta in deltas.values()
    ]
    stmt = pg_insert(ResourceCreditRow).values(rows)
    excluded = stmt.excluded
    upsert = stmt.on_conflict_do_update(
        index_elements=[ResourceCreditRow.tenant_id, ResourceCreditRow.resource_key],
        set_={
            "resource_kind": excluded.resource_kind,
            "resource_id": excluded.resource_id,
            "used_count": ResourceCreditRow.used_count + excluded.used_count,
            "pass_count": ResourceCreditRow.pass_count + excluded.pass_count,
            "critical_count": ResourceCreditRow.critical_count + excluded.critical_count,
            "credit_total": ResourceCreditRow.credit_total + excluded.credit_total,
            "last_seen_at": excluded.last_seen_at,
            "updated_at": now,
        },
    )
    await session.execute(upsert)
    for delta in deltas.values():
        resource_credit_update_total.labels(
            tenant_id=tenant_id,
            resource_kind=delta.resource_kind,
        ).inc(delta.used_count)
    return deltas


async def load_resource_credit_scores(
    session: AsyncSession,
    *,
    tenant_id: str,
    resource_keys: Iterable[str],
) -> dict[str, float]:
    """Load durable contribution scores for resource keys."""
    keys = sorted({key for key in resource_keys if key})
    if not keys:
        return {}
    result = await session.execute(
        select(ResourceCreditRow).where(
            ResourceCreditRow.tenant_id == tenant_id,
            ResourceCreditRow.resource_key.in_(keys),
        )
    )
    rows = result.scalars().all()
    scores: dict[str, float] = {}
    tracker = get_contribution_tracker()
    for row in rows:
        scores[row.resource_key] = contribution_score_from_counts(
            used_count=row.used_count,
            pass_count=row.pass_count,
            critical_count=row.critical_count,
        )
        tracker.seed_counts(
            row.resource_key,
            used_count=row.used_count,
            pass_count=row.pass_count,
            critical_count=row.critical_count,
            tenant_id=tenant_id,
        )
    return scores


async def load_top_resource_credit(
    session: AsyncSession,
    *,
    tenant_id: str,
    resource_kind: str | None = None,
    limit: int = 20,
) -> list[ResourceCreditSummary]:
    """Load the strongest durable MoE/resource contributors for NUO/CLI."""

    safe_limit = max(1, min(int(limit), 100))
    stmt = (
        select(ResourceCreditRow)
        .where(ResourceCreditRow.tenant_id == tenant_id)
        .order_by(ResourceCreditRow.credit_total.desc(), ResourceCreditRow.updated_at.desc())
        .limit(safe_limit)
    )
    if resource_kind:
        stmt = stmt.where(ResourceCreditRow.resource_kind == resource_kind)
    result = await session.execute(stmt)
    return resource_credit_summaries_from_rows(result.scalars().all())


def resource_credit_summaries_from_rows(rows: Iterable[Any]) -> list[ResourceCreditSummary]:
    """Convert ORM-ish rows to compact resource credit summaries."""

    summaries: list[ResourceCreditSummary] = []
    for row in rows:
        summaries.append(
            ResourceCreditSummary(
                resource_key=str(row.resource_key),
                resource_kind=str(row.resource_kind),
                resource_id=str(row.resource_id),
                contribution_score=round(
                    contribution_score_from_counts(
                        used_count=int(row.used_count),
                        pass_count=int(row.pass_count),
                        critical_count=int(row.critical_count),
                    ),
                    4,
                ),
                used_count=int(row.used_count),
                pass_count=int(row.pass_count),
                critical_count=int(row.critical_count),
                credit_total=round(float(row.credit_total), 4),
                last_seen_at=getattr(row, "last_seen_at", None),
            )
        )
    return summaries


async def hydrate_contribution_tracker_from_db(
    session: AsyncSession,
    *,
    tenant_id: str,
    resource_kinds: Iterable[str] | None = None,
    limit: int = 500,
    min_interval_sec: float = 300.0,
) -> int:
    """Seed the in-process contribution tracker from durable DB stats.

    这一步是 V4 里 MoE 闭环的关键补线：资源信用已经写进
    ``resource_credit_stats``，但进程重启后 Watchtower 只看热 cache 会变笨。
    在守望决策前按租户轻量预热一次，让策略包 / skill / memory 的历史信用
    能继续影响本次路径选择。
    """
    if not tenant_id:
        return 0
    kinds = tuple(sorted({str(kind) for kind in (resource_kinds or []) if str(kind)}))
    cache_key = (tenant_id, kinds)
    now = monotonic()
    last = _hydration_last_run.get(cache_key)
    if last is not None and min_interval_sec > 0 and now - last < min_interval_sec:
        return 0

    stmt = (
        select(ResourceCreditRow)
        .where(ResourceCreditRow.tenant_id == tenant_id)
        .order_by(ResourceCreditRow.updated_at.desc())
        .limit(max(1, int(limit)))
    )
    if kinds:
        stmt = stmt.where(ResourceCreditRow.resource_kind.in_(kinds))
    result = await session.execute(stmt)
    rows = result.scalars().all()
    tracker = get_contribution_tracker()
    for row in rows:
        tracker.seed_counts(
            row.resource_key,
            used_count=row.used_count,
            pass_count=row.pass_count,
            critical_count=row.critical_count,
            tenant_id=tenant_id,
        )
    _hydration_last_run[cache_key] = now
    return len(rows)


_tracker: ContributionTracker | None = None
_hydration_last_run: dict[tuple[str, tuple[str, ...]], float] = {}


def get_contribution_tracker() -> ContributionTracker:
    """singleton getter."""
    global _tracker
    if _tracker is None:
        _tracker = ContributionTracker()
    return _tracker


def reset_contribution_tracker() -> None:
    global _tracker
    _tracker = None
    _hydration_last_run.clear()


__all__ = [
    "ContributionTracker",
    "CreditAssignment",
    "ResourceCreditDelta",
    "ResourceCreditSummary",
    "StageKind",
    "StageReward",
    "StepCredit",
    "TaskCreditReport",
    "contribution_score_from_counts",
    "get_contribution_tracker",
    "heuristic_reflector",
    "hydrate_contribution_tracker_from_db",
    "llm_reflector_factory",
    "load_resource_credit_scores",
    "load_top_resource_credit",
    "make_resource_key",
    "persist_resource_credit_report",
    "reset_contribution_tracker",
    "resource_credit_summaries_from_rows",
    "split_resource_key",
]
