"""CodeCapability C28 tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from kun.skills.code_capability import (
    CodeCapability,
    CodeDebugger,
    CodeExecutor,
    CodeReader,
    CodeReviewer,
    CodeWriter,
)
from kun.skills.code_capability.writer import TextReplacement

FIXTURE_ROOT = Path("tests/fixtures/code_samples")


async def _collect(async_iter) -> list[str]:
    items: list[str] = []
    async for item in async_iter:
        items.append(item)
    return items


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_reader_find_anchor_file_by_symbol() -> None:
    reader = CodeReader(root=FIXTURE_ROOT)

    anchor = await reader.find_anchor_file("Calculator compute")

    assert anchor == "minimal_module.py"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_reader_get_dependencies_resolves_local_imports() -> None:
    reader = CodeReader(root=FIXTURE_ROOT)

    deps = await reader.get_dependencies("minimal_module.py")

    assert deps == ["helper.py"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_reader_get_callers_returns_file_line_snippets() -> None:
    reader = CodeReader(root=FIXTURE_ROOT)

    callers = await reader.get_callers("add_one")

    assert any(item.startswith("minimal_module.py:") for item in callers)
    assert any(item.startswith("test_minimal_module.py:") for item in callers)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_reader_explain_uses_mock_explainer() -> None:
    async def fake_explainer(path: str, content: str) -> str:
        return f"explained {path} with {len(content.splitlines())} lines"

    reader = CodeReader(root=FIXTURE_ROOT, explainer=fake_explainer)

    explanation = await reader.explain("minimal_module.py", lines=(1, 4))

    assert explanation == "explained minimal_module.py with 3 lines"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_reader_read_anchor_then_expand_yields_dependency_neighbor() -> None:
    reader = CodeReader(root=FIXTURE_ROOT)

    items = await _collect(reader.read_anchor_then_expand("Calculator", max_rounds=2))

    assert items == ["minimal_module.py", "helper.py"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_executor_execute_python_happy_path(tmp_path: Path) -> None:
    executor = CodeExecutor(workspace_root=tmp_path)

    result = await executor.execute_python("print('hello from code')", timeout_sec=5)

    assert result.ok is True
    assert result.returncode == 0
    assert "hello from code" in result.stdout
    assert result.sandbox["cwd_restricted"] is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_executor_execute_python_timeout(tmp_path: Path) -> None:
    executor = CodeExecutor(workspace_root=tmp_path)

    result = await executor.execute_python("while True: pass", timeout_sec=1)

    assert result.ok is False
    assert result.timed_out is True
    assert "timed out" in result.stderr


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_executor_execute_lint_pass(tmp_path: Path) -> None:
    target = tmp_path / "ok.py"
    target.write_text("def ok() -> int:\n    return 1\n", encoding="utf-8")
    executor = CodeExecutor(workspace_root=tmp_path)

    result = await executor.execute_lint(target, tool="ruff")

    assert result.ok is True
    assert result.issues == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_executor_execute_lint_fail(tmp_path: Path) -> None:
    target = tmp_path / "bad.py"
    target.write_text("import os\n\n\ndef bad():\n    return 1\n", encoding="utf-8")
    executor = CodeExecutor(workspace_root=tmp_path)

    result = await executor.execute_lint(target, tool="ruff")

    assert result.ok is False
    assert "F401" in result.output


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_executor_execute_test_pass_and_fail(tmp_path: Path) -> None:
    shutil.copytree(FIXTURE_ROOT, tmp_path, dirs_exist_ok=True)
    executor = CodeExecutor(workspace_root=tmp_path)

    passing = await executor.execute_test("test_minimal_module.py")
    failing = await executor.execute_test("failing_test.py")

    assert passing.ok is True
    assert passing.passed >= 1
    assert failing.ok is False
    assert failing.failed == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_executor_rejects_cwd_escape(tmp_path: Path) -> None:
    executor = CodeExecutor(workspace_root=tmp_path)

    with pytest.raises(ValueError, match="escapes code workspace"):
        await executor.execute_python("print('nope')", cwd=tmp_path.parent)


@pytest.mark.unit
def test_code_capability_singleton_and_placeholders() -> None:
    CodeCapability.reset()

    first = CodeCapability.get()
    second = CodeCapability.get()

    assert first is second
    assert isinstance(first.reader, CodeReader)
    assert isinstance(first.executor, CodeExecutor)
    assert isinstance(first.writer, CodeWriter)
    assert isinstance(first.debugger, CodeDebugger)
    assert isinstance(first.reviewer, CodeReviewer)


@pytest.mark.unit
def test_code_capability_custom_root() -> None:
    capability = CodeCapability(workspace_root=FIXTURE_ROOT)

    assert capability.reader.root == FIXTURE_ROOT.resolve()
    assert capability.executor.workspace_root == FIXTURE_ROOT.resolve()
    assert capability.writer.workspace_root == FIXTURE_ROOT.resolve()
    assert capability.reviewer.workspace_root == FIXTURE_ROOT.resolve()


@pytest.mark.unit
def test_code_writer_write_file_and_reject_escape(tmp_path: Path) -> None:
    writer = CodeWriter(workspace_root=tmp_path)

    result = writer.write_file("pkg/module.py", "VALUE = 1\n", create_dirs=True)

    assert result.ok is True
    assert result.created is True
    assert (tmp_path / "pkg/module.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    with pytest.raises(ValueError, match="escapes code workspace"):
        writer.write_file(tmp_path.parent / "escape.py", "bad")


@pytest.mark.unit
def test_code_writer_apply_replacements(tmp_path: Path) -> None:
    target = tmp_path / "module.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    writer = CodeWriter(workspace_root=tmp_path)

    result = writer.apply_replacements("module.py", [TextReplacement("VALUE = 1", "VALUE = 2")])

    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "VALUE = 2\n"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_code_writer_write_and_check_lint(tmp_path: Path) -> None:
    writer = CodeWriter(workspace_root=tmp_path)

    result = await writer.write_and_check(
        "ok.py",
        "def ok() -> int:\n    return 1\n",
        create_dirs=True,
        lint_tools=("ruff",),
    )

    assert result.ok is True
    assert len(result.lint_results) == 1
    assert result.lint_results[0].ok is True


@pytest.mark.unit
def test_code_debugger_classifies_syntax_and_timeout() -> None:
    debugger = CodeDebugger()

    syntax = debugger.analyze_failure(error='  File "bad.py", line 3\nSyntaxError: invalid')
    timeout = debugger.analyze_failure(error="timed out after 1s", timed_out=True)

    assert syntax.category == "syntax_error"
    assert syntax.path == "bad.py"
    assert syntax.line == 3
    assert timeout.category == "timeout"


@pytest.mark.unit
def test_code_debugger_classifies_assertion_and_lint() -> None:
    debugger = CodeDebugger()

    assertion = debugger.analyze_failure(output="E   AssertionError: expected 1")
    lint = debugger.analyze_failure(output="bad.py:1:1: F401 imported but unused")

    assert assertion.category == "assertion_failure"
    assert lint.category == "lint_error"
    assert lint.line == 1


@pytest.mark.unit
def test_code_reviewer_flags_dangerous_diff() -> None:
    reviewer = CodeReviewer(workspace_root=FIXTURE_ROOT)
    diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,4 @@
+import subprocess
+subprocess.run("rm -rf /", shell=True)
+token = "abcd1234SECRET"
+eval("1+1")
"""

    result = reviewer.review_diff(diff)

    assert result.ok is False
    assert {finding.rule for finding in result.findings} >= {
        "no-shell-true",
        "no-hardcoded-secret",
        "no-eval-exec",
    }


@pytest.mark.unit
def test_code_reviewer_review_file_and_reject_escape(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("except Exception:\n    print('recover')\n", encoding="utf-8")
    reviewer = CodeReviewer(workspace_root=tmp_path)

    result = reviewer.review_file("app.py")

    assert result.ok is True
    assert result.findings[0].rule == "broad-except"
    with pytest.raises(ValueError, match="escapes code workspace"):
        reviewer.review_file(tmp_path.parent / "escape.py")
