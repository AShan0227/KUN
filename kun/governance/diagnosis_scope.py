"""诊断范围圈定 — 工程化约束 LLM 不喂全 codebase (ADR-021).

任何 agent 做 trace 或诊断时, 不允许把整个 codebase 塞给 LLM.
必须先通过工程化方法 (事件 trace / 模块索引 / capability_card 关联)
**圈定 ≤ 5 个候选模块**, 再让 LLM 看这 5 个.

实施路径: L2 阶段实装. 现在是骨架.
"""

from __future__ import annotations

from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.governance.diagnosis_scope")

MAX_SCOPE_MODULES = 5


async def narrow_scope(
    symptom: str,
    *,
    evidence: list[dict[str, Any]] | None = None,
) -> list[str]:
    """从 symptom + evidence 圈定 ≤ 5 个候选模块.

    工程化路径 (按顺序尝试):
      1. event trace 反查 → emit 该 event 的模块
      2. capability_card 关联 → 涉及的 entity_id 对应模块
      3. import 反查 → symptom 关键词 grep 命中文件 + 它们的 importers
      4. 兜底: 根据 task_type 默认模块集 (e.g. coding.refactor → kun/agents/executor/)

    严格上限 5 个. 超过 → raise; 调用方必须自己再 narrow.

    L2 实装阶段加具体工程化逻辑.
    """
    # TODO L2: 真实现
    candidates: list[str] = []

    if len(candidates) > MAX_SCOPE_MODULES:
        raise ValueError(
            f"scope圈定后仍有 {len(candidates)} > {MAX_SCOPE_MODULES} 模块. "
            f"必须工程化收敛, 不允许喂给 LLM."
        )

    log.debug(
        "diagnosis_scope.narrowed",
        symptom_preview=symptom[:100],
        candidate_count=len(candidates),
        candidates=candidates,
    )
    return candidates
