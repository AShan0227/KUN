"""Builtin executable skills.

Importing this package registers the six bundled skills with the dispatcher.
Modules are imported lazily by ``dispatcher.autoload_builtins()`` so callers
that don't want skills can skip the cost.

主动用工具 layer 3 — 给 builtin 也注册 SkillManifest 到 SkillRegistry.
selector 和 proactive_dispatch (layer 3 扫描) 都靠 SkillRegistry 取信息;
没有这一步, builtin 在 layer 3 是隐形的, 只能靠 layer 1/2 触发.

每个 builtin 的 auto_trigger_when 通常留空 — 它们已经被 layer 1/2 yaml 触发器
覆盖. 写在这里只是为了"声明意图", 同时让用户开发的 starter pack SKILL.md
能把同一个 builtin 当 auto-trigger 目标 (复用 builtin 执行能力).
"""

from __future__ import annotations

from typing import Any

# skill_id → manifest 字段. autoload_builtin_manifests() 会把它们注册到
# SkillRegistry. 字段跟 SkillManifest 一致.
BUILTIN_MANIFESTS: dict[str, dict[str, Any]] = {
    "web-search": {
        "description": "联网搜索关键字, 返回 top-k 结果 (title/url/snippet)",
        "auto_trigger_when": [],
    },
    "python-exec": {
        "description": "在沙箱里执行 Python 代码, 返回 stdout/stderr/exit_code",
        "auto_trigger_when": [],
    },
    "shell-exec": {
        "description": "在沙箱里执行 shell 命令, 受 allowlist 约束",
        "auto_trigger_when": [],
    },
    "file-io": {
        "description": "读写沙箱内文件 (KUN_SKILL_FILE_ROOT 限定)",
        "auto_trigger_when": [],
    },
    "csv-query": {
        "description": "用 DuckDB 跑 SQL 查 CSV 文件",
        "auto_trigger_when": [],
    },
    "pdf-read": {
        "description": "读 PDF 文件并抽取文本内容",
        "auto_trigger_when": [],
    },
}


def autoload_builtin_manifests() -> None:
    """把 BUILTIN_MANIFESTS 注册到 SkillRegistry, 让 selector / layer 3 能扫到."""
    # 延迟 import 避免循环
    from kun.skills.loader import SkillManifest, SkillRecord, get_registry

    reg = get_registry()
    for skill_id, fields in BUILTIN_MANIFESTS.items():
        if reg.get(skill_id) is not None:
            continue
        manifest = SkillManifest(
            name=skill_id,
            description=fields.get("description", skill_id),
            auto_trigger_when=list(fields.get("auto_trigger_when") or []),
        )
        record = SkillRecord(
            skill_id=skill_id,
            manifest=manifest,
            body_md="",
            spdx_license=None,
            source_path=f"<builtin:{skill_id}>",
        )
        reg.register(record)


__all__ = ["BUILTIN_MANIFESTS", "autoload_builtin_manifests"]
