"""Hermes structured execution protocol.

The generator asks the LLM for one explicit Thought / Action / Outcome step
without binding to a concrete vendor API. Router fakes in tests may return raw
dicts, JSON strings, or LLMResponse-like objects, so parsing is intentionally
shape-tolerant.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, ClassVar, Literal, cast

from pydantic import BaseModel, field_validator

from kun.interface.llm import LLMMessage, LLMRequest, TaskProfile

logger = logging.getLogger(__name__)

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

# V2.2 §26 / Wire 29A: lab recipe → 额外 system prompt 注入
# 跟 Wire 25 ExecutionMode classifier 对称 — KUN-Lab 的 RecipePromoter 推 strategy
# 时如果 target_module="hermes_prompt_template", 这里查 registry 加 prompt 变体.
# 跟 ENSEMBLE DEFAULT_PATHS 的 system_prompt_override 一致.
_LAB_STRATEGY_PROMPT_HINT: dict[str, str] = {
    "chain_of_thought": (
        "[Lab-validated recipe] Think step by step. Show your reasoning briefly before the JSON."
    ),
    "diverse_perspective": (
        "[Lab-validated recipe] Take a contrarian view first. "
        "Challenge any default assumptions before deciding."
    ),
    "tier_top_low_temp": (
        "[Lab-validated recipe] Be conservative — high stakes detected. "
        "Prefer correctness over speed."
    ),
}


class ExecutionStep(BaseModel):
    """One structured execution decision for the orchestrator/watchtower path."""

    step_id: int
    thought: str
    action_type: ActionType
    action_payload: dict[str, Any]
    expected_outcome: str
    confidence: float = 0.5
    cost_estimate_usd: float = 0.0
    # V2.2 §27 (FaithCoT 启发) — thought 跟 action 的一致性 (0..1)
    # 默认 1.0 (FAST 模式跳过 check); SMART/MAX 模式 ThoughtActionConsistency.check 真算
    thought_action_consistency: float = 1.0
    # V2.2 §27 — 这条 step 重出过几次 (rethinking 触发, max 2)
    rethink_count: int = 0

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

    @field_validator("thought_action_consistency", mode="before")
    @classmethod
    def _clamp_consistency(cls, value: Any) -> float:
        try:
            v = float(value)
        except (TypeError, ValueError):
            v = 1.0
        return min(1.0, max(0.0, v))


# ============================================================================
# V2.2 §27 ThoughtActionConsistency (FaithCoT 启发)
# ============================================================================


class ThoughtActionConsistency:
    """检测 thought 跟 action 是否真一致 (不是事后解释).

    FaithCoT 揭示: 模型可能"会解释而不会思考". KUN 升级:
    - SMART/MAX 模式 LLM 出 ExecutionStep 后, 算 consistency
    - consistency < threshold (默认 0.5) → emit signal 让守望决定 rethink
    - FAST 模式跳过 (节省 latency)

    实装: 启发式 + LLM judge 二合
    - 启发式 (无 LLM call, 快): thought 含 keyword 是否对应 action_type
    - LLM judge (cheap model 一次性): 不一致时调用作 final 判定
    """

    # action_type → 期望出现的 keyword (启发式)
    _EXPECTED_KEYWORDS: ClassVar[dict[str, list[str]]] = {
        "use_memory": ["记忆", "历史", "回顾", "memory", "recall", "history"],
        "use_skill": ["调用", "skill", "工具", "tool"],
        "web_search": ["搜索", "查询", "search", "web", "网", "外部"],
        "ask_user": ["问", "确认", "ask", "user", "确定"],
        "direct_llm": [],  # 默认, 无强 keyword
    }

    def __init__(
        self,
        *,
        consistency_threshold: float = 0.5,
        llm_judge: Any = None,  # async fn(thought, action_type) → float, 可选
        lite_llm_judge: Any = None,  # SMART 模式用的轻量 jury, 可选
    ) -> None:
        self.consistency_threshold = consistency_threshold
        self._llm_judge = llm_judge
        self._lite_llm_judge = lite_llm_judge

    async def check(self, step: ExecutionStep, *, mode: str = "SMART") -> tuple[float, str]:
        """返 (consistency_score, reason)."""
        # 1. 启发式
        heuristic_score = self._heuristic_check(step.thought, step.action_type)

        # 2. 如果启发式高 (>=0.7) → 直接信
        if heuristic_score >= 0.7:
            return heuristic_score, f"heuristic_high:{heuristic_score:.2f}"

        # 3. 启发式低 + LLM judge 可用 → 调一次 LLM 兜底
        llm_judge = (
            self._lite_llm_judge
            if mode.upper() == "SMART" and self._lite_llm_judge is not None
            else self._llm_judge
        )
        if llm_judge is not None:
            try:
                llm_score = await llm_judge(step.thought, step.action_type)
                final = max(heuristic_score, float(llm_score))
                judge_kind = (
                    "lite_jury" if mode.upper() == "SMART" and self._lite_llm_judge else "llm_judge"
                )
                return final, f"{judge_kind}:{llm_score:.2f}"
            except Exception:
                logger.exception("ThoughtActionConsistency llm_judge failed")

        # 4. 没 LLM judge → 只用启发式
        return heuristic_score, f"heuristic_only:{heuristic_score:.2f}"

    def _heuristic_check(self, thought: str, action_type: str) -> float:
        """启发式: thought 含期望 keyword → 高 consistency."""
        keywords = self._EXPECTED_KEYWORDS.get(action_type, [])
        if not keywords:
            # direct_llm 没强 keyword, 默认 0.7 (中性偏高)
            return 0.7
        thought_lower = thought.lower()
        hits = sum(1 for kw in keywords if kw.lower() in thought_lower)
        if hits == 0:
            return 0.2
        if hits == 1:
            return 0.6
        return 0.9  # 多个 keyword 命中

    def needs_rethink(self, score: float) -> bool:
        """consistency 低于阈值 → 需要 rethink."""
        return score < self.consistency_threshold


def make_jury_consistency_judge(
    router: Any,
    *,
    judge_count: int = 5,
) -> Callable[[str, str], Awaitable[float]]:
    """Wrap multi_judge as a ThoughtActionConsistency llm_judge callback.

    Wire 35 owns the rethink loop inside StructuredStepGenerator. C34 only plugs
    the heavier jury signal into ThoughtActionConsistency so the existing loop
    can decide whether to regenerate.
    """
    normalized_count = max(3, min(5, judge_count))
    judge_models = [f"consistency_judge_{idx}" for idx in range(1, normalized_count + 1)]

    async def _judge(thought: str, action_type: str) -> float:
        from kun.engineering.multi_judge import jury_evaluate

        artifact = json.dumps(
            {"thought": thought, "action_type": action_type},
            ensure_ascii=False,
            sort_keys=True,
        )
        rubric = (
            "判断 thought 和 action_type 是否一致。"
            "如果 thought 明确支持这个 action_type，score 应接近 1；"
            "如果 thought 在说一件事但 action_type 做另一件事，score 应低于 0.5。"
        )
        verdict = await jury_evaluate(
            artifact=artifact,
            rubric=rubric,
            judge_models=judge_models,
            router=router,
        )
        if verdict.pass_:
            return verdict.avg_score
        return min(verdict.avg_score, 0.49)

    return _judge


def make_lite_jury_consistency_judge(
    router: Any,
    *,
    judge_count: int = 3,
) -> Callable[[str, str], Awaitable[float]]:
    """SMART 模式轻量 jury.

    Full jury 一次跑 3-5 个 judge; SMART 只需要便宜的早停版本。这里复用
    ``jury_evaluate_anchor_then_expand``，通常 2 个 judge 意见一致就停。
    """
    normalized_count = max(2, min(3, judge_count))
    judge_models = [f"lite_consistency_judge_{idx}" for idx in range(1, normalized_count + 1)]

    async def _judge(thought: str, action_type: str) -> float:
        from kun.engineering.multi_judge import jury_evaluate_anchor_then_expand

        artifact = json.dumps(
            {"thought": thought, "action_type": action_type},
            ensure_ascii=False,
            sort_keys=True,
        )
        rubric = "轻量判断 thought 和 action_type 是否一致。明确支持给高分；明显不一致给低分。"
        verdict = await jury_evaluate_anchor_then_expand(
            artifact=artifact,
            rubric=rubric,
            judge_models=judge_models,
            router=router,
            max_rounds=normalized_count,
            use_marginal_stop=True,
        )
        if verdict.pass_:
            return verdict.avg_score
        return min(verdict.avg_score, 0.49)

    return _judge


class StructuredStepGenerator:
    """Generate Hermes execution steps.

    Wire 11: 老接口 generate() 走 SMART/MAX 模式生成 ExecutionStep.
    Wire 35 (V2.2 §27): 加 Inference-Time Rethinking — consistency 低 → 自动
        重生最多 max_rethinks 次. 跟 ThoughtActionConsistency.needs_rethink 联动.
    """

    def __init__(
        self,
        llm_router: Any,
        *,
        consistency_checker: ThoughtActionConsistency | None = None,
        max_rethinks: int = 2,
    ) -> None:
        self._router = llm_router
        self._consistency = consistency_checker
        self._max_rethinks = max(0, max_rethinks)

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

        # Wire 35: rethink loop — consistency 不足 → 自动重生 (max N 次)
        rethink_attempts = 0
        prior_step: ExecutionStep | None = None
        last_score = 1.0
        while True:
            request = _build_request(prompt, context, normalized_mode, prior_step=prior_step)
            response = await _invoke_router(self._router, request)
            payload = _extract_payload(response)
            if payload is None:
                return _fallback_step(prompt, context=context, reason="unparseable_llm_response")

            step = _coerce_step(payload, prompt=prompt, context=context)
            step.rethink_count = rethink_attempts

            # 没装 consistency checker → Wire 17 行为, 直接返
            if self._consistency is None:
                return step

            score, _reason = await self._consistency.check(step, mode=normalized_mode)
            step.thought_action_consistency = score
            last_score = score

            if not self._consistency.needs_rethink(score):
                return step

            if rethink_attempts >= self._max_rethinks:
                logger.info(
                    "hermes.rethink_exhausted score=%.2f attempts=%d → 返最终 step (consistency 仍不足)",
                    score,
                    rethink_attempts,
                )
                return step

            rethink_attempts += 1
            prior_step = step
            logger.info(
                "hermes.rethink_triggered attempt=%d score=%.2f mode=%s",
                rethink_attempts,
                last_score,
                normalized_mode,
            )


def _normalize_mode(mode: str) -> ExecutionMode:
    upper = mode.upper()
    if upper in ("FAST", "SMART", "MAX"):
        return cast(ExecutionMode, upper)
    return "SMART"


def _build_request(
    prompt: str,
    context: dict[str, Any],
    mode: ExecutionMode,
    *,
    prior_step: ExecutionStep | None = None,
) -> LLMRequest:
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
    messages = [LLMMessage(role="system", content=_SYSTEM_PROMPT, cache=True)]
    # Wire 29A: lab recipe 推荐过 hermes_prompt_template strategy → 注入额外 system
    extra_system = _maybe_lab_recipe_prompt_hint(context)
    if extra_system:
        messages.append(LLMMessage(role="system", content=extra_system, cache=False))
    # Wire 35: rethink — 上轮 step thought 跟 action 不一致, 提示重生
    if prior_step is not None:
        rethink_hint = (
            "RETHINK: 上一次的 thought 跟 action 不太一致. "
            f"上次 thought={prior_step.thought!r}, action_type={prior_step.action_type}, "
            f"consistency={prior_step.thought_action_consistency:.2f}. "
            "请重新思考: 要么把 thought 写得跟 action_type 真匹配 "
            "(use_skill 提工具 / use_memory 提记忆 / web_search 提查询 / ask_user 提问题), "
            "要么换更合适的 action_type."
        )
        messages.append(LLMMessage(role="system", content=rethink_hint, cache=False))
    messages.append(LLMMessage(role="user", content=user_prompt))
    return LLMRequest(
        messages=messages,
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


def _maybe_lab_recipe_prompt_hint(context: dict[str, Any]) -> str | None:
    """查 LabRecipeRegistry → 该 task_type 的 hermes_prompt_template recipe.

    Returns 额外 system prompt 字符串, 没 lab recipe / 模块没装 → None.
    任何异常静默 None (不破 hermes 主流程).
    """
    task_type = str(context.get("task_type") or context.get("task_kind") or "")
    if not task_type:
        return None
    try:
        from kun.lab.recipe_registry import get_recipe_registry

        registry = get_recipe_registry()
        entry = registry.get(task_type, "hermes_prompt_template")
        if entry is None:
            return None
        return _LAB_STRATEGY_PROMPT_HINT.get(entry.strategy)
    except Exception:
        return None


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
