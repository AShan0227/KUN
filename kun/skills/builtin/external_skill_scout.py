"""external-skill-scout builtin skill.

This is KUN's safe "go look for outside capability" hook.  It does not fetch,
install, register, or execute external code.  It only turns a task need into a
review-only scout plan that Qi / NUO / a human can inspect.
"""

from __future__ import annotations

from time import perf_counter
from typing import Any

from kun.qi.external_skill_review import build_external_skill_candidate_source_plan
from kun.skills.dispatcher import SkillResult, register


async def execute(params: dict[str, Any]) -> SkillResult:
    start = perf_counter()
    task_need = params.get("task_need")
    if task_need is None:
        task_need = {
            key: params[key]
            for key in ("task_type", "summary", "description", "goal", "tags", "topics")
            if key in params
        }
    if task_need is None or task_need == {}:
        return SkillResult(
            skill_id="external-skill-scout",
            ok=False,
            error="provide task_need or task_type/summary/description",
            duration_sec=perf_counter() - start,
            metadata=_safe_metadata(),
        )

    plan = build_external_skill_candidate_source_plan(
        task_need,
        source_registry=params.get("source_registry") or params.get("candidate_sources"),
        candidates=params.get("candidates") or params.get("candidate_metadata"),
    )
    return SkillResult(
        skill_id="external-skill-scout",
        ok=True,
        output={
            **plan.model_dump(mode="json"),
            "message": "只生成外部能力来源/候选审查计划；不会抓取、安装或注册生产 skill。",
        },
        duration_sec=perf_counter() - start,
        metadata=_safe_metadata(),
    )


def _safe_metadata() -> dict[str, Any]:
    return {
        "review_only": True,
        "offline_only": True,
        "auto_fetch_allowed": False,
        "auto_install_allowed": False,
        "production_action": False,
        "promotion_allowed": False,
    }


register("external-skill-scout", execute)

__all__ = ["execute"]
