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

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from kun.api.cockpit_readers import (
    list_recent_auditor_reports,
    list_recent_lifecycle_transitions,
    list_recent_mission_reviews,
)
from kun.governance.capability_lifecycle import CapabilityLifecycleStage

router = APIRouter(prefix="/cockpit", tags=["cockpit"])


# ============================================================
# V7 §16.6 MF-3 — writes_wired_status (no fake-success)
# ============================================================


def _truthy(env_name: str, default: bool = False) -> bool:
    """Env var truthy check shared with the bridge module."""
    raw = os.environ.get(env_name, "").strip().lower()
    if not raw:
        return default
    return raw not in {"false", "0", "no", "off"}


def _writes_wired_status() -> dict[str, dict[str, Any]]:
    """Per-table writes-wired status — honest answer to "does this table真有
    生产 writer?".

    Phase X.B attacker-audit P1: cockpit was returning 真 DB 查询 even when
    no production code emitted to the table. Empty list看起来像success but
    means "wiring broken". This helper exposes the truth.

    Updated as MF-* items land:
      - mission_alignment_reviews: MF-1 wired ✅
      - ensemble_calls: MF-2 wired ✅ (opt-in via env)
      - lifecycle_transitions: still orphan (no MF yet)
      - auditor_reports: still orphan (no MF yet)
    """
    md_bridge_on = _truthy(
        "KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED", default=True
    )
    ensemble_on = _truthy("KUN_V7_ENSEMBLE_ENABLED", default=False)
    ensemble_tiers = os.environ.get("KUN_V7_ENSEMBLE_TIERS", "").strip()

    return {
        "mission_alignment_reviews": {
            "writes_wired": md_bridge_on,
            "writer": "kun.control_plane.MissionDirectorRunner.run() "
            "→ kun.integration.mission_director_v7_bridge (X.B.MF-1)",
            "env_gate": "KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED",
            "env_current_value_truthy": md_bridge_on,
            "warning": (
                None
                if md_bridge_on
                else (
                    "Bridge disabled — mission_alignment_reviews 表不会有新行. "
                    "set KUN_V7_MISSION_DIRECTOR_BRIDGE_ENABLED=true to enable."
                )
            ),
        },
        "ensemble_calls": {
            "writes_wired": ensemble_on
            and len([t for t in ensemble_tiers.split(",") if t.strip()]) >= 2,
            "writer": "kun.engineering.orchestrator._run_long_task_branch "
            "→ kun.integration.ensemble_invoker_factory (X.B.MF-2)",
            "env_gate": (
                "KUN_V7_ENSEMBLE_ENABLED=true AND "
                "KUN_V7_ENSEMBLE_TIERS=<≥2 cross-family tiers>"
            ),
            "env_current_enabled": ensemble_on,
            "env_current_tiers": ensemble_tiers or None,
            "warning": (
                None
                if ensemble_on
                else (
                    "Ensemble disabled — ensemble_calls 表不会有新行. "
                    "Default deployment is single-LLM. set "
                    "KUN_V7_ENSEMBLE_ENABLED=true + KUN_V7_ENSEMBLE_TIERS='top,cheap' to enable."
                )
            ),
        },
        "lifecycle_transitions": {
            "writes_wired": False,
            "writer": None,
            "warning": (
                "ORPHAN: no production code emits to lifecycle_transitions yet. "
                "Phase X.B.LC built the writer; MF-LC-wiring follow-up needed. "
                "Cockpit /capabilities will always show empty until then."
            ),
            "mf_followup_required": "MF-LC-wiring (TBD)",
        },
        "auditor_reports": {
            "writes_wired": False,
            "writer": None,
            "warning": (
                "ORPHAN: no production code emits to auditor_reports yet. "
                "Phase X.B.AR built the writer; auditor hat schedule wiring "
                "(periodic / pre-release / Canary→Production gate) is "
                "MF-AR-wiring follow-up."
            ),
            "mf_followup_required": "MF-AR-wiring (TBD)",
        },
    }


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
async def list_capabilities(
    tenant_id: str = Query("default", description="RLS tenant scope"),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """List recent capability lifecycle transitions (V7 §15).

    Phase X.B: 真从 lifecycle_transitions 表查 (Phase E.A 的 stub 已替换).
    7 层激活证据视图待 Phase E.C (frontend UI) — 现在只返 lifecycle 历史.
    """
    result = await list_recent_lifecycle_transitions(
        tenant_id=tenant_id, limit=limit
    )
    transitions = result.rows
    # Group by capability_id, latest stage per capability
    by_cap: dict[str, dict[str, Any]] = {}
    for t in transitions:
        cap_id = t["capability_id"]
        if cap_id not in by_cap:
            by_cap[cap_id] = {
                "capability_id": cap_id,
                "current_stage": t["to_stage"],
                "latest_transition_at": t["decided_at"],
                "transitions_count": 0,
            }
        by_cap[cap_id]["transitions_count"] += 1
    status = _writes_wired_status()["lifecycle_transitions"]
    return {
        "capabilities": list(by_cap.values()),
        "transitions": transitions,
        "total_capabilities": len(by_cap),
        "total_transitions": len(transitions),
        "tenant_id": tenant_id,
        "data_source": "lifecycle_transitions (V7 §15 9 阶段 lifecycle)",
        "writes_wired_status": status,
        "warning": status["warning"],
        # V7 §16.6 MF-6: surface read-side error_kind (tenant 无数据 vs DB 异常)
        "reader_error_kind": result.error_kind,
        "reader_error_detail": result.error_detail,
        "note": (
            "V7 Phase X.B 真 DB 查询. 完整 7 层激活证据视图 (capability_card "
            "+ runtime_capabilities 关联) 待 Phase E.C frontend UI."
        ),
    }


@router.get("/capabilities/{capability_id}")
async def get_capability(
    capability_id: str,
    tenant_id: str = Query("default", description="RLS tenant scope"),
    limit: int = Query(50, ge=1, le=100),
) -> dict[str, Any]:
    """Get single capability lifecycle history + current stage (V7 §15)."""
    result = await list_recent_lifecycle_transitions(
        tenant_id=tenant_id, capability_id=capability_id, limit=limit
    )
    transitions = result.rows
    if not transitions:
        # MF-6: distinguish "DB unreachable" vs "tenant truly has no data"
        if result.error_kind is not None:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"DB reader failed ({result.error_kind}): "
                    f"{result.error_detail}. Cockpit cannot confirm presence/"
                    f"absence of capability_id={capability_id!r}. Check Postgres "
                    f"connectivity + alembic 0015 applied."
                ),
            )
        raise HTTPException(
            status_code=404,
            detail=(
                f"capability_id={capability_id!r} (tenant={tenant_id!r}) 无 "
                f"lifecycle 记录. V7 §15 还没进 observation 阶段, 或 tenant_id 错."
            ),
        )
    current = transitions[0]  # ordered DESC
    status = _writes_wired_status()["lifecycle_transitions"]
    return {
        "capability_id": capability_id,
        "tenant_id": tenant_id,
        "current_stage": current["to_stage"],
        "current_stage_decided_at": current["decided_at"],
        "transitions": transitions,
        "total_transitions": len(transitions),
        "data_source": "lifecycle_transitions",
        "writes_wired_status": status,
        "warning": status["warning"],
        "reader_error_kind": result.error_kind,
        "reader_error_detail": result.error_detail,
    }


@router.get("/missions/{task_id}/alignment")
async def get_mission_alignment(
    task_id: str,
    tenant_id: str = Query("default", description="RLS tenant scope"),
    limit: int = Query(10, ge=1, le=100),
) -> dict[str, Any]:
    """Mission Director MissionAlignmentReview history for a task (V7 §9.7).

    Phase X.B: 真从 mission_alignment_reviews 表查. DB 不可用时返空 list +
    note 解释 (graceful degradation, log 记错).
    """
    result = await list_recent_mission_reviews(
        tenant_id=tenant_id, task_id=task_id, limit=limit
    )
    reviews = result.rows
    latest = reviews[0] if reviews else None
    status = _writes_wired_status()["mission_alignment_reviews"]
    return {
        "task_id": task_id,
        "tenant_id": tenant_id,
        "reviews": reviews,
        "latest": latest,
        "total": len(reviews),
        "data_source": "mission_alignment_reviews (V7 §9.7 交付总监)",
        "writes_wired_status": status,
        "warning": status["warning"],
        # V7 §16.6 MF-6: distinguish honest empty vs DB error
        "reader_error_kind": result.error_kind,
        "reader_error_detail": result.error_detail,
        "note": (
            "V7 Phase X.B 真 DB 查询. 空 list 可能是: tenant 没数据 / DB 不可用 "
            "(后者 reader_error_kind 非 None)."
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
    """最近 multi-LLM ensemble_invoke 调用 + divergence_score.

    Phase X.B.MF-2 wired the writer (ensemble_calls table真有数据 when env on),
    but a real DB reader for this endpoint is still pending. Status field
    honestly says so instead of returning empty as "success".
    """
    status = _writes_wired_status()["ensemble_calls"]
    return {
        "ensemble_calls": [],
        "total": 0,
        "limit": limit,
        "writes_wired_status": status,
        "warning": status["warning"]
        or "Reader for /ensemble/recent not yet wired — schema in alembic 0017, "
        "writer in X.B.MF-2, reader TBD.",
        "note": (
            "V7 Phase E.A stub for reader (writer wired in X.B.MF-2 — set "
            "KUN_V7_ENSEMBLE_ENABLED=true to write rows; reader endpoint待补)."
        ),
    }


@router.get("/writes-status")
async def get_writes_wired_status() -> dict[str, Any]:
    """Meta endpoint: which Phase X.B tables have真 production writers wired.

    Use this to audit "claim done" vs "真接生产路径" without curl-ing
    every endpoint. Per V7 §16.6 attacker-audit MF-3.
    """
    return {
        "tables": _writes_wired_status(),
        "mf_progress": {
            "MF-1": "V6 MD bridge — done (commit cef3767 + 1819c8d)",
            "MF-2": "LongTaskOrch ensemble wiring — done (commit 271c121)",
            "MF-3": "writes_wired_status — done (this endpoint)",
            "MF-LC-wiring": "lifecycle_transitions writer wiring — TBD",
            "MF-AR-wiring": "auditor_reports writer wiring — TBD",
            "MF-4": "AT-* acceptance tests — TBD",
            "MF-5": "real PG CHECK violation tests — TBD",
            "MF-6": "cockpit_readers error_kind — TBD",
        },
        "v7_doc_ref": "docs/v7/KUN-V7.md §16.6 attacker audit",
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
async def get_auditor_reports(
    tenant_id: str = Query("default", description="RLS tenant scope"),
    audited_capability: str | None = Query(
        None, description="过滤特定 capability"
    ),
    risk_level: str | None = Query(
        None, description="过滤 P0/P1/P2 风险等级", pattern="^P[0-2]$"
    ),
    limit: int = Query(20, ge=1, le=100),
) -> dict[str, Any]:
    """External Supervisor auditor hat 周期报告 (V7 §16.6).

    Phase X.B: 真从 auditor_reports 表查 (alembic 0016 + Phase X.B.AR).
    """
    result = await list_recent_auditor_reports(
        tenant_id=tenant_id,
        audited_capability=audited_capability,
        risk_level=risk_level,
        limit=limit,
    )
    reports = result.rows
    # Aggregate risk distribution
    risk_dist = {"P0": 0, "P1": 0, "P2": 0}
    for r in reports:
        rl = r["risk_level"]
        if rl in risk_dist:
            risk_dist[rl] += 1
    n_block_release = sum(1 for r in reports if not r["allow_release"])
    status = _writes_wired_status()["auditor_reports"]
    return {
        "auditor_reports": reports,
        "total": len(reports),
        "limit": limit,
        "tenant_id": tenant_id,
        "risk_distribution": risk_dist,
        "block_release_count": n_block_release,
        "data_source": "auditor_reports (V7 §16.6 外部监督者 auditor hat)",
        "writes_wired_status": status,
        "warning": status["warning"],
        "reader_error_kind": result.error_kind,
        "reader_error_detail": result.error_detail,
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
