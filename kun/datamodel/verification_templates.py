"""V2.3 Wire 46 — Verification 默认模板 (V2.3 §8.3).

按 task_type 自动加默认验收清单, LLM 可补 task-specific spec, 但不能减默认.
解决 "LLM 偷懒不写 verification_specs → 漏检" 问题.

跟 kun/datamodel/verification_spec.py 的 VerificationSpec 配套.
"""

from __future__ import annotations

import logging

from kun.datamodel.verification_spec import VerificationSpec

logger = logging.getLogger(__name__)


# 写作类任务默认验收
WRITING_TEMPLATE: list[VerificationSpec] = [
    VerificationSpec(
        kind="exact_output",
        spec={"min_length_chars": 30},
        required=True,
    ),
]

# 编程类任务默认验收
CODING_TEMPLATE: list[VerificationSpec] = [
    VerificationSpec(
        kind="lint_pass",
        spec={"linter": "ruff"},
        required=False,  # lint warning 不阻 done
    ),
]

# 决策类任务默认验收
DECISION_TEMPLATE: list[VerificationSpec] = [
    VerificationSpec(
        kind="exact_output",
        spec={"min_length_chars": 50, "must_contain_any": ["pros", "cons", "推荐", "建议"]},
        required=True,
    ),
]

# 研究类任务默认验收
RESEARCH_TEMPLATE: list[VerificationSpec] = [
    VerificationSpec(
        kind="exact_output",
        spec={"min_length_chars": 100, "must_contain_any": ["来源", "source", "参考", "reference"]},
        required=False,
    ),
]


# task_type 前缀 → template
_TEMPLATES_BY_PREFIX: dict[str, list[VerificationSpec]] = {
    "writing": WRITING_TEMPLATE,
    "coding": CODING_TEMPLATE,
    "decision": DECISION_TEMPLATE,
    "research": RESEARCH_TEMPLATE,
}


def get_default_verification_specs(task_type: str) -> list[VerificationSpec]:
    """根据 task_type 前缀返默认 verification specs.

    e.g.:
      "writing.creative.short" → WRITING_TEMPLATE
      "coding.python.fastapi" → CODING_TEMPLATE
      "general.x" → []  (无默认)

    复制返回 (调用方可自由 mutate, 不影响模板).
    """
    if not task_type:
        return []
    prefix = task_type.split(".", 1)[0].lower()
    template = _TEMPLATES_BY_PREFIX.get(prefix, [])
    return [VerificationSpec(**spec.model_dump()) for spec in template]


def merge_with_default(
    task_type: str, llm_provided: list[VerificationSpec] | None
) -> list[VerificationSpec]:
    """default + LLM 提供的 — LLM 可补, 不能减.

    重复 kind 时 LLM 的覆盖 default (允许调 spec).
    """
    defaults = get_default_verification_specs(task_type)
    if not llm_provided:
        return defaults
    # LLM 提供的同 kind 覆盖 default
    llm_kinds = {s.kind for s in llm_provided}
    final = [d for d in defaults if d.kind not in llm_kinds]
    final.extend(llm_provided)
    return final


__all__ = [
    "CODING_TEMPLATE",
    "DECISION_TEMPLATE",
    "RESEARCH_TEMPLATE",
    "WRITING_TEMPLATE",
    "get_default_verification_specs",
    "merge_with_default",
]
