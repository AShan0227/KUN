"""CodeReviewer — lightweight static review for code diffs and files.

Wire 29C (BATCH8b follow-up): 加 review_diff_with_jury — 启发式检查后可
经 V2.1 §17.10 multi_judge (3-5 个 LLM judge 并发投票) 跑深度 review,
拿到 JuryVerdict 跟启发式 ReviewResult 一起返.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from kun.engineering.multi_judge import JuryVerdict
    from kun.interface.llm import LLMRouter

logger = logging.getLogger(__name__)

Severity = Literal["info", "warning", "error"]


@dataclass(frozen=True)
class ReviewFinding:
    severity: Severity
    message: str
    rule: str
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class ReviewResult:
    ok: bool
    findings: list[ReviewFinding]


class CodeReviewer:
    """Deterministic first-pass code reviewer.

    This is not a replacement for human or LLM review. It catches sharp edges
    before the heavier review stack runs.
    """

    def __init__(self, *, workspace_root: str | Path = ".") -> None:
        self.workspace_root = Path(workspace_root).resolve()

    def review_diff(self, diff: str) -> ReviewResult:
        findings: list[ReviewFinding] = []
        current_path: str | None = None
        new_line: int | None = None
        for raw_line in diff.splitlines():
            if raw_line.startswith("+++ b/"):
                current_path = raw_line.removeprefix("+++ b/")
                new_line = None
                continue
            if raw_line.startswith("@@"):
                new_line = _parse_hunk_new_line(raw_line)
                continue
            if raw_line.startswith("+") and not raw_line.startswith("+++"):
                line_no = new_line
                findings.extend(_review_line(raw_line[1:], path=current_path, line_no=line_no))
                if new_line is not None:
                    new_line += 1
            elif raw_line.startswith("-") and not raw_line.startswith("---"):
                continue
            elif new_line is not None:
                new_line += 1
        return ReviewResult(ok=not any(f.severity == "error" for f in findings), findings=findings)

    async def review_diff_with_jury(
        self,
        diff: str,
        *,
        router: LLMRouter,
        judge_models: list[str] | None = None,
        rubric: str | None = None,
    ) -> tuple[ReviewResult, JuryVerdict | None]:
        """Wire 29C (BATCH8b): 启发式 review + V2.1 §17.10 multi_judge 并行.

        启发式 ReviewResult 永远返 (跟 review_diff 一致); jury 任何异常 → 第二
        返 None, reviewer 不爆.

        Args:
            diff: unified diff 文本
            router: LLMRouter (jury_evaluate 复用主仓库 LLMRouter)
            judge_models: judge tier list, 默认 3 个 (top/strong/cheap)
            rubric: 评分标准, 默认 "代码安全 + 可读性 + 副作用"

        Returns:
            (启发式 ReviewResult, multi_judge 结论 or None 失败时)
        """
        from kun.engineering.multi_judge import jury_evaluate

        result = self.review_diff(diff)

        models = judge_models or ["top", "strong", "cheap"]
        review_rubric = rubric or (
            "代码 diff review 标准: "
            "(1) 是否引入安全风险 (eval/exec/shell=True/硬编码 secret)? "
            "(2) 是否清晰易读? "
            "(3) 是否有副作用 / breaking change? "
            "pass=true 表示可合并; score 0-1 反映质量."
        )
        try:
            verdict = await jury_evaluate(
                artifact=diff,
                rubric=review_rubric,
                judge_models=models,
                router=router,
            )
        except Exception as e:
            logger.debug("reviewer.jury_skipped err=%s", e)
            return result, None
        return result, verdict

    def review_file(self, path: str | Path) -> ReviewResult:
        resolved = self._resolve_file(path)
        findings: list[ReviewFinding] = []
        for line_no, line in enumerate(resolved.read_text(encoding="utf-8").splitlines(), start=1):
            findings.extend(
                _review_line(line, path=_rel(resolved, self.workspace_root), line_no=line_no)
            )
        return ReviewResult(ok=not any(f.severity == "error" for f in findings), findings=findings)

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
            raise ValueError(f"not a file under workspace: {path}")
        return resolved


def _review_line(line: str, *, path: str | None, line_no: int | None) -> list[ReviewFinding]:
    findings: list[ReviewFinding] = []
    stripped = line.strip()
    if re.search(r"\b(eval|exec)\s*\(", stripped):
        findings.append(
            ReviewFinding(
                severity="error",
                message="Avoid dynamic eval/exec in generated code.",
                rule="no-eval-exec",
                path=path,
                line=line_no,
            )
        )
    if "shell=True" in stripped:
        findings.append(
            ReviewFinding(
                severity="error",
                message="Avoid subprocess shell=True; pass argv lists instead.",
                rule="no-shell-true",
                path=path,
                line=line_no,
            )
        )
    if re.search(r"(api[_-]?key|secret|token)\s*=\s*['\"][^'\"]{8,}", stripped, re.I):
        findings.append(
            ReviewFinding(
                severity="error",
                message="Possible hard-coded secret.",
                rule="no-hardcoded-secret",
                path=path,
                line=line_no,
            )
        )
    if stripped.startswith("except Exception") and "raise" not in stripped:
        findings.append(
            ReviewFinding(
                severity="warning",
                message="Broad exception handler needs a clear recovery path.",
                rule="broad-except",
                path=path,
                line=line_no,
            )
        )
    return findings


def _parse_hunk_new_line(line: str) -> int | None:
    match = re.search(r"\+(\d+)(?:,\d+)?", line)
    return int(match.group(1)) if match else None


def _rel(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root).as_posix()


__all__ = ["CodeReviewer", "ReviewFinding", "ReviewResult", "Severity"]
