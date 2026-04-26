"""工具输出自检器.

这层只做确定性校验: 文件哈希、真实 diff、pytest 复跑结果、git log 抄答案检测.
"""

from __future__ import annotations

import difflib
import hashlib
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PytestRunner = Callable[[str, Path, float], subprocess.CompletedProcess[str]]

_PYTEST_COUNT_RE = re.compile(
    r"\b(?P<count>\d+)\s+"
    r"(?P<kind>passed|failed|skipped|xfailed|xpassed|error|errors)\b",
    re.IGNORECASE,
)
_COMMIT_HASH_RE = re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE)


@dataclass(frozen=True)
class PytestSummary:
    """pytest 输出里和真假判断有关的摘要."""

    passed: int = 0
    failed: int = 0
    skipped: int = 0
    xfailed: int = 0
    xpassed: int = 0
    errors: int = 0
    no_tests_ran: bool = False

    @property
    def has_signal(self) -> bool:
        return self.no_tests_ran or any(
            [
                self.passed,
                self.failed,
                self.skipped,
                self.xfailed,
                self.xpassed,
                self.errors,
            ],
        )


class OutputVerifier:
    """对 agent 声称的工具输出做二次校验."""

    def __init__(
        self,
        *,
        cwd: str | Path | None = None,
        pytest_runner: PytestRunner | None = None,
        pytest_timeout_sec: float = 60.0,
    ) -> None:
        self._cwd = Path(cwd) if cwd is not None else Path.cwd()
        self._pytest_runner = pytest_runner or _default_pytest_runner
        self._pytest_timeout_sec = pytest_timeout_sec

    def hash_artifact(self, path: str) -> str:
        """对 agent 产物文件算 SHA256."""

        artifact_path = self._resolve_artifact_path(path)

        digest = hashlib.sha256()
        with artifact_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _resolve_artifact_path(self, path: str) -> Path:
        raw_path = Path(path)
        if ".." in raw_path.parts:
            raise ValueError("artifact path must not contain '..'")

        cwd = self._cwd.resolve()
        candidate = raw_path if raw_path.is_absolute() else cwd / raw_path
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(cwd)
        except ValueError:
            raise ValueError("artifact path must stay inside cwd") from None
        return resolved

    def verify_diff(self, before: str, after: str, expected_changes: list[str]) -> bool:
        """对比 agent 声称的改动和真实 diff 是否一致."""

        diff_lines = list(
            difflib.unified_diff(
                before.splitlines(),
                after.splitlines(),
                fromfile="before",
                tofile="after",
                lineterm="",
            ),
        )
        changed_lines = [
            line
            for line in diff_lines
            if (line.startswith("+") or line.startswith("-"))
            and not line.startswith(("+++", "---"))
        ]

        if not expected_changes:
            return not changed_lines
        if not changed_lines:
            return False

        changed_text = "\n".join(_normalize_line(line) for line in changed_lines)
        return all(_normalize_line(change) in changed_text for change in expected_changes)

    def check_pytest_output(self, output_text: str, test_file_path: str) -> bool:
        """把 agent 给出的 pytest 输出和真实复跑结果做摘要对比."""

        claimed = _parse_pytest_summary(output_text)
        if claimed is None:
            return False

        try:
            actual_process = self._pytest_runner(
                test_file_path,
                self._cwd,
                self._pytest_timeout_sec,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False

        actual_text = f"{actual_process.stdout}\n{actual_process.stderr}"
        actual = _parse_pytest_summary(actual_text)
        if actual is None:
            return False
        return claimed == actual

    def detect_git_log_answer_leak(self, agent_output: str, git_log_text: str) -> bool:
        """检测 agent 是否复制 git log 里的历史答案或 commit 信息."""

        normalized_output = _normalize_text(agent_output)
        output_hashes = {match.group(0).lower() for match in _COMMIT_HASH_RE.finditer(agent_output)}

        for commit_hash in _extract_git_hashes(git_log_text):
            if commit_hash.lower() in output_hashes:
                return True

        for phrase in _extract_git_log_phrases(git_log_text):
            normalized_phrase = _normalize_text(phrase)
            if _is_meaningful_phrase(normalized_phrase) and normalized_phrase in normalized_output:
                return True

        return False


def _default_pytest_runner(
    test_file_path: str,
    cwd: Path,
    timeout_sec: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", test_file_path, "-q"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )


def _parse_pytest_summary(output_text: str) -> PytestSummary | None:
    normalized = _normalize_text(output_text)
    counts: dict[str, int | bool] = {
        "passed": 0,
        "failed": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "errors": 0,
        "no_tests_ran": "no tests ran" in normalized or "no test ran" in normalized,
    }

    for match in _PYTEST_COUNT_RE.finditer(output_text):
        kind = match.group("kind").lower()
        target = "errors" if kind in {"error", "errors"} else kind
        counts[target] = int(counts[target]) + int(match.group("count"))

    summary = PytestSummary(
        passed=int(counts["passed"]),
        failed=int(counts["failed"]),
        skipped=int(counts["skipped"]),
        xfailed=int(counts["xfailed"]),
        xpassed=int(counts["xpassed"]),
        errors=int(counts["errors"]),
        no_tests_ran=bool(counts["no_tests_ran"]),
    )
    return summary if summary.has_signal else None


def _extract_git_hashes(git_log_text: str) -> set[str]:
    return {match.group(0).lower() for match in _COMMIT_HASH_RE.finditer(git_log_text)}


def _extract_git_log_phrases(git_log_text: str) -> list[str]:
    phrases: list[str] = []
    for raw_line in git_log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        oneline_match = re.match(r"^[0-9a-f]{7,40}\s+(?P<subject>.+)$", line, re.IGNORECASE)
        if oneline_match is not None:
            phrases.append(oneline_match.group("subject"))
            continue

        if line.startswith(("commit ", "Author:", "Date:", "Merge:")):
            continue
        phrases.append(line)
    return phrases


def _is_meaningful_phrase(phrase: str) -> bool:
    if len(phrase) < 12:
        return False
    return any(char.isalpha() for char in phrase)


def _normalize_line(value: str) -> str:
    return " ".join(value.strip().split())


def _normalize_text(value: str) -> str:
    return " ".join(value.lower().split())
