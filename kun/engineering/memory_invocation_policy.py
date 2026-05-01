"""Memory invocation policy — sparse MoE memory selection.

This layer decides *whether* memory should be used before ContextPacker decides
*which concrete assets* to rank.  It is intentionally deterministic and small:
simple tasks stay fast, complex/high-risk/retry tasks get a deeper but still
sparse memory slice.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.context.assets import AssetKind
from kun.datamodel.task import RiskLevel, TaskRef
from kun.memory.policy import MemoryDepth, MemoryLayer, MemoryPolicyTicket

ExplicitMemoryMode = Literal["auto", "off", "light", "targeted", "deep"]


class MemoryInvocationInput(BaseModel):
    """Small, auditable input for sparse memory invocation."""

    model_config = ConfigDict(extra="forbid")

    task_type: str
    risk_level: RiskLevel = "low"
    complexity_score: float = Field(default=0.3, ge=0.0, le=1.0)
    text: str = ""
    explicit_mode: ExplicitMemoryMode = "auto"
    retry_count: int = Field(default=0, ge=0)
    previous_failure: bool = False
    strategy_pack_id: str = ""
    strategy_tags: list[str] = Field(default_factory=list)
    historical_resource_credit: dict[str, float] = Field(default_factory=dict)


class MemoryInvocationTicket(BaseModel):
    """Result of the memory invocation policy.

    This is richer than ``MemoryPolicyTicket`` for inspection, but can be
    converted into the existing ticket consumed by Orchestrator/ContextPacker.
    """

    model_config = ConfigDict(extra="forbid")

    use_memory: bool
    memory_depth: MemoryDepth
    memory_layers: list[MemoryLayer] = Field(default_factory=list)
    asset_kinds: list[AssetKind] = Field(default_factory=list)
    strategy_tags: list[str] = Field(default_factory=list)
    max_items: int = Field(default=0, ge=0)
    allow_mid_run_retrieval: bool = False
    avoid_memory_layers: list[MemoryLayer] = Field(default_factory=list)
    high_risk_task: bool = False
    risk_flags: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)

    def as_context_packer_kwargs(self) -> dict[str, Any]:
        """Return kwargs compatible with ``ContextPacker.pack``."""

        return {
            "limit": self.max_items,
            "kinds": list(self.asset_kinds),
            "preferred_tags": list(self.strategy_tags),
            "memory_layers": [layer.value for layer in self.memory_layers],
            "avoid_memory_layers": [layer.value for layer in self.avoid_memory_layers],
            "high_risk_task": self.high_risk_task,
        }

    def to_memory_policy_ticket(self) -> MemoryPolicyTicket:
        """Convert to the current production ticket consumed by Orchestrator."""

        return MemoryPolicyTicket(
            use_memory=self.use_memory,
            depth=self.memory_depth,
            layers=list(self.memory_layers),
            asset_kinds=list(self.asset_kinds),
            preferred_tags=list(self.strategy_tags),
            max_items=self.max_items,
            allow_mid_run_retrieval=self.allow_mid_run_retrieval,
            avoid_layers=list(self.avoid_memory_layers),
            risk=self.high_risk_task,
            risk_flags=list(self.risk_flags),
            reason="; ".join(self.reasons),
        )


def decide_memory_invocation(data: MemoryInvocationInput) -> MemoryInvocationTicket:
    """Decide sparse memory use from task shape, risk, retry and credit signals."""

    text = _normalize_text(f"{data.task_type} {data.text}")
    risk_flags = _risk_flags(data, text)
    high_risk = bool(risk_flags)
    retry = data.previous_failure or data.retry_count > 0 or _looks_like_retry(text)
    family = _task_family(data.task_type, text)
    reasons = [
        f"family={family}",
        f"risk={data.risk_level}",
        f"complexity={data.complexity_score:.2f}",
    ]
    if data.strategy_pack_id:
        reasons.append(f"strategy_pack={data.strategy_pack_id}")
    if data.explicit_mode != "auto":
        reasons.append(f"explicit_mode={data.explicit_mode}")
    if retry:
        reasons.append("retry_or_previous_failure")

    if data.explicit_mode == "off":
        return MemoryInvocationTicket(
            use_memory=False,
            memory_depth=MemoryDepth.NO_MEMORY,
            avoid_memory_layers=[
                MemoryLayer.EXECUTION_PROCESS,
                MemoryLayer.META_DECISION,
                MemoryLayer.METHODOLOGY,
                MemoryLayer.BEHAVIOR,
            ],
            high_risk_task=high_risk,
            risk_flags=risk_flags,
            reasons=[*reasons, "explicit_off=no_memory"],
        )

    if (
        data.explicit_mode == "auto"
        and not high_risk
        and not retry
        and _is_simple_query(data, text)
    ):
        return MemoryInvocationTicket(
            use_memory=False,
            memory_depth=MemoryDepth.NO_MEMORY,
            avoid_memory_layers=[
                MemoryLayer.EXECUTION_PROCESS,
                MemoryLayer.META_DECISION,
                MemoryLayer.METHODOLOGY,
                MemoryLayer.BEHAVIOR,
            ],
            reasons=[*reasons, "simple_task_fast_lane=no_memory"],
        )

    layers = _base_layers(family)
    asset_kinds = _base_asset_kinds(family)
    strategy_tags = _base_strategy_tags(family, text, data)
    depth = _base_depth(data.complexity_score)
    max_items = _max_items(depth)
    allow_mid_run = data.complexity_score >= 0.55
    avoid_layers: list[MemoryLayer] = []

    if retry:
        layers = _prepend_layers(
            layers,
            [
                MemoryLayer.EXECUTION_PROCESS,
                MemoryLayer.TASK_RESULT,
                MemoryLayer.META_DECISION,
            ],
        )
        strategy_tags = _dedupe_text(
            [*strategy_tags, "retry", "failure", "postmortem", "known_solution"]
        )
        depth = MemoryDepth.DEEP
        max_items = max(max_items, 5)
        allow_mid_run = True

    if high_risk:
        depth = _raise_depth(depth)
        max_items = max(max_items, 3 if depth == MemoryDepth.TARGETED else 4)
        allow_mid_run = True
        if family not in {"coding", "debug"}:
            avoid_layers = _dedupe_layers([*avoid_layers, MemoryLayer.BEHAVIOR])
        strategy_tags = _dedupe_text([*strategy_tags, "risk", "rollback", "approval"])
        reasons.append(f"risk_flags={','.join(risk_flags)}")

    depth, max_items = _apply_explicit_depth(data.explicit_mode, depth, max_items)
    credit_layers, credit_kinds, credit_tags, credit_avoid = _credit_hints(
        data.historical_resource_credit
    )
    if credit_layers:
        layers = _prepend_layers(layers, credit_layers)
        reasons.append("historical_credit_layers")
    if credit_kinds:
        asset_kinds = _dedupe_kinds([*credit_kinds, *asset_kinds])
        reasons.append("historical_credit_asset_kinds")
    if credit_tags:
        strategy_tags = _dedupe_text([*strategy_tags, *credit_tags])
        reasons.append("historical_credit_tags")
    if credit_avoid:
        avoid_layers = _dedupe_layers([*avoid_layers, *credit_avoid])
        reasons.append("historical_credit_avoid_layers")

    layers = [layer for layer in _dedupe_layers(layers) if layer not in set(avoid_layers)]
    if not layers:
        layers = [MemoryLayer.TASK_RESULT]
    if retry and MemoryLayer.TASK_RESULT not in layers:
        layers.append(MemoryLayer.TASK_RESULT)

    return MemoryInvocationTicket(
        use_memory=True,
        memory_depth=depth,
        memory_layers=layers[: max(1, max_items)],
        asset_kinds=_dedupe_kinds(asset_kinds),
        strategy_tags=strategy_tags[:10],
        max_items=max_items,
        allow_mid_run_retrieval=allow_mid_run,
        avoid_memory_layers=avoid_layers,
        high_risk_task=high_risk,
        risk_flags=risk_flags,
        reasons=reasons,
    )


def decide_memory_invocation_for_task(
    task_ref: TaskRef,
    *,
    explicit_mode: ExplicitMemoryMode = "auto",
    historical_resource_credit: dict[str, float] | None = None,
    retry_count: int = 0,
    previous_failure: bool = False,
    strategy_pack: Any | None = None,
) -> MemoryInvocationTicket:
    """Build policy input from ``TaskRef`` and optional strategy pack."""

    return decide_memory_invocation(
        MemoryInvocationInput(
            task_type=task_ref.meta.task_type,
            risk_level=task_ref.meta.risk_level,
            complexity_score=task_ref.meta.complexity_score,
            text=_task_text(task_ref),
            explicit_mode=explicit_mode,
            retry_count=retry_count,
            previous_failure=previous_failure,
            strategy_pack_id=str(getattr(strategy_pack, "pack_id", "") or ""),
            strategy_tags=_strategy_tags_from_pack(strategy_pack),
            historical_resource_credit=historical_resource_credit or {},
        )
    )


def _task_text(task_ref: TaskRef) -> str:
    parts = [task_ref.meta.success_criteria_short]
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
        parts.append(task_ref.layer3_context.summary(max_chars=500))
    return " ".join(part for part in parts if part)


def _strategy_tags_from_pack(strategy_pack: Any | None) -> list[str]:
    if strategy_pack is None:
        return []
    out: list[str] = []
    for attr in ("pack_id",):
        value = getattr(strategy_pack, attr, "")
        if value:
            out.append(str(value))
    for attr in ("context_tags", "methodology_refs", "skill_hints"):
        raw = getattr(strategy_pack, attr, [])
        if isinstance(raw, list):
            out.extend(str(item) for item in raw if str(item))
    return _dedupe_text(out)


def _normalize_text(text: str) -> str:
    return text.lower()


def _task_family(task_type: str, text: str) -> str:
    task = task_type.lower()
    code_blob = f"{task} {text}"
    if task.startswith(("coding", "code", "software", "dev")) or _contains_any(
        code_blob,
        (
            "bug",
            "pytest",
            "mypy",
            "ruff",
            "debug",
            "重构",
            "代码",
            "测试",
            "修复",
        ),
    ):
        return "coding"
    if _contains_any(
        f"{task} {text}",
        (
            "strategy",
            "decision",
            "business",
            "growth",
            "commercial",
            "product.ops",
            "定价",
            "增长",
            "商业",
            "策略",
            "决策",
            "运营",
            "复盘",
        ),
    ):
        return "decision"
    if _contains_any(f"{task} {text}", ("education", "learning", "course", "学习", "课程")):
        return "education"
    if _contains_any(f"{task} {text}", ("email", "external", "collab", "客户", "发邮件", "外部")):
        return "external"
    return "general"


def _is_simple_query(data: MemoryInvocationInput, text: str) -> bool:
    if data.complexity_score > 0.20 or data.risk_level not in {"low", "medium"}:
        return False
    return _contains_any(
        f"{data.task_type.lower()} {text}",
        (
            "query",
            "lookup",
            "status",
            "confirm",
            "确认",
            "查询",
            "看一下",
            "收到",
            "回复一句",
            "现在",
        ),
    )


def _looks_like_retry(text: str) -> bool:
    return _contains_any(
        text,
        (
            "retry",
            "retrying",
            "previously failed",
            "failed before",
            "again",
            "regression",
            "重试",
            "上次失败",
            "之前失败",
            "再试",
            "没通过",
        ),
    )


def _risk_flags(data: MemoryInvocationInput, text: str) -> list[str]:
    flags: list[str] = []
    if data.risk_level in {"high", "critical"}:
        flags.append(f"task_risk_{data.risk_level}")
    if _contains_any(
        text,
        (
            "delete",
            "send",
            "transfer",
            "production",
            "删除",
            "发送",
            "支付",
            "转账",
            "生产",
        ),
    ):
        flags.append("irreversible_or_external_action")
    return _dedupe_text(flags)


def _base_layers(family: str) -> list[MemoryLayer]:
    if family == "coding":
        return [
            MemoryLayer.EXECUTION_PROCESS,
            MemoryLayer.META_DECISION,
            MemoryLayer.BEHAVIOR,
            MemoryLayer.METHODOLOGY,
        ]
    if family == "decision":
        return [
            MemoryLayer.META_DECISION,
            MemoryLayer.METHODOLOGY,
            MemoryLayer.TASK_RESULT,
        ]
    if family == "education":
        return [MemoryLayer.BEHAVIOR, MemoryLayer.METHODOLOGY, MemoryLayer.TASK_RESULT]
    if family == "external":
        return [MemoryLayer.META_DECISION, MemoryLayer.METHODOLOGY, MemoryLayer.TASK_RESULT]
    return [MemoryLayer.TASK_RESULT, MemoryLayer.METHODOLOGY]


def _base_asset_kinds(family: str) -> list[AssetKind]:
    if family == "coding":
        return ["memory", "methodology", "skill", "knowledge"]
    if family == "decision":
        return ["memory", "methodology", "knowledge", "skill", "role_template"]
    if family == "education":
        return ["memory", "methodology", "knowledge", "skill"]
    if family == "external":
        return ["memory", "methodology", "knowledge", "handoff", "skill"]
    return ["memory", "methodology", "knowledge"]


def _base_strategy_tags(family: str, text: str, data: MemoryInvocationInput) -> list[str]:
    out = [*data.strategy_tags, family]
    if family == "coding":
        out.extend(["repo", "tests", "debug", "architecture"])
    elif family == "decision":
        out.extend(["strategy", "metrics", "growth", "tradeoff"])
    elif family == "education":
        out.extend(["education", "learning_profile", "curriculum"])
    elif family == "external":
        out.extend(["world_gateway", "approval", "handoff", "rollback"])
    if "pytest" in text:
        out.append("pytest")
    if "pricing" in text or "定价" in text:
        out.append("pricing")
    return _dedupe_text(out)


def _base_depth(complexity: float) -> MemoryDepth:
    if complexity < 0.25:
        return MemoryDepth.LIGHT
    if complexity < 0.65:
        return MemoryDepth.TARGETED
    return MemoryDepth.DEEP


def _raise_depth(depth: MemoryDepth) -> MemoryDepth:
    if depth in {MemoryDepth.NO_MEMORY, MemoryDepth.LIGHT}:
        return MemoryDepth.TARGETED
    return MemoryDepth.DEEP


def _max_items(depth: MemoryDepth) -> int:
    return {
        MemoryDepth.NO_MEMORY: 0,
        MemoryDepth.LIGHT: 1,
        MemoryDepth.TARGETED: 3,
        MemoryDepth.DEEP: 5,
    }[depth]


def _apply_explicit_depth(
    explicit_mode: ExplicitMemoryMode,
    depth: MemoryDepth,
    max_items: int,
) -> tuple[MemoryDepth, int]:
    if explicit_mode == "light":
        return MemoryDepth.LIGHT, min(max_items or 1, 1)
    if explicit_mode == "targeted":
        return MemoryDepth.TARGETED, max(max_items, 3)
    if explicit_mode == "deep":
        return MemoryDepth.DEEP, max(max_items, 5)
    return depth, max_items


def _credit_hints(
    credit: dict[str, float],
) -> tuple[list[MemoryLayer], list[AssetKind], list[str], list[MemoryLayer]]:
    layers: list[MemoryLayer] = []
    kinds: list[AssetKind] = []
    tags: list[str] = []
    avoid: list[MemoryLayer] = []
    for raw_key, raw_score in credit.items():
        key = str(raw_key).strip().lower()
        try:
            score = float(raw_score)
        except (TypeError, ValueError):
            continue
        if score <= -0.25:
            layer = _layer_from_key(key)
            if layer is not None:
                avoid.append(layer)
            continue
        if score < 0.55:
            continue
        layer = _layer_from_key(key)
        if layer is not None:
            layers.append(layer)
            continue
        kind = _asset_kind_from_key(key)
        if kind is not None:
            kinds.append(kind)
            continue
        tag = _tag_from_key(key)
        if tag:
            tags.append(tag)
    return _dedupe_layers(layers), _dedupe_kinds(kinds), _dedupe_text(tags), _dedupe_layers(avoid)


def _layer_from_key(key: str) -> MemoryLayer | None:
    for prefix in ("memory_layer:", "layer:", "memory:"):
        if key.startswith(prefix):
            value = key.removeprefix(prefix)
            return _parse_layer(value)
    return _parse_layer(key)


def _asset_kind_from_key(key: str) -> AssetKind | None:
    for prefix in ("asset_kind:", "kind:"):
        if key.startswith(prefix):
            value = key.removeprefix(prefix)
            if value in {
                "skill",
                "memory",
                "knowledge",
                "task",
                "handoff",
                "role_template",
                "methodology",
            }:
                return value  # type: ignore[return-value]
    return None


def _tag_from_key(key: str) -> str:
    for prefix in ("tag:", "strategy_tag:", "context_tag:"):
        if key.startswith(prefix):
            return key.removeprefix(prefix).strip()
    return ""


def _parse_layer(value: str) -> MemoryLayer | None:
    normalized = value.strip().lower()
    aliases = {
        "process": MemoryLayer.EXECUTION_PROCESS,
        "execution": MemoryLayer.EXECUTION_PROCESS,
        "execution_process": MemoryLayer.EXECUTION_PROCESS,
        "meta": MemoryLayer.META_DECISION,
        "meta_decision": MemoryLayer.META_DECISION,
        "decision": MemoryLayer.META_DECISION,
        "methodology": MemoryLayer.METHODOLOGY,
        "behavior": MemoryLayer.BEHAVIOR,
        "task_result": MemoryLayer.TASK_RESULT,
        "result": MemoryLayer.TASK_RESULT,
    }
    return aliases.get(normalized)


def _prepend_layers(
    layers: list[MemoryLayer],
    preferred: list[MemoryLayer],
) -> list[MemoryLayer]:
    return _dedupe_layers([*preferred, *layers])


def _dedupe_layers(items: list[MemoryLayer]) -> list[MemoryLayer]:
    out: list[MemoryLayer] = []
    seen: set[MemoryLayer] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _dedupe_kinds(items: list[AssetKind]) -> list[AssetKind]:
    out: list[AssetKind] = []
    seen: set[str] = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _dedupe_text(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = item.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return out


def _contains_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle.lower() in text for needle in needles)


_WORD_RE = re.compile(r"[\w.-]+")


def token_set(text: str) -> set[str]:
    """Expose a tiny tokenizer for tests/debug tooling."""

    return {match.group(0).lower() for match in _WORD_RE.finditer(text) if match.group(0)}


__all__ = [
    "ExplicitMemoryMode",
    "MemoryInvocationInput",
    "MemoryInvocationTicket",
    "decide_memory_invocation",
    "decide_memory_invocation_for_task",
    "token_set",
]
