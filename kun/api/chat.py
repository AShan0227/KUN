"""HTTP chat endpoint (non-streaming).

For streaming / interactive use the WebSocket endpoint at /ws (ADR-010).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from kun.api.runtime import get_orchestrator
from kun.engineering.orchestrator import TaskResult

router = APIRouter()


class ChatRequest(BaseModel):
    message: str


@router.post("/run", response_model=TaskResult)
async def run_task(
    req: ChatRequest,
    request: Request,
    output_kind: str = Query(default="user"),
) -> TaskResult:
    """Run one task end-to-end, blocking until completion."""
    return await get_orchestrator(request.app).run(req.message, output_kind=output_kind)
