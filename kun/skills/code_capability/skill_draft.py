"""Review-only skill drafts from successful CodeCapability changes."""

from __future__ import annotations

from pathlib import Path

from kun.compiler.internal_assets import compile_skill_markdown_asset
from kun.context.assets import LayeredAsset


def build_code_change_skill_draft_asset(
    *,
    tenant_id: str,
    task_id: str,
    path: str,
    mode: str,
    phase: str,
    checks_passed: bool,
    review_ok: bool | None,
    bytes_changed: int,
    diff_sha256: str,
    reason: str = "",
    strategy_search_records: list[dict[str, object]] | None = None,
) -> LayeredAsset | None:
    """Build a review-only SKILL.md asset from a successful code change.

    这里故意非常保守: 只把“这次代码变更形成了一个可复用做法”写成 draft
    skill 资产，不注册 dispatcher，不自动安装，也不允许生产晋升。
    """

    if not task_id or phase != "done" or not checks_passed or review_ok is False:
        return None
    ext = _extension(path)
    markdown = _skill_markdown(
        task_id=task_id,
        path=path,
        mode=mode,
        ext=ext,
        bytes_changed=bytes_changed,
        diff_sha256=diff_sha256,
        reason=reason,
    )
    asset = compile_skill_markdown_asset(
        markdown,
        tenant_id=tenant_id,
        skill_id=f"code-change-{ext}",
        source_uri=f"code-capability:{task_id}:{diff_sha256 or 'no-diff'}",
    )
    asset.l1_metadata.update(
        {
            "source": {
                "type": "code_capability",
                "uri": f"code-capability:{task_id}:{diff_sha256 or 'no-diff'}",
            },
            "review_state": "draft_review_only",
            "promotion_allowed": False,
            "production_action": False,
            "auto_install_allowed": False,
            "task_id": task_id,
            "path_ext": ext,
            "mode": mode,
            "phase": phase,
            "checks_passed": checks_passed,
            "review_ok": review_ok,
            "bytes_changed": bytes_changed,
            "diff_sha256": diff_sha256,
            "reason": reason,
            "strategy_search_records": strategy_search_records or [],
        }
    )
    asset.tags = _dedupe(
        [
            *asset.tags,
            "code_capability",
            "draft_skill",
            "review_only",
            "no_auto_install",
            f"ext:{ext}",
            f"mode:{mode}",
        ]
    )
    return asset


def _skill_markdown(
    *,
    task_id: str,
    path: str,
    mode: str,
    ext: str,
    bytes_changed: int,
    diff_sha256: str,
    reason: str,
) -> str:
    title = f"Code change pattern for .{ext} files"
    return "\n".join(
        [
            f"# {title}",
            "",
            "Use this only as a review-only draft skill candidate.",
            "",
            "## When to consider",
            f"- Similar code changes touch `*.{ext}` files.",
            "- The task needs a small, testable patch with review before apply.",
            "- A dry-run or explicit apply workflow can run lint/test checks.",
            "",
            "## Safety",
            "- Do not auto-install this skill.",
            "- Do not promote this skill without human or strong-model review.",
            "- Keep all file writes inside the configured CodeCapability workspace.",
            "- Run review before write and run checks after staging.",
            "",
            "## Evidence",
            f"- source_task_id: {task_id}",
            f"- source_path: {path}",
            f"- mode: {mode}",
            f"- bytes_changed: {bytes_changed}",
            f"- diff_sha256: {diff_sha256 or 'n/a'}",
            f"- reason: {reason or 'n/a'}",
        ]
    )


def _extension(path: str) -> str:
    suffix = Path(path).suffix.lstrip(".").lower()
    if not suffix:
        return "none"
    return "".join(ch if ch.isalnum() else "_" for ch in suffix)[:32] or "unknown"


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        cleaned = str(item).strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


__all__ = ["build_code_change_skill_draft_asset"]
