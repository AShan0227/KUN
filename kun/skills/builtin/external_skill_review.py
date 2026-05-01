"""external-skill-review builtin skill.

This is the safe second half of external capability discovery: KUN can inspect
offline candidate metadata against a task need, but it still cannot fetch,
install, register, or execute external code from this skill.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from kun.qi.external_skill_review import review_external_skill_candidate
from kun.skills.dispatcher import SkillResult, register


async def execute(params: dict[str, Any]) -> SkillResult:
    start = perf_counter()
    task_need = params.get("task_need")
    candidate = params.get("candidate")
    if task_need is None:
        task_need = {
            key: params[key]
            for key in ("task_type", "summary", "description", "goal", "tags", "topics")
            if key in params
        }
    if not task_need:
        return _result(ok=False, error="provide task_need or task_type/summary", start=start)
    if not isinstance(candidate, dict):
        return _result(ok=False, error="candidate must be an object", start=start)

    package = review_external_skill_candidate(task_need=task_need, candidate=candidate)
    return _result(
        ok=package.status != "blocked",
        output={
            **package.model_dump(mode="json"),
            "message": "只做外部能力安全鉴别；不会联网、安装、注册或执行外部代码。",
        },
        error=None if package.status != "blocked" else "external candidate blocked",
        start=start,
        metadata={
            "review_only": True,
            "auto_fetch_allowed": False,
            "auto_install_allowed": False,
            "production_action": False,
            "promotion_allowed": False,
            "status": package.status,
            "risk_level": package.risk_level,
        },
    )


def _result(
    *,
    ok: bool,
    start: float,
    output: Any = None,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> SkillResult:
    return SkillResult(
        skill_id="external-skill-review",
        ok=ok,
        output=output,
        error=error,
        duration_sec=perf_counter() - start,
        metadata=metadata
        or {
            "review_only": True,
            "auto_fetch_allowed": False,
            "auto_install_allowed": False,
            "production_action": False,
            "promotion_allowed": False,
        },
    )


register("external-skill-review", execute)

__all__ = ["execute"]
