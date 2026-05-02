"""中央重要度打分器。

这个模块只做纯工程打分，不调 LLM。后续如果接 Qdrant/embedding，只要把
``embed_text`` 函数传进 ``ImportanceScorer`` 即可。
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from kun.context.assets import LayeredAsset
from kun.core.scoring import ScoreDescriptor

EmbedText = Callable[[str], Sequence[float]]
ImportanceTier = Literal["permanent", "long", "short"]

# V2.1.2 §3.2 / §18.2: 5 维基线权重 (按场景动态算, 不写死任何一维上限)
DEFAULT_WEIGHTS = {
    "semantic": 0.20,
    "frequency": 0.20,
    "recency": 0.20,
    "dependency": 0.20,
    "pin": 0.20,
}
# V1 兼容: 旧 3 维权重 (传入只含 semantic/frequency/recency 时自动 backfill)
LEGACY_3D_WEIGHTS = {
    "semantic": 0.5,
    "frequency": 0.3,
    "recency": 0.2,
}
FREQUENCY_SATURATION_COUNT = 100
LONG_HALF_LIFE_DAYS = 11.25
PIN_HALF_LIFE_DAYS = 90.0  # V2 §3.5 tier 1 用户 pin
SHORT_HALF_LIFE_DAYS = 5.0


@dataclass(frozen=True)
class ImportanceScore:
    """Context 资产重要度分数 (V2.2 §25 6 维)."""

    overall: float
    semantic: float
    frequency: float
    recency: float
    dependency: float = 0.0  # V2 任务依赖度
    pin: float = 0.0  # V2 用户显式 pin
    contribution: float = 0.0  # V2.2 §25 历史贡献度 (CreditAssignment 反推)
    rationale: str = ""


class ImportanceScorer:
    """按 5 维 (语义相关 + 访问频率 + 近期性 + 任务依赖 + 用户 pin) 给资产打分.

    V2.1.2 §3.2 / §18.2 修订:
    - 不再写死"近期性 ≤ 0.25 铁律", 5 维权重按场景动态算
    - 加 dependency (任务硬依赖) 和 pin (用户显式 pin) 两维
    - 兼容 V1 旧 3 维权重 (会自动 backfill dependency=0 / pin=0)
    """

    def __init__(
        self,
        *,
        embed_text: EmbedText | None = None,
        weights: dict[str, float] | None = None,
    ) -> None:
        self._embed_text = embed_text
        self.weights = _normalize_weights(weights or DEFAULT_WEIGHTS)

    def score(
        self,
        *,
        asset: LayeredAsset,
        query: str | None = None,
        now: datetime | None = None,
        task_dependency_score: float = 0.0,
        pin_boost: float = 0.0,
        weights_override: dict[str, float] | None = None,
    ) -> ImportanceScore:
        """给一个资产打 0..1 的重要度分 (5 维 V2).

        Args:
            task_dependency_score: 0-1, 该资产是否当前任务硬依赖
                (TASK.md required_resources 命中 / 关联任务的能力卡 / 等)
            pin_boost: 0-1, 用户显式 pin 加权 (来自 AttentionAnchor.boost_for_asset)
            weights_override: V2 §18.2 按场景动态算的权重, 不传用 self.weights
        """
        now = now or datetime.now(UTC)
        semantic = self.semantic(asset=asset, query=query)
        frequency = self.frequency(asset.access_count)
        recency = self.recency(asset=asset, now=now)
        dependency = _clamp01(task_dependency_score)
        pin = _clamp01(pin_boost)

        weights = _normalize_weights(weights_override or self.weights)
        overall = _clamp01(
            weights.get("semantic", 0.0) * semantic
            + weights.get("frequency", 0.0) * frequency
            + weights.get("recency", 0.0) * recency
            + weights.get("dependency", 0.0) * dependency
            + weights.get("pin", 0.0) * pin
        )
        return ImportanceScore(
            overall=overall,
            semantic=semantic,
            frequency=frequency,
            recency=recency,
            dependency=dependency,
            pin=pin,
            rationale=(
                f"semantic={semantic:.3f}, frequency={frequency:.3f}, "
                f"recency={recency:.3f}, dependency={dependency:.3f}, "
                f"pin={pin:.3f}; weights={weights}"
            ),
        )

    def semantic(self, *, asset: LayeredAsset, query: str | None = None) -> float:
        """资产和 query 的语义相关度。"""
        if not query:
            return 1.0

        asset_text = _asset_text(asset)
        if not asset_text.strip():
            return 0.0

        if self._embed_text is not None:
            try:
                return _cosine_score(self._embed_text(query), self._embed_text(asset_text))
            except (TypeError, ValueError, ZeroDivisionError):
                # embedding 服务或注入函数异常时，回到本地兜底，不让打分器中断主流程。
                pass

        return _lexical_similarity(query, asset_text)

    @staticmethod
    def frequency(access_count: int) -> float:
        """访问频率分：100 次左右饱和到 1。"""
        n = max(0, access_count)
        return _clamp01(math.log1p(n) / math.log1p(FREQUENCY_SATURATION_COUNT))

    def recency(self, *, asset: LayeredAsset, now: datetime | None = None) -> float:
        """近期性分：按资产层级使用不同半衰期。"""
        half_life = half_life_days(asset)
        if half_life is None:
            return 1.0

        now = now or datetime.now(UTC)
        elapsed_days = (now - asset.last_accessed).total_seconds() / 86400
        if elapsed_days <= 0:
            return 1.0
        return _clamp01(math.exp(-elapsed_days / half_life))

    def score_descriptor(
        self,
        *,
        asset: LayeredAsset,
        query: str | None = None,
        now: datetime | None = None,
        task_dependency_score: float = 0.0,
        pin_boost: float = 0.0,
        weights_override: dict[str, float] | None = None,
    ) -> ScoreDescriptor:
        """兼容现有统一打分展示层 (V2.1.2 5 维)."""
        score = self.score(
            asset=asset,
            query=query,
            now=now,
            task_dependency_score=task_dependency_score,
            pin_boost=pin_boost,
            weights_override=weights_override,
        )
        half_life = half_life_days(asset)
        weights_used = _normalize_weights(weights_override or self.weights)
        return ScoreDescriptor(
            kind="importance",
            value=score.overall,
            components={
                "semantic": score.semantic,
                "frequency": score.frequency,
                "recency": score.recency,
                "dependency": score.dependency,
                "pin": score.pin,
            },
            weights=weights_used,
            sample_size=max(0, asset.access_count),
            decay_half_life_days=None if half_life is None else round(half_life),
        )

    def score_with_anchors(
        self,
        *,
        asset: LayeredAsset,
        query: str | None = None,
        now: datetime | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        task_meta: dict[str, object] | None = None,
        user_meta: dict[str, object] | None = None,
        context_meta: dict[str, object] | None = None,
    ) -> ImportanceScore:
        """V2.1 wire 便捷入口: 自动从 AttentionManager 拉 pin boost +
        从场景算动态权重.

        相当于:
        - boost_for_asset(asset, user, project) → pin_boost (§3.5 tier 1)
        - compute_dimension_weights(task, user, context) → weights_override
        - 返 5 维 score (含 dependency / pin)

        让调用方不需要手动算 anchor + 权重.
        """
        from kun.core.attention_anchor import get_manager  # 局部 import 避免循环

        # 取该 asset 的 pin boost
        asset_ref = asset.l1_metadata.get("asset_id") or getattr(asset, "asset_id", "")
        pin_boost = 0.0
        if asset_ref:
            try:
                pin_boost = get_manager().boost_for_asset(
                    str(asset_ref),
                    user_id=user_id,
                    project_id=project_id,
                )
            except Exception:
                pin_boost = 0.0

        # 任务依赖度: 从 task_meta.required_resources 简单匹配
        task_dependency_score = 0.0
        if task_meta:
            required = task_meta.get("required_resources", []) or []
            if isinstance(required, list) and asset_ref:
                if str(asset_ref) in required:
                    task_dependency_score = 1.0
                # 部分匹配 (asset 含 required keyword)
                else:
                    text_for_match = (
                        str(asset.l2_summary or "")
                        + " "
                        + " ".join(str(t) for t in (asset.tags or []))
                    ).lower()
                    for r in required:
                        if str(r).lower() in text_for_match:
                            task_dependency_score = max(task_dependency_score, 0.6)

        # 按场景算权重
        weights = self.compute_dimension_weights(
            task_meta=task_meta,
            user_meta=user_meta,
            context_meta=context_meta,
        )

        return self.score(
            asset=asset,
            query=query,
            now=now,
            task_dependency_score=task_dependency_score,
            pin_boost=pin_boost,
            weights_override=weights,
        )

    def compute_dimension_weights(
        self,
        *,
        task_meta: dict[str, object] | None = None,
        user_meta: dict[str, object] | None = None,
        context_meta: dict[str, object] | None = None,
    ) -> dict[str, float]:
        """V2.1.2 §18.2.1: 按场景动态算 5 维权重 (不写死任何上限).

        基线:5 维等权 0.20. 按场景调整:
        - 任务明示"按最新偏好" → recency +0.20
        - 任务有 required_resources → dependency +0.15
        - 长对话频繁切话题 → semantic +0.15
        - 用户刚 pin 资产 → pin +0.20
        - 高频复用任务 → frequency +0.10
        - 大决策 → 全维放平
        - critical 风险 → 单维 cap 0.50 防被一维带跑
        """
        task = task_meta or {}
        user = user_meta or {}
        context = context_meta or {}

        w = dict(DEFAULT_WEIGHTS)

        # 场景 1: 任务明示按最新
        intent = str(task.get("intent_text", "")).lower()
        if any(s in intent for s in ("最新", "现在", "刚才", "latest", "recent")):
            w["recency"] += 0.20

        # 场景 2: 有 required_resources
        if task.get("required_resources"):
            w["dependency"] += 0.15

        # 场景 3: 长对话频繁切话题
        topic_switches = context.get("topic_switches_in_session", 0)
        if isinstance(topic_switches, int) and topic_switches >= 3:
            w["semantic"] += 0.15

        # 场景 4: 用户刚 pin
        if user.get("recent_pin_action"):
            w["pin"] += 0.20

        # 场景 5: 高频复用 task_type
        if task.get("is_high_frequency_type"):
            w["frequency"] += 0.10

        # 场景 6: 大决策 → 全维放平 (向基线靠拢)
        if task.get("is_major_decision"):
            for k in w:
                w[k] = (w[k] + DEFAULT_WEIGHTS[k]) / 2

        # 场景 7: critical 风险 → 单维 cap 0.50
        if task.get("risk_level") == "critical":
            w = {k: min(v, 0.50) for k, v in w.items()}

        return _normalize_weights(w)

    def review_needed(self, *, asset: LayeredAsset, score: ImportanceScore) -> bool:
        """判断是否需要后续便宜模型复审。

        这里只返回信号，不在打分器里直接调模型或写事件，避免 T1 污染主流程。
        """
        repeatedly_used_but_low = asset.access_count >= 10 and score.overall < 0.25
        long_unvisited_but_kept = asset.access_count == 0 and score.recency < 0.05
        return repeatedly_used_but_low or long_unvisited_but_kept

    async def score_with_contribution_boost(
        self,
        candidates: list[LayeredAsset],
        *,
        query: str | None = None,
        now: datetime | None = None,
        contribution_lookup: Callable[[str], float] | None = None,
        boost_weight: float = 0.20,
    ) -> list[tuple[LayeredAsset, ImportanceScore]]:
        """V2.2 §25 wire: 用 contribution_history 加 boost.

        contribution_lookup: callable(asset_id) → float [0..1]. None → 退化普通 score.

        典型用法:
        - 调用方注入 lookup_fn (从 CreditAssignment / capability_card 拿历史 credit)
        - 高 contribution 资产自动 boost overall
        - 跟 graph_boost / pin / dependency 累加 (cap 1.0)

        Returns:
            list[(LayeredAsset, ImportanceScore)] 按 overall 降序
        """
        scoring_now = now or datetime.now(UTC)
        scored: list[tuple[LayeredAsset, ImportanceScore]] = []
        for asset in candidates:
            base_score = self.score(asset=asset, query=query, now=scoring_now)
            asset_id = str(asset.asset_id)

            # 拿 contribution
            contribution = 0.0
            if contribution_lookup is not None:
                try:
                    contribution = float(contribution_lookup(asset_id))
                    contribution = max(0.0, min(1.0, contribution))
                except Exception:
                    contribution = 0.0

            # 加权进 overall (cap 1.0)
            boosted_overall = min(1.0, base_score.overall + boost_weight * contribution)

            scored.append(
                (
                    asset,
                    ImportanceScore(
                        overall=boosted_overall,
                        semantic=base_score.semantic,
                        frequency=base_score.frequency,
                        recency=base_score.recency,
                        dependency=base_score.dependency,
                        pin=base_score.pin,
                        contribution=contribution,
                        rationale=(
                            base_score.rationale
                            + f"; contribution={contribution:.2f} (boost={boost_weight:.2f})"
                            if contribution > 0
                            else base_score.rationale
                        ),
                    ),
                )
            )

        scored.sort(key=lambda x: _stable_score_sort_key(x[0], x[1]))
        return scored

    async def score_with_graph_boost(
        self,
        candidates: list[LayeredAsset],
        *,
        anchor_entity_kind: str = "memory",
        anchor_entity_id: str | None = None,
        tenant_id: str,
        query: str | None = None,
        now: datetime | None = None,
        graph_boost: float = 0.15,
        relation_types: list[str] | None = None,
        min_confidence: float = 0.5,
    ) -> list[tuple[LayeredAsset, ImportanceScore]]:
        """V2.2 §20.3 wire: 沿知识图谱 (entity_relationships) 邻接 boost.

        给一个 anchor (e.g. 上一步用的 memory), 查它 outgoing 关系, 找邻接 asset_ids.
        如果 candidate 在邻接集合, overall += graph_boost (默认 +0.15, cap 1.0).

        效果: ImportanceScorer 不只看相似度, 还看"沿路径走" — 上一步用了 A,
        跟 A 有 depends_on/mentions/similar_to 关系的 B/C 自动加分.

        Args:
            candidates: 待打分资产
            anchor_entity_kind: anchor 的 kind (默认 "memory")
            anchor_entity_id: anchor 的 id, 为 None 时不做 graph boost (退化到普通 score)
            tenant_id: RLS 隔离
            graph_boost: 邻接节点的 boost 增量
            relation_types: 限定 relation 类型 (默认全部)
            min_confidence: 关系 confidence 下限

        Returns:
            list[(LayeredAsset, ImportanceScore)] 按 overall 降序
        """
        scoring_now = now or datetime.now(UTC)
        # 1. 查 anchor 的邻接节点
        related_asset_ids: set[str] = set()
        if anchor_entity_id:
            try:
                from kun.datamodel.relationship import get_relationships_from

                rels = await get_relationships_from(
                    entity_kind=anchor_entity_kind,
                    entity_id=anchor_entity_id,
                    tenant_id=tenant_id,
                    relation_types=relation_types,  # type: ignore[arg-type]
                    min_confidence=min_confidence,
                )
                for rel in rels:
                    related_asset_ids.add(rel.target_entity_id)
            except Exception:
                # DB / migration 缺失 → 退化到普通 score
                related_asset_ids = set()

        # 2. 对每个 candidate 算 score, 加 graph boost
        scored: list[tuple[LayeredAsset, ImportanceScore]] = []
        for asset in candidates:
            base_score = self.score(asset=asset, query=query, now=scoring_now)
            asset_id_str = str(asset.asset_id)
            if asset_id_str in related_asset_ids:
                # 邻接节点加 boost (cap 1.0)
                boosted_overall = min(1.0, base_score.overall + graph_boost)
                boosted_score = ImportanceScore(
                    overall=boosted_overall,
                    semantic=base_score.semantic,
                    frequency=base_score.frequency,
                    recency=base_score.recency,
                    dependency=base_score.dependency,
                    pin=base_score.pin,
                    rationale=base_score.rationale + f"; graph_boost=+{graph_boost:.2f}",
                )
                scored.append((asset, boosted_score))
            else:
                scored.append((asset, base_score))

        scored.sort(key=lambda x: _stable_score_sort_key(x[0], x[1]))
        return scored

    def score_anchor_then_expand(
        self,
        candidates: list[LayeredAsset],
        *,
        query: str | None = None,
        now: datetime | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        task_meta: dict[str, object] | None = None,
        user_meta: dict[str, object] | None = None,
        context_meta: dict[str, object] | None = None,
        max_rounds: int = 3,
        use_marginal_stop: bool = True,
        graph_traversal: Any = None,
        graph_hops: int = 1,
        candidate_entity_kind: str = "asset",
    ) -> Any:
        """V2.2 §19.3 + §20.3: 按需扩展式打分 + mempalace 路径走查 (Wire 30).

        不一次性打全部 K 个候选, 而是流式 yield (asset, score):
        1. 第 1 轮: yield 评分最高的 anchor
        2. 后续轮次:
           - 没 graph_traversal → yield 评分次高 (现有行为)
           - 有 graph_traversal → 优先沿 anchor 的 entity_relationships 邻接展开
             (mempalace 精髓: 沿 path 走, 不是"找最像的")
        3. ≤ max_rounds 个 (默认 3), marginal_roi 自动判停

        用法 (V2.1 现有):
            async for asset, score in scorer.score_anchor_then_expand(candidates, ...):
                use(asset)
                if my_caller_satisfied: break

        用法 (V2.2 §20 mempalace, Wire 30):
            from kun.context.graph_traversal import GraphTraversal
            traversal = GraphTraversal()
            async for asset, score in scorer.score_anchor_then_expand(
                candidates, graph_traversal=traversal, graph_hops=1,
                candidate_entity_kind="asset",
            ):
                ...

        Args:
            graph_traversal: 可选 GraphTraversal 实例. 给了 → 沿 entity_relationships
                              图扩展; None → 现有行为 (按 score 降序).
            graph_hops: 图扩展跳数 (默认 1 = 直接邻居)
            candidate_entity_kind: candidates 在 entity_relationships 表里的 kind
                                    (默认 "asset"; capability_card / skill / soul_file 等)

        Returns:
            AsyncIterator[tuple[LayeredAsset, ImportanceScore]]
        """
        from kun.core.anchor_expand import AnchorExpandIterator
        from kun.engineering.marginal_roi import (
            MarginalROIStopCriterion,
            ModulePresets,
            ValueEstimator,
        )

        # 预 score 全部, 按 stable score 降序. 这里统一采样 now，避免同一批候选
        # 因为微秒级 recency 抖动导致 anchor 选择飘。
        scoring_now = now or datetime.now(UTC)
        scored: list[tuple[LayeredAsset, ImportanceScore]] = []
        for asset in candidates:
            sc = self.score_with_anchors(
                asset=asset,
                query=query,
                now=scoring_now,
                user_id=user_id,
                project_id=project_id,
                task_meta=task_meta,
                user_meta=user_meta,
                context_meta=context_meta,
            )
            scored.append((asset, sc))
        scored.sort(key=lambda x: _stable_score_sort_key(x[0], x[1]))

        # asset_id → index in scored, 让 graph traversal 拿到 entity_id 后能反查
        id_to_idx = {self._asset_entity_id(a): i for i, (a, _) in enumerate(scored)}

        async def anchor_fn() -> tuple[LayeredAsset, ImportanceScore]:
            if not scored:
                raise StopAsyncIteration
            return scored[0]

        async def expand_fn(
            anchor: tuple[LayeredAsset, ImportanceScore],
            prior: list[tuple[LayeredAsset, ImportanceScore]],
        ) -> tuple[LayeredAsset, ImportanceScore] | None:
            consumed_idxs = {id_to_idx.get(self._asset_entity_id(a)) for a, _ in prior}
            consumed_idxs.add(0)  # anchor 是 index 0
            consumed_idxs.discard(None)

            # Wire 30: graph_traversal 给了 → 优先邻接
            if graph_traversal is not None:
                anchor_asset = anchor[0]
                anchor_id = self._asset_entity_id(anchor_asset)
                try:
                    neighbors = await graph_traversal.neighbors(
                        kind=candidate_entity_kind,
                        entity_id=anchor_id,
                        hops=graph_hops,
                    )
                except Exception:
                    neighbors = []
                for n in neighbors:
                    nidx = id_to_idx.get(n.entity_id)
                    if nidx is None or nidx in consumed_idxs:
                        continue
                    return scored[nidx]
                # graph 没邻接 / 邻接全消费过 → fallback 按 score 降序

            for nidx, _pair in enumerate(scored):
                if nidx in consumed_idxs:
                    continue
                return scored[nidx]
            return None

        criterion: MarginalROIStopCriterion | None = None
        estimator: ValueEstimator | None = None
        if use_marginal_stop:
            criterion = ModulePresets.for_memory_expand()
            estimator = ValueEstimator(
                custom_fn=lambda item, prior: float(item[1].overall),
            )

        return AnchorExpandIterator(
            anchor_fn=anchor_fn,
            expand_fn=expand_fn,
            max_rounds=max_rounds,
            stop_criterion=criterion,
            value_estimator=estimator,
        )

    @staticmethod
    def _asset_entity_id(asset: LayeredAsset) -> str:
        """LayeredAsset → entity_relationships.entity_id 字符串.

        优先级: l1_metadata['entity_id'] > l1_metadata['asset_id'] > id(asset).
        Wire 30 graph traversal 用这个匹配 entity_relationships 表.
        """
        meta = getattr(asset, "l1_metadata", None) or {}
        for key in ("entity_id", "asset_id", "ref"):
            v = meta.get(key)
            if isinstance(v, str) and v:
                return v
        return f"asset-{id(asset)}"


def half_life_days(asset: LayeredAsset) -> float | None:
    """根据资产元数据/类型得到半衰期。None 表示永久档。"""
    tier = _importance_tier(asset)
    if tier == "permanent":
        return None
    if tier == "short":
        return SHORT_HALF_LIFE_DAYS
    return LONG_HALF_LIFE_DAYS


def _importance_tier(asset: LayeredAsset) -> ImportanceTier:
    raw = str(
        asset.l1_metadata.get("importance_tier")
        or asset.l1_metadata.get("retention_tier")
        or asset.l1_metadata.get("tier")
        or ""
    ).lower()
    tags = {tag.lower() for tag in asset.tags}
    if raw in {"permanent", "tier0", "forever"} or tags & {"permanent", "tier0"}:
        return "permanent"
    if raw in {"short", "short_term", "temporary"} or asset.asset_kind in {"task", "handoff"}:
        return "short"
    return "long"


def _asset_text(asset: LayeredAsset) -> str:
    parts: list[str] = [
        asset.asset_kind,
        " ".join(asset.tags),
        asset.l2_summary or "",
        " ".join(f"{key} {value}" for key, value in asset.l1_metadata.items()),
    ]
    return " ".join(parts)


def _stable_score_sort_key(
    asset: LayeredAsset,
    score: ImportanceScore,
) -> tuple[float, float, float, float, float, str]:
    """Deterministic sort key for context candidates.

    重要度相同的资产很常见：例如短 summary、无 query、同一批 freshly-created
    memory。排序如果只看 ``overall``，anchor-expand 会被全局状态或插入顺序带偏。
    这里统一按分数降序、metadata importance_signal 降序、稳定 id 升序排序。
    """

    return (
        -_sort_primary_score(asset, score),
        -score.overall,
        -score.semantic,
        -score.frequency,
        -score.recency,
        _stable_asset_id(asset),
    )


def _sort_primary_score(asset: LayeredAsset, score: ImportanceScore) -> float:
    signal = _metadata_importance_signal(asset)
    return signal if signal > 0 else score.overall


def _metadata_importance_signal(asset: LayeredAsset) -> float:
    raw = asset.l1_metadata.get("importance_signal")
    if raw is None:
        raw = asset.l1_metadata.get("score")
    if raw is None:
        return 0.0
    try:
        return _clamp01(float(raw))
    except (TypeError, ValueError):
        return 0.0


def _stable_asset_id(asset: LayeredAsset) -> str:
    meta = getattr(asset, "l1_metadata", None) or {}
    for key in ("entity_id", "asset_id", "ref"):
        value = meta.get(key)
        if isinstance(value, str) and value:
            return value
    return str(getattr(asset, "asset_id", ""))


def _cosine_score(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding vectors must have the same length")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    cosine = dot / (left_norm * right_norm)
    return _clamp01((cosine + 1.0) / 2.0)


def _lexical_similarity(query: str, asset_text: str) -> float:
    query_terms = _terms(query)
    asset_terms = _terms(asset_text)
    if not query_terms or not asset_terms:
        return 0.0
    overlap = len(query_terms & asset_terms)
    return _clamp01(overlap / math.sqrt(len(query_terms) * len(asset_terms)))


def _terms(text: str) -> set[str]:
    return {part.lower() for part in re.findall(r"[\w.-]+", text) if len(part) >= 2}


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    """V2.1.2: 接受 5 维 (semantic/frequency/recency/dependency/pin) 或
    V1 兼容 3 维 (semantic/frequency/recency, 自动 backfill dependency=0/pin=0).

    所有未指定维度按 0 填充, 然后归一化.
    """
    expected = {"semantic", "frequency", "recency", "dependency", "pin"}
    actual = set(weights.keys())
    unknown = actual - expected
    if unknown:
        raise ValueError(
            f"importance weights contain unknown dims {sorted(unknown)}. "
            f"Allowed: {sorted(expected)}"
        )
    # backfill 未指定的维度 = 0 (V1 旧 3 维传进来时, dependency / pin 自动 0)
    full = {key: float(weights.get(key, 0.0)) for key in expected}
    total = sum(full.values())
    if total <= 0:
        raise ValueError("importance weights must sum to a positive number")
    return {key: value / total for key, value in full.items()}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = [
    "ImportanceScore",
    "ImportanceScorer",
    "half_life_days",
]
