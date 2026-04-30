"""Recall compact similar-task experience for Watchtower routing.

This is deliberately not a full vector database.  It is the first honest loop:
past task result / meta-decision / execution-process memories are searched
deterministically, then routing and context packing get a small evidence packet.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from kun.context.assets import LayeredAsset
from kun.context.storage import AssetStore, get_store
from kun.datamodel.task import ExecutionMode, TaskRef


class SimilarTaskExperience(BaseModel):
    """Small evidence packet from memory into the decision plane."""

    model_config = ConfigDict(extra="forbid")

    asset_id: str
    memory_layer: str
    task_type: str
    summary: str = ""
    tags: list[str] = Field(default_factory=list)
    strategy_pack_id: str | None = None
    execution_mode: ExecutionMode | None = None
    step_id: int | None = None
    skill_used: str | None = None
    provider: str | None = None
    model: str | None = None
    tier: str | None = None
    validation_outcome: str | None = None
    status: str | None = None
    score_overall: float | None = None
    cost_usd: float | None = None
    similarity_score: float = Field(ge=0.0)
    reason: str = ""

    @property
    def positive_weight(self) -> float:
        """How strongly this experience should reinforce its path."""

        if self.validation_outcome == "fail" or self.status == "failed":
            return 0.0
        base = {
            "pass": 1.0,
            "partial": 0.45,
            None: 0.5,
        }.get(self.validation_outcome, 0.5)
        if self.score_overall is not None:
            base = (base + max(0.0, min(1.0, self.score_overall))) / 2.0
        return round(base * self.similarity_score, 4)


async def recall_similar_task_experiences(
    *,
    tenant_id: str,
    task_ref: TaskRef,
    store: AssetStore | None = None,
    limit: int = 5,
    scan_limit: int = 200,
) -> list[SimilarTaskExperience]:
    """Recall the most useful prior experiences for the current task.

    The goal is not perfect semantic search yet.  It is a cheap, auditable
    bridge from memory writeback into MoE routing:
    - task_result tells us whether a path worked;
    - meta_decision tells us which strategy/model/skills were chosen;
    - execution_process tells us concrete step/tool/model experience;
    - matching task_type/tags/text turns those into sparse strategy/context evidence.
    """

    target_store = store or get_store()
    assets: list[LayeredAsset] = []
    for kind in ("memory", "methodology"):
        assets.extend(
            await target_store.list(
                tenant_id=tenant_id,
                asset_kind=kind,
                limit=scan_limit,
            )
        )

    scored: list[SimilarTaskExperience] = []
    target_text = _task_text(task_ref)
    target_tokens = _tokens(target_text)
    target_tags = _target_tags(task_ref)
    for asset in assets:
        experience = _experience_from_asset(
            asset=asset,
            target_task_type=task_ref.meta.task_type,
            target_tokens=target_tokens,
            target_tags=target_tags,
        )
        if experience is None:
            continue
        if experience.similarity_score <= 0:
            continue
        scored.append(experience)

    scored.sort(
        key=lambda item: (
            -item.positive_weight,
            -item.similarity_score,
            item.asset_id,
        )
    )
    return scored[: max(0, limit)]


def summarize_strategy_votes(
    experiences: list[SimilarTaskExperience],
) -> dict[str, float]:
    """Aggregate positive strategy evidence for Watchtower metadata."""

    votes: defaultdict[str, float] = defaultdict(float)
    for experience in experiences:
        if not experience.strategy_pack_id:
            continue
        weight = experience.positive_weight
        if weight <= 0:
            continue
        votes[experience.strategy_pack_id] += weight
    return {
        key: round(value, 4)
        for key, value in sorted(votes.items(), key=lambda item: (-item[1], item[0]))
    }


def summarize_execution_process_experiences(
    experiences: list[SimilarTaskExperience],
    *,
    limit: int = 3,
) -> list[str]:
    """Return compact prompt-ready hints from related execution-process memories."""

    process_experiences = [
        experience
        for experience in experiences
        if experience.memory_layer == "execution_process" and experience.similarity_score > 0
    ]
    process_experiences.sort(
        key=lambda item: (
            -item.similarity_score,
            item.step_id if item.step_id is not None else 9999,
            item.asset_id,
        )
    )

    lines: list[str] = []
    for experience in process_experiences[: max(0, limit)]:
        route = " / ".join(
            part for part in [experience.skill_used, experience.model, experience.tier] if part
        )
        prefix = f"step={experience.step_id}" if experience.step_id is not None else "step=?"
        if route:
            prefix = f"{prefix}; {route}"
        text = _compact(experience.summary, max_chars=220)
        lines.append(f"{prefix}; similarity={experience.similarity_score:.2f}; {text}")
    return lines


def _experience_from_asset(
    *,
    asset: LayeredAsset,
    target_task_type: str,
    target_tokens: set[str],
    target_tags: set[str],
) -> SimilarTaskExperience | None:
    metadata = asset.l1_metadata
    memory_layer = str(metadata.get("memory_layer") or "")
    if memory_layer not in {"task_result", "meta_decision", "execution_process"}:
        return None
    task_type = str(metadata.get("task_type") or "")
    if not task_type:
        return None

    summary = asset.l2_summary or ""
    tag_set = set(asset.tags)
    similarity, reasons = _similarity(
        target_task_type=target_task_type,
        candidate_task_type=task_type,
        target_tokens=target_tokens,
        candidate_tokens=_tokens(" ".join([summary, " ".join(asset.tags)])),
        target_tags=target_tags,
        candidate_tags=tag_set,
    )
    if similarity <= 0:
        return None

    return SimilarTaskExperience(
        asset_id=asset.asset_id,
        memory_layer=memory_layer,
        task_type=task_type,
        summary=summary,
        tags=list(asset.tags),
        strategy_pack_id=_optional_str(metadata.get("strategy_pack_id")),
        execution_mode=_execution_mode(metadata.get("execution_mode")),
        step_id=_optional_int(metadata.get("step_id")),
        skill_used=_optional_str(metadata.get("skill_used")),
        provider=_optional_str(metadata.get("provider")),
        model=_optional_str(metadata.get("model")),
        tier=_optional_str(metadata.get("tier")),
        validation_outcome=_optional_str(metadata.get("validation_outcome")),
        status=_optional_str(metadata.get("status")),
        score_overall=_optional_float(metadata.get("score_overall")),
        cost_usd=_optional_float(metadata.get("cost_usd")),
        similarity_score=round(min(1.0, similarity), 4),
        reason="+".join(reasons),
    )


def _similarity(
    *,
    target_task_type: str,
    candidate_task_type: str,
    target_tokens: set[str],
    candidate_tokens: set[str],
    target_tags: set[str],
    candidate_tags: set[str],
) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if candidate_task_type == target_task_type:
        score += 0.55
        reasons.append("task_type_exact")
    elif _same_task_family(target_task_type, candidate_task_type):
        score += 0.32
        reasons.append("task_type_family")

    tag_overlap = target_tags & candidate_tags
    if tag_overlap:
        score += min(0.25, 0.08 * len(tag_overlap))
        reasons.append("tag_overlap")

    token_overlap = target_tokens & candidate_tokens
    if token_overlap:
        score += min(0.30, len(token_overlap) / max(len(target_tokens), 1))
        reasons.append("text_overlap")

    return score, reasons


def _same_task_family(left: str, right: str) -> bool:
    left_head = left.split(".", 1)[0]
    right_head = right.split(".", 1)[0]
    return bool(left_head and left_head == right_head)


_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]{2,}")


def _tokens(text: str) -> set[str]:
    return {token.lower() for token in _TOKEN_RE.findall(text)}


def _task_text(task_ref: TaskRef) -> str:
    parts = [task_ref.meta.task_type, task_ref.meta.success_criteria_short]
    if task_ref.spec is not None:
        parts.extend(
            [
                task_ref.spec.goal_detail,
                " ".join(task_ref.spec.success_metrics),
                " ".join(task_ref.spec.required_skills),
                " ".join(task_ref.spec.required_tools),
                " ".join(task_ref.spec.subtasks_hint),
            ]
        )
    if task_ref.layer3_context is not None:
        parts.append(task_ref.layer3_context.summary(max_chars=600))
    return " ".join(part for part in parts if part)


def _target_tags(task_ref: TaskRef) -> set[str]:
    tags = {task_ref.meta.task_type, task_ref.meta.risk_level, task_ref.meta.execution_mode}
    tags.update(part for part in task_ref.meta.task_type.split(".") if part)
    if task_ref.spec is not None:
        tags.update(task_ref.spec.required_skills)
        tags.update(task_ref.spec.required_tools)
    return tags


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _execution_mode(value: Any) -> ExecutionMode | None:
    if value in {"FAST", "SMART", "MAX", "ENSEMBLE"}:
        return cast(ExecutionMode, value)
    return None


def _compact(text: str, *, max_chars: int) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


__all__ = [
    "SimilarTaskExperience",
    "recall_similar_task_experiences",
    "summarize_execution_process_experiences",
    "summarize_strategy_votes",
]
