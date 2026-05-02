"""CodeDebugger — classify failures and produce deterministic fix hints.

Wire 29C (BATCH8a follow-up): 加 enrich_with_diagnose_runner — 启发式分析后
可以经 V2.1 §10.6 DiagnoseRunner (含 5 类 fix handler chain) 跑深度诊断,
拿到 plans / outcomes 并入 DebugFinding.fix_hint.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from kun.security.diagnose_runner import DiagnoseRunner

logger = logging.getLogger(__name__)

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

    async def enrich_with_diagnose_runner(
        self,
        finding: DebugFinding,
        runner: DiagnoseRunner,
        *,
        output: str = "",
        error: str = "",
        user_id: str = "kun_lab",
        tenant_id: str = "u-sylvan",
    ) -> DebugFinding:
        """Wire 29C (BATCH8a): 用 V2.1 §10.6 DiagnoseRunner 增强 fix_hint.

        DiagnoseRunner 跑 5 步管道 (range / cause / plan / execute / verify) +
        5 类 fix handler chain (clean/accelerate/failover/network_guard/privacy).
        我们把 debugger 的 finding 喂进去, 让 runner 给出更具体的修复方案.

        异常静默 (返原 finding) — debugger 不能因 runner 挂掉而失效.
        """
        from kun.core.ids import new_id
        from kun.security.diagnose_runner import DiagnoseRequest

        try:
            hint_text = (
                f"category={finding.category} summary={finding.summary} "
                f"output={output[:500]} error={error[:500]}"
            )
            request = DiagnoseRequest(
                request_id=new_id("diag"),
                trigger="anomaly_detection",
                user_id=user_id,
                tenant_id=tenant_id,
                hint_text=hint_text,
            )
            report = await runner.run(request)
        except Exception as e:
            logger.debug("debugger.diagnose_runner_skipped err=%s", e)
            return finding

        # 把 plan + outcome 摘要并入 fix_hint
        plan_summary = _summarize_diagnose_report(report)
        if not plan_summary:
            return finding
        enriched_hint = f"{finding.fix_hint} | DiagnoseRunner: {plan_summary}"
        # confidence 提升 (有 runner 加持)
        new_confidence = min(1.0, finding.confidence + 0.05)
        return replace(finding, fix_hint=enriched_hint, confidence=new_confidence)


def _summarize_diagnose_report(report: Any) -> str:
    """把 DiagnoseReport 的 plans + outcomes 浓缩成一行."""
    parts: list[str] = []
    plans = getattr(report, "plans", []) or []
    outcomes = getattr(report, "outcomes", []) or []
    for plan in plans[:2]:
        desc = getattr(plan, "description", "") or getattr(plan, "category", "")
        if desc:
            parts.append(f"plan={desc}")
    for outcome in outcomes[:2]:
        status = getattr(outcome, "status", "")
        if status:
            parts.append(f"outcome={status}")
    return "; ".join(parts)


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
