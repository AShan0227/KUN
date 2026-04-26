"""Skill dispatcher — execute a registered skill by id with structured params.

Selector picks candidate skills; the dispatcher actually runs the chosen
one. This is the **execution side** that was missing before — selection
without dispatch left every skill as decoration.

Each builtin skill exposes ``async def execute(params: dict) -> SkillResult``
and is registered at module import. Future skills can register from any
package by importing it.

Sandboxing:
  - shell-exec / python-exec run in a subprocess with a timeout
  - file-io is restricted to KUN_SKILL_FILE_ROOT (default /tmp/kun-skills/)
  - web-search hits a public, rate-limited endpoint with a UA header

Returned ``SkillResult`` is JSON-serializable so it can be fed back into
the LLM as a tool_result message in a future agent loop.
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, Field

from kun.core.logging import get_logger

log = get_logger("kun.skills.dispatcher")


class SkillResult(BaseModel):
    """One skill execution outcome."""

    skill_id: str
    ok: bool
    output: Any = None
    error: str | None = None
    duration_sec: float = 0.0
    metadata: dict[str, Any] = Field(default_factory=dict)


SkillExecutor = Callable[[dict[str, Any]], Awaitable[SkillResult]]


_REGISTRY: dict[str, SkillExecutor] = {}


def register(skill_id: str, executor: SkillExecutor) -> None:
    """Register a skill executor. Re-registration overwrites with a warning."""
    if skill_id in _REGISTRY:
        log.warning("skill.dispatcher.override", skill_id=skill_id)
    _REGISTRY[skill_id] = executor


def is_registered(skill_id: str) -> bool:
    return skill_id in _REGISTRY


def list_registered() -> list[str]:
    return sorted(_REGISTRY)


async def dispatch(skill_id: str, params: dict[str, Any] | None = None) -> SkillResult:
    """Execute a registered skill. Unknown skill returns ok=False."""
    params = params or {}
    executor = _REGISTRY.get(skill_id)
    if executor is None:
        return SkillResult(
            skill_id=skill_id,
            ok=False,
            error=f"skill not registered: {skill_id}",
        )
    try:
        return await executor(params)
    except Exception as e:
        log.exception("skill.dispatch_failed", skill_id=skill_id, error=str(e))
        return SkillResult(
            skill_id=skill_id,
            ok=False,
            error=f"{type(e).__name__}: {e}",
        )


def autoload_builtins() -> None:
    """Import builtin skill modules so their @register calls run.

    同时把 builtin 的 SkillManifest 注册到 SkillRegistry, 让 selector 和
    proactive_dispatch layer 3 能扫到内置 skill (主动用工具 layer 3).
    """
    for mod in (
        "kun.skills.builtin.web_search",
        "kun.skills.builtin.python_exec",
        "kun.skills.builtin.shell_exec",
        "kun.skills.builtin.file_io",
        "kun.skills.builtin.csv_query",
        "kun.skills.builtin.pdf_read",
        "kun.skills.builtin.web_summarize",
        "kun.skills.builtin.pdf_extract",
        "kun.skills.builtin.image_describe",
        "kun.skills.builtin.code_lint",
        "kun.skills.builtin.code_format",
        "kun.skills.builtin.git_diff_review",
        "kun.skills.builtin.sql_query",
        "kun.skills.builtin.csv_analyze",
        "kun.skills.builtin.markdown_to_docx",
        "kun.skills.builtin.markdown_to_pdf",
        "kun.skills.builtin.translate",
        "kun.skills.builtin.regex_explain",
        "kun.skills.builtin.cron_explain",
        "kun.skills.builtin.json_validate",
        "kun.skills.builtin.time_zone_convert",
    ):
        try:
            importlib.import_module(mod)
        except Exception as e:
            log.warning("skill.builtin.import_failed", module=mod, error=str(e))

    # 把 builtin manifest 注册到 SkillRegistry (layer 3 需要)
    try:
        from kun.skills.builtin import autoload_builtin_manifests

        autoload_builtin_manifests()
    except Exception as e:
        log.warning("skill.builtin.manifest_register_failed", error=str(e))


__all__ = [
    "SkillExecutor",
    "SkillResult",
    "autoload_builtins",
    "dispatch",
    "is_registered",
    "list_registered",
    "register",
]
