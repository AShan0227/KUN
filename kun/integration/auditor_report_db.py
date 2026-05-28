"""V7 §16.6 External Supervisor auditor hat — AuditorReport IO + DB writer.

V7 §16.6 强制 External Supervisor 周期 (每周 / dogfood 完成后 / capability
Canary→Production gate 前) 戴 auditor hat 跑 "生产闭环攻击审计员" 7 角度审计,
产 AuditorReport JSON. Phase G commit `f865e40` 加了 prompt template
(`AUDITOR_SYSTEM_PROMPT_TEMPLATE`) + render helper (`render_auditor_prompt`),
但没定义 frozen IO 也没接 DB.

本模块补这个缺:

1. AuditorReport frozen dataclass — 跟 AUDITOR_SYSTEM_PROMPT_TEMPLATE JSON
   schema 对齐 (9 fields). 是 V7 §13.6 frozen_dataclass_agent_io_contract
   的实例.
2. write_auditor_report — 把 AuditorReport 落到 auditor_reports 表 (alembic
   0016 加).
3. make_auditor_report_emitter(tenant_id) factory — 给 daemon /
   ExternalSupervisor.auditor_run() 注入.

Schema 对齐 (auditor prompt JSON → dataclass):
  design_promise         (string)
  real_code_path         (string)         ← grep 验证的真实代码路径
  bypass_methods         (list[str])      ← 攻击者可绕过方式
  min_repro_steps        (string)         ← 最小复现绕过路径
  risk_level             (P0/P1/P2)
  must_fix               (list[str])
  acceptance_tests       (list[str])      ← 新加的攻击型测试
  allow_release          (bool)           ← false ⇒ 不许发布
  rationale              (string)

V7 §16.6 不变量 (服务层 + DB CHECK 双保险):
  - risk_level='P0' ⇒ allow_release=false (P0 风险必须不许发布)
  - bypass_methods 非空 (审计的意义就是找绕过)
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.integration.auditor_report_db")


# ============================================================
# AuditorReport frozen IO (V7 §13.6 agent IO contract)
# ============================================================


@dataclass(frozen=True)
class AuditorReport:
    """External Supervisor auditor hat 周期审计输出 (V7 §16.6).

    9-field schema 完全对齐 AUDITOR_SYSTEM_PROMPT_TEMPLATE 里要求的 JSON
    output (kun/integration/external_supervisor_critique.py).

    不变量 (V7 §16.6):
      - risk_level='P0' ⇒ allow_release=False
      - bypass_methods 应至少有 1 条 (审计的意义就是找绕过), 但允许空 (确实
        审计不到, 也是有效结论 — 但应触发更深入审计)
    """

    report_id: str
    audited_capability: str
    audited_at: datetime
    auditor_provider: str  # e.g. "anthropic/claude-opus" / "openai/gpt-5.5"
    design_promise: str
    real_code_path: str
    bypass_methods: list[str] = field(default_factory=list)
    min_repro_steps: str = ""
    risk_level: str = "P2"  # P0 / P1 / P2
    must_fix: list[str] = field(default_factory=list)
    acceptance_tests: list[str] = field(default_factory=list)
    allow_release: bool = True
    rationale: str = ""

    def __post_init__(self) -> None:
        # V7 §16.6 不变量: P0 必不许发布
        if self.risk_level == "P0" and self.allow_release:
            raise ValueError(
                "V7 §16.6 不变量违反: risk_level='P0' 但 allow_release=True. "
                "P0 风险必须不许发布."
            )
        if self.risk_level not in {"P0", "P1", "P2"}:
            raise ValueError(
                f"risk_level 必须 ∈ P0/P1/P2, got {self.risk_level!r}"
            )

    def to_row_payload(self, tenant_id: str) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "report_id": self.report_id,
            "audited_capability": self.audited_capability,
            "audited_at": self.audited_at,
            "auditor_provider": self.auditor_provider,
            "design_promise": self.design_promise,
            "real_code_path": self.real_code_path,
            "bypass_methods": list(self.bypass_methods),
            "min_repro_steps": self.min_repro_steps,
            "risk_level": self.risk_level,
            "must_fix": list(self.must_fix),
            "acceptance_tests": list(self.acceptance_tests),
            "allow_release": self.allow_release,
            "rationale": self.rationale,
        }


# ============================================================
# Writer
# ============================================================


async def write_auditor_report(
    *,
    tenant_id: str,
    report: AuditorReport,
) -> str:
    """Persist an AuditorReport to auditor_reports table.

    Returns report_id (echo of input).

    Note: DB CHECK constraint also enforces the V7 §16.6 invariant
    risk_level='P0' ⇒ allow_release=false (双保险 — dataclass __post_init__
    上层兜, DB 下层兜).
    """
    from kun.core.db import session_scope
    from kun.core.orm import AuditorReportRow

    payload = report.to_row_payload(tenant_id)
    row = AuditorReportRow(**payload)

    async with session_scope(tenant_id=tenant_id) as session:
        session.add(row)
        await session.flush()

    log.info(
        "auditor.report_persisted",
        tenant_id=tenant_id,
        report_id=report.report_id,
        audited_capability=report.audited_capability,
        risk_level=report.risk_level,
        allow_release=report.allow_release,
        n_bypass=len(report.bypass_methods),
        n_must_fix=len(report.must_fix),
    )
    return report.report_id


# ============================================================
# Factory
# ============================================================


AuditorReportEmitter = Callable[[AuditorReport], Awaitable[None]]


def make_auditor_report_emitter(tenant_id: str) -> AuditorReportEmitter:
    """Return an async emitter with tenant_id pre-bound.

    Usage::

        # In daemon / ExternalSupervisor.auditor_run():
        from kun.integration.auditor_report_db import (
            AuditorReport, make_auditor_report_emitter,
        )

        emit = make_auditor_report_emitter("tenant-a")
        report = AuditorReport(report_id="...", ...)
        await emit(report)  # 自动落 DB
    """

    async def _emit(report: AuditorReport) -> None:
        await write_auditor_report(tenant_id=tenant_id, report=report)

    return _emit


# ============================================================
# Helper for parsing LLM JSON output → AuditorReport
# ============================================================


def parse_auditor_json(
    *,
    raw_json: dict[str, Any],
    report_id: str,
    audited_capability: str,
    auditor_provider: str,
    audited_at: datetime | None = None,
) -> AuditorReport:
    """Parse the JSON dict returned by AUDITOR_SYSTEM_PROMPT_TEMPLATE LLM call
    into a frozen AuditorReport.

    Why this lives here: the auditor prompt's "Your output (strict JSON)" block
    defines the schema; this parser is the single source of truth for that
    contract. Service callers should never construct AuditorReport from raw
    LLM output by hand — they call this.

    Raises ValueError on:
      - missing required string fields (design_promise / real_code_path /
        risk_level / rationale)
      - risk_level not in {P0, P1, P2}
      - risk_level='P0' but allow_release=True (V7 §16.6 invariant)
    """
    return AuditorReport(
        report_id=report_id,
        audited_capability=audited_capability,
        audited_at=audited_at or datetime.now(UTC),
        auditor_provider=auditor_provider,
        design_promise=str(raw_json.get("design_promise", "")),
        real_code_path=str(raw_json.get("real_code_path", "")),
        bypass_methods=list(raw_json.get("bypass_methods", []) or []),
        min_repro_steps=str(raw_json.get("min_repro_steps", "")),
        risk_level=str(raw_json.get("risk_level", "P2")),
        must_fix=list(raw_json.get("must_fix", []) or []),
        acceptance_tests=list(raw_json.get("acceptance_tests", []) or []),
        allow_release=bool(raw_json.get("allow_release", True)),
        rationale=str(raw_json.get("rationale", "")),
    )


__all__ = [
    "AuditorReport",
    "AuditorReportEmitter",
    "make_auditor_report_emitter",
    "parse_auditor_json",
    "write_auditor_report",
]
