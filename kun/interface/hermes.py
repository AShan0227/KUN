"""Hermes full-chain communication adapter.

Hermes is the thin translation layer between KUN's internal task state and the
object that will consume it: an LLM, a skill, an API, an external agent, or a
human collaborator.  It does not execute work.  It shapes messages so the
executor receives the right amount of structured information.
"""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from kun.compiler.models import CanonicalMaterial
from kun.interface.adapters import translate_for

HermesTarget = Literal["user", "llm", "skill", "api", "external_agent", "human"]


class HermesEnvelope(BaseModel):
    """A normalized handoff packet produced by Hermes."""

    target: HermesTarget
    kind: str
    payload: dict[str, Any]
    rendered: str = ""
    format: str = "json"
    metadata: dict[str, Any] = Field(default_factory=dict)


class HermesAdapter(Protocol):
    """Runtime protocol consumed by orchestrator and agent_loop."""

    def render_llm_step_prompt(
        self,
        *,
        base_prompt: str,
        task_id: str,
        task_type: str,
        risk_level: str,
        step_description: str,
        pre_dispatched_block: str = "",
    ) -> str: ...

    async def adapt_skill_input(
        self,
        *,
        skill_id: str,
        params: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def adapt_skill_result(
        self,
        *,
        skill_id: str,
        result: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    async def translate_external(
        self,
        *,
        target: Literal["api", "external_agent", "human"],
        payload: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> HermesEnvelope: ...

    async def translate_material(
        self,
        *,
        material: CanonicalMaterial,
        target: HermesTarget,
        context: dict[str, Any] | None = None,
        max_l2_chars: int = 2200,
    ) -> HermesEnvelope: ...


class DefaultHermesAdapter:
    """Default Hermes implementation used by production runtime.

    The first slice intentionally stays conservative:
    - LLM step prompts are wrapped into a stable structured envelope.
    - Skill params/results are normalized but not semantically rewritten.
    - API/external/human formatting delegates to the existing output adapters.
    """

    name = "default"

    def render_llm_step_prompt(
        self,
        *,
        base_prompt: str,
        task_id: str,
        task_type: str,
        risk_level: str,
        step_description: str,
        pre_dispatched_block: str = "",
    ) -> str:
        envelope = HermesEnvelope(
            target="llm",
            kind="step_execution_prompt",
            format="structured_text",
            payload={
                "task_id": task_id,
                "task_type": task_type,
                "risk_level": risk_level,
                "step_description": step_description,
                "instructions": base_prompt,
                "prefetched_tool_results": pre_dispatched_block.strip(),
            },
            metadata={"version": "v3.3"},
        )
        return self._render_structured_text(envelope)

    async def adapt_skill_input(
        self,
        *,
        skill_id: str,
        params: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        envelope = HermesEnvelope(
            target="skill",
            kind="skill_input",
            payload={
                "skill_id": skill_id,
                "params": dict(params),
                "contract": "json_object_params",
            },
            metadata={"context": context or {}, "version": "v3.3"},
        )
        translated = envelope.payload.get("params", {})
        return translated if isinstance(translated, dict) else {}

    async def adapt_skill_result(
        self,
        *,
        skill_id: str,
        result: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        translated = dict(result)
        metadata = translated.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        metadata.setdefault("hermes", {})
        if isinstance(metadata["hermes"], dict):
            metadata["hermes"].update(
                {
                    "target": "llm",
                    "kind": "skill_result",
                    "skill_id": skill_id,
                    "version": "v3.3",
                    "context": context or {},
                }
            )
        translated["metadata"] = metadata
        return translated

    async def translate_external(
        self,
        *,
        target: Literal["api", "external_agent", "human"],
        payload: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> HermesEnvelope:
        recipient_kind = {
            "api": "rest",
            "external_agent": "agent",
            "human": "human",
        }[target]
        rendered = await translate_for(
            payload=payload,
            recipient_kind=recipient_kind,
            context=context,
        )
        return HermesEnvelope(
            target=target,
            kind="external_translation",
            payload=payload,
            rendered=rendered,
            format=recipient_kind,
            metadata={"context": context or {}, "version": "v3.3"},
        )

    async def translate_material(
        self,
        *,
        material: CanonicalMaterial,
        target: HermesTarget,
        context: dict[str, Any] | None = None,
        max_l2_chars: int = 2200,
    ) -> HermesEnvelope:
        """Compile a CanonicalMaterial into the right packet for its consumer.

        Compiler decides what the material *is*. Hermes decides how much of it
        each receiver should see and in what contract. This is the first real
        bridge between the V5 compiler layer and the full communication layer.
        """

        payload = _material_payload(material, max_l2_chars=max_l2_chars)
        payload["contract"] = _material_contract(target)
        if target == "llm":
            rendered = self._render_material_for_llm(payload)
            fmt = "structured_text"
        elif target == "user":
            rendered = self._render_material_for_user(payload)
            fmt = "plain_text"
        elif target == "skill":
            rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            fmt = "json"
        elif target in {"api", "external_agent", "human"}:
            recipient_kind = {
                "api": "rest",
                "external_agent": "agent",
                "human": "human",
            }[target]
            rendered = await translate_for(
                payload=payload,
                recipient_kind=recipient_kind,
                context=context,
            )
            fmt = recipient_kind
        else:  # pragma: no cover - HermesTarget is exhaustive.
            rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            fmt = "json"
        return HermesEnvelope(
            target=target,
            kind="canonical_material",
            payload=payload,
            rendered=rendered,
            format=fmt,
            metadata={"context": context or {}, "version": "v5.compiler"},
        )

    def _render_structured_text(self, envelope: HermesEnvelope) -> str:
        payload = envelope.payload
        lines = [
            "[Hermes v3.3]",
            f"target: {envelope.target}",
            f"kind: {envelope.kind}",
            f"task_id: {payload.get('task_id', '')}",
            f"task_type: {payload.get('task_type', '')}",
            f"risk_level: {payload.get('risk_level', '')}",
            "",
            "current_step:",
            str(payload.get("step_description", "")),
            "",
            "task_packet:",
            str(payload.get("instructions", "")),
        ]
        prefetched = str(payload.get("prefetched_tool_results") or "").strip()
        if prefetched:
            lines.extend(["", "prefetched_tool_results:", prefetched])
        lines.extend(
            [
                "",
                "hermes_contract:",
                "- 只根据 task_packet 和可用工具结果行动。",
                "- 工具调用必须使用约定的 <skill> JSON 块。",
                "- 信息不足时说缺什么，不要编造。",
            ]
        )
        return "\n".join(lines)

    def _render_material_for_llm(self, payload: dict[str, Any]) -> str:
        risk_raw = payload.get("risk")
        risk: dict[str, Any] = risk_raw if isinstance(risk_raw, dict) else {}
        permissions_raw = payload.get("permissions")
        permissions: dict[str, Any] = permissions_raw if isinstance(permissions_raw, dict) else {}
        lines = [
            "[Hermes v5.compiler]",
            "target: llm",
            "kind: canonical_material",
            f"asset_id: {payload.get('asset_id', '')}",
            f"material_kind: {payload.get('material_kind', '')}",
            f"status: {payload.get('status', '')}",
            f"tokens_estimate: {payload.get('tokens_estimate', 0)}",
            f"risk_level: {risk.get('level', '')}",
            f"risk_flags: {', '.join(risk.get('flags', []) or [])}",
            "",
            "source:",
            json.dumps(payload.get("source", {}), ensure_ascii=False, sort_keys=True),
            "",
            "l1_summary:",
            str(payload.get("l1", "")),
            "",
            "l2_content:",
            str(payload.get("l2", "")),
            "",
            "material_contract:",
            "- 这是一份经过 KUN Compiler 编译后的标准材料，不要再假设原始格式。",
            "- 优先使用 l1/l2；只有拿到明确授权和 l3_ref 才能请求完整内容。",
            "- 如果 status 不是 compiled，只能把它当作占位或拒绝记录，不能假装已经读取。",
            f"- store_l2={permissions.get('store_l2', False)}; l3_ref={payload.get('l3_ref') or ''}",
        ]
        return "\n".join(lines)

    def _render_material_for_user(self, payload: dict[str, Any]) -> str:
        risk_raw = payload.get("risk")
        risk: dict[str, Any] = risk_raw if isinstance(risk_raw, dict) else {}
        return "\n".join(
            [
                f"资料已编译：{payload.get('material_kind', 'unknown')} / {payload.get('status', '')}",
                f"摘要：{payload.get('l1', '')}",
                f"估算 token：{payload.get('tokens_estimate', 0)}",
                f"风险：{risk.get('level', 'unknown')} ({', '.join(risk.get('flags', []) or [])})",
                f"资产 ID：{payload.get('asset_id', '')}",
            ]
        )


class NoopHermesAdapter:
    """Compatibility adapter used when KUN_HERMES_ADAPTER_ENABLED=0."""

    name = "noop"

    def render_llm_step_prompt(
        self,
        *,
        base_prompt: str,
        task_id: str,
        task_type: str,
        risk_level: str,
        step_description: str,
        pre_dispatched_block: str = "",
    ) -> str:
        if pre_dispatched_block:
            return base_prompt + pre_dispatched_block
        return base_prompt

    async def adapt_skill_input(
        self,
        *,
        skill_id: str,
        params: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return dict(params)

    async def adapt_skill_result(
        self,
        *,
        skill_id: str,
        result: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return dict(result)

    async def translate_external(
        self,
        *,
        target: Literal["api", "external_agent", "human"],
        payload: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> HermesEnvelope:
        return HermesEnvelope(
            target=target,
            kind="external_translation",
            payload=payload,
            rendered=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            format="json",
            metadata={"context": context or {}, "version": "noop"},
        )

    async def translate_material(
        self,
        *,
        material: CanonicalMaterial,
        target: HermesTarget,
        context: dict[str, Any] | None = None,
        max_l2_chars: int = 2200,
    ) -> HermesEnvelope:
        payload = _material_payload(material, max_l2_chars=max_l2_chars)
        return HermesEnvelope(
            target=target,
            kind="canonical_material",
            payload=payload,
            rendered=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            format="json",
            metadata={"context": context or {}, "version": "noop"},
        )


def _material_payload(material: CanonicalMaterial, *, max_l2_chars: int) -> dict[str, Any]:
    l2 = material.l2 or ""
    truncated = False
    if max_l2_chars >= 0 and len(l2) > max_l2_chars:
        l2 = l2[: max_l2_chars - 20].rstrip() + "\n...<truncated>"
        truncated = True
    return {
        "asset_id": material.asset_id,
        "material_kind": material.kind,
        "status": material.status,
        "tenant_id": material.tenant_id,
        "source": material.source.model_dump(mode="json"),
        "l1": material.l1,
        "l2": l2,
        "l2_truncated": truncated,
        "l3_ref": material.l3_ref,
        "tokens_estimate": material.tokens_estimate,
        "risk": material.risk.model_dump(mode="json"),
        "permissions": material.permissions.model_dump(mode="json"),
        "provenance": material.provenance.model_dump(mode="json"),
        "compiler_profile": material.compiler_profile.model_dump(mode="json"),
        "material_metadata": material.metadata,
    }


def _material_contract(target: HermesTarget) -> str:
    if target == "llm":
        return "use_l1_l2_only_unless_l3_authorized"
    if target == "skill":
        return "canonical_material_json"
    if target == "user":
        return "plain_language_material_status"
    if target == "api":
        return "rest_payload_canonical_material"
    if target == "external_agent":
        return "agent_protocol_canonical_material"
    return "human_review_material_status"


def dumps_envelope(envelope: HermesEnvelope) -> str:
    """Compact JSON helper for logs/tests."""

    return json.dumps(envelope.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


__all__ = [
    "DefaultHermesAdapter",
    "HermesAdapter",
    "HermesEnvelope",
    "HermesTarget",
    "NoopHermesAdapter",
    "dumps_envelope",
]
