from __future__ import annotations

import pytest
from kun.skills.dispatcher import autoload_builtins, dispatch, is_registered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_propose_change_builtin_dry_runs_without_writing(tmp_path) -> None:
    autoload_builtins()
    target = tmp_path / "demo.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")

    result = await dispatch(
        "code-propose-change",
        {
            "workspace_root": str(tmp_path),
            "path": "demo.py",
            "replacement_content": "VALUE = 2\n",
        },
    )

    assert is_registered("code-propose-change") is True
    assert result.ok is True
    assert result.metadata["review_only"] is True
    assert result.metadata["file_written"] is False
    assert result.metadata["production_action"] is False
    assert result.output["mode"] == "dry_run"
    assert result.output["applied"] is False
    assert "VALUE = 2" in result.output["diff"]
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_propose_change_builtin_rejects_apply_without_env(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    autoload_builtins()
    monkeypatch.delenv("KUN_CODE_PROPOSE_CHANGE_SKILL_ALLOW_APPLY", raising=False)
    target = tmp_path / "demo.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")

    result = await dispatch(
        "code-propose-change",
        {
            "workspace_root": str(tmp_path),
            "path": "demo.py",
            "replacement_content": "VALUE = 2\n",
            "allow_apply": True,
        },
    )

    assert result.ok is False
    assert "refuses real writes" in (result.error or "")
    assert result.metadata["apply_requested"] is True
    assert result.metadata["apply_allowed"] is False
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
