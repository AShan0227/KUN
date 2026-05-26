"""Evidence Ledger · 任务证据账本 (ADR-024).

每个任务从启动到结束的全链路证据 — 给 Gate 做准入决策 + 给 RSI 闭环
step 9 合议层用 + 给后续审计回放用.

主要字段 (alembic 0011 实装):
  - task_id
  - artifacts (含 hash)
  - test_reports
  - diagnostic_id  (ADR-021 必填 if 修复场景)
  - external_supervisor_debrief_id (ADR-023 必填 if Mode B 跑完)
  - diagnostic_level_reached (ADR-021 必填)
  - decisions (Gate / Supervisor / Strategist 决策点)
  - timestamps

实施路径: L1.3 alembic 0011 建表. 现在是骨架.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from kun.core.logging import get_logger

log = get_logger("kun.governance.evidence_ledger")


class EvidenceEntry(BaseModel):
    """单条证据 — append-only 写入 ledger."""

    entry_id: str
    task_id: str
    kind: str  # "artifact" / "test_report" / "diagnostic" / "debrief" / "decision"
    payload: dict[str, Any]
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


async def append(
    task_id: str,
    *,
    kind: str,
    payload: dict[str, Any],
) -> str:
    """追加一条证据到 task_id 的账本.

    Append-only — 不允许修改既有 entries (审计可信赖).
    """
    # TODO L1.3: 真写入 evidence_ledger 表 (alembic 0011)
    entry_id = _new_id()
    log.debug(
        "evidence_ledger.append",
        task_id=task_id,
        kind=kind,
        entry_id=entry_id,
    )
    return entry_id


async def get_trace(task_id: str) -> list[EvidenceEntry]:
    """读完整 trace — 用于 RCDH backward 回溯 + 复盘."""
    # TODO L1.3: 真读
    return []


def _new_id() -> str:
    from kun.core.ids import new_id

    return new_id("ev")
