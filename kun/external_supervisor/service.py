"""ExternalSupervisorService — 本地模型驱动的独立监督 (ADR-023).

接受结构化 observation request → 调本地 LLM (LocalLLMProvider) 出结构化
verdict + rationale + recommended_action.

设计核心:
  1. **物理 / 模型隔离**: LLM 注入 (默认 LocalLLMProvider), 不复用主线 router
  2. **结构化 prompt**: anchor + observation 都进 system prompt 顶部 (anchor pinning)
  3. **JSON-first parser + 文本兜底**: 尽量解 JSON, 解不出走启发式
  4. **限并发**: asyncio.Semaphore — 本地推理慢, 不能让 caller 把它打挂

L2.4 提供基础 analyze_observation; L2.5 加 Mode A (同步) / Mode B (复盘) /
self_aggrandizement_check (自嗨检测) 三个上层 API.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kun.core.ids import new_id
from kun.core.logging import get_logger
from kun.interface.llm.base import LLMMessage, LLMProvider, LLMRequest

log = get_logger("kun.external_supervisor.service")


_VERDICTS = ("ok", "concerning", "alarming")
_DEFAULT_MAX_TOKENS = 512
_DEFAULT_TEMPERATURE = 0.2


@dataclass(frozen=True)
class ExternalSupervisorObservation:
    """监督一次性观察结果."""

    observation_id: str
    observed_at: datetime
    obs_kind: str  # "task_complete" / "drift_check" / "gate_review" / ...
    target_task_id: str | None
    target_anchor_id: str | None
    verdict: str  # ok / concerning / alarming
    rationale: str
    recommended_action: str | None
    raw_llm_content: str  # 原始返回 (debug)
    model_used: str
    extras: dict[str, Any] = field(default_factory=dict)


_SYSTEM_PROMPT_TEMPLATE = """═══ EXTERNAL SUPERVISOR (ADR-023, immutable, pinned) ═══

You are KUN's External Supervisor — an INDEPENDENT auditor running in
a separate process with a LOCAL model. Your job is to second-guess the
main agent line by checking observations against the goal anchor.

You must NOT be sycophantic. If the observation looks fine, say "ok".
If something feels off, say "concerning". If the observation breaks the
anchor or fabricates evidence, say "alarming".

Output STRICT JSON ONLY (no prose around it):
{{
  "verdict": "ok" | "concerning" | "alarming",
  "rationale": "<one sentence>",
  "recommended_action": "<null or short imperative>"
}}

═══ GOAL ANCHOR ═══
{anchor_block}

═══ OBSERVATION (kind = {obs_kind}) ═══
{observation_block}

═══ END ═══

Return JSON only.
"""


def _format_anchor(anchor: dict[str, Any] | None) -> str:
    if not anchor:
        return "(no goal anchor — observation is unconstrained)"
    parts: list[str] = []
    if "goal_statement" in anchor:
        parts.append(f"Goal: {anchor['goal_statement']}")
    if "success_criteria" in anchor:
        sc = anchor["success_criteria"]
        if isinstance(sc, list) and sc:
            parts.append("Success criteria:")
            parts.extend(f"  - {c}" for c in sc)
    if "out_of_scope" in anchor:
        oos = anchor["out_of_scope"]
        if isinstance(oos, list) and oos:
            parts.append("Out of scope:")
            parts.extend(f"  - {c}" for c in oos)
    if "invariants" in anchor:
        inv = anchor["invariants"]
        if isinstance(inv, list) and inv:
            parts.append("Invariants:")
            parts.extend(f"  - {c}" for c in inv)
    return "\n".join(parts) if parts else "(empty anchor)"


def _format_observation(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


_JSON_BLOCK_RE = re.compile(r"\{[\s\S]*\}", re.DOTALL)


def _parse_llm_response(raw: str) -> dict[str, Any]:
    """Parse LLM 结构化输出. JSON-first, 文本兜底."""
    text = raw.strip()
    if not text:
        return {"verdict": "concerning", "rationale": "empty supervisor response"}

    # 尝试整体 JSON
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 尝试提取首个 JSON 块
    m = _JSON_BLOCK_RE.search(text)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # 文本启发式
    low = text.lower()
    if "alarm" in low or "broke" in low or "fabricate" in low:
        verdict = "alarming"
    elif "concern" in low or "drift" in low or "off-track" in low:
        verdict = "concerning"
    else:
        verdict = "ok"
    return {
        "verdict": verdict,
        "rationale": text[:300],
        "recommended_action": None,
        "_parsed_via": "heuristic",
    }


def _coerce_verdict(value: Any) -> str:
    """Normalize verdict 到三档."""
    if not isinstance(value, str):
        return "concerning"
    v = value.strip().lower()
    if v in _VERDICTS:
        return v
    # 别名
    if v in ("warn", "warning"):
        return "concerning"
    if v in ("critical", "fail", "alert", "alarm"):
        return "alarming"
    if v in ("pass", "fine", "good", "aligned"):
        return "ok"
    return "concerning"


class ExternalSupervisorService:
    """External Supervisor 服务实例.

    单实例 + asyncio.Semaphore 限并发. 本地推理慢, 不限并发会让 caller queue
    炸. 通常 max_concurrent=2 够.
    """

    def __init__(
        self,
        *,
        llm_provider: LLMProvider,
        max_concurrent: int = 2,
        temperature: float = _DEFAULT_TEMPERATURE,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self._llm = llm_provider
        self._sem = asyncio.Semaphore(max_concurrent)
        self._temperature = temperature
        self._max_tokens = max_tokens

    async def analyze_observation(
        self,
        *,
        obs_kind: str,
        observation_payload: dict[str, Any],
        anchor: dict[str, Any] | None = None,
        target_task_id: str | None = None,
        target_anchor_id: str | None = None,
    ) -> ExternalSupervisorObservation:
        """通用观察分析 → 结构化 verdict.

        Mode A (同步 gate review) + Mode B (任务尾复盘) + 自嗨检测都基于此.
        L2.5 在上层加专用 wrapper.
        """
        prompt = _SYSTEM_PROMPT_TEMPLATE.format(
            anchor_block=_format_anchor(anchor),
            obs_kind=obs_kind,
            observation_block=_format_observation(observation_payload),
        )

        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=prompt),
                LLMMessage(role="user", content="Audit the observation above and respond with JSON."),
            ],
            temperature=self._temperature,
            max_tokens=self._max_tokens,
        )

        async with self._sem:
            try:
                response = await self._llm.invoke(request)
            except Exception as e:
                log.warning(
                    "external_supervisor.llm_invoke_failed",
                    error=str(e),
                    obs_kind=obs_kind,
                )
                # Fail-loud — caller 决定如何降级
                raise

        parsed = _parse_llm_response(response.content)
        verdict = _coerce_verdict(parsed.get("verdict"))
        rationale = str(parsed.get("rationale", "") or "").strip()
        recommended = parsed.get("recommended_action")
        if isinstance(recommended, str):
            recommended = recommended.strip() or None
        elif recommended is not None:
            recommended = str(recommended)

        return ExternalSupervisorObservation(
            observation_id=new_id("evidence"),
            observed_at=datetime.now(UTC),
            obs_kind=obs_kind,
            target_task_id=target_task_id,
            target_anchor_id=target_anchor_id,
            verdict=verdict,
            rationale=rationale,
            recommended_action=recommended if isinstance(recommended, str) else None,
            raw_llm_content=response.content,
            model_used=response.model or self._llm.model_id,
            extras={
                "parsed_via": parsed.get("_parsed_via", "json"),
                "provider": response.provider,
                "latency_ms": response.latency_ms,
            },
        )


__all__ = [
    "ExternalSupervisorObservation",
    "ExternalSupervisorService",
]
