"""Compile KUN-native objects into LayeredAsset records.

The material compiler cleans external files.  This module handles KUN's own
work objects: skills, tasks, and protocols.  The goal is the same: make every
object cheap to route, retrieve, audit, and reuse through the shared Context
system instead of leaving each subsystem with a private format.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from kun.context.assets import AssetLayer, LayeredAsset
from kun.datamodel.task import TaskRef

INTERNAL_COMPILER_NAME = "kun-v5-internal-asset-compiler"


def compile_skill_markdown_asset(
    markdown: str,
    *,
    tenant_id: str,
    skill_id: str | None = None,
    source_uri: str = "inline:SKILL.md",
    layer: AssetLayer = AssetLayer.L2_PROJECT,
) -> LayeredAsset:
    """Compile a SKILL.md-like document into a sparse skill context asset."""

    title = _first_heading(markdown) or skill_id or "unnamed_skill"
    normalized_skill_id = _slug(skill_id or title)
    summary = _compact_markdown(markdown, max_chars=900)
    description = _first_description(markdown) or summary[:240]
    return LayeredAsset(
        asset_id=_compiled_asset_id("skill", tenant_id, source_uri, markdown),
        asset_kind="skill",
        tenant_id=tenant_id,
        l1_metadata={
            "source": {"type": "kun_internal", "uri": source_uri},
            "compiler": INTERNAL_COMPILER_NAME,
            "compiled_kind": "skill",
            "skill_id": normalized_skill_id,
            "title": title,
            "description": description,
            "tokens_estimate": _estimate_tokens(markdown),
            "production_action": False,
        },
        l2_summary=summary,
        l3_ref=None,
        layer=layer,
        tags=_dedupe(
            [
                "compiler",
                "compiled_internal",
                "skill",
                "kind:skill",
                normalized_skill_id,
                *_keyword_tags(markdown),
            ]
        ),
    )


def compile_task_ref_asset(
    task_ref: TaskRef,
    *,
    tenant_id: str | None = None,
    layer: AssetLayer = AssetLayer.L1_TASK,
) -> LayeredAsset:
    """Compile TASK.md-like runtime task data into a context asset."""

    owner_tenant = getattr(getattr(task_ref.meta, "owner", None), "tenant_id", None)
    resolved_tenant = tenant_id or str(owner_tenant or "u-sylvan")
    task_id = str(task_ref.meta.task_id)
    summary = _task_summary(task_ref)
    return LayeredAsset(
        asset_id=_compiled_asset_id("task", resolved_tenant, task_id, summary),
        asset_kind="task",
        tenant_id=resolved_tenant,
        l1_metadata={
            "source": {"type": "kun_internal", "uri": f"task:{task_id}"},
            "compiler": INTERNAL_COMPILER_NAME,
            "compiled_kind": "task",
            "task_id": task_id,
            "task_type": task_ref.meta.task_type,
            "risk_level": task_ref.meta.risk_level,
            "complexity_score": task_ref.meta.complexity_score,
            "estimated_cost_usd": task_ref.meta.estimated_cost_usd,
            "success_criteria_short": task_ref.meta.success_criteria_short,
            "production_action": False,
        },
        l2_summary=summary,
        l3_ref=getattr(task_ref, "layer3_ref", None),
        layer=layer,
        tags=_dedupe(
            [
                "compiler",
                "compiled_internal",
                "task",
                "kind:task",
                task_ref.meta.task_type,
                f"risk:{task_ref.meta.risk_level}",
            ]
        ),
    )


def compile_protocol_asset(
    protocol: Any,
    *,
    tenant_id: str | None = None,
    layer: AssetLayer = AssetLayer.L2_PROJECT,
) -> LayeredAsset:
    """Compile a Qi Protocol into a methodology asset usable by ContextPacker."""

    protocol_id = str(getattr(protocol, "protocol_id", "unknown_protocol"))
    version = str(getattr(protocol, "version", "unknown"))
    resolved_tenant = tenant_id or str(getattr(protocol, "tenant_id", "u-sylvan"))
    payload = _model_dump(protocol)
    trigger = _as_dict(payload.get("trigger"))
    execution = _as_dict(payload.get("execution"))
    skill_chain = _as_list(payload.get("skill_chain"))
    verification = _as_list(payload.get("verification"))
    summary = _protocol_summary(
        protocol_id=protocol_id,
        version=version,
        trigger=trigger,
        execution=execution,
        skill_chain=skill_chain,
        verification=verification,
    )
    return LayeredAsset(
        asset_id=_compiled_asset_id("methodology", resolved_tenant, protocol_id, version),
        asset_kind="methodology",
        tenant_id=resolved_tenant,
        l1_metadata={
            "source": {"type": "kun_internal", "uri": f"protocol:{protocol_id}@{version}"},
            "compiler": INTERNAL_COMPILER_NAME,
            "compiled_kind": "protocol",
            "protocol_id": protocol_id,
            "version": version,
            "status": str(getattr(protocol, "status", payload.get("status", "experimental"))),
            "task_type_pattern": str(trigger.get("task_type_pattern", "")),
            "execution_mode": str(execution.get("mode", "")),
            "expected_cost_usd": execution.get("expected_cost_usd"),
            "skill_chain": [
                str(_as_dict(step).get("skill", ""))
                for step in skill_chain
                if str(_as_dict(step).get("skill", "")).strip()
            ],
            "verification_count": len(verification),
            "reward_weights": _as_dict(payload.get("reward_weights")),
            "production_action": False,
        },
        l2_summary=summary,
        l3_ref=None,
        layer=layer,
        tags=_dedupe(
            [
                "compiler",
                "compiled_internal",
                "methodology",
                "protocol",
                "kind:protocol",
                protocol_id,
                str(trigger.get("task_type_pattern", "")),
                str(execution.get("mode", "")),
            ]
        ),
    )


def _task_summary(task_ref: TaskRef) -> str:
    parts = [
        f"任务类型: {task_ref.meta.task_type}",
        f"风险: {task_ref.meta.risk_level}",
        f"复杂度: {task_ref.meta.complexity_score:.2f}",
        f"成功标准: {task_ref.meta.success_criteria_short}",
    ]
    if task_ref.spec is not None:
        if task_ref.spec.goal_detail:
            parts.append(f"目标: {task_ref.spec.goal_detail}")
        if task_ref.spec.success_metrics:
            parts.append(f"验收: {', '.join(task_ref.spec.success_metrics[:5])}")
        if task_ref.spec.required_skills:
            parts.append(f"需要 skill: {', '.join(task_ref.spec.required_skills[:5])}")
    return "\n".join(parts)


def _protocol_summary(
    *,
    protocol_id: str,
    version: str,
    trigger: dict[str, Any],
    execution: dict[str, Any],
    skill_chain: list[Any],
    verification: list[Any],
) -> str:
    skills = [
        str(_as_dict(step).get("skill", ""))
        for step in skill_chain
        if str(_as_dict(step).get("skill", "")).strip()
    ]
    checks = [
        str(_as_dict(item).get("kind", ""))
        for item in verification
        if str(_as_dict(item).get("kind", "")).strip()
    ]
    return "\n".join(
        [
            f"协议: {protocol_id}@{version}",
            f"触发: {trigger.get('task_type_pattern', '*')}",
            f"执行: mode={execution.get('mode', 'SMART')}; max_steps={execution.get('max_steps', 'n/a')}",
            f"skill_chain: {', '.join(skills) if skills else 'n/a'}",
            f"verification: {', '.join(checks) if checks else 'n/a'}",
        ]
    )


def _first_heading(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
    return ""


def _first_description(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("---"):
            continue
        if ":" in stripped and stripped.split(":", 1)[0].lower() in {
            "name",
            "title",
        }:
            continue
        return stripped[:300]
    return ""


def _compact_markdown(markdown: str, *, max_chars: int) -> str:
    lines = [line.rstrip() for line in markdown.splitlines()]
    compact = "\n".join(line for line in lines if line.strip())
    return compact[: max_chars - 3] + "..." if len(compact) > max_chars else compact


def _keyword_tags(text: str) -> list[str]:
    lowered = text.lower()
    candidates = {
        "code": ("code", "coding", "代码", "pytest", "ruff", "mypy"),
        "research": ("research", "搜索", "资料", "论文"),
        "writing": ("writing", "文案", "写作"),
        "browser": ("browser", "浏览器", "网页"),
        "data": ("data", "csv", "spreadsheet", "数据"),
    }
    return [tag for tag, needles in candidates.items() if any(item in lowered for item in needles)]


def _compiled_asset_id(kind: str, tenant_id: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join([tenant_id, kind, *parts]).encode()).hexdigest()[:24]
    prefix = "skill" if kind == "skill" else "task" if kind == "task" else "memory"
    return f"{prefix}_{digest}"


def _estimate_tokens(text: str) -> int:
    return max(1, (len(text) + 3) // 4) if text else 0


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", value.strip().lower()).strip("-")
    return slug or "unnamed"


def _model_dump(value: Any) -> dict[str, Any]:
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, dict) else {}
    return dict(value) if isinstance(value, dict) else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        cleaned = str(item).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


__all__ = [
    "INTERNAL_COMPILER_NAME",
    "compile_protocol_asset",
    "compile_skill_markdown_asset",
    "compile_task_ref_asset",
]
