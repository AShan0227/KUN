"""HTTP chat endpoint (non-streaming).

For streaming / interactive use the WebSocket endpoint at /ws (ADR-010).

V2.1 wire (§17.4a 速度铁律): 所有任务先走 FastPath pre-check, 命中直接出
结果, 不走完整决策链. Token 使用记到 TokenMeter (§5.2.1 / T46).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Request
from pydantic import BaseModel, Field

from kun.api.input_payload import Attachment, translate_chat_input
from kun.api.runtime import get_fast_path, get_orchestrator, get_token_meter
from kun.core.tenancy import current_tenant
from kun.engineering.orchestrator import TaskResult

router = APIRouter()


class ChatRequest(BaseModel):
    message: str
    attachments: list[Attachment] = Field(default_factory=list)


class FastPathResult(BaseModel):
    """快速路径直接出的结果 (跳过完整决策链)."""

    fast_path: bool = True
    hit: str
    reason: str
    decided_in_ms: int
    payload: dict[str, Any]


@router.post("/run")
async def run_task(
    req: ChatRequest,
    request: Request,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
    output_kind: str = Query(default="user"),
) -> TaskResult | FastPathResult:
    """Run one task end-to-end.

    V2.1 §17.4a: 先走 FastPath pre-check.
    - 命中 → 直接返 FastPathResult (≤200ms)
    - 未命中 → 走完整 orchestrator 决策链
    """
    translated = await translate_chat_input(req.message, req.attachments)
    user_message = translated.message
    fast = get_fast_path(request.app)
    tenant = current_tenant()
    tenant_id = tenant.tenant_id
    user_id = x_user_id or tenant.user_id or tenant_id

    # V2.1 §17.4a 快速路径
    fp_decision = fast.try_fast(
        task_meta={
            "user_message": user_message,
            "task_type": "chat.unstructured",
            "input_descriptors": translated.descriptors,
        },
        user_meta={
            "user_id": user_id,
            "tenant_id": tenant_id,
        },
    )
    if fp_decision.is_fast and fp_decision.response_payload:
        return FastPathResult(
            fast_path=True,
            hit=fp_decision.hit or "unknown",
            reason=fp_decision.reason,
            decided_in_ms=fp_decision.decided_in_ms,
            payload=fp_decision.response_payload,
        )

    # 完整决策链
    orchestrator = get_orchestrator(request.app)
    result = await orchestrator.run(user_message, output_kind=output_kind)

    # V2.1 T46: 记 token 用量
    if user_id != "u-anon":
        meter = get_token_meter(request.app)
        rs = getattr(result, "runtime_state", None)
        tokens_used = int(getattr(rs, "accumulated_tokens", 0) or 0)
        if tokens_used > 0:
            meter.record_usage(
                user_id=user_id,
                task_id=result.task_id,
                tokens_used=tokens_used,
            )
    return result


@router.get("/usage")
def get_usage_dashboard(
    request: Request,
    x_user_id: Annotated[str, Header(alias="X-User-Id")],
) -> dict[str, Any]:
    """V2.1 T46: token 实时仪表盘 (NUO 第 1 层置顶)."""
    meter = get_token_meter(request.app)
    return meter.get_dashboard(x_user_id)
