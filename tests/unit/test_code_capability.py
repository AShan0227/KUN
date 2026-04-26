"""CodeCapability C28 tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from kun.skills.code_capability import CodeCapability, CodeExecutor, CodeReader

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
    assert first.writer is None
    assert first.debugger is None
    assert first.reviewer is None


@pytest.mark.unit
def test_code_capability_custom_root() -> None:
    capability = CodeCapability(workspace_root=FIXTURE_ROOT)

    assert capability.reader.root == FIXTURE_ROOT.resolve()
    assert capability.executor.workspace_root == FIXTURE_ROOT.resolve()
