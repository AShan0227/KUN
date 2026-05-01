"""Conservative CodeCapability policy for orchestrator coding tasks.

The policy makes code tasks more likely to use the real CodeCapability skills
without presenting KUN as a fully autonomous coder.  It is prompt/skill wiring
only: real writes remain blocked by the code-propose-change skill unless the
existing environment gate explicitly allows them.
"""

from __future__ import annotations

import re
from typing import Any

from kun.datamodel.task import TaskRef

CODE_REVIEW_SKILL_ID = "code-review"
CODE_PROPOSE_CHANGE_SKILL_ID = "code-propose-change"
CODE_PROPOSE_CHANGE_APPLY_ENV = "KUN_CODE_PROPOSE_CHANGE_SKILL_ALLOW_APPLY"

_CODE_TASK_TYPE_PREFIXES = (
    "code",
    "coding",
    "debug",
    "debugging",
    "refactor",
    "refactoring",
    "test",
    "testing",
)
_CODE_TEXT_MARKERS = (
    "code",
    "coding",
    "programming",
    "bug",
    "debug",
    "refactor",
    "patch",
    "diff",
    "pytest",
    "unit test",
    "integration test",
    "mypy",
    "ruff",
    "eslint",
    "typescript",
    "python",
    "fastapi",
    "react",
    "修代码",
    "修 bug",
    "修bug",
    "调试",
    "重构",
    "补丁",
    "单测",
    "类型检查",
)


def is_code_task(task_ref: TaskRef) -> bool:
    """Return whether a task should receive CodeCapability execution policy."""

    task_type = task_ref.meta.task_type.lower()
    first_segment = task_type.split(".", 1)[0]
    if first_segment in _CODE_TASK_TYPE_PREFIXES:
        return True

    text_parts = [
        task_type,
        task_ref.meta.success_criteria_short,
    ]
    if task_ref.spec is not None:
        text_parts.extend(
            [
                task_ref.spec.goal_detail,
                " ".join(task_ref.spec.success_metrics),
                " ".join(task_ref.spec.required_skills),
                " ".join(task_ref.spec.required_tools),
                " ".join(task_ref.spec.subtasks_hint),
            ]
        )
    text = " ".join(part for part in text_parts if part).lower()
    return any(_contains_marker(text, marker) for marker in _CODE_TEXT_MARKERS)


def code_task_directive(task_ref: TaskRef) -> str:
    """Render the conservative CodeCapability prompt addendum, if applicable."""

    if not is_code_task(task_ref):
        return ""
    return (
        "[CodeCapability coding-task policy]\n"
        "- 这是 coding/debug/refactor/test 类任务时的保守执行规则。\n"
        f"- 优先使用 `{CODE_REVIEW_SKILL_ID}` 做只读审查；它只能 review diff/path, "
        "不会写文件、不会执行代码、不会自动修复。\n"
        f"- 如果确实需要改代码，只能调用 `{CODE_PROPOSE_CHANGE_SKILL_ID}` 提出改动；"
        "默认 dry-run，只返回 diff/验证结果，不会写真实工作区。\n"
        f"- 不要把自己描述成全自动 coder，也不要声称会自动接管仓库。"
        f"真实写入必须同时满足用户明确要求 apply 且环境开关 "
        f"`{CODE_PROPOSE_CHANGE_APPLY_ENV}=1` 已开启；否则必须保持 dry-run。\n"
        "- dry-run 之后给出审查结果、建议补丁和验证建议，由用户决定是否应用。"
    )


def code_capability_skill_summaries() -> list[tuple[str, str, dict[str, Any]]]:
    """Return builtin CodeCapability skill specs for agent-loop directives."""

    return [
        (
            CODE_REVIEW_SKILL_ID,
            (
                "只读代码审查 skill。输入 unified diff 或 workspace 内文件路径，返回 "
                "finding；不写文件、不执行代码、不自动修复。"
            ),
            {
                "type": "object",
                "properties": {
                    "diff": {"type": "string"},
                    "path": {"type": "string"},
                    "workspace_root": {"type": "string"},
                },
            },
        ),
        (
            CODE_PROPOSE_CHANGE_SKILL_ID,
            (
                "受控代码改动提案。默认 dry-run，不写真实工作区；真实写入必须显式开启 "
                f"{CODE_PROPOSE_CHANGE_APPLY_ENV}=1。"
            ),
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "patch_text": {"type": "string"},
                    "replacement_content": {"type": "string"},
                    "allow_apply": {"type": "boolean"},
                    "checks": {"type": "array"},
                },
                "required": ["path"],
            },
        ),
    ]


def merge_code_capability_skill_summaries(
    existing: list[tuple[str, str, dict[str, Any]]],
) -> list[tuple[str, str, dict[str, Any]]]:
    """Append CodeCapability skills if missing, preserving existing order."""

    merged = list(existing)
    seen = {skill_id for skill_id, _, _ in merged}
    for summary in code_capability_skill_summaries():
        if summary[0] not in seen:
            merged.append(summary)
            seen.add(summary[0])
    return merged


def _contains_marker(text: str, marker: str) -> bool:
    if marker.isascii() and re.match(r"^[a-z0-9_ -]+$", marker):
        return re.search(rf"(?<![a-z0-9_-]){re.escape(marker)}(?![a-z0-9_-])", text) is not None
    return marker in text


__all__ = [
    "CODE_PROPOSE_CHANGE_APPLY_ENV",
    "CODE_PROPOSE_CHANGE_SKILL_ID",
    "CODE_REVIEW_SKILL_ID",
    "code_capability_skill_summaries",
    "code_task_directive",
    "is_code_task",
    "merge_code_capability_skill_summaries",
]
