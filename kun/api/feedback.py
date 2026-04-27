"""V2.3 Wire 48 — 用户反馈 API (V2.3 §8.2 / L2).

L2 缺口: KUN 现在主要靠 machine-judge. 用户 👍/👎 没强 wire 到决策.
V2.3: POST /api/tasks/{task_id}/feedback → emit user.feedback.received event.

启窗口内拉 feedback events → 调整 protocol reward_weights.
跟 Predictive Coding 联动: 用户反馈也算一种 actual.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Path
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()


class UserFeedbackRequest(BaseModel):
    """POST /api/tasks/{task_id}/feedback 请求 body."""

    rating: int = Field(ge=1, le=5, description="1-5 stars")
    comment: str = Field(default="", max_length=2000)
    tags: list[str] = Field(
        default_factory=list,
        description="e.g. ['inaccurate', 'too_slow', 'wrong_skill']",
    )


class UserFeedbackResponse(BaseModel):
    """反馈接收响应."""

    received: bool = True
    task_id: str
    rating: int


@router.post("/api/tasks/{task_id}/feedback", response_model=UserFeedbackResponse)
async def submit_feedback(
    task_id: Annotated[str, Path(min_length=1)],
    payload: UserFeedbackRequest,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> UserFeedbackResponse:
    """用户提交 task 反馈.

    最简实装: emit user.feedback.received event 进 events bus.
    后续启窗口拉 events → 影响 protocol reward_weights.

    err: 422 if rating out of range; 401 if no X-User-Id.
    """
    if not x_user_id:
        raise HTTPException(status_code=401, detail="X-User-Id header required")

    # Best-effort emit event
    try:
        from kun.core.db import session_scope
        from kun.core.events import emit
        from kun.core.tenancy import current_tenant
        from kun.datamodel.events import Event

        tenant = current_tenant()
        async with session_scope(tenant_id=tenant.tenant_id) as s:
            await emit(
                s,
                Event.build(
                    tenant_id=tenant.tenant_id,
                    event_type="user.feedback",
                    payload={
                        "task_id": task_id,
                        "user_id": x_user_id,
                        "rating": payload.rating,
                        "comment": payload.comment[:500],
                        "tags": payload.tags[:10],
                    },
                    task_ref=task_id,
                ),
            )
        logger.info(
            "user.feedback received task=%s user=%s rating=%d tags=%s",
            task_id,
            x_user_id,
            payload.rating,
            payload.tags,
        )
    except Exception as e:
        # 不阻塞用户 — 即使 events bus 挂了, 用户应该收到 200
        logger.exception("user.feedback emit failed task=%s err=%s", task_id, e)

    return UserFeedbackResponse(received=True, task_id=task_id, rating=payload.rating)


__all__ = ["UserFeedbackRequest", "UserFeedbackResponse", "router", "submit_feedback"]
