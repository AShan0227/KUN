"""Plan Review prompt renderer (LT.B — ADR-022 Layer 4 runtime wiring).

当 PlanReviewHeartbeat 判定需要复审时, 把这一段拼入 Executor 的下一次
LLM call 的 system prompt, 让 Executor 给出结构化自评 JSON.

LLM 看到 GOAL ANCHOR (顶部 pinned) + 这段 review prompt 后, 应该:
  1. 思考"我在 anchor 的哪一步?"
  2. 评估最近几步是否真推进了 success criteria
  3. 诚实回报 drift_risk (anti-sycophancy directive 配合)

response 是结构化 JSON, 由 PlanReviewService 解析 → 调 evaluate_executor_self_report.
"""

from __future__ import annotations

from typing import Any

_PLAN_REVIEW_PROMPT_TEMPLATE = """\
═══ PLAN REVIEW (heartbeat triggered, immutable, answer honestly) ═══

The plan review heartbeat just fired (trigger: {reason}). Before continuing
the task, you MUST output a single JSON object on the topic of
"am I still on the GOAL ANCHOR pinned at the top of this prompt?".

Output strict JSON only, no prose around it:

{{
  "current_step": "<short description of which step you're on, ≤ 60 chars>",
  "on_anchor": true | false,
  "scope_creep_detected": true | false,
  "criteria_done_count": <integer ≥ 0>,
  "criteria_done_count_prev": <integer ≥ 0, or null if first review>,
  "recent_step_summary": "<one sentence summarising the last 3 actions, with goal-relevant keywords>",
  "drift_risk": "low" | "medium" | "high",
  "drift_explanation": "<one sentence reason, or empty>"
}}

Rules:
- Be honest. If a recent action did NOT advance any success criteria, set on_anchor=false.
- The supervisor cross-checks your self-report against the goal anchor and
  may pause you if your claim disagrees with observable evidence — don't
  pretend things are going well.
- Then, after this JSON, proceed with the next step normally.

═══ END PLAN REVIEW ═══
"""


def render_plan_review_prompt(
    *,
    reason: str,
    recent_steps: list[str] | None = None,
    extras: dict[str, Any] | None = None,
) -> str:
    """Render the plan_review system-prompt segment.

    Args:
      reason: e.g. "step_threshold" / "time_threshold" / both
      recent_steps: optional list of recent step descriptions (≤ 3 used)
      extras: optional dict appended as a footer (e.g. {"reviews_done": 2})
    """
    prompt = _PLAN_REVIEW_PROMPT_TEMPLATE.format(reason=reason or "unspecified")
    extra_lines: list[str] = []
    if recent_steps:
        extra_lines.append("")
        extra_lines.append("Last steps the supervisor can see in events:")
        for s in recent_steps[-3:]:
            snippet = s.strip()
            if len(snippet) > 100:
                snippet = snippet[:97] + "..."
            extra_lines.append(f"  - {snippet}")
    if extras:
        extra_lines.append("")
        extra_lines.append("Heartbeat context:")
        for k, v in sorted(extras.items()):
            extra_lines.append(f"  - {k}: {v}")
    if extra_lines:
        prompt = prompt + "\n".join(extra_lines) + "\n"
    return prompt


__all__ = [
    "render_plan_review_prompt",
]
