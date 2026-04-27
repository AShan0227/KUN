"""CodeDebugger — classify failures and produce deterministic fix hints."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

DebugCategory = Literal[
    "syntax_error",
    "import_error",
    "assertion_failure",
    "type_error",
    "lint_error",
    "timeout",
    "path_error",
    "unknown",
]


@dataclass(frozen=True)
class DebugFinding:
    category: DebugCategory
    summary: str
    fix_hint: str
    confidence: float
    path: str | None = None
    line: int | None = None


class CodeDebugger:
    """Small rule-based debugger for test/lint/execute outputs."""

    def analyze_failure(
        self,
        output: str = "",
        error: str = "",
        *,
        returncode: int | None = None,
        timed_out: bool = False,
    ) -> DebugFinding:
        text = "\n".join(part for part in (output, error) if part)
        location = _extract_location(text)
        if timed_out or "timed out" in text.lower():
            return DebugFinding(
                category="timeout",
                summary="Command exceeded its timeout.",
                fix_hint="Reduce the input size, remove infinite loops, or raise timeout only if safe.",
                confidence=0.95,
                path=location[0],
                line=location[1],
            )
        if "SyntaxError" in text:
            return DebugFinding(
                category="syntax_error",
                summary=_first_matching_line(text, "SyntaxError"),
                fix_hint="Open the reported file and fix the invalid Python syntax near the line shown.",
                confidence=0.92,
                path=location[0],
                line=location[1],
            )
        if "ModuleNotFoundError" in text or "ImportError" in text:
            return DebugFinding(
                category="import_error",
                summary=_first_matching_line(text, "ImportError", "ModuleNotFoundError"),
                fix_hint="Check local import paths, package installation, and test cwd.",
                confidence=0.9,
                path=location[0],
                line=location[1],
            )
        if "AssertionError" in text or "assert " in text:
            return DebugFinding(
                category="assertion_failure",
                summary=_first_matching_line(text, "AssertionError", "assert "),
                fix_hint="Compare expected vs actual values, then update implementation before tests.",
                confidence=0.82,
                path=location[0],
                line=location[1],
            )
        if "TypeError" in text or "mypy" in text.lower():
            return DebugFinding(
                category="type_error",
                summary=_first_matching_line(text, "TypeError", "error:"),
                fix_hint="Check the failing call signature or type annotation mismatch.",
                confidence=0.78,
                path=location[0],
                line=location[1],
            )
        if re.search(r"\b[EFWIC]\d{3,4}\b", text):
            return DebugFinding(
                category="lint_error",
                summary=_first_lint_code(text),
                fix_hint="Run the formatter or fix the reported lint rule before rerunning tests.",
                confidence=0.8,
                path=location[0],
                line=location[1],
            )
        if "path escapes" in text or "No such file" in text or "FileNotFoundError" in text:
            return DebugFinding(
                category="path_error",
                summary=_first_matching_line(
                    text, "path escapes", "FileNotFoundError", "No such file"
                ),
                fix_hint="Resolve paths relative to the workspace and reject traversal.",
                confidence=0.82,
                path=location[0],
                line=location[1],
            )
        return DebugFinding(
            category="unknown",
            summary=f"Command failed with returncode={returncode}",
            fix_hint="Inspect stdout/stderr, identify the first real failure, then rerun the narrowest check.",
            confidence=0.35,
            path=location[0],
            line=location[1],
        )


def _extract_location(text: str) -> tuple[str | None, int | None]:
    for pattern in (r'File "([^"]+)", line (\d+)', r"([^:\s]+\.py):(\d+):"):
        match = re.search(pattern, text)
        if match:
            return match.group(1), int(match.group(2))
    return None, None


def _first_matching_line(text: str, *needles: str) -> str:
    for line in text.splitlines():
        if any(needle in line for needle in needles):
            return line.strip()
    return text.splitlines()[0].strip() if text.splitlines() else ""


def _first_lint_code(text: str) -> str:
    match = re.search(r"\b[EFWIC]\d{3,4}\b.*", text)
    return match.group(0).strip() if match else "lint rule failed"


__all__ = ["CodeDebugger", "DebugCategory", "DebugFinding"]
