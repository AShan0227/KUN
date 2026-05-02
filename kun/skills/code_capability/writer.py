"""CodeWriter — write code under a workspace and run check loops."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kun.skills.code_capability.executor import CodeExecutor, LintResult, LintTool, TestResult


@dataclass(frozen=True)
class TextReplacement:
    old: str
    new: str
    count: int = 1


@dataclass(frozen=True)
class WriteResult:
    ok: bool
    path: str
    bytes_written: int = 0
    created: bool = False
    error: str = ""
    lint_results: list[LintResult] = field(default_factory=list)
    test_results: list[TestResult] = field(default_factory=list)


class CodeWriter:
    """Safe workspace-local file writer.

    The writer intentionally refuses path escapes and can optionally run lint
    and tests after writing. It does not call an LLM; higher layers decide what
    content to write.
    """

    def __init__(
        self,
        *,
        workspace_root: str | Path = ".",
        executor: CodeExecutor | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.executor = executor or CodeExecutor(workspace_root=self.workspace_root)

    def write_file(
        self,
        path: str | Path,
        content: str,
        *,
        create_dirs: bool = False,
    ) -> WriteResult:
        resolved = self._resolve_path(path, must_exist=False)
        created = not resolved.exists()
        if create_dirs:
            resolved.parent.mkdir(parents=True, exist_ok=True)
        elif not resolved.parent.exists():
            return WriteResult(
                ok=False,
                path=_rel(resolved, self.workspace_root),
                error=f"parent directory does not exist: {resolved.parent}",
            )

        resolved.write_text(content, encoding="utf-8")
        return WriteResult(
            ok=True,
            path=_rel(resolved, self.workspace_root),
            bytes_written=len(content.encode("utf-8")),
            created=created,
        )

    def apply_replacements(
        self,
        path: str | Path,
        replacements: list[TextReplacement],
    ) -> WriteResult:
        resolved = self._resolve_path(path)
        original = resolved.read_text(encoding="utf-8")
        updated = original
        for replacement in replacements:
            if replacement.old not in updated:
                return WriteResult(
                    ok=False,
                    path=_rel(resolved, self.workspace_root),
                    error=f"replacement text not found: {replacement.old[:80]}",
                )
            count = max(1, replacement.count)
            updated = updated.replace(replacement.old, replacement.new, count)
        if updated == original:
            return WriteResult(ok=True, path=_rel(resolved, self.workspace_root), bytes_written=0)
        return self.write_file(_rel(resolved, self.workspace_root), updated)

    async def write_and_check(
        self,
        path: str | Path,
        content: str,
        *,
        lint_tools: tuple[LintTool, ...] = ("ruff",),
        test_paths: tuple[str, ...] = (),
        create_dirs: bool = False,
    ) -> WriteResult:
        write = self.write_file(path, content, create_dirs=create_dirs)
        if not write.ok:
            return write

        lint_results: list[LintResult] = []
        for tool in lint_tools:
            lint_results.append(await self.executor.execute_lint(Path(write.path), tool=tool))

        test_results: list[TestResult] = []
        for test_path in test_paths:
            test_results.append(await self.executor.execute_test(test_path))

        ok = all(result.ok for result in lint_results) and all(result.ok for result in test_results)
        return WriteResult(
            ok=ok,
            path=write.path,
            bytes_written=write.bytes_written,
            created=write.created,
            lint_results=lint_results,
            test_results=test_results,
        )

    def _resolve_path(
        self,
        path: str | Path,
        *,
        must_exist: bool = True,
    ) -> Path:
        candidate = Path(path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.workspace_root / candidate).resolve()
        )
        if self.workspace_root != resolved and self.workspace_root not in resolved.parents:
            raise ValueError(f"path escapes code workspace: {path}")
        if must_exist and not resolved.exists():
            raise ValueError(f"path does not exist under workspace: {path}")
        return resolved


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


__all__ = ["CodeWriter", "TextReplacement", "WriteResult"]
