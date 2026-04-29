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
