"""Skill selector — pick best skill(s) for a task (§2.1 原子 / 组合 / 元技能).

V4 rule: required_skills / Watchtower hints are strong evidence, not a bypass.
They should lead the list, but historical credit, graph neighbors and capability
cards must still be able to add or reorder nearby candidates. Otherwise the
"MoE learns from experience" loop becomes decorative.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from kun.core.logging import get_logger
from kun.datamodel.task import TaskRef
from kun.skills.loader import SkillRecord, SkillRegistry, get_registry

if TYPE_CHECKING:
    from kun.engineering.capability_cache import CapabilityCardCache

log = get_logger("kun.skills.selector")


class SkillSelector:
    """Map a TaskRef → ordered list of SkillRecord candidates."""

    def __init__(
        self,
        registry: SkillRegistry | None = None,
        *,
        capability_cache: CapabilityCardCache | None = None,
        graph_traversal: Any = None,
    ) -> None:
        self._reg = registry or get_registry()
        self._capability_cache = capability_cache or _default_capability_cache()
        self._graph_traversal = graph_traversal

    def select(
        self,
        task_ref: TaskRef,
        *,
        top_k: int = 3,
        prior_skill: str | None = None,
    ) -> list[SkillRecord]:
        # 1. Required skills / protocol hints are high-priority candidates.
        # They no longer short-circuit the selector; other relevant skills can
        # still enter the list and be boosted by credit/capability evidence.
        scored = _base_skill_candidates(self._reg, task_ref)

        # 2. V4 MoE credit 加成 — 已经被历史证明有贡献的同类 skill 排前.
        # 注意: 只给已有 overlap 的候选加分，不让高信用 skill 跨任务类型乱抢。
        scored = _apply_skill_credit_boost(scored)

        # 3. V2.3 Wire 47: Pheromone 加成 — 上一 skill → this skill 走过路径强 → 加分
        # 蚁群涌现: 多 task 走过的链路自然推上来 (无需手写 graph)
        if prior_skill:
            try:
                from kun.qi.pheromone import get_pheromone_storage, neighbor_pheromone_score

                storage = get_pheromone_storage()
                tenant_id = task_ref.meta.owner.tenant_id if task_ref.meta.owner else "u-sylvan"
                if hasattr(storage, "get_pheromone"):  # InMemory
                    boosted = []
                    for overlap, rec in scored:
                        pheromone = storage.get_pheromone(
                            tenant_id, "skill", prior_skill, "skill", rec.skill_id, "follows"
                        )
                        # base score (overlap) × pheromone_score
                        boost = neighbor_pheromone_score(1.0, pheromone)
                        boosted.append((overlap * boost, rec))
                    scored = boosted
            except Exception:
                log.debug("skill_selector.pheromone_boost_skipped", exc_info=True)

        scored.sort(key=lambda t: (-t[0], t[1].skill_id))
        return [rec for _, rec in scored[:top_k]]

    async def select_with_graph_and_capability(
        self,
        task_ref: TaskRef,
        *,
        top_k: int = 3,
        tenant_id: str | None = None,
        graph_hops: int = 1,
    ) -> list[SkillRecord]:
        """V2.3 Wire 47/49: use skill graph + realtime capability cache.

        Base selection remains deterministic. When graph edges exist, selected
        skills pull adjacent skill nodes into the candidate set. Then skill
        capability cards boost candidates that have worked on this task_type.
        """

        # Pull a slightly wider base set because graph/capability can lift
        # adjacent skills above the initial anchor.
        base = self.select(task_ref, top_k=max(top_k, 5))
        candidates: dict[str, tuple[float, SkillRecord]] = {
            rec.skill_id: (1.0 - idx * 0.05, rec) for idx, rec in enumerate(base)
        }
        tenant = tenant_id or _current_tenant_id()

        if graph_hops > 0:
            traversal = self._graph_traversal or _default_graph_traversal()
            if traversal is not None:
                for rec in base:
                    for neighbor in await _skill_neighbors(traversal, rec.skill_id, graph_hops):
                        if neighbor.entity_kind != "skill":
                            continue
                        neighbor_rec = self._reg.get(neighbor.entity_id)
                        if neighbor_rec is None:
                            continue
                        score = 0.65 + 0.35 * neighbor.score
                        current = candidates.get(neighbor_rec.skill_id)
                        if current is None or score > current[0]:
                            candidates[neighbor_rec.skill_id] = (score, neighbor_rec)

        rescored: list[tuple[float, SkillRecord]] = []
        for base_score, rec in candidates.values():
            score = _apply_single_skill_credit_boost(base_score, rec)
            cap_bonus = 0.0
            if tenant is not None:
                cap = await self._capability_cache.best_capability(
                    tenant_id=tenant,
                    entity_type="skill",
                    entity_id=rec.skill_id,
                    task_type=task_ref.meta.task_type,
                )
                if cap is not None:
                    cap_bonus = 0.5 * cap.capability_score().value
            rescored.append((score + cap_bonus, rec))

        rescored.sort(key=lambda item: (-item[0], item[1].skill_id))
        return [rec for _, rec in rescored[:top_k]]

    def summary(self, skills: list[SkillRecord]) -> str:
        """Produce a compact 'available skills' summary for the LLM prompt.

        Per §3.6 三级渐进披露 — L1 only: name + description.
        """
        if not skills:
            return ""
        lines = [f"- {s.skill_id}: {s.manifest.description}" for s in skills]
        return f"可用技能 (top {len(lines)}):\n" + "\n".join(lines)

    def skill_ids(self) -> list[str]:
        """Return registered skill ids for credit preheating."""

        return [rec.skill_id for rec in self._reg]

    def select_anchor_then_expand(
        self,
        task_ref: TaskRef,
        *,
        max_rounds: int = 3,
        use_marginal_stop: bool = True,
    ) -> Any:
        """V2.2 §19.3: 按需扩展 skill 选择.

        流式 yield SkillRecord, 而不是一次性返 top-K.
        - 第 1 轮: 评分最高的 skill (anchor)
        - 后续: 沿 overlap 排序的次优, ≤ max_rounds
        - marginal_stop: 上一个 skill 跟 anchor overlap 接近 → 停 (避免拉一堆同类)

        用法:
            async for skill in selector.select_anchor_then_expand(task_ref):
                consider(skill)
                if my_caller_satisfied: break

        Returns:
            AsyncIterator[SkillRecord]
        """
        from kun.core.anchor_expand import AnchorExpandIterator
        from kun.engineering.marginal_roi import (
            MarginalROIStopCriterion,
            ValueEstimator,
        )

        # 1. 显式 required_skills 优先
        if task_ref.spec and task_ref.spec.required_skills:
            ordered: list[tuple[int, SkillRecord]] = []
            for sid in task_ref.spec.required_skills:
                rec = self._reg.get(sid)
                if rec is not None:
                    ordered.append((10, rec))  # required = 高 score
        else:
            ordered = []

        # 2. heuristic overlap 排序
        if not ordered:
            task_type = task_ref.meta.task_type
            parts = set(task_type.split("."))
            for rec in self._reg:
                name_parts = set(rec.skill_id.replace("_", "-").split("-"))
                overlap = len(parts & name_parts)
                if overlap > 0:
                    ordered.append((overlap, rec))
            ordered.sort(key=lambda t: (-t[0], t[1].skill_id))

        async def anchor_fn() -> tuple[int, SkillRecord]:
            if not ordered:
                raise StopAsyncIteration
            return ordered[0]

        async def expand_fn(
            anchor: tuple[int, SkillRecord], prior: list[tuple[int, SkillRecord]]
        ) -> tuple[int, SkillRecord] | None:
            idx = len(prior)
            if idx >= len(ordered):
                return None
            return ordered[idx]

        criterion: MarginalROIStopCriterion | None = None
        estimator: ValueEstimator | None = None
        if use_marginal_stop:
            # skill overlap 跌得快就停 (next overlap < 0.5 * anchor overlap)
            criterion = MarginalROIStopCriterion(
                delta_threshold=-0.3,  # value 是 overlap, 跌幅 > 0.3 算明显
                window_k=1,
                min_steps=2,
            )
            estimator = ValueEstimator(
                custom_fn=lambda item, prior: float(item[0]) / 10.0,  # 归一到 0..1
            )

        return AnchorExpandIterator(
            anchor_fn=anchor_fn,
            expand_fn=expand_fn,
            max_rounds=max_rounds,
            stop_criterion=criterion,
            value_estimator=estimator,
        )


_selector: SkillSelector | None = None


def _apply_skill_credit_boost(
    scored: list[tuple[float, SkillRecord]],
) -> list[tuple[float, SkillRecord]]:
    return [(_apply_single_skill_credit_boost(score, rec), rec) for score, rec in scored]


def _base_skill_candidates(
    registry: SkillRegistry,
    task_ref: TaskRef,
) -> list[tuple[float, SkillRecord]]:
    """Build initial candidates without bypassing the MoE evidence layer."""

    candidates: dict[str, tuple[float, SkillRecord]] = {}
    if task_ref.spec and task_ref.spec.required_skills:
        for idx, sid in enumerate(task_ref.spec.required_skills):
            rec = registry.get(sid)
            if rec is not None:
                # Required/hinted skills get a strong head start, not an
                # absolute veto. A very strong, task-relevant capability signal
                # can still lift another candidate above a stale hint.
                candidates[rec.skill_id] = (3.0 - idx * 0.1, rec)

    task_type = task_ref.meta.task_type
    parts = set(task_type.split("."))
    for rec in registry:
        name_parts = set(rec.skill_id.replace("_", "-").split("-"))
        overlap = float(len(parts & name_parts))
        if overlap <= 0:
            continue
        current = candidates.get(rec.skill_id)
        if current is None or overlap > current[0]:
            candidates[rec.skill_id] = (overlap, rec)

    return list(candidates.values())


def _apply_single_skill_credit_boost(score: float, rec: SkillRecord) -> float:
    """Boost relevant skills by durable MoE contribution hot-cache.

    The DB load happens elsewhere (orchestrator finalization / context packer);
    this sync selector only reads the in-process cache.  If the cache is empty,
    behavior is unchanged.
    """

    try:
        from kun.engineering.credit_assignment import get_contribution_tracker

        contribution = get_contribution_tracker().contribution_score(rec.skill_id, "skill")
    except Exception:
        log.debug("skill_selector.credit_boost_skipped", exc_info=True)
        return score
    if contribution <= 0:
        return score
    return score * (1.0 + min(contribution, 1.0))


def get_selector() -> SkillSelector:
    global _selector
    if _selector is None:
        _selector = SkillSelector()
    return _selector


def reset_selector() -> None:
    global _selector
    _selector = None


def _current_tenant_id() -> str | None:
    try:
        from kun.core.tenancy import current_tenant

        return current_tenant().tenant_id
    except Exception:
        return None


def _default_graph_traversal() -> Any:
    try:
        from kun.context.graph_traversal import GraphTraversal

        return GraphTraversal(relation_types=("similar_to", "co_occurs", "depends_on"))
    except Exception:
        return None


def _default_capability_cache() -> Any:
    from kun.engineering.capability_cache import get_capability_card_cache

    return get_capability_card_cache()


async def _skill_neighbors(traversal: Any, skill_id: str, hops: int) -> list[Any]:
    try:
        return cast(
            list[Any], await traversal.neighbors(kind="skill", entity_id=skill_id, hops=hops)
        )
    except Exception:
        log.debug("skill_selector.graph_neighbors_skipped", skill_id=skill_id, exc_info=True)
        return []
