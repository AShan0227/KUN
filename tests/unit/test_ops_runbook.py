from __future__ import annotations

from pathlib import Path

import pytest


@pytest.mark.unit
def test_ops_runbook_uses_current_safe_env_names() -> None:
    text = Path("docs/ops/runbook.md").read_text(encoding="utf-8")

    assert "KUN_PG_DSN=" in text
    assert "KUN_PG_ADMIN_DSN=" in text
    assert "DATABASE_URL=" not in text
    assert "KUN_NATS_URL=" in text
    assert "KUN_S3_ENDPOINT=" in text
    assert "KUN_S3_ACCESS_KEY=" in text
    assert "KUN_S3_SECRET_KEY=" in text
    assert "please-use-32+-chars" not in text
    assert "cron 跑 7 个 step" not in text
