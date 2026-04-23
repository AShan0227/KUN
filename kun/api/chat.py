"""HTTP chat endpoint (non-streaming).

For streaming / interactive use the WebSocket endpoint at /ws (ADR-010).
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from kun.engineering.orchestrator import Orchestrator, TaskResult

router = APIRouter()

_orchestrator = Orchestrator()


class ChatRequest(BaseModel):
    message: str


@router.post("/run", response_model=TaskResult)
async def run_task(req: ChatRequest) -> TaskResult:
    """Run one task end-to-end, blocking until completion."""
    return await _orchestrator.run(req.message)
