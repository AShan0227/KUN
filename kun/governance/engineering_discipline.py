"""V7 §11.5 Claude Code 工程纪律到 multi-LLM 蒸馏 enforcement.

V7 §11.5 表里 10 维 Claude Code 工程纪律映射到 multi-LLM ensemble runtime
落地. 本 module 提供:

1. ``EngineeringDiscipline`` enum — 10 维 + 扩展项
2. ``DisciplineCheck`` — 每条纪律的检查结果 (frozen dataclass)
3. ``EngineeringDisciplineEnforcer`` — 运行时检查 LLM 输出是否符合
4. 每条纪律的 default check 函数 (heuristic-based, 不调 LLM, V7 §4.3
   engineering_first_with_llm_fallback)

设计 (V7 §13.6):
- frozen dataclass IO
- engineering-first 规则检查 (不烧 LLM)
- 检查失败不抛, 返 list[DisciplineCheck] 给 caller 决定 (e.g. RepairTicket)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from kun.core.logging import get_logger

log = get_logger("kun.governance.engineering_discipline")


class EngineeringDiscipline(StrEnum):
    """V7 §11.5 + §23.2 — Claude Code 工程纪律 10 维 + 扩展项."""

    # 10 主维度 (V7 §4.3)
    TASK_DECOMPOSITION = "task_decomposition"  # 任务拆解 (TodoWrite / PlanTree)
    PARALLEL_SUBAGENT = "parallel_subagent"  # 并行 sub-agent dispatch
    GREP_VERIFY_BEFORE_ASSUME = "grep_verify_before_assume"  # grep 验证假设
    TEST_DRIVEN_FAIL_FAST = "test_driven_fail_fast"  # 失败测试先于实现
    COMMIT_DISCIPLINE = "commit_discipline"  # commit ≤ 1000 行
    ERROR_FIX_NOT_HIDE = "error_fix_not_hide"  # 错误立修不藏
    DEV_LOG_SEDIMENT = "dev_log_sediment"  # ADR-025 dev log 沉淀
    DECISION_PAUSE_ASK = "decision_pause_ask"  # 决策点停下问
    READ_WITH_OFFSET_LIMIT = "read_with_offset_limit"  # Read with offset+limit
    BASH_RESTRAINT = "bash_restraint"  # Bash 克制 (专用工具优先)

    # 扩展项 (V7 §4.3 扩展)
    COAUTHOR_TRAIL = "coauthor_trail"  # Co-Authored-By in commit
    SINGLE_COMMIT_FILES = "single_commit_files"  # 单 commit ≤ 5 文件
    RUFF_PYTEST_PRE_COMMIT = "ruff_pytest_pre_commit"  # ruff + pytest 全过才 commit
    GIT_STATUS_DIFF_PRIORITY = "git_status_diff_priority"  # git status/diff 优先


@dataclass(frozen=True)
class DisciplineCheck:
    """单条纪律检查结果 — frozen, V7 §13.6 IO 契约."""

    discipline: EngineeringDiscipline
    passed: bool
    rationale: str
    evidence_refs: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DisciplineReport:
    """N 条纪律检查的整体报告 (一次 LLM 响应或一次 ensemble 输出 检查后)."""

    checks: list[DisciplineCheck]
    overall_score: float  # 0-1, passed / total

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def failed_disciplines(self) -> list[EngineeringDiscipline]:
        return [c.discipline for c in self.checks if not c.passed]


# Default heuristic checks (engineering-first, no LLM call).
# Each check takes a context dict and returns DisciplineCheck.


def _check_commit_discipline(context: dict[str, Any]) -> DisciplineCheck:
    """commit ≤ 1000 行 (V7 §4.3)."""
    lines_added = context.get("commit_lines_added", 0)
    lines_deleted = context.get("commit_lines_deleted", 0)
    total = lines_added + lines_deleted
    passed = total <= 1000
    return DisciplineCheck(
        discipline=EngineeringDiscipline.COMMIT_DISCIPLINE,
        passed=passed,
        rationale=(
            f"commit lines = {total} ({lines_added}+/{lines_deleted}-), "
            f"{'≤ 1000 ✓' if passed else '> 1000 violation'}"
        ),
    )


def _check_single_commit_files(context: dict[str, Any]) -> DisciplineCheck:
    """单 commit ≤ 5 文件 (V7 §4.3 扩展项)."""
    files_changed = context.get("commit_files_changed", 0)
    passed = files_changed <= 5
    return DisciplineCheck(
        discipline=EngineeringDiscipline.SINGLE_COMMIT_FILES,
        passed=passed,
        rationale=f"files changed = {files_changed}, {'≤ 5 ✓' if passed else '> 5 should split'}",
    )


def _check_coauthor_trail(context: dict[str, Any]) -> DisciplineCheck:
    """commit message 必带 Co-Authored-By."""
    commit_msg = context.get("commit_message", "")
    passed = "Co-Authored-By:" in commit_msg
    return DisciplineCheck(
        discipline=EngineeringDiscipline.COAUTHOR_TRAIL,
        passed=passed,
        rationale="Co-Authored-By: 在 commit message" if passed else "缺 Co-Authored-By",
    )


def _check_grep_verify(context: dict[str, Any]) -> DisciplineCheck:
    """grep verify before assume — LLM 输出 / 改代码前是否用过 grep-verify skill."""
    skill_calls = context.get("skill_calls_in_response", [])
    code_changes = context.get("has_code_changes", False)
    if not code_changes:
        # No code changes — discipline N/A
        return DisciplineCheck(
            discipline=EngineeringDiscipline.GREP_VERIFY_BEFORE_ASSUME,
            passed=True,
            rationale="no code changes — N/A",
        )
    used_grep_verify = any(
        call.get("skill") in {"grep-verify", "grep_verify"}
        for call in skill_calls
        if isinstance(call, dict)
    )
    return DisciplineCheck(
        discipline=EngineeringDiscipline.GREP_VERIFY_BEFORE_ASSUME,
        passed=used_grep_verify,
        rationale=(
            "grep-verify skill called ≥ 1 time before code change"
            if used_grep_verify
            else "code change without grep-verify — V7 §11.5 violation"
        ),
    )


def _check_bash_restraint(context: dict[str, Any]) -> DisciplineCheck:
    """Bash 克制: shell-exec 调用应 < 专用 skill 调用."""
    skill_calls = context.get("skill_calls_in_response", [])
    shell_count = sum(
        1
        for call in skill_calls
        if isinstance(call, dict) and call.get("skill") in {"shell-exec", "shell_exec"}
    )
    specialized_count = sum(
        1
        for call in skill_calls
        if isinstance(call, dict) and call.get("skill") not in {"shell-exec", "shell_exec"}
    )
    if shell_count == 0 and specialized_count == 0:
        return DisciplineCheck(
            discipline=EngineeringDiscipline.BASH_RESTRAINT,
            passed=True,
            rationale="no skill calls — N/A",
        )
    passed = shell_count <= specialized_count
    return DisciplineCheck(
        discipline=EngineeringDiscipline.BASH_RESTRAINT,
        passed=passed,
        rationale=(
            f"shell-exec: {shell_count}, specialized: {specialized_count} — "
            f"{'OK' if passed else 'shell over-used'}"
        ),
    )


def _check_read_with_offset_limit(context: dict[str, Any]) -> DisciplineCheck:
    """Read with offset+limit: 大文件 read 必须带 limit/offset."""
    skill_calls = context.get("skill_calls_in_response", [])
    large_reads_without_limit = []
    for call in skill_calls:
        if not isinstance(call, dict):
            continue
        if call.get("skill") not in {"self-reflect", "file-io"}:
            continue
        params = call.get("params", {})
        if params.get("op") != "read":
            continue
        # Heuristic: if file size hint > 10K bytes and no limit, flag
        file_size = params.get("expected_file_size_hint", 0)
        if file_size > 10_000 and not params.get("limit"):
            large_reads_without_limit.append(call)
    passed = not large_reads_without_limit
    return DisciplineCheck(
        discipline=EngineeringDiscipline.READ_WITH_OFFSET_LIMIT,
        passed=passed,
        rationale=(
            f"large reads without limit: {len(large_reads_without_limit)} — "
            f"{'OK' if passed else 'V7 §11.5 violation, should chunk'}"
        ),
    )


def _check_test_driven(context: dict[str, Any]) -> DisciplineCheck:
    """测试驱动: bug fix 应有 failing test 在 fix 之前."""
    is_bug_fix = context.get("is_bug_fix", False)
    has_failing_test_first = context.get("has_failing_test_first", False)
    if not is_bug_fix:
        return DisciplineCheck(
            discipline=EngineeringDiscipline.TEST_DRIVEN_FAIL_FAST,
            passed=True,
            rationale="not a bug fix — N/A",
        )
    return DisciplineCheck(
        discipline=EngineeringDiscipline.TEST_DRIVEN_FAIL_FAST,
        passed=has_failing_test_first,
        rationale=(
            "failing test before fix ✓"
            if has_failing_test_first
            else "bug fix without failing test first — V7 violation"
        ),
    )


def _check_decision_pause(context: dict[str, Any]) -> DisciplineCheck:
    """决策点停下问: 高风险动作前应 raise CollaborationTicket."""
    high_risk_action = context.get("high_risk_action_attempted", False)
    raised_ticket = context.get("raised_collaboration_ticket", False)
    if not high_risk_action:
        return DisciplineCheck(
            discipline=EngineeringDiscipline.DECISION_PAUSE_ASK,
            passed=True,
            rationale="no high-risk action — N/A",
        )
    return DisciplineCheck(
        discipline=EngineeringDiscipline.DECISION_PAUSE_ASK,
        passed=raised_ticket,
        rationale=(
            "CollaborationTicket raised before high-risk action ✓"
            if raised_ticket
            else "high-risk action without user pause — V7 violation"
        ),
    )


def _check_dev_log(context: dict[str, Any]) -> DisciplineCheck:
    """dev_log 沉淀 (ADR-025): commit 后必更新 dev_log."""
    has_code_commit = context.get("has_code_commit", False)
    dev_log_updated = context.get("dev_log_updated_in_session", False)
    if not has_code_commit:
        return DisciplineCheck(
            discipline=EngineeringDiscipline.DEV_LOG_SEDIMENT,
            passed=True,
            rationale="no code commit — N/A",
        )
    return DisciplineCheck(
        discipline=EngineeringDiscipline.DEV_LOG_SEDIMENT,
        passed=dev_log_updated,
        rationale=(
            "dev_log updated post commit ✓"
            if dev_log_updated
            else "ADR-025 violation: commit without dev_log update"
        ),
    )


# Default check registry
_DEFAULT_CHECKS = {
    EngineeringDiscipline.COMMIT_DISCIPLINE: _check_commit_discipline,
    EngineeringDiscipline.SINGLE_COMMIT_FILES: _check_single_commit_files,
    EngineeringDiscipline.COAUTHOR_TRAIL: _check_coauthor_trail,
    EngineeringDiscipline.GREP_VERIFY_BEFORE_ASSUME: _check_grep_verify,
    EngineeringDiscipline.BASH_RESTRAINT: _check_bash_restraint,
    EngineeringDiscipline.READ_WITH_OFFSET_LIMIT: _check_read_with_offset_limit,
    EngineeringDiscipline.TEST_DRIVEN_FAIL_FAST: _check_test_driven,
    EngineeringDiscipline.DECISION_PAUSE_ASK: _check_decision_pause,
    EngineeringDiscipline.DEV_LOG_SEDIMENT: _check_dev_log,
}


class EngineeringDisciplineEnforcer:
    """V7 §11.5 + §23.2 Claude Code 工程纪律 runtime enforcer.

    Enforces 10 维 + 扩展项 on every LLM output / commit / RSI candidate.

    Usage:
        enforcer = EngineeringDisciplineEnforcer()
        report = enforcer.check(context={
            "commit_lines_added": 500,
            "commit_lines_deleted": 50,
            "commit_files_changed": 4,
            "commit_message": "...Co-Authored-By: Claude...",
            "skill_calls_in_response": [...],
            "has_code_changes": True,
            ...
        })
        if report.failed_count > 0:
            # 触发 RepairTicket / 阻断 / log
            ...
    """

    def __init__(
        self,
        *,
        disciplines_to_check: list[EngineeringDiscipline] | None = None,
    ) -> None:
        """
        Args:
            disciplines_to_check: 哪些纪律要检查. None → 全部 default checks.
        """
        if disciplines_to_check is None:
            self._disciplines = list(_DEFAULT_CHECKS.keys())
        else:
            self._disciplines = list(disciplines_to_check)

    def check(self, context: dict[str, Any]) -> DisciplineReport:
        """Run all configured discipline checks on the given context.

        Returns DisciplineReport with overall_score = passed / total.
        """
        checks = []
        for disc in self._disciplines:
            check_fn = _DEFAULT_CHECKS.get(disc)
            if check_fn is None:
                # Discipline doesn't have a default check (e.g. PARALLEL_SUBAGENT
                # which is observed at ensemble level not single output)
                continue
            try:
                result = check_fn(context)
            except Exception as e:
                log.warning(
                    "discipline_check_failed",
                    discipline=disc.value,
                    error=str(e),
                )
                result = DisciplineCheck(
                    discipline=disc,
                    passed=False,
                    rationale=f"check raised exception: {e}",
                )
            checks.append(result)

        total = len(checks)
        passed = sum(1 for c in checks if c.passed)
        overall_score = passed / total if total > 0 else 1.0

        return DisciplineReport(checks=checks, overall_score=overall_score)


__all__ = [
    "DisciplineCheck",
    "DisciplineReport",
    "EngineeringDiscipline",
    "EngineeringDisciplineEnforcer",
]
