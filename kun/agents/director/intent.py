"""Intent Interpreter (§7.1 L1).

Takes user natural-language → structured TaskMeta + TaskSpec.
Uses top-tier model (ADR-003 Claude Code delegates to LLM, we just call router).

Output format constrained by Pydantic schema via structured prompt.
"""

from __future__ import annotations

import json
from typing import Any, cast

from kun.core.logging import get_logger
from kun.datamodel.task import Owner, TaskMeta, TaskRef, TaskSpec
from kun.interface.llm import (
    LLMMessage,
    LLMRequest,
    LLMRouter,
    TaskProfile,
)

log = get_logger("kun.agents.director.intent")


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
- constraints.kind 只能是 no_external_paid_api / path_only / budget_cap / no_irreversible / custom.
- 如果约束是 workspace、branch、port、sandbox、review gate 等自定义约束, kind 必须用 custom, 原始类别写进 detail.
"""


_ALLOWED_CONSTRAINT_KINDS = {
    "no_external_paid_api",
    "path_only",
    "budget_cap",
    "no_irreversible",
    "custom",
}

_CONSTRAINT_DETAIL_KEYS = ("detail", "description", "text", "value", "reason")


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
        # OTel: business-level span around intent parsing.
        from opentelemetry import trace

        tracer = trace.get_tracer("kun.agents.director.intent")
        with tracer.start_as_current_span("kun.intent.interpret") as span:
            span.set_attribute("kun.tenant_id", owner.tenant_id)
            span.set_attribute("kun.user_message_len", len(user_message))

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
            span.set_attribute("kun.cost_usd_equivalent", response.cost_usd_equivalent)

        fingerprint = TaskMeta.compute_fingerprint(user_message, owner)
        parsed = self._parse_json(response.content)
        if not parsed:
            log.error(
                "intent.parse_failed_using_defaults",
                tenant_id=owner.tenant_id,
                fingerprint=fingerprint,
                provider_response_sample=response.content[:200] if response.content else "",
            )
        # L1.7 (ADR-020 + ADR-022): 派生 complexity / priority_profile / estimated_steps
        # 这些字段决定: 路由是否启用 cost downgrade / 是否进 long-task mode / 是否生成 GoalAnchor
        complexity_score = float(parsed.get("complexity_score", 0.3))
        risk_level = parsed.get("risk_level", "low")
        estimated_duration = float(parsed.get("estimated_duration_sec", 30.0))
        estimated_steps = int(parsed.get("estimated_steps", 1))

        complexity_label = _derive_complexity(complexity_score, risk_level, estimated_steps)
        priority_profile = _derive_priority_profile(complexity_label, risk_level)

        meta = TaskMeta(
            fingerprint=fingerprint,
            task_type=parsed.get("task_type", "general.default"),
            risk_level=risk_level,
            complexity_score=complexity_score,
            complexity=complexity_label,
            priority_profile=priority_profile,
            owner=owner,
            estimated_cost_usd=float(parsed.get("estimated_cost_usd", 0.05)),
            estimated_duration_sec=estimated_duration,
            estimated_steps=estimated_steps,
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
                constraints=self._normalize_constraints(parsed.get("constraints", [])),
                foreseen_risks=parsed.get("foreseen_risks", []),
                fallback_plan=parsed.get("fallback_plan"),
            )

        # L1.7 + ADR-022: complex 任务自动生成 GoalAnchor (long-task mode 准备)
        # GoalAnchor 是否真 pin 进 system prompt 在 L1.8 实装; 这里只生成 + 留 ref.
        goal_anchor: Any = None
        if complexity_label == "complex" or _is_long_task(meta):
            goal_anchor = self._build_goal_anchor(meta, spec, user_message, parsed)

        log.info(
            "intent.parsed",
            task_id=meta.task_id,
            task_type=meta.task_type,
            risk=meta.risk_level,
            complexity=complexity_label,
            priority_profile=priority_profile,
            has_goal_anchor=goal_anchor is not None,
        )
        task_ref = TaskRef(meta=meta, spec=spec)
        if goal_anchor is not None:
            # 暂时挂在 TaskRef 上 (TaskRef 用 extra="allow" or via setattr).
            # L1.8 阶段把 anchor 写入 goal_anchors 表 + Executor 真读 + pin.
            task_ref.goal_anchor = goal_anchor
        return task_ref

    @staticmethod
    def _build_goal_anchor(
        meta: TaskMeta,
        spec: TaskSpec | None,
        user_message: str,
        parsed: dict[str, Any],
    ) -> Any:
        """从 TaskMeta + TaskSpec 构造 GoalAnchor (long-task mode 用).

        goal_statement 优先用 success_criteria_short, 否则截 user_message.
        invariants / out_of_scope 优先用 spec, 否则空.
        """
        from kun.agents.director.anchor import GoalAnchor

        goal_statement = meta.success_criteria_short
        if len(goal_statement) > 200:
            goal_statement = goal_statement[:197] + "..."

        success_criteria: list[str] = []
        out_of_scope: list[str] = []
        invariants: list[str] = []
        if spec is not None:
            success_criteria = list(spec.success_metrics)[:5]
            invariants = [c.get("detail", "") for c in spec.constraints if isinstance(c, dict)][:5]
        # 从 parsed 的额外字段读取 out_of_scope (LLM 可主动列出)
        out_of_scope = list(parsed.get("out_of_scope", []))[:5]

        return GoalAnchor(
            task_id=meta.task_id,
            goal_statement=goal_statement,
            success_criteria=success_criteria,
            out_of_scope=out_of_scope,
            invariants=invariants,
        )

    @staticmethod
    def _normalize_constraints(raw_constraints: Any) -> list[dict[str, str]]:
        """Preserve model-proposed constraints while fitting the TASK.md schema.

        Models often invent useful domain labels such as ``workspace_isolation``
        or ``review_gate``.  Those labels are semantically valuable but are not
        legal ``Constraint.kind`` values, so keep them in ``detail`` and store
        the constraint as ``custom`` instead of rejecting the whole task.
        """
        if raw_constraints is None:
            return []
        if not isinstance(raw_constraints, list):
            raw_constraints = [raw_constraints]

        normalized: list[dict[str, str]] = []
        for raw in raw_constraints:
            if isinstance(raw, str):
                detail = raw.strip()
                if detail:
                    normalized.append({"kind": "custom", "detail": detail})
                continue

            if not isinstance(raw, dict):
                detail = str(raw).strip()
                if detail:
                    normalized.append({"kind": "custom", "detail": detail})
                continue

            raw_kind = str(raw.get("kind") or "custom").strip()
            kind = raw_kind if raw_kind in _ALLOWED_CONSTRAINT_KINDS else "custom"
            detail = ""
            for key in _CONSTRAINT_DETAIL_KEYS:
                value = raw.get(key)
                if value is not None and str(value).strip():
                    detail = str(value).strip()
                    break
            if not detail:
                extra = {k: v for k, v in raw.items() if k != "kind"}
                detail = (
                    json.dumps(extra, ensure_ascii=False, sort_keys=True) if extra else raw_kind
                )
            if kind == "custom" and raw_kind not in _ALLOWED_CONSTRAINT_KINDS:
                detail = f"{raw_kind}: {detail}"
            normalized.append({"kind": kind, "detail": detail})

        return normalized

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        import json
        import re

        # Try direct JSON
        stripped = text.strip()
        try:
            return cast(dict[str, Any], json.loads(stripped))
        except json.JSONDecodeError:
            pass

        # Look for first ```json ... ``` block
        m = re.search(r"```(?:json)?\s*\n(.+?)\n```", stripped, re.DOTALL)
        if m:
            try:
                return cast(dict[str, Any], json.loads(m.group(1)))
            except json.JSONDecodeError:
                pass

        # Look for any { ... } block
        m = re.search(r"\{.*\}", stripped, re.DOTALL)
        if m:
            try:
                return cast(dict[str, Any], json.loads(m.group(0)))
            except json.JSONDecodeError:
                pass

        log.warning("intent.parse_fallback", sample=stripped[:200])
        return {}


# ===================== L1.7 helpers · complexity / priority / long-task =====================


def _derive_complexity(
    complexity_score: float,
    risk_level: str,
    estimated_steps: int,
) -> str:
    """派生 complexity 档位 (simple / medium / complex).

    规则 (Director 的 complexity 三因素):
      - complexity_score >= 0.6 OR estimated_steps >= 5 OR risk_level in {high, critical}
        → complex
      - complexity_score >= 0.3 OR estimated_steps >= 2 OR risk_level == medium
        → medium
      - 否则 simple
    """
    if complexity_score >= 0.6 or estimated_steps >= 5 or risk_level in {"high", "critical"}:
        return "complex"
    if complexity_score >= 0.3 or estimated_steps >= 2 or risk_level == "medium":
        return "medium"
    return "simple"


def _derive_priority_profile(complexity: str, risk_level: str) -> str:
    """派生 priority_profile (speed_first / cost_first).

    新核心原则 (ADR-020 #1):
      - 复杂任务 (complex) → 效果 > 速度 > 成本 → speed_first (路由不启用 cost downgrade)
      - 简单任务 (simple/medium) → 效果 > 成本 > 速度 → cost_first (允许下沉到便宜档)
      - critical risk 强制 speed_first (不为成本牺牲效果)
    """
    if complexity == "complex" or risk_level == "critical":
        return "speed_first"
    return "cost_first"


def _is_long_task(meta: TaskMeta) -> bool:
    """ADR-022 long-task mode 触发条件 (任一即进入).

    long-task mode 下: Executor 在 system prompt 顶部强制 pin GoalAnchor,
    Supervisor 周期性注入 plan_review heartbeat, Input Classifier 6 类分流.
    """
    return (
        meta.complexity == "complex"
        or meta.estimated_duration_sec > 600  # 10 分钟
        or meta.estimated_steps > 5
        or meta.risk_level in {"high", "critical"}
    )
