"""Intent Interpreter (§7.1 L1).

Takes user natural-language → structured TaskMeta + TaskSpec.
Uses top-tier model (ADR-003 Claude Code delegates to LLM, we just call router).

Output format constrained by Pydantic schema via structured prompt.
"""

from __future__ import annotations

from kun.core.logging import get_logger
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.interface.llm import (
    LLMMessage,
    LLMRequest,
    LLMRouter,
    TaskProfile,
)

log = get_logger("kun.brain.intent")


_SYSTEM_PROMPT = """你是 KUN 的意图理解层. 用户发来自然语言任务, 你把它转成结构化 TASK.md.

输出 JSON, 严格按以下字段:

{
  "task_type": "coding.python.fastapi",    // 层级分类: 最细 3-4 层
  "risk_level": "low|medium|high|critical", // 是否涉及金额 / 不可逆 / 合规
  "complexity_score": 0.0-1.0,              // 预估复杂度
  "estimated_cost_usd": 0.0,                // 预估成本
  "estimated_duration_sec": 0.0,            // 预估时长
  "success_criteria_short": "一句话",        // <= 200 字符
  "goal_detail": "具体可验证的目标描述",
  "success_metrics": ["...", "..."],
  "required_skills": ["skill-xxx"],
  "required_tools": ["bash", "file_edit"],
  "external_resources": ["..."],
  "constraints": [{"kind":"budget_cap","detail":"预算上限 $0.5"}],
  "foreseen_risks": [{"description":"...","severity":"medium"}],
  "fallback_plan": null
}

识别原则:
- 能明确说出"成功"的标准 (可验证)
- 宁可保守估 cost / duration (不高估用户信任度)
- 不确定就选 low complexity / medium risk
- task_type 从已知 taxonomy 里匹配最接近的, 实在找不到用 "general.*"
"""


class IntentInterpreter:
    """Natural-language → TASK.md interpreter."""

    def __init__(self, router: LLMRouter) -> None:
        self.router = router

    async def interpret(
        self,
        user_message: str,
        *,
        owner: Owner,
    ) -> TaskRef:
        """Parse user message into a TaskRef (meta + spec)."""
        request = LLMRequest(
            messages=[
                LLMMessage(role="system", content=_SYSTEM_PROMPT, cache=True),
                LLMMessage(role="user", content=user_message),
            ],
            temperature=0.2,
            max_tokens=1024,
            profile=TaskProfile(needs_reasoning=True),
        )
        response = await self.router.invoke(request, purpose="intent")

        parsed = self._parse_json(response.content)

        fingerprint = TaskMeta.compute_fingerprint(user_message, owner)
        meta = TaskMeta(
            fingerprint=fingerprint,
            task_type=parsed.get("task_type", "general.default"),
            risk_level=parsed.get("risk_level", "low"),
            complexity_score=float(parsed.get("complexity_score", 0.3)),
            owner=owner,
            estimated_cost_usd=float(parsed.get("estimated_cost_usd", 0.05)),
            estimated_duration_sec=float(parsed.get("estimated_duration_sec", 30.0)),
            success_criteria_short=parsed.get("success_criteria_short", user_message[:200]),
        )

        spec: TaskSpec | None = None
        if any(k in parsed for k in ("goal_detail", "success_metrics", "required_skills")):
            spec = TaskSpec(
                goal_detail=parsed.get("goal_detail", user_message),
                success_metrics=parsed.get("success_metrics", []),
                required_skills=parsed.get("required_skills", []),
                required_tools=parsed.get("required_tools", []),
                external_resources=parsed.get("external_resources", []),
                constraints=parsed.get("constraints", []),
                foreseen_risks=parsed.get("foreseen_risks", []),
                fallback_plan=parsed.get("fallback_plan"),
            )

        log.info(
            "intent.parsed",
            task_id=meta.task_id,
            task_type=meta.task_type,
            risk=meta.risk_level,
        )
        return TaskRef(meta=meta, spec=spec)

    @staticmethod
    def _parse_json(text: str) -> dict:
        import json
        import re

        # Try direct JSON
        stripped = text.strip()
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

        # Look for first ```json ... ``` block
        m = re.search(r"```(?:json)?\s*\n(.+?)\n```", stripped, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass

        # Look for any { ... } block
        m = re.search(r"\{.*\}", stripped, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass

        log.warning("intent.parse_fallback", sample=stripped[:200])
        return {}
