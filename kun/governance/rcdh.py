"""RCDH · Root-Cause Diagnostic Hierarchy 强制诊断层级 (ADR-021).

任何修复行为前**必须**按 4 级顺序排查; 不允许越级.

  L0 产品设计层    → 是不是 ADR/PROMISES 写错？(Director + 人)
  L1 功能区激活层  → runtime_capabilities / feature flag / 调用链有没接？(Gate)
  L2 模块开发层    → 哪个模块本身写错？(Supervisor + Strategist)
  L3 代码层        → 具体 bug fix (Executor)

工程化护栏:
  1. 修复必须有 diagnostic_id (Gate 拒绝裸修)
  2. StrategyExperiment 必须声明 target_level
  3. 重复 ≥ 3 次同症状强制升 L0/L1
  4. 诊断范围 narrow_scope ≤ 5 模块, 不允许喂全 codebase

实施路径: L2 阶段实装. 现在是骨架.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from kun.core.logging import get_logger

log = get_logger("kun.governance.rcdh")

DiagnosticLevel = Literal[0, 1, 2, 3]
"""0: 产品设计 / 1: 功能区激活 / 2: 模块开发 / 3: 代码"""


class LevelCheckResult(BaseModel):
    """单层诊断结果."""

    skipped: bool = False
    skip_reason: str | None = None
    is_root_cause: bool = False
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class DiagnosticRecord(BaseModel):
    """完整诊断记录 — 写入 diagnostic_records 表 (alembic 0011)."""

    diagnostic_id: str  # ULID
    triggered_by_event_id: str
    symptom_summary: str
    repeat_history_count: int = 0  # 同症状之前出现几次

    level_0_check: LevelCheckResult = Field(default_factory=LevelCheckResult)
    level_1_check: LevelCheckResult = Field(default_factory=LevelCheckResult)
    level_2_check: LevelCheckResult = Field(default_factory=LevelCheckResult)
    level_3_check: LevelCheckResult = Field(default_factory=LevelCheckResult)

    root_cause_level: DiagnosticLevel | None = None
    recommended_action: Literal["redesign", "activate", "module_rsi", "code_fix"] | None = None
    scope_modules: list[str] = Field(default_factory=list, max_length=5)  # ≤5 强制

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


async def run_diagnostic(
    symptom: str,
    *,
    triggered_by_event_id: str,
    repeat_history_count: int = 0,
    scope_modules: list[str] | None = None,
) -> DiagnosticRecord:
    """走 RCDH 4 级诊断.

    重复 ≥ 3 次同症状强制升 L0/L1 — 不允许在 L3 再修一次.

    L2 实装阶段加: 4 级具体检查逻辑 (LLM + 工程化检查).
    """
    if scope_modules and len(scope_modules) > 5:
        raise ValueError(
            f"scope_modules must be ≤ 5 (got {len(scope_modules)}). Use narrow_scope() to圈定."
        )

    record = DiagnosticRecord(
        diagnostic_id=_new_id(),
        triggered_by_event_id=triggered_by_event_id,
        symptom_summary=symptom,
        repeat_history_count=repeat_history_count,
        scope_modules=scope_modules or [],
    )

    # TODO L2: 实装 4 级具体检查
    # if repeat_history_count >= 3:
    #     # 强制从 L0 开始
    #     await _check_level_0(record, ...)
    # else:
    #     # 按顺序 L0 → L3
    #     for level in (0, 1, 2, 3):
    #         await _check_level(record, level)
    #         if record.root_cause_level is not None:
    #             break

    log.info("rcdh.diagnostic_started", diagnostic_id=record.diagnostic_id)
    return record


def _new_id() -> str:
    from kun.core.ids import new_id

    return new_id("diagnostic")
