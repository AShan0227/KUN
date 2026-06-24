"""合议层 — 多 Explorer 输出的 dedup + cluster + rank (L4.4).

PROGRESS L4 §合议层: "dedup (embedding 相似度 > 0.85 合并) / cluster / 排序".
本提交工程化实现, 不用 embedding (避免 L4 阶段就引入 embedding model 依赖):

  dedup: Jaccard 相似度 (target_module + change_spec key set + kind)
         ≥ DEDUP_THRESHOLD → 合并 (保留 acceptance_threshold 更严的那条)

  cluster: 按 (target_module, change_spec.kind) 自然聚类

  rank: explorer_mode 优先级 + sampling_rate 风险阶梯 + acceptance 严格度

工程化优先 — L5+ 闭环再加 embedding 相似度 (LLM 兜底).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kun.agents.strategist.service import StrategyExperiment


DEFAULT_DEDUP_JACCARD_THRESHOLD = 0.7
"""Jaccard 相似度 ≥ 此值 → 视为同类候选; 0.7 比 embedding 经验值 0.85 更宽
(无 embedding 精度补偿). 实践: 同 target_module + 同 kind 自然 ≥ 0.6."""


_EXPLORER_RANK: dict[str, int] = {
    "backward": 0,  # 回滚先, 已知安全态
    "conservative": 1,  # 低风险
    "performance": 2,  # shadow 不影响生产
    "aggressive": 3,  # 高风险
    "experimental": 4,  # 未来 mode
}


@dataclass(frozen=True)
class CandidateCluster:
    """同 (target_module, kind) 的候选聚为一簇."""

    cluster_key: str  # f"{target_module}::{change_spec.kind}"
    target_module: str
    change_kind: str
    candidates: list[object]  # list[StrategyExperiment]; 用 object 避免循环类型


def _candidate_signature(c: StrategyExperiment) -> set[str]:
    """计算候选签名 set (用于 Jaccard).

    包含:
      target_module / target_level / explorer_mode / change_spec.kind /
      change_spec keys / rollout_mode
    """
    sig: set[str] = set()
    sig.add(f"target:{c.target_module}")
    sig.add(f"level:{c.target_level}")
    sig.add(f"mode:{c.explorer_mode}")
    sig.add(f"rollout:{c.rollout_mode}")
    kind = c.change_spec.get("kind", "unknown")
    sig.add(f"kind:{kind}")
    for k in c.change_spec:
        sig.add(f"spec_key:{k}")
    return sig


def jaccard_similarity(a: set[str], b: set[str]) -> float:
    """Jaccard 相似度: |A ∩ B| / |A ∪ B|."""
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def deduplicate(
    candidates: list[StrategyExperiment],
    *,
    threshold: float = DEFAULT_DEDUP_JACCARD_THRESHOLD,
) -> list[StrategyExperiment]:
    """两两比对 Jaccard 相似度, ≥ threshold 合并.

    合并策略: 保留 acceptance_threshold 更严的 (forward strict 优先)
    在风险敏感场景里这等于"保留保守候选".
    """
    if len(candidates) <= 1:
        return list(candidates)

    sigs = [_candidate_signature(c) for c in candidates]
    kept: list[int] = []
    dropped: set[int] = set()

    for i in range(len(candidates)):
        if i in dropped:
            continue
        for j in range(i + 1, len(candidates)):
            if j in dropped:
                continue
            sim = jaccard_similarity(sigs[i], sigs[j])
            if sim >= threshold:
                # 决定保留哪个: acceptance_threshold 更严的胜出 (forward 候选
                # 通常严格度反映"目标改善幅度", 严 = 更保守的目标)
                a = candidates[i]
                b = candidates[j]
                if _candidate_score(a) >= _candidate_score(b):
                    dropped.add(j)
                else:
                    dropped.add(i)
                    break
        if i not in dropped:
            kept.append(i)

    return [candidates[k] for k in kept]


def _candidate_score(c: StrategyExperiment) -> float:
    """合并时的"保留分" — 越高越保留.

    引导规则: acceptance_threshold 更严 (近 1.0 for success_rate; 近 0.0 for
    failure_rate-type) + explorer_mode 偏 conservative 加分.
    """
    base = float(c.acceptance_threshold)
    mode_bonus = {
        "conservative": 0.5,
        "backward": 0.4,
        "performance": 0.3,
        "aggressive": 0.1,
        "experimental": 0.0,
    }.get(c.explorer_mode, 0.0)
    return base + mode_bonus


def cluster_by_kind(
    candidates: list[StrategyExperiment],
) -> list[CandidateCluster]:
    """按 (target_module, change_spec.kind) 聚类."""
    buckets: dict[str, list[StrategyExperiment]] = defaultdict(list)
    for c in candidates:
        kind = c.change_spec.get("kind", "unknown")
        key = f"{c.target_module}::{kind}"
        buckets[key].append(c)
    return [
        CandidateCluster(
            cluster_key=key,
            target_module=members[0].target_module,
            change_kind=members[0].change_spec.get("kind", "unknown"),
            candidates=list(members),
        )
        for key, members in buckets.items()
    ]


def rank_candidates(
    candidates: list[StrategyExperiment],
) -> list[StrategyExperiment]:
    """优先级排序 — 返回新列表, 不改原 list.

    排序规则 (key tuple, 元素越小越优先):
      1. explorer_mode 排名 (backward=0 < conservative=1 < ... < aggressive=3)
      2. requires_human_review False 优先 (自动可处理优先)
      3. sampling_rate 较低优先 (风险更小先尝试)
      4. acceptance_threshold 反向 — 严格目标先 (希望减半失败率 vs 仅减 10%)
    """
    def sort_key(c: StrategyExperiment) -> tuple[Any, ...]:
        return (
            _EXPLORER_RANK.get(c.explorer_mode, 99),
            0 if not c.requires_human_review else 1,
            float(c.sampling_rate),
            # acceptance: 取负让"严格 (高数值)" 排前
            -float(c.acceptance_threshold),
        )

    return sorted(candidates, key=sort_key)


def deliberate(
    candidates: list[StrategyExperiment],
    *,
    dedup_threshold: float = DEFAULT_DEDUP_JACCARD_THRESHOLD,
) -> list[StrategyExperiment]:
    """合议层主入口: dedup → rank → 返回精排候选列表.

    不调 cluster — cluster 是给上层 (Gate / 日志 / UI) 展示用的, deliberate
    本身不修改候选只重排.
    """
    if not candidates:
        return []
    deduped = deduplicate(candidates, threshold=dedup_threshold)
    return rank_candidates(deduped)


__all__ = [
    "DEFAULT_DEDUP_JACCARD_THRESHOLD",
    "CandidateCluster",
    "cluster_by_kind",
    "deduplicate",
    "deliberate",
    "jaccard_similarity",
    "rank_candidates",
]
