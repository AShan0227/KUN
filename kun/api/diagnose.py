"""傩诊断 API (V2.1 §10.6 / M3.2 提前).

3 endpoint:
- POST /api/diagnose/run        — 触发一次诊断 (用户健康检查按钮)
- POST /api/diagnose/confirm    — 用户确认 user_confirm_required 类 fix
- GET  /api/diagnose/audit-log  — 查 fix audit log (用户审计权)
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from kun.api.runtime import get_diagnose_runner
from kun.core.ids import new_id
from kun.core.tenancy import current_tenant
from kun.security.diagnose_runner import DiagnoseRequest, DiagnoseTrigger
from kun.security.fix_handlers import get_fix_audit_log

router = APIRouter()


class DiagnoseRunPayload(BaseModel):
    trigger: DiagnoseTrigger = "user_health_check_button"
    hint_text: str = ""


class DiagnoseConfirmPayload(BaseModel):
    confirm_token: str
    accept: bool = True


@router.post("/run")
async def diagnose_run(
    payload: DiagnoseRunPayload,
    request: Request,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> dict[str, Any]:
    """触发一次诊断."""
    tenant = current_tenant()
    runner = get_diagnose_runner(request.app)
    req = DiagnoseRequest(
        request_id=new_id("diag"),
        trigger=payload.trigger,
        user_id=x_user_id,
        tenant_id=tenant.tenant_id,
        hint_text=payload.hint_text,
    )
    report = await runner.run(req)
    return {
        "request_id": report.request_id,
        "duration_sec": report.duration_sec,
        "findings": [
            {
                "finding_id": f.finding_id,
                "subsystem": f.subsystem,
                "category": f.category,
                "severity": f.severity,
                "description": f.description,
                "root_cause": f.root_cause,
                "cause_method": f.cause_method,
            }
            for f in report.findings
        ],
        "plans": [
            {
                "plan_id": p.plan_id,
                "target_finding_id": p.target_finding_id,
                "fix_kind": p.fix_kind,
                "description": p.description,
                "confirm_token": p.confirm_token,
            }
            for p in report.plans
        ],
        "outcomes": [
            {
                "plan_id": o.plan_id,
                "success": o.success,
                "verified": o.verified,
                "notes": o.notes,
            }
            for o in report.outcomes
        ],
    }


@router.post("/confirm")
async def diagnose_confirm(
    payload: DiagnoseConfirmPayload,
    request: Request,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> dict[str, Any]:
    """用户确认 user_confirm_required 类 fix."""
    runner = get_diagnose_runner(request.app)
    accepted = runner.confirm_user_fix(payload.confirm_token, accept=payload.accept)
    if not accepted and payload.accept:
        raise HTTPException(404, "confirm_token not found or already consumed")
    return {"accepted": accepted, "user_id": x_user_id}


@router.get("/audit-log")
async def diagnose_audit_log(
    request: Request,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
    limit: int = 100,
) -> dict[str, Any]:
    """查 fix audit log (用户审计权 — 看 KUN 自动修了哪些)."""
    log = get_fix_audit_log()
    return {
        "user_id": x_user_id,
        "total": len(log),
        "entries": log[-limit:],
    }
