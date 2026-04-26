"""Hermes structured execution protocol.

The generator asks the LLM for one explicit Thought / Action / Outcome step
without binding to a concrete vendor API. Router fakes in tests may return raw
dicts, JSON strings, or LLMResponse-like objects, so parsing is intentionally
shape-tolerant.
"""

from __future__ import annotations

import inspect
import json
import re
from typing import Any, Literal, cast

from pydantic import BaseModel, field_validator

from kun.interface.llm import LLMMessage, LLMRequest, TaskProfile

ActionType = Literal["use_memory", "use_skill", "web_search", "ask_user", "direct_llm"]
ExecutionMode = Literal["FAST", "SMART", "MAX"]

_ACTION_TYPES: set[str] = {
    "use_memory",
    "use_skill",
    "web_search",
    "ask_user",
    "direct_llm",
}

_SYSTEM_PROMPT = """You are Hermes, KUN's structured execution planner.
Return exactly one JSON object matching this schema:
{
  "step_id": integer,
  "thought": string,
  "action_type": "use_memory" | "use_skill" | "web_search" | "ask_user" | "direct_llm",
  "action_payload": object,
  "expected_outcome": string,
  "confidence": number between 0 and 1,
  "cost_estimate_usd": non-negative number
}
Do not include markdown fences or extra prose.
"""


class ExecutionStep(BaseModel):
    """One structured execution decision for the orchestrator/watchtower path."""

    step_id: int
    thought: str
    action_type: ActionType
    action_payload: dict[str, Any]
    expected_outcome: str
    confidence: float = 0.5
    cost_estimate_usd: float = 0.0

    @field_validator("confidence", mode="before")
    @classmethod
    def _clamp_confidence(cls, value: Any) -> float:
        try:
            confidence = float(value)
        except (TypeError, ValueError):
            confidence = 0.5
        return min(1.0, max(0.0, confidence))

    @field_validator("cost_estimate_usd", mode="before")
    @classmethod
    def _clamp_cost(cls, value: Any) -> float:
        try:
            cost = float(value)
        except (TypeError, ValueError):
            cost = 0.0
        return max(0.0, cost)


class StructuredStepGenerator:
    """Generate Hermes execution steps.

    # TODO: orchestrator + watchtower wire by Claude in V2.2
    # orchestrator calls generator.generate() -> watchtower evaluate(step)
    # -> decide block/replace/insert/observe.
    """

    def __init__(self, llm_router: Any) -> None:
        self._router = llm_router

    async def generate(
        self,
        prompt: str,
        context: dict[str, Any],
        *,
        mode: str = "SMART",
    ) -> ExecutionStep:
        normalized_mode = _normalize_mode(mode)
        if normalized_mode == "FAST":
            return _fallback_step(prompt, context=context, reason="fast_mode")

        request = _build_request(prompt, context, normalized_mode)
        response = await _invoke_router(self._router, request)
        payload = _extract_payload(response)
        if payload is None:
            return _fallback_step(prompt, context=context, reason="unparseable_llm_response")

        return _coerce_step(payload, prompt=prompt, context=context)


def _normalize_mode(mode: str) -> ExecutionMode:
    upper = mode.upper()
    if upper in ("FAST", "SMART", "MAX"):
        return cast(ExecutionMode, upper)
    return "SMART"


def _build_request(prompt: str, context: dict[str, Any], mode: ExecutionMode) -> LLMRequest:
    risk_level = str(context.get("risk_level", "low"))
    profile = TaskProfile(
        task_type="execution_protocol",
        risk_level=risk_level,
        needs_reasoning=mode == "MAX",
        prefer_speed=mode == "SMART",
        max_cost_usd=_optional_float(context.get("max_cost_usd")),
    )
    user_prompt = (
        f"Mode: {mode}\n"
        f"Prompt:\n{prompt}\n\n"
        f"Context JSON:\n{json.dumps(context, ensure_ascii=False, sort_keys=True, default=str)}"
    )
    return LLMRequest(
        messages=[
            LLMMessage(role="system", content=_SYSTEM_PROMPT, cache=True),
            LLMMessage(role="user", content=user_prompt),
        ],
        temperature=0.1,
        max_tokens=512,
        profile=profile,
        # V2.2 Wire 11: response_format strict mode (provider 支持时启用)
        # Anthropic 用 tool calling 模拟, OpenAI 用 json_schema, fallback 到 prompt-only
        response_format={
            "type": "json_schema",
            "json_schema": {
                "name": "execution_step",
                "schema": ExecutionStep.model_json_schema(),
            },
        },
    )


async def _invoke_router(router: Any, request: LLMRequest) -> Any:
    if hasattr(router, "invoke"):
        result = router.invoke(request, purpose="execution")
    elif hasattr(router, "ainvoke"):
        result = router.ainvoke(request)
    elif callable(router):
        result = router(request)
    else:
        raise TypeError("llm_router must expose invoke(), ainvoke(), or be callable")

    if inspect.isawaitable(result):
        return await result
    return result


def _extract_payload(response: Any) -> dict[str, Any] | None:
    if isinstance(response, ExecutionStep):
        return response.model_dump()
    if isinstance(response, dict):
        return _payload_from_dict(response)
    if isinstance(response, str):
        return _parse_json_object(response)

    content = getattr(response, "content", None)
    if isinstance(content, dict):
        return _payload_from_dict(content)
    if isinstance(content, str):
        parsed = _parse_json_object(content)
        if parsed is not None:
            return parsed

    raw = getattr(response, "raw", None)
    if isinstance(raw, dict):
        return _payload_from_dict(raw)
    return None


def _payload_from_dict(value: dict[str, Any]) -> dict[str, Any] | None:
    if "execution_step" in value and isinstance(value["execution_step"], dict):
        return cast(dict[str, Any], value["execution_step"])
    if "step" in value and isinstance(value["step"], dict):
        return cast(dict[str, Any], value["step"])
    if "content" in value and isinstance(value["content"], str):
        return _parse_json_object(value["content"])
    return value


def _parse_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed)

    match = re.search(r"\{.*\}", stripped, re.DOTALL)
    if match is None:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return cast(dict[str, Any], parsed)
    return None


def _coerce_step(
    payload: dict[str, Any],
    *,
    prompt: str,
    context: dict[str, Any],
) -> ExecutionStep:
    action_type = payload.get("action_type", "direct_llm")
    if action_type not in _ACTION_TYPES:
        action_type = "direct_llm"

    action_payload = payload.get("action_payload", {})
    if not isinstance(action_payload, dict):
        action_payload = {"value": action_payload}

    return ExecutionStep(
        step_id=_int_or_default(payload.get("step_id"), 1),
        thought=_str_or_default(payload.get("thought"), "Fallback to direct LLM execution."),
        action_type=cast(ActionType, action_type),
        action_payload=cast(dict[str, Any], action_payload),
        expected_outcome=_str_or_default(
            payload.get("expected_outcome"),
            "Answer the prompt directly.",
        ),
        confidence=payload.get("confidence", context.get("default_confidence", 0.5)),
        cost_estimate_usd=payload.get("cost_estimate_usd", 0.0),
    )


def _fallback_step(prompt: str, *, context: dict[str, Any], reason: str) -> ExecutionStep:
    return ExecutionStep(
        step_id=_int_or_default(context.get("step_id"), 1),
        thought=f"Use direct LLM execution ({reason}).",
        action_type="direct_llm",
        action_payload={"prompt": prompt, "context": context},
        expected_outcome="Answer the prompt directly.",
        confidence=context.get("default_confidence", 0.5),
        cost_estimate_usd=0.0,
    )


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_or_default(value: Any, default: str) -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
