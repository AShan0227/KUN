"""Agent loop tests — parse / dispatch / formatting."""

from __future__ import annotations

import pytest
from kun.engineering.agent_loop import (
    build_skill_directive,
    format_tool_results,
    parse_skill_calls,
)
from kun.skills.dispatcher import autoload_builtins, register
from kun.skills.dispatcher import dispatch as _dispatch  # noqa: F401 — registers builtins


@pytest.fixture(autouse=True)
def _ensure_builtins() -> None:
    autoload_builtins()


@pytest.mark.unit
def test_parse_skill_calls_finds_one_block() -> None:
    text = """
    需要查一下最新的进度。
    <skill name="web-search">{"query": "kun project"}</skill>
    然后我会基于结果继续。
    """
    calls = parse_skill_calls(text)
    assert len(calls) == 1
    assert calls[0].name == "web-search"
    assert calls[0].params == {"query": "kun project"}


@pytest.mark.unit
def test_parse_skill_calls_skips_unknown_skill() -> None:
    text = '<skill name="this-doesnt-exist">{"x": 1}</skill>'
    assert parse_skill_calls(text) == []


@pytest.mark.unit
def test_parse_skill_calls_handles_multiple_blocks() -> None:
    text = """
    <skill name="python-exec">{"code": "print(1)"}</skill>
    <skill name="shell-exec">{"command": "echo hi"}</skill>
    """
    calls = parse_skill_calls(text)
    assert [c.name for c in calls] == ["python-exec", "shell-exec"]


@pytest.mark.unit
def test_parse_skill_calls_skips_invalid_json() -> None:
    text = '<skill name="python-exec">{ this is not json }</skill>'
    assert parse_skill_calls(text) == []


@pytest.mark.unit
def test_build_skill_directive_lists_skills_with_schema() -> None:
    out = build_skill_directive(
        [
            ("python-exec", "Run Python code", {"code": "string"}),
            ("web-search", "Search the web", {}),
        ]
    )
    assert "python-exec" in out
    assert "web-search" in out
    assert '<skill name="工具名">' in out
    assert '"code": "string"' in out


@pytest.mark.unit
def test_build_skill_directive_empty_when_no_skills() -> None:
    assert build_skill_directive([]) == ""


@pytest.mark.unit
def test_format_tool_results_includes_skill_id_and_status() -> None:
    text = format_tool_results(
        [
            {"skill_id": "python-exec", "ok": True, "output": {"stdout": "42"}},
            {"skill_id": "web-search", "ok": False, "error": "rate limited"},
        ]
    )
    assert "python-exec" in text
    assert "web-search" in text
    assert "rate limited" in text
    assert "42" in text


@pytest.mark.unit
def test_format_tool_results_truncates_long_output() -> None:
    big = {"data": "x" * 5000}
    text = format_tool_results([{"skill_id": "csv-query", "ok": True, "output": big}])
    assert "(truncated)" in text


@pytest.mark.unit
def test_register_dispatch_round_trip() -> None:
    """Manual register works for an externally-defined skill."""
    from kun.skills.dispatcher import SkillResult, is_registered

    async def _fake_skill(params: dict) -> SkillResult:
        return SkillResult(skill_id="fake", ok=True, output=params.get("v"))

    register("test-fake-skill", _fake_skill)
    try:
        assert is_registered("test-fake-skill")
        text = '<skill name="test-fake-skill">{"v": 7}</skill>'
        calls = parse_skill_calls(text)
        assert len(calls) == 1
        assert calls[0].params == {"v": 7}
    finally:
        # Restore registry — direct mutation acceptable for tests
        from kun.skills import dispatcher as d

        d._REGISTRY.pop("test-fake-skill", None)
