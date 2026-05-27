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

L2.6 实装策略: engineering-first 4 级检查, 各层独立 evidence;
LLM 兜底放 L3+ (本提交不引入). 重复 ≥ 3 次强制从 L0 起检查.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from kun.core.ids import new_id
from kun.core.logging import get_logger
from kun.governance.bug_case_library import (
    BugCase,
    BugCaseReader,
    BugCaseWriter,
    lookup_case,
    record_case,
)
from kun.governance.diagnosis_scope import MAX_SCOPE_MODULES, narrow_scope

log = get_logger("kun.governance.rcdh")

DiagnosticLevel = Literal[0, 1, 2, 3]
"""0: 产品设计 / 1: 功能区激活 / 2: 模块开发 / 3: 代码"""

REPEAT_FORCE_ESCALATION_THRESHOLD = 3
"""同症状重复 N 次 → 强制从 L0 起诊断, 不允许停在 L2/L3."""


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

    # Fast-path fields (alembic 0013, bug_case_library) — 命中案例库时填充
    fast_path: bool = False
    """True 表示这条 record 是案例库命中走捷径产出, 没跑完整 4 级诊断."""
    fix_pattern: str | None = None
    """命中案例库时 carry 上来的修法描述 (案例库 row.fix_pattern)."""
    case_id: str | None = None
    """命中案例库时关联的 BugRootCase.case_id."""
    root_cause_kind: str | None = None
    """命中案例库时 carry 上来的根因 kind (e.g. 'race_condition')."""

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ---- 工程化层级检查 ----

_L0_DESIGN_KEYWORDS = (
    r"\badr\b",
    r"\bspecification\b",
    r"\bcontract\b",
    r"\bpolicy\b",
    r"\bdesign decision\b",
    r"\bmissing requirement\b",
    r"\bambiguous\b",
    r"\bunderspecified\b",
    r"\bschema mismatch\b",
)

_L1_ACTIVATION_KEYWORDS = (
    r"\bfeature flag\b",
    r"\bfeature_flag\b",
    r"\bnot wired\b",
    r"\bnot enabled\b",
    r"\bdisabled\b",
    r"\bcapability not active\b",
    r"\bnot activated\b",
    r"\bno caller\b",
    r"\bdead code\b",
    r"\bnever invoked\b",
)

_L3_STACKTRACE_RE = re.compile(
    r"(?:Traceback|File \"|line \d+|at [a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_])"
)


def _check_level_0_design(
    symptom: str, evidence: list[dict[str, Any]] | None
) -> LevelCheckResult:
    """L0 — 产品设计层: ADR 缺失 / 多模块都受影响 / 契约不一致."""
    text = symptom.lower()
    hits: list[dict[str, Any]] = []

    for pattern in _L0_DESIGN_KEYWORDS:
        m = re.search(pattern, text)
        if m:
            hits.append({"type": "keyword", "keyword": m.group(0)})

    # 跨模块受影响 — evidence 中多个不同 module
    modules_in_evidence = {
        ev.get("module") for ev in (evidence or []) if ev.get("module")
    }
    if len(modules_in_evidence) >= 3:
        hits.append(
            {
                "type": "cross_module_impact",
                "affected_module_count": len(modules_in_evidence),
            }
        )

    return LevelCheckResult(
        is_root_cause=len(hits) > 0,
        evidence=hits,
    )


def _check_level_1_activation(
    symptom: str,
    evidence: list[dict[str, Any]] | None,
    *,
    capability_state: dict[str, bool] | None = None,
) -> LevelCheckResult:
    """L1 — 功能区激活: 关键 capability 没激活 / feature flag 错 / 调用链断."""
    text = symptom.lower()
    hits: list[dict[str, Any]] = []

    for pattern in _L1_ACTIVATION_KEYWORDS:
        m = re.search(pattern, text)
        if m:
            hits.append({"type": "keyword", "keyword": m.group(0)})

    # capability_state 提供时, 检查 evidence 提到的 capability 是否 disabled
    if capability_state:
        for ev in evidence or []:
            cap = ev.get("capability") or ev.get("capability_id")
            if isinstance(cap, str) and capability_state.get(cap) is False:
                hits.append(
                    {
                        "type": "capability_disabled",
                        "capability": cap,
                    }
                )

    return LevelCheckResult(
        is_root_cause=len(hits) > 0,
        evidence=hits,
    )


def _check_level_2_module(
    symptom: str,
    evidence: list[dict[str, Any]] | None,
    *,
    scope_modules: list[str],
) -> LevelCheckResult:
    """L2 — 模块开发: 1-3 个模块明确出现在 evidence + symptom 中."""
    hits: list[dict[str, Any]] = []

    if not scope_modules:
        return LevelCheckResult(is_root_cause=False, evidence=[])

    if 1 <= len(scope_modules) <= 3:
        hits.append(
            {
                "type": "narrow_scope_module_focus",
                "modules": scope_modules,
            }
        )

    # 同一 module 在多条 evidence 中重复出现 → 强信号
    counter: dict[str, int] = {}
    for ev in evidence or []:
        m = ev.get("module")
        if isinstance(m, str):
            counter[m] = counter.get(m, 0) + 1
    for m, count in counter.items():
        if count >= 2:
            hits.append(
                {
                    "type": "module_repeat_in_evidence",
                    "module": m,
                    "occurrences": count,
                }
            )

    return LevelCheckResult(
        is_root_cause=len(hits) > 0,
        evidence=hits,
    )


def _check_level_3_code(
    symptom: str, evidence: list[dict[str, Any]] | None
) -> LevelCheckResult:
    """L3 — 代码层: 有 stack trace / 明确函数行号 / 单点 evidence."""
    hits: list[dict[str, Any]] = []

    if _L3_STACKTRACE_RE.search(symptom):
        hits.append({"type": "stack_trace_present"})

    for ev in evidence or []:
        if ev.get("kind") == "stack_trace":
            hits.append({"type": "evidence_stack_trace"})
        if ev.get("file") and ev.get("line"):
            hits.append(
                {
                    "type": "file_line_localized",
                    "file": ev["file"],
                    "line": ev["line"],
                }
            )

    return LevelCheckResult(
        is_root_cause=len(hits) > 0,
        evidence=hits,
    )


_ACTION_BY_LEVEL: dict[DiagnosticLevel, str] = {
    0: "redesign",
    1: "activate",
    2: "module_rsi",
    3: "code_fix",
}


def _record_from_case_hit(
    case: BugCase,
    *,
    triggered_by_event_id: str,
    symptom: str,
    repeat_history_count: int,
    effective_scope: list[str],
) -> DiagnosticRecord:
    """案例库命中 → 构造 fast-path DiagnosticRecord.

    Level 1 语义 (capability activation 层): 案例库命中等价于"该 trace pattern
    已被识别为已知问题, 直接应用 fix_pattern". 选 L1 + action='activate' 因为
    fast-path 本质是"激活已知修法", 不是 redesign / module_rsi / code_fix 的
    完整诊断结论. fix_pattern + root_cause_kind + case_id 字段供消费方读修法.
    """
    record = DiagnosticRecord(
        diagnostic_id=new_id("diagnostic"),
        triggered_by_event_id=triggered_by_event_id,
        symptom_summary=symptom,
        repeat_history_count=repeat_history_count,
        scope_modules=effective_scope,
        fast_path=True,
        fix_pattern=case.fix_pattern,
        case_id=case.case_id,
        root_cause_kind=case.root_cause_kind,
    )
    # L1 = activation 层 — fast-path 命中等同于"激活已知修法"
    record.root_cause_level = 1
    record.recommended_action = "activate"
    record.level_1_check = LevelCheckResult(
        is_root_cause=True,
        evidence=[
            {
                "type": "bug_case_library_hit",
                "case_id": case.case_id,
                "trace_signature": case.trace_signature,
                "root_cause_kind": case.root_cause_kind,
                "hit_count": case.hit_count,
            }
        ],
    )
    return record


async def run_diagnostic(
    symptom: str,
    *,
    triggered_by_event_id: str,
    repeat_history_count: int = 0,
    scope_modules: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    capability_state: dict[str, bool] | None = None,
    error_type: str | None = None,
    trace_lines: list[str] | None = None,
    tenant_id: str | None = None,
    case_reader: BugCaseReader | None = None,
    case_writer: BugCaseWriter | None = None,
) -> DiagnosticRecord:
    """走 RCDH 4 级诊断 — engineering-first, 不调 LLM (L2.6 范围).

    流程:
      0. 案例库 fast-path (alembic 0013): 若 case_reader + error_type + trace_lines
         都给了 → lookup_case. 命中 → 返 fast_path=True record, 不走 4 级诊断.
      1. 自动 narrow_scope 圈定 ≤5 模块 (若调用方未提供)
      2. 按 0→3 顺序依次检查
      3. 命中即定 root_cause_level + recommended_action
      4. 重复 ≥ REPEAT_FORCE_ESCALATION_THRESHOLD 次同症状强制升:
         若 root_cause 仍落在 L2/L3, 改 root_cause_level=0 + recommended_action="redesign"
      5. 诊断完成 → 若 case_writer 给了且诊断有结论 → record_case 落新条 (best-effort).
    """
    if scope_modules and len(scope_modules) > MAX_SCOPE_MODULES:
        raise ValueError(
            f"scope_modules must be ≤ {MAX_SCOPE_MODULES} (got {len(scope_modules)}). "
            f"Use narrow_scope() to圈定."
        )

    effective_scope = list(scope_modules) if scope_modules else []
    if not effective_scope:
        effective_scope = await narrow_scope(symptom, evidence=evidence)

    # ---- Step 0: Bug case library fast-path ----
    case_lookup_enabled = (
        case_reader is not None
        and error_type is not None
        and trace_lines is not None
        and tenant_id is not None
    )
    if case_lookup_enabled:
        # type narrowing — case_lookup_enabled implies all four not None
        assert error_type is not None
        assert trace_lines is not None
        assert tenant_id is not None

        hit = await lookup_case(
            tenant_id=tenant_id,
            error_type=error_type,
            trace_lines=trace_lines,
            reader=case_reader,
            writer=case_writer,
        )
        if hit is not None:
            fast_record = _record_from_case_hit(
                hit,
                triggered_by_event_id=triggered_by_event_id,
                symptom=symptom,
                repeat_history_count=repeat_history_count,
                effective_scope=effective_scope,
            )
            log.info(
                "rcdh.fast_path_case_hit",
                diagnostic_id=fast_record.diagnostic_id,
                case_id=hit.case_id,
                root_cause_kind=hit.root_cause_kind,
                hit_count=hit.hit_count,
            )
            return fast_record

    # ---- Step 1-4: 走完整 4 级诊断 ----
    record = DiagnosticRecord(
        diagnostic_id=new_id("diagnostic"),
        triggered_by_event_id=triggered_by_event_id,
        symptom_summary=symptom,
        repeat_history_count=repeat_history_count,
        scope_modules=effective_scope,
    )

    # 4 级独立检查 — 都跑, 留全 evidence
    record.level_0_check = _check_level_0_design(symptom, evidence)
    record.level_1_check = _check_level_1_activation(
        symptom, evidence, capability_state=capability_state
    )
    record.level_2_check = _check_level_2_module(
        symptom, evidence, scope_modules=effective_scope
    )
    record.level_3_check = _check_level_3_code(symptom, evidence)

    # 命中规则: 从 L0 升到 L3 取第一个 is_root_cause=True
    for level in (0, 1, 2, 3):
        check = (
            record.level_0_check,
            record.level_1_check,
            record.level_2_check,
            record.level_3_check,
        )[level]
        if check.is_root_cause:
            record.root_cause_level = level  # type: ignore[assignment]
            record.recommended_action = _ACTION_BY_LEVEL[level]  # type: ignore[assignment]
            break

    # 强制升级: 重复 ≥ N 次 + 当前定 L2/L3 → 强制 L0 重审
    if (
        repeat_history_count >= REPEAT_FORCE_ESCALATION_THRESHOLD
        and record.root_cause_level is not None
        and record.root_cause_level >= 2
    ):
        log.warning(
            "rcdh.force_escalation_to_l0",
            diagnostic_id=record.diagnostic_id,
            repeat_count=repeat_history_count,
            original_level=record.root_cause_level,
        )
        record.root_cause_level = 0
        record.recommended_action = "redesign"
        # 在 L0 evidence 补一条"强制升级"标记
        record.level_0_check.evidence.append(
            {
                "type": "force_escalation",
                "repeat_count": repeat_history_count,
                "original_level": record.level_2_check.evidence
                if record.level_2_check.is_root_cause
                else record.level_3_check.evidence,
            }
        )
        record.level_0_check.is_root_cause = True

    log.info(
        "rcdh.diagnostic_done",
        diagnostic_id=record.diagnostic_id,
        root_cause_level=record.root_cause_level,
        recommended_action=record.recommended_action,
        scope_modules=record.scope_modules,
    )

    # ---- Step 5: 诊断有结论 → record_case 落案例库 (best-effort) ----
    if (
        case_lookup_enabled
        and case_writer is not None
        and record.root_cause_level is not None
        and record.recommended_action is not None
    ):
        assert error_type is not None
        assert trace_lines is not None
        assert tenant_id is not None

        try:
            new_case_id = await record_case(
                tenant_id=tenant_id,
                error_type=error_type,
                trace_lines=trace_lines,
                root_cause_kind=record.recommended_action,
                fix_pattern=f"RCDH 诊断: L{record.root_cause_level} → "
                f"action={record.recommended_action}, scope={record.scope_modules}",
                evidence_dx_id=record.diagnostic_id,
                reader=case_reader,
                writer=case_writer,
            )
            record.case_id = new_case_id
            log.info(
                "rcdh.case_recorded",
                diagnostic_id=record.diagnostic_id,
                case_id=new_case_id,
            )
        except Exception as e:
            log.warning(
                "rcdh.case_record_failed",
                diagnostic_id=record.diagnostic_id,
                error=str(e),
            )

    return record


__all__ = [
    "REPEAT_FORCE_ESCALATION_THRESHOLD",
    "DiagnosticLevel",
    "DiagnosticRecord",
    "LevelCheckResult",
    "run_diagnostic",
]
