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
    "web_summarize": {
        "description": "抓取 URL 并抽取正文摘要",
        "auto_trigger_when": ["summarize url", "网页摘要"],
    },
    "pdf_extract": {
        "description": "PDF 文本/表格抽取入口, 复用 pdf-read 执行能力",
        "auto_trigger_when": ["pdf extract", "提取 PDF"],
    },
    "image_describe": {
        "description": "图片文件基础描述和元数据提取",
        "auto_trigger_when": ["describe image", "图片描述"],
    },
    "code_lint": {
        "description": "运行代码 lint, 默认 ruff check",
        "auto_trigger_when": ["lint code", "代码检查"],
    },
    "code_format": {
        "description": "运行代码格式化, 默认 ruff format",
        "auto_trigger_when": ["format code", "格式化代码"],
    },
    "git_diff_review": {
        "description": "分析 git diff 的文件、增删和基础风险提示",
        "auto_trigger_when": ["review diff", "审查 diff"],
    },
    "sql_query": {
        "description": "对 SQLite 文件执行只读 SQL 查询",
        "auto_trigger_when": ["sql query", "查询数据库"],
    },
    "csv_analyze": {
        "description": "CSV 行列统计和数值列摘要",
        "auto_trigger_when": ["analyze csv", "分析 CSV"],
    },
    "markdown_to_docx": {
        "description": "Markdown 转最小 DOCX 文件",
        "auto_trigger_when": ["markdown to docx", "转 docx"],
    },
    "markdown_to_pdf": {
        "description": "Markdown 转最小 PDF 文件",
        "auto_trigger_when": ["markdown to pdf", "转 pdf"],
    },
    "translate": {
        "description": "轻量翻译占位入口, 后续可接 LLM 翻译器",
        "auto_trigger_when": ["translate", "翻译"],
    },
    "regex_explain": {
        "description": "解释正则表达式常见结构",
        "auto_trigger_when": ["explain regex", "解释正则"],
    },
    "cron_explain": {
        "description": "解释 5 字段 cron 表达式",
        "auto_trigger_when": ["explain cron", "解释 cron"],
    },
    "json_validate": {
        "description": "JSON 解析和基础 schema 校验",
        "auto_trigger_when": ["validate json", "校验 JSON"],
    },
    "time_zone_convert": {
        "description": "ISO 时间在不同时区之间转换",
        "auto_trigger_when": ["timezone convert", "时区转换"],
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
