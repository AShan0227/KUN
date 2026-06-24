"""External Supervisor critique prompt — render N-step look-back for ADR-023.

The critique hook lives one layer above ``ExternalSupervisorService``: rather
than wait for ``PlanReviewService`` to surface a self-report and then hand it
to the supervisor (Mode A heartbeat path), the orchestrator calls the
supervisor **directly** every K main-line steps to ask "is the executor still
on anchor?". The supervisor sees the last K step summaries + the goal anchor
and returns ok / concerning / alarming.

This module owns the prompt rendering only. The orchestrator (caller) handles
counting steps, gathering summaries, calling
``ExternalSupervisorService.analyze_observation`` with the rendered prompt as
the observation payload, and deciding whether to emit
``long_task.drift_alarm``.

Why a separate template (vs reusing ``ExternalSupervisorService``'s built-in
prompt)?
  - The built-in template handles single-observation review (e.g. one
    self-report). The critique is **retrospective over the last K steps** —
    different framing ("did we drift?") and different output spec
    (drift_signals[] + self_aggrandizement_detected).
  - Keeping the critique-specific structure in the integration layer means
    ``ExternalSupervisorService`` stays a generic primitive — Mode A / Mode B /
    critique are all just different prompts feeding the same API.
"""

from __future__ import annotations

from typing import Any

# Max number of steps we ever paste into the critique prompt. Anything longer
# is truncated from the front so the most recent steps survive.
MAX_STEPS_IN_PROMPT = 10

# Maximum characters per step summary inside the prompt. Cuts long tool outputs
# so the supervisor sees a digest, not an entire transcript.
MAX_CHARS_PER_STEP = 600


CRITIQUE_SYSTEM_PROMPT_TEMPLATE = """\
═══ EXTERNAL CRITIC (ADR-023, immutable) ═══

你是 KUN 的独立批判者. 用一个**不同的 LLM** 看主线最近几步, 评估它是否还
在 GOAL ANCHOR 上, 是否产生了不可见的偏移, 是否在自夸 (self-aggrandizement).

不要客气, 直说. 如果看起来正常, 输出 verdict='ok'. 看到偏移 'concerning'.
看到偏离 anchor 或在编 evidence 'alarming'.

═══ GOAL ANCHOR ═══
{anchor_block}

═══ LAST {n_steps} STEPS ═══
{steps_block}

═══ 你的输出 (严格 JSON) ═══
{{
  "verdict": "ok" | "concerning" | "alarming",
  "drift_signals": ["最近某步与 anchor 不一致的具体描述", ...],
  "self_aggrandizement_detected": true | false,
  "rationale": "<一句话>",
  "recommended_action": "<null or short imperative>"
}}

只输出 JSON, 不要 prose.
"""


def _format_anchor_block(anchor: dict[str, Any]) -> str:
    """Render the GOAL ANCHOR section. Mirrors the supervisor service's own
    formatter so the critic sees the same anchor structure as Mode A reviews.
    """
    parts: list[str] = []
    goal = anchor.get("goal_statement")
    if goal:
        parts.append(f"Goal: {goal}")
    sc = anchor.get("success_criteria")
    if isinstance(sc, list) and sc:
        parts.append("Success criteria:")
        parts.extend(f"  - {c}" for c in sc)
    oos = anchor.get("out_of_scope")
    if isinstance(oos, list) and oos:
        parts.append("Out of scope:")
        parts.extend(f"  - {c}" for c in oos)
    inv = anchor.get("invariants")
    if isinstance(inv, list) and inv:
        parts.append("Invariants:")
        parts.extend(f"  - {c}" for c in inv)
    return "\n".join(parts) if parts else "(empty anchor)"


def _format_steps_block(steps: list[dict[str, Any]]) -> str:
    """Render the LAST N STEPS section.

    Each step is rendered as::

        [step N] <summary truncated>

    Truncation per step is bounded by ``MAX_CHARS_PER_STEP``.
    """
    if not steps:
        return "(no recent steps)"
    lines: list[str] = []
    for s in steps:
        idx = s.get("step_idx", "?")
        summary = str(s.get("summary", "")).strip()
        if len(summary) > MAX_CHARS_PER_STEP:
            summary = summary[: MAX_CHARS_PER_STEP - 3] + "..."
        if not summary:
            summary = "(no summary recorded)"
        lines.append(f"[step {idx}] {summary}")
    return "\n".join(lines)


def render_critique_prompt(
    *,
    anchor: dict[str, Any],
    last_steps: list[dict[str, Any]],
    max_steps_in_prompt: int = MAX_STEPS_IN_PROMPT,
) -> str:
    """Render the critique prompt for ExternalSupervisor.

    Args:
      anchor: dict with at minimum a ``goal_statement`` key. Required —
        a critique without an anchor is meaningless (nothing to compare
        against). Pass ``{}`` only for tests; real callers always have an
        anchor in long-task mode.
      last_steps: most-recent-last list of dicts shaped like
        ``{"step_idx": int, "summary": str}``. If longer than
        ``max_steps_in_prompt`` the **front** is truncated so the latest
        steps survive (drift usually shows up in the most recent activity).
      max_steps_in_prompt: hard cap on steps rendered into the prompt.
        Defaults to ``MAX_STEPS_IN_PROMPT`` (=10).

    Raises:
      ValueError: if ``anchor`` is None or empty (we refuse to render a
        critique with nothing to compare against).

    Returns:
      The fully rendered system prompt string. The orchestrator passes this
      to ``ExternalSupervisorService.analyze_observation`` as part of the
      observation payload.
    """
    if not anchor:
        raise ValueError(
            "render_critique_prompt requires a non-empty anchor — critique "
            "without an anchor has nothing to compare against"
        )
    if max_steps_in_prompt < 1:
        raise ValueError("max_steps_in_prompt must be >= 1")

    truncated = list(last_steps[-max_steps_in_prompt:])
    return CRITIQUE_SYSTEM_PROMPT_TEMPLATE.format(
        anchor_block=_format_anchor_block(anchor),
        n_steps=len(truncated),
        steps_block=_format_steps_block(truncated),
    )


# ============================================================
# V7 §16.6 auditor hat — 生产闭环攻击审计员 prompt
# ============================================================
#
# External Supervisor 戴两顶帽子 (V7 §10.2.3 watchdog + §16.6 auditor):
#   - watchdog hat: 持续 tick critique 主 Executor 行为 (这个 module 上半部分)
#   - auditor hat: 周期/pre-release/dogfood 后 跑 7 角度生产闭环审计 (下面新增)
#
# 7 角度审计 prompt 来自用户 Claude Code 复盘里的"生产闭环攻击审计员"工程化.
# V7 §16.6 强制 External Supervisor 周期 (默认每周 / dogfood 完成后 / capability
# Canary→Production gate 前) 戴这顶帽子跑一次审计.

AUDITOR_SYSTEM_PROMPT_TEMPLATE = """\
═══ KUN 生产闭环攻击审计员 (V7 §16.6, immutable) ═══

你是这个项目的"生产闭环攻击审计员". 目标不是证明功能存在, 是证明系统**不能被绕过**.

请从攻击者视角检查方案、代码、测试、真实运行路径和产物.

重点检查 (V7 §16.2 6 反模式衍生 + X.I-1 production-path traceability):

1. 文档声称 done 的能力, 真实生产路径是否必经?
2. 是否存在旧入口、脚本入口、调试入口、直接渲染入口绕过核心能力?
3. 是否有 schema / helper / mock / fallback 被包装成真实完成?
4. 测试是否只测模块成功, 还是测试坏样例必须失败?
5. 每个产物是否有 trace: 输入、决策、门禁、评分、失败原因、修复记录?
6. 如果某能力缺失, 系统是降级并阻断, 还是继续假装成功?
7. 真实用户最关心的结果, 是否被端到端验收覆盖?
8. **(Angle 8 / X.I-1)** **Production-path traceability**: 这个能力是否能从 docs/PRODUCTION_ENTRIES.md 列的真生产入口 (orchestrator.py / daemon.py / idle_batch.py / api/main.py / api/ws.py / api/chat.py) 至少一处真实例化或调用? 如果"在 class signature 中存在"但"没有任何生产入口调用 (只有 test / dogfood script 调)", 则**这是 X.H 自检的核心失败模式**: nominal wired 但实际孤儿. 必须 risk_level≥P1, allow_release=false, must_fix 加上 "wire to at least one production entry".

═══ 审计输入 ═══
{audit_input_block}

═══ 你的输出 (严格 JSON) ═══
{{
  "design_promise": "<本次审计目标 capability 的设计承诺>",
  "real_code_path": "<grep 验证的真实代码路径>",
  "bypass_methods": ["<可绕过方式 1>", "<可绕过方式 2>", ...],
  "min_repro_steps": "<最小复现绕过路径>",
  "risk_level": "P0" | "P1" | "P2",
  "must_fix": ["<必须修复项 1>", "<必须修复项 2>", ...],
  "acceptance_tests": ["<新加的攻击型测试 1>", ...],
  "allow_release": true | false,
  "rationale": "<一段话总结>"
}}

只输出 JSON, 不要 prose.
"""


def render_auditor_prompt(
    *,
    capability_name: str,
    design_doc_excerpt: str,
    code_paths_to_audit: list[str],
    test_files_to_audit: list[str],
    recent_dogfood_summary: str | None = None,
    production_path_check: dict[str, object] | None = None,
) -> str:
    """Render the V7 §16.6 auditor hat prompt for External Supervisor.

    Args:
        capability_name: 被审计能力名 (e.g. "self-reflect skill", "Mission Director runner").
        design_doc_excerpt: V7 / TaskPlan 里对此能力的设计承诺摘录 (≤ 500 字).
        code_paths_to_audit: 跟此能力相关的代码路径列表 (e.g. ["kun/agents/director/planner.py"]).
        test_files_to_audit: 跟此能力相关的测试文件 (e.g. ["tests/unit/test_planner.py"]).
        recent_dogfood_summary: 可选, 最近一次 dogfood 任务里此能力实际表现摘要.

    Returns:
        完整渲染的 auditor prompt 字符串.
    """
    parts: list[str] = []
    parts.append(f"被审计能力: {capability_name}")
    parts.append("")
    parts.append("=== 设计承诺 (文档摘录) ===")
    parts.append(design_doc_excerpt[:1500] or "(无文档摘录)")
    parts.append("")
    parts.append("=== 待审计代码路径 ===")
    if code_paths_to_audit:
        for p in code_paths_to_audit:
            parts.append(f"  - {p}")
    else:
        parts.append("  (无代码路径)")
    parts.append("")
    parts.append("=== 待审计测试文件 ===")
    if test_files_to_audit:
        for p in test_files_to_audit:
            parts.append(f"  - {p}")
    else:
        parts.append("  (无测试文件)")
    if recent_dogfood_summary:
        parts.append("")
        parts.append("=== 最近 dogfood 表现 ===")
        parts.append(recent_dogfood_summary[:1500])
    if production_path_check:
        parts.append("")
        parts.append("=== X.I-1 Production-path Reachability (auto-computed) ===")
        parts.append(
            f"symbol audited: {production_path_check.get('symbol', '?')}"
        )
        parts.append(
            f"reachable from production entry? "
            f"{production_path_check.get('reachable', False)}"
        )
        entries_hit = production_path_check.get("entries_hit") or []
        if entries_hit:
            parts.append("entries hit:")
            for ent in entries_hit[:8]:
                parts.append(f"  - {ent}")
        else:
            parts.append(
                "entries hit: NONE — this is the X.H 'class wired but orphan' "
                "pattern. risk_level must reflect this."
            )
    audit_input_block = "\n".join(parts)

    return AUDITOR_SYSTEM_PROMPT_TEMPLATE.format(audit_input_block=audit_input_block)


__all__ = [
    "AUDITOR_SYSTEM_PROMPT_TEMPLATE",
    "CRITIQUE_SYSTEM_PROMPT_TEMPLATE",
    "MAX_CHARS_PER_STEP",
    "MAX_STEPS_IN_PROMPT",
    "render_auditor_prompt",
    "render_critique_prompt",
]
