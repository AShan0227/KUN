"""Deterministic memory retrieval policy for KUN V5.

The policy answers two separate questions:
1. whether this task should use memory at all;
2. if it should, which logical memory layers should be recalled.

It intentionally does not fetch memories.  Callers can use the ticket to tune
ContextPacker / similar-task recall without coupling orchestration to memory
selection rules.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from kun.context.assets import AssetKind
from kun.datamodel.task import TaskRef


class MemoryLayer(StrEnum):
    """Logical memory layers the retrieval policy can request."""

    TASK_RESULT = "task_result"
    EXECUTION_PROCESS = "execution_process"
    META_DECISION = "meta_decision"
    METHODOLOGY = "methodology"
    BEHAVIOR = "behavior"


class MemoryDepth(StrEnum):
    """How much memory the current task should pull before execution."""

    NO_MEMORY = "no_memory"
    LIGHT = "light"
    TARGETED = "targeted"
    DEEP = "deep"


class MemoryPolicyTicket(BaseModel):
    """Auditable decision about memory use for one task."""

    model_config = ConfigDict(extra="forbid")

    use_memory: bool
    depth: MemoryDepth
    layers: list[MemoryLayer] = Field(default_factory=list)
    asset_kinds: list[AssetKind] = Field(default_factory=list)
    preferred_tags: list[str] = Field(default_factory=list)
    max_items: int = Field(default=0, ge=0)
    allow_mid_run_retrieval: bool = False
    avoid_layers: list[MemoryLayer] = Field(default_factory=list)
    risk: bool = False
    risk_flags: list[str] = Field(default_factory=list)
    reason: str

    def as_context_packer_kwargs(self) -> dict[str, Any]:
        """Return small kwargs-compatible knobs for retrieval callers."""

        return {
            "limit": self.max_items,
            "kinds": list(self.asset_kinds),
            "preferred_tags": list(self.preferred_tags),
            "memory_layers": [layer.value for layer in self.layers],
            "avoid_memory_layers": [layer.value for layer in self.avoid_layers],
            "high_risk_task": self.risk,
        }


def decide_memory_policy(
    task_ref: TaskRef,
    *,
    watchtower_decision: Any | None = None,
    strategy_pack: Any | None = None,
) -> MemoryPolicyTicket:
    """Choose memory depth and layers deterministically from task metadata.

    ``watchtower_decision`` and ``strategy_pack`` are optional and duck-typed so
    this module stays independent from Watchtower internals.
    """

    text = _task_text(task_ref)
    task_type = task_ref.meta.task_type
    risk_level = task_ref.meta.risk_level
    complexity = task_ref.meta.complexity_score
    pack_id = _optional_text(getattr(strategy_pack, "pack_id", None)) or _optional_text(
        getattr(watchtower_decision, "strategy_pack_id", None)
    )

    risk_flags = _risk_flags(task_ref=task_ref, text=text, watchtower_decision=watchtower_decision)
    risky = bool(risk_flags) or risk_level in {"high", "critical"}
    avoid_layers: list[MemoryLayer] = []
    reason_parts: list[str] = [
        f"task_type={task_type}",
        f"risk={risk_level}",
        f"complexity={complexity:.2f}",
    ]
    if pack_id:
        reason_parts.append(f"strategy_pack={pack_id}")
    preferred_tags = _preferred_tags(
        task_type=task_type,
        text=text,
        strategy_pack=strategy_pack,
        watchtower_decision=watchtower_decision,
    )
    if preferred_tags:
        reason_parts.append(f"preferred_tags={','.join(preferred_tags[:5])}")

    if _is_simple_task(task_ref, text=text) and not risky:
        if complexity <= 0.15:
            return MemoryPolicyTicket(
                use_memory=False,
                depth=MemoryDepth.NO_MEMORY,
                layers=[],
                asset_kinds=[],
                preferred_tags=[],
                max_items=0,
                allow_mid_run_retrieval=False,
                avoid_layers=[
                    MemoryLayer.EXECUTION_PROCESS,
                    MemoryLayer.META_DECISION,
                    MemoryLayer.METHODOLOGY,
                    MemoryLayer.BEHAVIOR,
                ],
                reason="; ".join([*reason_parts, "simple_low_complexity=no_memory"]),
            )
        return MemoryPolicyTicket(
            use_memory=True,
            depth=MemoryDepth.LIGHT,
            layers=[MemoryLayer.TASK_RESULT],
            asset_kinds=["memory"],
            preferred_tags=preferred_tags[:4],
            max_items=1,
            allow_mid_run_retrieval=False,
            avoid_layers=[
                MemoryLayer.EXECUTION_PROCESS,
                MemoryLayer.META_DECISION,
                MemoryLayer.METHODOLOGY,
                MemoryLayer.BEHAVIOR,
            ],
            reason="; ".join([*reason_parts, "simple_low_complexity=light_result_only"]),
        )

    layers = _preferred_layers(task_type=task_type, text=text, pack_id=pack_id)
    asset_kinds = _preferred_asset_kinds(task_type=task_type, text=text, pack_id=pack_id)
    max_items = _base_max_items(complexity)
    depth = _base_depth(complexity)
    allow_mid_run_retrieval = complexity >= 0.55

    if risky:
        layers = _risk_trimmed_layers(layers)
        avoid_layers = _dedupe_layers(
            [
                MemoryLayer.BEHAVIOR,
                *(
                    [MemoryLayer.EXECUTION_PROCESS]
                    if MemoryLayer.EXECUTION_PROCESS not in layers
                    else []
                ),
            ]
        )
        max_items = min(max_items, 2)
        depth = MemoryDepth.TARGETED
        allow_mid_run_retrieval = True
        reason_parts.append(f"risk_flags={','.join(risk_flags) or risk_level}")

    if _is_bug_or_code_task(task_type, text):
        reason_parts.append("code_or_bug_prefers_process_behavior")
    if _is_strategy_or_ops_task(task_type, text, pack_id):
        reason_parts.append("strategy_ops_prefers_meta_methodology")

    return MemoryPolicyTicket(
        use_memory=True,
        depth=depth,
        layers=layers[:max_items],
        asset_kinds=asset_kinds,
        preferred_tags=preferred_tags[:8],
        max_items=max_items,
        allow_mid_run_retrieval=allow_mid_run_retrieval,
        avoid_layers=avoid_layers,
        risk=risky,
        risk_flags=risk_flags,
        reason="; ".join(reason_parts),
    )


def _task_text(task_ref: TaskRef) -> str:
    parts = [
        task_ref.meta.task_type,
        task_ref.meta.success_criteria_short,
    ]
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
        parts.extend(risk.description for risk in task_ref.spec.foreseen_risks)
        parts.extend(constraint.detail for constraint in task_ref.spec.constraints)
    if task_ref.layer3_context is not None:
        parts.append(task_ref.layer3_context.summary(max_chars=600))
    return " ".join(part for part in parts if part).lower()


def _is_simple_task(task_ref: TaskRef, *, text: str) -> bool:
    if task_ref.meta.risk_level != "low" or task_ref.meta.complexity_score > 0.25:
        return False
    if _is_bug_or_code_task(task_ref.meta.task_type, text):
        return False
    if _is_strategy_or_ops_task(task_ref.meta.task_type, text, None):
        return False
    memory_request_words = ("参考历史", "复盘", "过去", "previous", "history", "learned")
    return not any(word in text for word in memory_request_words)


def _preferred_layers(
    *,
    task_type: str,
    text: str,
    pack_id: str | None,
) -> list[MemoryLayer]:
    if _is_bug_or_code_task(task_type, text):
        return [
            MemoryLayer.EXECUTION_PROCESS,
            MemoryLayer.BEHAVIOR,
            MemoryLayer.TASK_RESULT,
            MemoryLayer.META_DECISION,
        ]
    if _is_strategy_or_ops_task(task_type, text, pack_id):
        return [
            MemoryLayer.META_DECISION,
            MemoryLayer.METHODOLOGY,
            MemoryLayer.TASK_RESULT,
            MemoryLayer.BEHAVIOR,
        ]
    return [
        MemoryLayer.TASK_RESULT,
        MemoryLayer.METHODOLOGY,
        MemoryLayer.META_DECISION,
    ]


def _preferred_asset_kinds(
    *,
    task_type: str,
    text: str,
    pack_id: str | None,
) -> list[AssetKind]:
    if _is_bug_or_code_task(task_type, text):
        return ["memory", "methodology", "skill", "knowledge"]
    if _is_strategy_or_ops_task(task_type, text, pack_id):
        return ["memory", "methodology", "knowledge", "skill", "role_template"]
    return ["memory", "knowledge", "methodology"]


def _preferred_tags(
    *,
    task_type: str,
    text: str,
    strategy_pack: Any | None,
    watchtower_decision: Any | None,
) -> list[str]:
    tags: list[str] = []
    context_tags = getattr(strategy_pack, "context_tags", None)
    if isinstance(context_tags, list):
        tags.extend(str(tag) for tag in context_tags if str(tag))
    methodology_refs = getattr(strategy_pack, "methodology_refs", None)
    if isinstance(methodology_refs, list):
        tags.extend(str(ref) for ref in methodology_refs if str(ref))
    metadata = getattr(watchtower_decision, "metadata", None)
    if isinstance(metadata, dict):
        for key in ("context_tags", "methodology_refs"):
            raw = metadata.get(key)
            if isinstance(raw, list):
                tags.extend(str(item) for item in raw if str(item))

    if _is_bug_or_code_task(task_type, text):
        tags.extend(["repo", "tests", "architecture", "debug"])
    elif _is_strategy_or_ops_task(task_type, text, None):
        tags.extend(["business", "product", "growth", "metrics"])
    elif task_type.startswith(("education", "learning", "course", "teaching")):
        tags.extend(["education", "curriculum", "learning_profile"])
    return _dedupe_text([tag.lower() for tag in tags if tag])


def _risk_trimmed_layers(layers: list[MemoryLayer]) -> list[MemoryLayer]:
    preferred = [
        MemoryLayer.TASK_RESULT,
        MemoryLayer.META_DECISION,
        MemoryLayer.METHODOLOGY,
        MemoryLayer.EXECUTION_PROCESS,
    ]
    ordered = [layer for layer in preferred if layer in layers]
    if not ordered:
        ordered = [MemoryLayer.TASK_RESULT, MemoryLayer.META_DECISION]
    if MemoryLayer.BEHAVIOR in ordered:
        ordered.remove(MemoryLayer.BEHAVIOR)
    return ordered[:2]


def _base_max_items(complexity: float) -> int:
    if complexity >= 0.75:
        return 4
    if complexity >= 0.45:
        return 3
    return 2


def _base_depth(complexity: float) -> MemoryDepth:
    if complexity >= 0.75:
        return MemoryDepth.DEEP
    if complexity >= 0.35:
        return MemoryDepth.TARGETED
    return MemoryDepth.LIGHT


def _risk_flags(
    *,
    task_ref: TaskRef,
    text: str,
    watchtower_decision: Any | None,
) -> list[str]:
    flags: list[str] = []
    if task_ref.meta.risk_level in {"high", "critical"}:
        flags.append(f"task_risk_{task_ref.meta.risk_level}")
    if any(word in text for word in ("支付", "转账", "删除", "drop table", "生产")) or any(
        word in text for word in ("production", "prod env", "prod环境")
    ):
        flags.append("sensitive_or_irreversible_terms")
    foreseen_risks = task_ref.spec.foreseen_risks if task_ref.spec else []
    if any(risk.severity in {"high", "critical"} for risk in foreseen_risks):
        flags.append("foreseen_high_risk")
    alert_flags = getattr(watchtower_decision, "alert_flags", None)
    if isinstance(alert_flags, list):
        flags.extend(str(flag) for flag in alert_flags if str(flag))
    return _dedupe_text(flags)


def _is_bug_or_code_task(task_type: str, text: str) -> bool:
    code_prefixes = ("coding", "code", "software", "dev")
    code_words = (
        "bug",
        "fix",
        "pytest",
        "ci",
        "test",
        "测试",
        "报错",
        "修复",
        "代码",
        "回归",
        "debug",
    )
    return task_type.startswith(code_prefixes) or any(word in text for word in code_words)


def _is_strategy_or_ops_task(task_type: str, text: str, pack_id: str | None) -> bool:
    if pack_id in {"commercialization", "product_ops", "strategy", "business"}:
        return True
    strategy_prefixes = ("business", "growth", "sales", "marketing", "commercial", "product", "ops")
    strategy_words = (
        "策略",
        "运营",
        "增长",
        "留存",
        "转化",
        "漏斗",
        "商业化",
        "定价",
        "roadmap",
        "strategy",
    )
    return task_type.startswith(strategy_prefixes) or any(word in text for word in strategy_words)


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _dedupe_layers(layers: list[MemoryLayer]) -> list[MemoryLayer]:
    seen: set[MemoryLayer] = set()
    out: list[MemoryLayer] = []
    for layer in layers:
        if layer in seen:
            continue
        seen.add(layer)
        out.append(layer)
    return out


def _dedupe_text(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


__all__ = [
    "MemoryDepth",
    "MemoryLayer",
    "MemoryPolicyTicket",
    "decide_memory_policy",
]
