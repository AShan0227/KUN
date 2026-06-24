"""Input Classifier — ADR-022 Layer 3 长任务模式下的输入分流.

长任务执行期间, 用户消息 / 外部事件 / 工具输出 进入 working context 前
必须先分类, 决定是否真的喂给 Executor:

  on_topic_progress       推进当前目标 → 正常 integrate
  on_topic_clarification  澄清当前目标 → integrate + 更新 success_criteria
  off_topic_noise         与目标无关 → 礼貌回复但不进 working context
  scope_expansion         试图扩大范围 → 触发 RCDH L0 + ask user
  explicit_pivot          用户明确换任务 → pause + 创建新 task
  interrupt               中断/取消 → 走 cancel 流程

工程化优先 (规则 + 关键词), LLM 兜底.

这是 sycophancy 的工程化解药 — 噪音根本不进模型 context, 模型不会
"看到最近的用户消息就回应".
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from kun.core.logging import get_logger

log = get_logger("kun.agents.director.input_classifier")


InputCategory = Literal[
    "on_topic_progress",
    "on_topic_clarification",
    "off_topic_noise",
    "scope_expansion",
    "explicit_pivot",
    "interrupt",
]


class InputClassification(BaseModel):
    """单次输入的分类结果."""

    category: InputCategory
    confidence: float = Field(ge=0.0, le=1.0)
    matched_signal: str = ""  # 命中的规则 / 关键词
    integration_hint: dict[str, Any] = Field(default_factory=dict)


# ---- 工程化关键词规则 ----

# 中断/取消信号 — 最高优先级, 任何长度
_INTERRUPT_PATTERNS = [
    re.compile(r"\b(stop|cancel|abort|halt|kill)\b", re.IGNORECASE),
    re.compile(r"(停止|取消|中断|算了|不要做了|别做了)"),
]

# 明确换任务/换方向信号
_PIVOT_PATTERNS = [
    re.compile(r"\b(instead|switch to|actually do|do something else)\b", re.IGNORECASE),
    re.compile(r"(换成|改成|现在做|换个任务|不做.{0,8}了.{0,4}做|我要做别的|跳过.*做)"),
]

# 澄清/补充信号 — 通常表示在当前目标范围内 refine
_CLARIFICATION_PATTERNS = [
    re.compile(
        r"\b(actually|let me clarify|I meant|to be more specific|more precisely)\b",
        re.IGNORECASE,
    ),
    re.compile(r"(其实我想|我意思是|具体来说|更精确地说|准确说|补充一下)"),
]

# 扩展范围信号 — 加事项, 可能跑题
# "and" / "plus" 太通用易误判, 不放. "also" 在句首才作为强信号
# (避免 "I also think the weather..." 误判)
_SCOPE_EXPANSION_PATTERNS = [
    re.compile(r"\b(additionally|in addition to|on top of that)\b", re.IGNORECASE),
    re.compile(r"\balso (please|let'?s|add|include|do)\b", re.IGNORECASE),  # also + 动词
    re.compile(r"(再加|顺便|另外.{0,4}还要|额外|外加)"),
]


def _extract_keywords(text: str, min_len: int = 2) -> set[str]:
    """从 GoalAnchor 字段提取关键词集合 (小写, 去标点, 去短词).

    用于判断输入与目标的相关度.
    """
    # 简单 tokenize: 把非字母数字非中文字符当分隔符
    tokens = re.findall(rf"[一-鿿]+|[a-zA-Z0-9]{{{min_len},}}", text.lower())
    return set(tokens)


def _overlap_ratio(input_tokens: set[str], goal_tokens: set[str]) -> float:
    """计算输入 vs 目标的关键词重叠比例 (input ∩ goal / input)."""
    if not input_tokens:
        return 0.0
    return len(input_tokens & goal_tokens) / len(input_tokens)


def classify_input(
    new_input: str,
    *,
    goal_anchor: Any | None = None,
    source: str = "user",
) -> InputClassification:
    """同步分类器 (工程化规则, 无 LLM 调用).

    规则优先级 (从高到低):
      1. interrupt 关键词命中 → interrupt
      2. pivot 关键词命中 + 与 goal 重叠 < 0.3 → explicit_pivot
      3. clarification 关键词命中 + 与 goal 重叠 >= 0.3 → on_topic_clarification
      4. out_of_scope 命中 + 与 goal 重叠 < 0.3 → off_topic_noise (优先 scope_expansion 判定)
      5. scope_expansion 关键词命中 + 与 goal 部分重叠 → scope_expansion
      6. 与 goal 关键词重叠 >= 0.3 → on_topic_progress
      7. 其他 → off_topic_noise (保守默认)

    confidence 来自匹配强度: 关键词 hit + overlap ratio 综合.

    无 goal_anchor 时所有相关度归零, 默认走 off_topic_noise (除 interrupt / pivot
    强信号直接命中外).
    """
    cleaned = new_input.strip()
    if not cleaned:
        return InputClassification(
            category="off_topic_noise",
            confidence=1.0,
            matched_signal="empty_input",
        )

    # 1. interrupt 最高优先级
    for pattern in _INTERRUPT_PATTERNS:
        m = pattern.search(cleaned)
        if m:
            return InputClassification(
                category="interrupt",
                confidence=0.95,
                matched_signal=f"interrupt:{m.group(0)}",
                integration_hint={"action": "cancel_task"},
            )

    # 提取 goal 关键词 + 输入关键词
    goal_tokens: set[str] = set()
    out_of_scope_tokens: set[str] = set()
    if goal_anchor is not None:
        goal_tokens = (
            _extract_keywords(getattr(goal_anchor, "goal_statement", ""))
            | _extract_keywords(" ".join(getattr(goal_anchor, "success_criteria", [])))
            | _extract_keywords(" ".join(getattr(goal_anchor, "invariants", [])))
        )
        out_of_scope_tokens = _extract_keywords(
            " ".join(getattr(goal_anchor, "out_of_scope", []))
        )

    input_tokens = _extract_keywords(cleaned)
    goal_overlap = _overlap_ratio(input_tokens, goal_tokens)
    out_of_scope_overlap = _overlap_ratio(input_tokens, out_of_scope_tokens)

    # 2. explicit_pivot: 换方向关键词 + 与 goal 几乎无关
    for pattern in _PIVOT_PATTERNS:
        m = pattern.search(cleaned)
        if m and goal_overlap < 0.3:
            return InputClassification(
                category="explicit_pivot",
                confidence=0.85,
                matched_signal=f"pivot:{m.group(0)}",
                integration_hint={"action": "pause_and_confirm"},
            )

    # 4 优先于 3: 命中 out_of_scope 关键词 → 直接 off_topic_noise (用户写在 anchor.out_of_scope 里的)
    if out_of_scope_overlap >= 0.5:
        return InputClassification(
            category="off_topic_noise",
            confidence=0.9,
            matched_signal=f"matches_out_of_scope:overlap={out_of_scope_overlap:.2f}",
            integration_hint={"action": "queue_for_post_task", "reason": "user_declared_out_of_scope"},
        )

    # 3. on_topic_clarification: 澄清关键词 + 至少 minor 相关 (任一 goal token 命中)
    goal_intersection_count = len(input_tokens & goal_tokens) if goal_tokens else 0
    for pattern in _CLARIFICATION_PATTERNS:
        m = pattern.search(cleaned)
        if m and goal_intersection_count >= 1:
            return InputClassification(
                category="on_topic_clarification",
                confidence=0.8,
                matched_signal=f"clarification:{m.group(0)}",
                integration_hint={"action": "integrate_and_refine_criteria"},
            )

    # 5. scope_expansion: 扩展关键词命中 + 非 interrupt/pivot/clarification
    # 长任务模式下用户说 "also/再加" 几乎都是 scope_expansion 信号, 即使关键词
    # 与 goal 不直接 token 重叠 (可能是 conceptually 相关 — 比如 anchor 是
    # "authentication" 而用户加 "password reset", 没 token 重叠但概念相关).
    for pattern in _SCOPE_EXPANSION_PATTERNS:
        m = pattern.search(cleaned)
        if m:
            return InputClassification(
                category="scope_expansion",
                confidence=0.7 if goal_overlap >= 0.2 else 0.55,
                matched_signal=f"scope_expansion:{m.group(0)}",
                integration_hint={
                    "action": "trigger_rcdh_level_0",
                    "reason": "potential_scope_creep",
                },
            )

    # 6. on_topic_progress: 与 goal 关键词重叠 >= 0.3
    if goal_overlap >= 0.3:
        return InputClassification(
            category="on_topic_progress",
            confidence=min(1.0, 0.5 + goal_overlap),
            matched_signal=f"goal_overlap:{goal_overlap:.2f}",
            integration_hint={"action": "integrate"},
        )

    # 7. 兜底 → off_topic_noise (anti-sycophancy 默认: 不喂给 Executor)
    return InputClassification(
        category="off_topic_noise",
        confidence=0.6,
        matched_signal=f"low_overlap:{goal_overlap:.2f}",
        integration_hint={"action": "queue_for_post_task"},
    )


__all__ = [
    "InputCategory",
    "InputClassification",
    "classify_input",
]
