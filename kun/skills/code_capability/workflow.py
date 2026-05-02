"""CodeCapability change workflow.

This service ties the deterministic reader/writer/executor/debugger/reviewer
pieces into a first-pass automatic programming loop.
"""

from __future__ import annotations

import difflib
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from kun.skills.code_capability.debugger import CodeDebugger, DebugFinding
from kun.skills.code_capability.executor import CodeExecutor, LintResult, LintTool, TestResult
from kun.skills.code_capability.reviewer import CodeReviewer, ReviewResult
from kun.skills.code_capability.writer import CodeWriter, WriteResult

ChangeMode = Literal["dry_run", "apply"]
ChangePhase = Literal["input", "resolve", "review", "write", "check", "done"]
CheckKind = Literal["lint", "test"]

_SKIP_COPY_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
}


@dataclass(frozen=True)
class ChangeCheckSpec:
    """One workspace-local check to run after staging the proposed change."""

    kind: CheckKind = "lint"
    target: str | None = None
    tool: LintTool = "ruff"
    timeout_sec: int = 60


@dataclass(frozen=True)
class ChangeWorkflowResult:
    """Structured result for the review -> write/dry-run -> check loop."""

    ok: bool
    path: str
    mode: ChangeMode
    phase: ChangePhase
    applied: bool = False
    rolled_back: bool = False
    bytes_changed: int = 0
    diff: str = ""
    review: ReviewResult | None = None
    write_result: WriteResult | None = None
    lint_results: list[LintResult] = field(default_factory=list)
    test_results: list[TestResult] = field(default_factory=list)
    debug: DebugFinding | None = None
    error: str = ""
    rollback_hint: str = ""


class CodeChangeWorkflow:
    """Review, stage/apply, check, and roll back a single-file code change."""

    def __init__(
        self,
        *,
        workspace_root: str | Path = ".",
        writer: CodeWriter | None = None,
        executor: CodeExecutor | None = None,
        reviewer: CodeReviewer | None = None,
        debugger: CodeDebugger | None = None,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.executor = executor or CodeExecutor(workspace_root=self.workspace_root)
        self.writer = writer or CodeWriter(
            workspace_root=self.workspace_root,
            executor=self.executor,
        )
        self.reviewer = reviewer or CodeReviewer(workspace_root=self.workspace_root)
        self.debugger = debugger or CodeDebugger()

    async def propose_change(
        self,
        path: str | Path,
        *,
        patch_text: str | None = None,
        replacement_content: str | None = None,
        allow_apply: bool = False,
        checks: tuple[ChangeCheckSpec, ...] | None = None,
    ) -> ChangeWorkflowResult:
        """Run the automatic coding loop for one target file.

        ``allow_apply`` defaults to False. In that mode the proposed file is
        staged in a temporary workspace copy for checks, leaving the real
        workspace unchanged.
        """

        mode: ChangeMode = "apply" if allow_apply else "dry_run"
        try:
            target = self._resolve_file(path)
            rel_path = _rel(target, self.workspace_root)
        except ValueError as exc:
            return _error_result(
                path=str(path),
                mode=mode,
                phase="resolve",
                error=str(exc),
                rollback_hint="No workspace changes were made; choose a path inside the code workspace.",
                debugger=self.debugger,
            )

        try:
            original = target.read_text(encoding="utf-8")
            proposed = _proposed_content(
                original,
                patch_text=patch_text,
                replacement_content=replacement_content,
            )
        except ValueError as exc:
            return _error_result(
                path=rel_path,
                mode=mode,
                phase="input",
                error=str(exc),
                rollback_hint="No workspace changes were made; provide exactly one valid patch_text or replacement_content.",
                debugger=self.debugger,
            )

        diff = _unified_diff(rel_path, original, proposed)
        review = self.reviewer.review_diff(diff)
        if not review.ok:
            return ChangeWorkflowResult(
                ok=False,
                path=rel_path,
                mode=mode,
                phase="review",
                diff=diff,
                review=review,
                error="review rejected the proposed change",
                rollback_hint="No workspace changes were made; address review findings before applying.",
            )

        try:
            check_specs = self._normalize_checks(checks, default_target=rel_path)
        except ValueError as exc:
            return ChangeWorkflowResult(
                ok=False,
                path=rel_path,
                mode=mode,
                phase="resolve",
                diff=diff,
                review=review,
                debug=self.debugger.analyze_failure(error=str(exc)),
                error=str(exc),
                rollback_hint="No workspace changes were made; choose check targets inside the code workspace.",
            )
        if not allow_apply:
            return await self._dry_run(
                rel_path=rel_path,
                proposed=proposed,
                diff=diff,
                review=review,
                checks=check_specs,
            )
        return await self._apply(
            rel_path=rel_path,
            target=target,
            original=original,
            proposed=proposed,
            diff=diff,
            review=review,
            checks=check_specs,
        )

    async def _dry_run(
        self,
        *,
        rel_path: str,
        proposed: str,
        diff: str,
        review: ReviewResult,
        checks: tuple[ChangeCheckSpec, ...],
    ) -> ChangeWorkflowResult:
        with tempfile.TemporaryDirectory(prefix="kun-code-change-") as tmp:
            dry_root = Path(tmp) / "workspace"
            try:
                shutil.copytree(
                    self.workspace_root,
                    dry_root,
                    ignore=shutil.ignore_patterns(*_SKIP_COPY_DIRS),
                    symlinks=True,
                )
            except OSError as exc:
                return ChangeWorkflowResult(
                    ok=False,
                    path=rel_path,
                    mode="dry_run",
                    phase="write",
                    diff=diff,
                    review=review,
                    debug=self.debugger.analyze_failure(error=str(exc)),
                    error=str(exc),
                    rollback_hint="No workspace changes were made; dry-run workspace copy failed before touching the real workspace.",
                )
            dry_executor = CodeExecutor(workspace_root=dry_root)
            dry_writer = CodeWriter(workspace_root=dry_root, executor=dry_executor)
            try:
                write = dry_writer.write_file(rel_path, proposed)
            except (OSError, ValueError) as exc:
                return ChangeWorkflowResult(
                    ok=False,
                    path=rel_path,
                    mode="dry_run",
                    phase="write",
                    diff=diff,
                    review=review,
                    debug=self.debugger.analyze_failure(error=str(exc)),
                    error=str(exc),
                    rollback_hint="No workspace changes were made; dry-run staging failed before touching the real workspace.",
                )
            if not write.ok:
                return ChangeWorkflowResult(
                    ok=False,
                    path=rel_path,
                    mode="dry_run",
                    phase="write",
                    diff=diff,
                    review=review,
                    write_result=write,
                    error=write.error,
                    rollback_hint="No workspace changes were made; dry-run staging failed before touching the real workspace.",
                )
            try:
                lint_results, test_results = await _run_checks(
                    executor=dry_executor,
                    checks=checks,
                    default_target=rel_path,
                )
            except ValueError as exc:
                return ChangeWorkflowResult(
                    ok=False,
                    path=rel_path,
                    mode="dry_run",
                    phase="check",
                    diff=diff,
                    review=review,
                    write_result=write,
                    debug=self.debugger.analyze_failure(error=str(exc)),
                    error=str(exc),
                    rollback_hint="No workspace changes were made; dry-run check target resolution failed.",
                )
        return _checked_result(
            path=rel_path,
            mode="dry_run",
            diff=diff,
            review=review,
            write_result=write,
            lint_results=lint_results,
            test_results=test_results,
            applied=False,
            rollback_hint_on_failure="No workspace changes were made; fix check failures before setting allow_apply=True.",
            debugger=self.debugger,
        )

    async def _apply(
        self,
        *,
        rel_path: str,
        target: Path,
        original: str,
        proposed: str,
        diff: str,
        review: ReviewResult,
        checks: tuple[ChangeCheckSpec, ...],
    ) -> ChangeWorkflowResult:
        try:
            write = self.writer.write_file(rel_path, proposed)
        except (OSError, ValueError) as exc:
            return ChangeWorkflowResult(
                ok=False,
                path=rel_path,
                mode="apply",
                phase="write",
                diff=diff,
                review=review,
                debug=self.debugger.analyze_failure(error=str(exc)),
                error=str(exc),
                rollback_hint="Write failed before checks completed; inspect the target file and restore from version control if needed.",
            )
        if not write.ok:
            return ChangeWorkflowResult(
                ok=False,
                path=rel_path,
                mode="apply",
                phase="write",
                diff=diff,
                review=review,
                write_result=write,
                error=write.error,
                rollback_hint="No completed write was recorded; inspect the error and retry after fixing the target path.",
            )

        try:
            lint_results, test_results = await _run_checks(
                executor=self.executor,
                checks=checks,
                default_target=rel_path,
            )
        except ValueError as exc:
            rolled_back, rollback_error = _restore_file(target, original)
            return ChangeWorkflowResult(
                ok=False,
                path=rel_path,
                mode="apply",
                phase="check",
                applied=True,
                rolled_back=rolled_back,
                bytes_changed=write.bytes_written,
                diff=diff,
                review=review,
                write_result=write,
                debug=self.debugger.analyze_failure(error=str(exc)),
                error=str(exc),
                rollback_hint=_rollback_hint(
                    rolled_back=rolled_back,
                    rollback_error=rollback_error,
                    prefix="Check target resolution failed after apply.",
                ),
            )
        if _checks_ok(lint_results, test_results):
            return ChangeWorkflowResult(
                ok=True,
                path=rel_path,
                mode="apply",
                phase="done",
                applied=True,
                bytes_changed=write.bytes_written,
                diff=diff,
                review=review,
                write_result=write,
                lint_results=lint_results,
                test_results=test_results,
            )

        rolled_back, rollback_error = _restore_file(target, original)

        debug = _debug_check_failure(self.debugger, lint_results, test_results)
        return ChangeWorkflowResult(
            ok=False,
            path=rel_path,
            mode="apply",
            phase="check",
            applied=True,
            rolled_back=rolled_back,
            bytes_changed=write.bytes_written,
            diff=diff,
            review=review,
            write_result=write,
            lint_results=lint_results,
            test_results=test_results,
            debug=debug,
            error="checks failed after apply",
            rollback_hint=_rollback_hint(
                rolled_back=rolled_back,
                rollback_error=rollback_error,
                prefix="Checks failed after apply.",
            ),
        )

    def _resolve_file(self, path: str | Path) -> Path:
        candidate = Path(path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.workspace_root / candidate).resolve()
        )
        if self.workspace_root != resolved and self.workspace_root not in resolved.parents:
            raise ValueError(f"path escapes code workspace: {path}")
        if not resolved.exists() or not resolved.is_file():
            raise ValueError(f"path does not exist under workspace: {path}")
        return resolved

    def _normalize_checks(
        self,
        checks: tuple[ChangeCheckSpec, ...] | None,
        *,
        default_target: str,
    ) -> tuple[ChangeCheckSpec, ...]:
        if checks is None:
            if Path(default_target).suffix == ".py":
                return (ChangeCheckSpec(kind="lint", target=default_target),)
            return ()
        normalized: list[ChangeCheckSpec] = []
        for check in checks:
            target = check.target or default_target
            try:
                resolved = self._resolve_check_target(target)
            except ValueError:
                raise
            normalized.append(
                ChangeCheckSpec(
                    kind=check.kind,
                    target=resolved,
                    tool=check.tool,
                    timeout_sec=max(1, min(300, int(check.timeout_sec))),
                )
            )
        return tuple(normalized)

    def _resolve_check_target(self, target: str) -> str:
        candidate = Path(target)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (self.workspace_root / candidate).resolve()
        )
        if self.workspace_root != resolved and self.workspace_root not in resolved.parents:
            raise ValueError(f"path escapes code workspace: {target}")
        return _rel(resolved, self.workspace_root)


async def _run_checks(
    *,
    executor: CodeExecutor,
    checks: tuple[ChangeCheckSpec, ...],
    default_target: str,
) -> tuple[list[LintResult], list[TestResult]]:
    lint_results: list[LintResult] = []
    test_results: list[TestResult] = []
    for check in checks:
        target = check.target or default_target
        if check.kind == "test":
            test_results.append(await executor.execute_test(target, timeout_sec=check.timeout_sec))
        else:
            lint_results.append(
                await executor.execute_lint(
                    Path(target),
                    tool=check.tool,
                    timeout_sec=check.timeout_sec,
                )
            )
    return lint_results, test_results


def _checked_result(
    *,
    path: str,
    mode: ChangeMode,
    diff: str,
    review: ReviewResult,
    write_result: WriteResult,
    lint_results: list[LintResult],
    test_results: list[TestResult],
    applied: bool,
    rollback_hint_on_failure: str,
    debugger: CodeDebugger,
) -> ChangeWorkflowResult:
    if _checks_ok(lint_results, test_results):
        return ChangeWorkflowResult(
            ok=True,
            path=path,
            mode=mode,
            phase="done",
            applied=applied,
            bytes_changed=write_result.bytes_written,
            diff=diff,
            review=review,
            write_result=write_result,
            lint_results=lint_results,
            test_results=test_results,
        )
    return ChangeWorkflowResult(
        ok=False,
        path=path,
        mode=mode,
        phase="check",
        applied=applied,
        bytes_changed=write_result.bytes_written,
        diff=diff,
        review=review,
        write_result=write_result,
        lint_results=lint_results,
        test_results=test_results,
        debug=_debug_check_failure(debugger, lint_results, test_results),
        error="checks failed",
        rollback_hint=rollback_hint_on_failure,
    )


def _error_result(
    *,
    path: str,
    mode: ChangeMode,
    phase: ChangePhase,
    error: str,
    rollback_hint: str,
    debugger: CodeDebugger,
) -> ChangeWorkflowResult:
    return ChangeWorkflowResult(
        ok=False,
        path=path,
        mode=mode,
        phase=phase,
        debug=debugger.analyze_failure(error=error),
        error=error,
        rollback_hint=rollback_hint,
    )


def _checks_ok(lint_results: list[LintResult], test_results: list[TestResult]) -> bool:
    return all(result.ok for result in lint_results) and all(result.ok for result in test_results)


def _debug_check_failure(
    debugger: CodeDebugger,
    lint_results: list[LintResult],
    test_results: list[TestResult],
) -> DebugFinding | None:
    for lint in lint_results:
        if not lint.ok:
            return debugger.analyze_failure(
                output=lint.output,
                error=lint.error,
                returncode=lint.returncode,
                timed_out=lint.timed_out,
            )
    for test in test_results:
        if not test.ok:
            return debugger.analyze_failure(
                output=test.output,
                error=test.error,
                returncode=test.returncode,
                timed_out=test.timed_out,
            )
    return None


def _restore_file(target: Path, original: str) -> tuple[bool, str]:
    try:
        target.write_text(original, encoding="utf-8")
    except OSError as exc:
        return False, str(exc)
    return True, ""


def _rollback_hint(*, rolled_back: bool, rollback_error: str, prefix: str) -> str:
    if rolled_back:
        return f"{prefix} Original file content was restored automatically."
    return (
        f"{prefix} Automatic rollback failed; restore the target file from version control "
        f"or a saved copy. rollback_error={rollback_error}"
    )


def _proposed_content(
    original: str,
    *,
    patch_text: str | None,
    replacement_content: str | None,
) -> str:
    has_patch = patch_text is not None and patch_text.strip() != ""
    has_replacement = replacement_content is not None
    if has_patch == has_replacement:
        raise ValueError("provide exactly one of patch_text or replacement_content")
    if has_replacement:
        return replacement_content or ""
    return _apply_unified_patch(original, patch_text or "")


def _apply_unified_patch(original: str, patch_text: str) -> str:
    original_lines = original.splitlines()
    output: list[str] = []
    cursor = 0
    saw_hunk = False
    lines = patch_text.splitlines()
    idx = 0
    while idx < len(lines):
        header = lines[idx]
        if not header.startswith("@@"):
            idx += 1
            continue
        match = re.match(
            r"@@ -(?P<old_start>\d+)(?:,\d+)? \+(?P<new_start>\d+)(?:,\d+)? @@(?: .*)?$",
            header,
        )
        if match is None:
            raise ValueError(f"invalid unified diff hunk header: {header}")
        saw_hunk = True
        old_index = max(0, int(match.group("old_start")) - 1)
        if old_index < cursor:
            raise ValueError("unified diff hunks overlap or are out of order")
        output.extend(original_lines[cursor:old_index])
        cursor = old_index
        idx += 1
        while idx < len(lines) and not lines[idx].startswith("@@"):
            line = lines[idx]
            if line.startswith("\\"):
                idx += 1
                continue
            if not line:
                raise ValueError("invalid unified diff line without prefix")
            prefix = line[0]
            text = line[1:]
            if prefix == " ":
                _expect_original_line(original_lines, cursor, text)
                output.append(original_lines[cursor])
                cursor += 1
            elif prefix == "-":
                _expect_original_line(original_lines, cursor, text)
                cursor += 1
            elif prefix == "+":
                output.append(text)
            else:
                raise ValueError(f"invalid unified diff line prefix: {prefix}")
            idx += 1
    if not saw_hunk:
        raise ValueError("patch_text must contain at least one unified diff hunk")
    output.extend(original_lines[cursor:])
    proposed = "\n".join(output)
    if proposed and (original.endswith("\n") or patch_text.endswith("\n")):
        proposed += "\n"
    return proposed


def _expect_original_line(original_lines: list[str], cursor: int, expected: str) -> None:
    if cursor >= len(original_lines) or original_lines[cursor] != expected:
        actual = original_lines[cursor] if cursor < len(original_lines) else "<eof>"
        raise ValueError(f"patch context mismatch: expected {expected!r}, got {actual!r}")


def _unified_diff(path: str, original: str, proposed: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            proposed.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


__all__ = [
    "ChangeCheckSpec",
    "ChangeMode",
    "ChangePhase",
    "ChangeWorkflowResult",
    "CheckKind",
    "CodeChangeWorkflow",
]
