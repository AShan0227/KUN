from __future__ import annotations

from kun.skills.code_capability.skill_draft import build_code_change_skill_draft_asset


def test_code_change_skill_draft_is_review_only_context_asset() -> None:
    asset = build_code_change_skill_draft_asset(
        tenant_id="tenant-code",
        task_id="task-1",
        path="kun/foo.py",
        mode="dry_run",
        phase="done",
        checks_passed=True,
        review_ok=True,
        bytes_changed=128,
        diff_sha256="abc123",
        reason="提炼可复用代码修改路径",
    )

    assert asset is not None
    assert asset.asset_kind == "skill"
    assert asset.tenant_id == "tenant-code"
    assert asset.l1_metadata["review_state"] == "draft_review_only"
    assert asset.l1_metadata["production_action"] is False
    assert asset.l1_metadata["auto_install_allowed"] is False
    assert asset.l1_metadata["task_id"] == "task-1"
    assert asset.l1_metadata["path_ext"] == "py"
    assert "draft_skill" in asset.tags
    assert "review_only" in asset.tags
    assert "no_auto_install" in asset.tags
    assert "Code change pattern" in (asset.l2_summary or "")


def test_code_change_skill_draft_skips_failed_or_unchecked_changes() -> None:
    failed = build_code_change_skill_draft_asset(
        tenant_id="tenant-code",
        task_id="task-1",
        path="kun/foo.py",
        mode="dry_run",
        phase="review",
        checks_passed=True,
        review_ok=False,
        bytes_changed=128,
        diff_sha256="abc123",
    )
    unchecked = build_code_change_skill_draft_asset(
        tenant_id="tenant-code",
        task_id="task-1",
        path="kun/foo.py",
        mode="dry_run",
        phase="done",
        checks_passed=False,
        review_ok=True,
        bytes_changed=128,
        diff_sha256="abc123",
    )

    assert failed is None
    assert unchecked is None
