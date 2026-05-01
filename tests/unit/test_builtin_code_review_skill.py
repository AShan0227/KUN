from __future__ import annotations

import pytest
from kun.skills.dispatcher import autoload_builtins, dispatch, is_registered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_review_builtin_reviews_diff_without_side_effects() -> None:
    autoload_builtins()

    result = await dispatch(
        "code-review",
        {
            "diff": """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,2 @@
+TOKEN = "super-secret-token"
+eval("1+1")
""",
        },
    )

    assert is_registered("code-review") is True
    assert result.skill_id == "code-review"
    assert result.ok is False
    assert result.output["review_only"] is True
    assert result.output["production_action"] is False
    assert result.output["file_written"] is False
    assert result.output["code_executed"] is False
    assert {finding["rule"] for finding in result.output["findings"]} >= {
        "no-eval-exec",
        "no-hardcoded-secret",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_review_builtin_reviews_workspace_file(tmp_path) -> None:
    target = tmp_path / "module.py"
    target.write_text(
        "def run() -> None:\n    try:\n        risky()\n    except Exception:\n        pass\n",
        encoding="utf-8",
    )
    autoload_builtins()

    result = await dispatch(
        "code-review",
        {
            "workspace_root": str(tmp_path),
            "path": "module.py",
        },
    )

    assert result.ok is True
    assert result.metadata["review_only"] is True
    assert result.metadata["production_action"] is False
    assert result.output["findings"][0]["rule"] == "broad-except"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_review_builtin_rejects_ambiguous_input() -> None:
    autoload_builtins()

    result = await dispatch("code-review", {"diff": "+x = 1\n", "path": "app.py"})

    assert result.ok is False
    assert "exactly one" in (result.error or "")
