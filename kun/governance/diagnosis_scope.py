"""诊断范围圈定 — 工程化约束 LLM 不喂全 codebase (ADR-021).

任何 agent 做 trace 或诊断时, 不允许把整个 codebase 塞给 LLM.
必须先通过工程化方法 (事件 trace / 模块索引 / 关键词匹配)
**圈定 ≤ 5 个候选模块**, 再让 LLM 看这 5 个.

L2.6 实装策略 — engineering-first, 无 LLM:
  1. evidence 里显式 module 字段 → 直接收
  2. symptom 中的路径 token (kun/agents/...) → 直接收
  3. symptom 关键词命中已知模块名 (executor / supervisor / strategist 等)
  4. 兜底空 list — caller (RCDH) 自己处理

严格上限 5 个. 命中数 > 5 时按以下优先级裁剪:
  evidence 显式 > symptom 路径 > 关键词 > 兜底
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.governance.diagnosis_scope")

MAX_SCOPE_MODULES = 5

# 已知 KUN 模块根 — symptom 中出现这些 token 时收为候选
_KNOWN_MODULE_ROOTS = {
    "director",
    "executor",
    "tester",
    "gate",
    "supervisor",
    "strategist",
    "external_supervisor",
    "router",
    "router_capability",
    "capability_router",
    "anchor",
    "input_classifier",
    "rcdh",
    "plan_review",
    "supervisor_service",
    "orchestrator",
    "watchtower",
    "validation",
    "multi_judge",
    "outbox",
    "skill",
    "memory",
    "knowledge",
    "context",
}

# 提取 kun/.../ 风格路径片段
_PATH_RE = re.compile(r"\bkun(?:/[a-zA-Z_][a-zA-Z0-9_]*){1,4}\b")
# Python 模块路径 kun.foo.bar
_DOTTED_RE = re.compile(r"\bkun(?:\.[a-zA-Z_][a-zA-Z0-9_]*){1,4}\b")
# 一般的标识符 (snake_case 或 CamelCase)
_IDENT_RE = re.compile(r"\b[a-zA-Z_][a-zA-Z0-9_]{2,}\b")


def _extract_module_paths(text: str) -> list[str]:
    """从文本提取 kun/foo/bar 或 kun.foo.bar 形式路径."""
    paths: list[str] = []
    for m in _PATH_RE.finditer(text):
        paths.append(m.group(0))
    for m in _DOTTED_RE.finditer(text):
        # kun.foo.bar → kun/foo/bar 统一表示
        paths.append(m.group(0).replace(".", "/"))
    return paths


def _keyword_match_modules(text: str) -> list[str]:
    """命中已知模块根 → 输出 kun/<root> 规范化路径."""
    found: set[str] = set()
    low = text.lower()
    for ident in _IDENT_RE.findall(low):
        if ident in _KNOWN_MODULE_ROOTS:
            found.add(f"kun/<scope>/{ident}")
    return sorted(found)


async def narrow_scope(
    symptom: str,
    *,
    evidence: list[dict[str, Any]] | None = None,
) -> list[str]:
    """从 symptom + evidence 圈定 ≤ 5 个候选模块.

    优先级:
      1. evidence 中显式 module 字段
      2. symptom 中的路径 token (kun/agents/...)
      3. symptom 关键词命中已知模块根
    """
    ranked: Counter[str] = Counter()

    # 1. evidence 显式 module — 高权重
    for ev in evidence or []:
        mod = ev.get("module")
        if isinstance(mod, str) and mod.strip():
            ranked[mod.strip()] += 10

    # 2. symptom 路径 token — 中权重
    for path in _extract_module_paths(symptom):
        ranked[path] += 5

    # 3. 关键词命中 — 低权重
    for path in _keyword_match_modules(symptom):
        ranked[path] += 1

    if not ranked:
        log.debug("diagnosis_scope.no_candidates", symptom_preview=symptom[:80])
        return []

    # 按权重排序, 截 5
    candidates = [m for m, _ in ranked.most_common(MAX_SCOPE_MODULES)]

    log.debug(
        "diagnosis_scope.narrowed",
        symptom_preview=symptom[:100],
        candidate_count=len(candidates),
        candidates=candidates,
    )

    if len(candidates) > MAX_SCOPE_MODULES:
        # 防御: most_common 已截 5 不应走到这, 但 invariant 保持
        raise ValueError(
            f"scope圈定后仍有 {len(candidates)} > {MAX_SCOPE_MODULES} 模块. "
            f"必须工程化收敛, 不允许喂给 LLM."
        )
    return candidates


__all__ = ["MAX_SCOPE_MODULES", "narrow_scope"]
