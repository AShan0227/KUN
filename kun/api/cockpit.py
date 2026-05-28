"""V7 §20 任务驾驶舱 API — 最小可行版 (Phase E 起步).

V7 §20 设计目标是完整任务驾驶舱 UI (3-4 周工程量). Phase E 最小可行版只做
JSON API endpoints, 让外部 UI (frontend 后续 session 做) 或 CLI cockpit
(scripts/cockpit_cli.py) 可消费.

提供的 endpoint (V7 §20.2 数据来源):

GET  /cockpit/capabilities                     — 列所有 capability + 当前 lifecycle stage
GET  /cockpit/capabilities/{capability_id}     — 单个 capability 7 层证据 + lifecycle history
GET  /cockpit/missions/{task_id}/alignment     — Mission Director MissionAlignmentReview 最新
GET  /cockpit/missions/{task_id}/rsi-trifecta  — RSI 三线 trifecta 状态 (过去 / 现在 / 未来)
GET  /cockpit/ensemble/recent                  — 最近 ensemble_invoke 调用 + divergence_score
GET  /cockpit/discipline/recent                — 最近 EngineeringDiscipline 检查结果
GET  /cockpit/supervisor/auditor-reports       — External Supervisor auditor hat 周期报告

实装 stage (V7 Phase E):
- Phase E.A (本 commit): API endpoint schema + 占位实现 + frozen IO contract
- Phase E.B (后续): 接真 DB (mission_alignment_reviews / lifecycle_transitions / ...)
- Phase E.C (后续): 接 frontend UI (3-4 周 separate phase)

API 设计原则 (V7 §20.3):
- 中文角色名 + 英文括号: "启 (Qi)" / "傩 (Nuo)" / "外部监督者" 等出现在 response
- 普通用户也能看懂: 不只是工程日志
- 监督结论必须可点击跳转 (response 含 url field)
- "已绑定 / 已使用 / 真实验证" 三态分开 (V7 §16.1 7 层证据驱动)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from kun.governance.capability_lifecycle import CapabilityLifecycleStage

router = APIRouter(prefix="/cockpit", tags=["cockpit"])


# ============================================================
# Response schemas (V7 §13.6 frozen dataclass IO contract)
# ============================================================


@dataclass(frozen=True)
class CapabilityActivationLayer:
    """V7 §16.1 7 层激活证据 of a single capability."""

    capability_id: str
    capability_name: str
    current_layer: int  # 0-6 (V7 §16.1 7 层 0-6)
    layer_name: str  # "方案能力" / "代码存在" / ... / "真实 mission e2e + 攻击测试"
    evidence_refs: list[str]
    is_runtime_enabled: bool  # True only for Layer 4+


@dataclass(frozen=True)
class CapabilityLifecycleState:
    """启 (Qi) capability lifecycle current stage."""

    capability_id: str
    current_stage: CapabilityLifecycleStage
    is_strict_acceptance_stage: bool
    can_promote_next: bool
    next_stage_options: list[str]


@dataclass(frozen=True)
class MissionAlignmentSnapshot:
    """Mission Director (交付总监) MissionAlignmentReview snapshot."""

    task_id: str
    task_plan_version: str
    verdict: str  # ok / drifting / off_anchor / needs_human
    alignment_score: float
    info_gap_coverage: float
    decomposition_coverage: float
    evidence_coverage: float
    findings: list[str]
    reviewed_at: datetime


@dataclass(frozen=True)
class RsiTrifectaStatus:
    """V7 §12.4 RSI 三线并行 trifecta status for a task."""

    task_id: str
    past_line_enabled: bool  # 启 post-hoc retrospect
    past_line_findings_count: int  # bug_root_cause_cases 累积数
    present_line_enabled: bool  # External Supervisor watchdog
    present_line_tick_interval_sec: int | None
    present_line_recent_verdict: str | None  # ok / concerning / alarming
    future_line_enabled: bool  # 启 Explorer Pool 候选并行
    future_line_candidates_count: int  # 3 模式各 1, 共 0-3
    cost_multiplier: float  # 估 1x (off) → 5-6x (full trifecta)


@dataclass(frozen=True)
class EnsembleRecentCall:
    """最近 multi-LLM ensemble_invoke 调用 snapshot."""

    invoke_id: str
    providers: list[str]  # "anthropic/claude-opus" / "openai/gpt-5.5" 等
    consensus_strategy: str
    divergence_score: float
    divergence_signals: list[str]
    invoked_at: datetime
    total_cost_usd: float


@dataclass(frozen=True)
class DisciplineRecentCheck:
    """最近 EngineeringDiscipline 检查结果."""

    check_id: str
    checked_at: datetime
    overall_score: float
    passed_count: int
    failed_count: int
    failed_disciplines: list[str]


@dataclass(frozen=True)
class AuditorReport:
    """External Supervisor auditor hat 周期报告 (V7 §16.6)."""

    report_id: str
    audited_capability: str
    audited_at: datetime
    design_promise: str
    bypass_methods: list[str]
    risk_level: str  # P0 / P1 / P2
    must_fix: list[str]
    allow_release: bool
    rationale: str


# ============================================================
# Endpoints (Phase E.A — stub implementations, return mock data)
# ============================================================


@router.get("/capabilities")
async def list_capabilities() -> dict[str, Any]:
    """List all capabilities with 7 层激活证据 + lifecycle stage.

    Phase E.A: stub data. Phase E.B 接真 DB.
    """
    # Stub — 实际 Phase E.B 应 query runtime_capabilities + capability_card
    return {
        "capabilities": [],
        "total": 0,
        "note": (
            "V7 Phase E.A stub. Phase E.B 接 runtime_capabilities DB. "
            "Phase E.C 接 frontend UI (3-4 周 separate session)."
        ),
    }


@router.get("/capabilities/{capability_id}")
async def get_capability(capability_id: str) -> dict[str, Any]:
    """Get single capability 7 层证据 + lifecycle history."""
    raise HTTPException(
        status_code=501,
        detail=(
            f"V7 Phase E.A stub for capability_id={capability_id}. "
            f"Phase E.B 接 capability_card + lifecycle_transitions DB."
        ),
    )


@router.get("/missions/{task_id}/alignment")
async def get_mission_alignment(task_id: str) -> dict[str, Any]:
    """Mission Director MissionAlignmentReview latest snapshot for a task."""
    return {
        "task_id": task_id,
        "status": "not_yet_implemented",
        "note": (
            "V7 Phase E.A stub. Phase E.B 接 mission_alignment_reviews DB. "
            "Mission Director 一级子系统 (V7 §9.7) 由 Phase B daemon 注册."
        ),
    }


@router.get("/missions/{task_id}/rsi-trifecta")
async def get_rsi_trifecta_status(task_id: str) -> dict[str, Any]:
    """V7 §12.4 RSI 三线并行 trifecta status."""
    return {
        "task_id": task_id,
        "trifecta": {
            "past_line": {
                "enabled": False,
                "findings_count": 0,
                "note": "启 (Qi) post-hoc retrospect, bug_root_cause_cases 库",
            },
            "present_line": {
                "enabled": False,
                "tick_interval_sec": None,
                "note": "外部监督者 watchdog hat (V7 §10.2.3)",
            },
            "future_line": {
                "enabled": False,
                "candidates_count": 0,
                "note": "启 (Qi) Explorer Pool 3 模式并行 (V7 §9.6)",
            },
        },
        "cost_multiplier_estimate": 1.0,
        "note": "V7 Phase E.A stub. Phase E.B 接真 runtime 状态.",
    }


@router.get("/ensemble/recent")
async def get_recent_ensemble_calls(limit: int = 20) -> dict[str, Any]:
    """最近 multi-LLM ensemble_invoke 调用 + divergence_score."""
    return {
        "ensemble_calls": [],
        "total": 0,
        "limit": limit,
        "note": "V7 Phase E.A stub. Phase E.B 接 LLMRouter ensemble_invoke 日志.",
    }


@router.get("/discipline/recent")
async def get_recent_discipline_checks(limit: int = 20) -> dict[str, Any]:
    """最近 EngineeringDiscipline 检查结果."""
    return {
        "discipline_checks": [],
        "total": 0,
        "limit": limit,
        "note": "V7 Phase E.A stub. Phase E.B 接 Claude Code 工程纪律 enforcer 日志.",
    }


@router.get("/supervisor/auditor-reports")
async def get_auditor_reports(limit: int = 20) -> dict[str, Any]:
    """External Supervisor auditor hat 周期报告 (V7 §16.6)."""
    return {
        "auditor_reports": [],
        "total": 0,
        "limit": limit,
        "note": "V7 Phase E.A stub. Phase E.B 接 auditor_reports DB (待 alembic migration).",
    }


@router.get("/health")
async def cockpit_health() -> dict[str, Any]:
    """V7 §20 cockpit API health check + 现状声明."""
    return {
        "status": "ok",
        "phase": "V7 Phase E.A (minimum viable)",
        "ui_status_warning": (
            "V7 §20 ⚠️ 现状: 当前 KUN 任务驾驶舱 UI 几乎不存在. "
            "本 API 是 Phase E 起步, 提供 JSON endpoints. "
            "Frontend UI 是 Phase E.C, 3-4 周 separate session."
        ),
        "endpoints_count": 7,
        "v7_doc_ref": "docs/v7/KUN-V7.md §20",
    }


__all__ = [
    "AuditorReport",
    "CapabilityActivationLayer",
    "CapabilityLifecycleState",
    "DisciplineRecentCheck",
    "EnsembleRecentCall",
    "MissionAlignmentSnapshot",
    "RsiTrifectaStatus",
    "router",
]
