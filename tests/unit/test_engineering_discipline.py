"""V7 Phase F — Claude Code 工程纪律 enforcer 单测."""

from __future__ import annotations

import pytest
from kun.governance.engineering_discipline import (
    DisciplineCheck,
    DisciplineReport,
    EngineeringDiscipline,
    EngineeringDisciplineEnforcer,
)


@pytest.mark.unit
class TestCommitDiscipline:
    def test_under_1000_lines_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.COMMIT_DISCIPLINE]
        )
        report = enforcer.check({"commit_lines_added": 500, "commit_lines_deleted": 100})
        assert report.passed_count == 1

    def test_over_1000_lines_fails(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.COMMIT_DISCIPLINE]
        )
        report = enforcer.check({"commit_lines_added": 800, "commit_lines_deleted": 300})
        assert report.failed_count == 1
        assert "> 1000" in report.checks[0].rationale


@pytest.mark.unit
class TestSingleCommitFiles:
    def test_under_5_files_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.SINGLE_COMMIT_FILES]
        )
        report = enforcer.check({"commit_files_changed": 3})
        assert report.passed_count == 1

    def test_over_5_files_fails(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.SINGLE_COMMIT_FILES]
        )
        report = enforcer.check({"commit_files_changed": 7})
        assert report.failed_count == 1


@pytest.mark.unit
class TestCoauthorTrail:
    def test_with_coauthor_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.COAUTHOR_TRAIL]
        )
        report = enforcer.check(
            {"commit_message": "feat: do X\n\nCo-Authored-By: Claude <foo>"}
        )
        assert report.passed_count == 1

    def test_without_coauthor_fails(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.COAUTHOR_TRAIL]
        )
        report = enforcer.check({"commit_message": "feat: do X"})
        assert report.failed_count == 1


@pytest.mark.unit
class TestGrepVerify:
    def test_no_code_changes_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.GREP_VERIFY_BEFORE_ASSUME]
        )
        report = enforcer.check({"has_code_changes": False})
        assert report.passed_count == 1
        assert "N/A" in report.checks[0].rationale

    def test_code_changes_with_grep_verify_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.GREP_VERIFY_BEFORE_ASSUME]
        )
        report = enforcer.check(
            {
                "has_code_changes": True,
                "skill_calls_in_response": [{"skill": "grep-verify", "params": {}}],
            }
        )
        assert report.passed_count == 1

    def test_code_changes_without_grep_verify_fails(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.GREP_VERIFY_BEFORE_ASSUME]
        )
        report = enforcer.check(
            {"has_code_changes": True, "skill_calls_in_response": []}
        )
        assert report.failed_count == 1


@pytest.mark.unit
class TestBashRestraint:
    def test_specialized_over_shell_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.BASH_RESTRAINT]
        )
        report = enforcer.check(
            {
                "skill_calls_in_response": [
                    {"skill": "self-reflect"},
                    {"skill": "self-reflect"},
                    {"skill": "shell-exec"},
                ]
            }
        )
        assert report.passed_count == 1

    def test_shell_over_specialized_fails(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.BASH_RESTRAINT]
        )
        report = enforcer.check(
            {
                "skill_calls_in_response": [
                    {"skill": "shell-exec"},
                    {"skill": "shell-exec"},
                    {"skill": "shell-exec"},
                    {"skill": "file-io"},
                ]
            }
        )
        assert report.failed_count == 1


@pytest.mark.unit
class TestReadOffsetLimit:
    def test_small_read_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.READ_WITH_OFFSET_LIMIT]
        )
        report = enforcer.check(
            {
                "skill_calls_in_response": [
                    {"skill": "self-reflect", "params": {"op": "read", "expected_file_size_hint": 5000}}
                ]
            }
        )
        assert report.passed_count == 1

    def test_large_read_without_limit_fails(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.READ_WITH_OFFSET_LIMIT]
        )
        report = enforcer.check(
            {
                "skill_calls_in_response": [
                    {
                        "skill": "self-reflect",
                        "params": {"op": "read", "expected_file_size_hint": 50000},
                    }
                ]
            }
        )
        assert report.failed_count == 1

    def test_large_read_with_limit_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.READ_WITH_OFFSET_LIMIT]
        )
        report = enforcer.check(
            {
                "skill_calls_in_response": [
                    {
                        "skill": "self-reflect",
                        "params": {
                            "op": "read",
                            "expected_file_size_hint": 50000,
                            "limit": 200,
                        },
                    }
                ]
            }
        )
        assert report.passed_count == 1


@pytest.mark.unit
class TestTestDriven:
    def test_not_bug_fix_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.TEST_DRIVEN_FAIL_FAST]
        )
        report = enforcer.check({"is_bug_fix": False})
        assert report.passed_count == 1

    def test_bug_fix_with_failing_test_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.TEST_DRIVEN_FAIL_FAST]
        )
        report = enforcer.check(
            {"is_bug_fix": True, "has_failing_test_first": True}
        )
        assert report.passed_count == 1

    def test_bug_fix_without_failing_test_fails(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.TEST_DRIVEN_FAIL_FAST]
        )
        report = enforcer.check(
            {"is_bug_fix": True, "has_failing_test_first": False}
        )
        assert report.failed_count == 1


@pytest.mark.unit
class TestDecisionPause:
    def test_no_high_risk_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.DECISION_PAUSE_ASK]
        )
        report = enforcer.check({"high_risk_action_attempted": False})
        assert report.passed_count == 1

    def test_high_risk_with_ticket_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.DECISION_PAUSE_ASK]
        )
        report = enforcer.check(
            {
                "high_risk_action_attempted": True,
                "raised_collaboration_ticket": True,
            }
        )
        assert report.passed_count == 1

    def test_high_risk_without_ticket_fails(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.DECISION_PAUSE_ASK]
        )
        report = enforcer.check(
            {
                "high_risk_action_attempted": True,
                "raised_collaboration_ticket": False,
            }
        )
        assert report.failed_count == 1


@pytest.mark.unit
class TestDevLog:
    def test_no_commit_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.DEV_LOG_SEDIMENT]
        )
        report = enforcer.check({"has_code_commit": False})
        assert report.passed_count == 1

    def test_commit_with_dev_log_passes(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.DEV_LOG_SEDIMENT]
        )
        report = enforcer.check(
            {"has_code_commit": True, "dev_log_updated_in_session": True}
        )
        assert report.passed_count == 1

    def test_commit_without_dev_log_fails(self) -> None:
        enforcer = EngineeringDisciplineEnforcer(
            disciplines_to_check=[EngineeringDiscipline.DEV_LOG_SEDIMENT]
        )
        report = enforcer.check(
            {"has_code_commit": True, "dev_log_updated_in_session": False}
        )
        assert report.failed_count == 1


@pytest.mark.unit
class TestFullReport:
    def test_all_pass_overall_score_1(self) -> None:
        enforcer = EngineeringDisciplineEnforcer()
        report = enforcer.check({
            "commit_lines_added": 100,
            "commit_lines_deleted": 50,
            "commit_files_changed": 2,
            "commit_message": "feat: x\n\nCo-Authored-By: Claude <foo>",
            "skill_calls_in_response": [],
            "has_code_changes": False,
            "is_bug_fix": False,
            "high_risk_action_attempted": False,
            "has_code_commit": False,
        })
        assert report.overall_score == 1.0
        assert report.failed_count == 0

    def test_mixed_passes_and_failures(self) -> None:
        enforcer = EngineeringDisciplineEnforcer()
        report = enforcer.check({
            "commit_lines_added": 1500,  # 违反 commit_discipline
            "commit_lines_deleted": 0,
            "commit_files_changed": 8,  # 违反 single_commit_files
            "commit_message": "x",  # 违反 coauthor_trail
            "skill_calls_in_response": [],
            "has_code_changes": False,
            "is_bug_fix": False,
            "high_risk_action_attempted": False,
            "has_code_commit": False,
        })
        assert report.failed_count >= 3
        assert report.overall_score < 1.0
        failed = {c.discipline for c in report.checks if not c.passed}
        assert EngineeringDiscipline.COMMIT_DISCIPLINE in failed
        assert EngineeringDiscipline.SINGLE_COMMIT_FILES in failed
        assert EngineeringDiscipline.COAUTHOR_TRAIL in failed


@pytest.mark.unit
def test_discipline_report_is_frozen() -> None:
    """V7 §13.6 frozen_dataclass IO."""
    from dataclasses import FrozenInstanceError

    report = DisciplineReport(checks=[], overall_score=1.0)
    with pytest.raises(FrozenInstanceError):
        report.overall_score = 0.5  # type: ignore[misc]


@pytest.mark.unit
def test_discipline_check_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    check = DisciplineCheck(
        discipline=EngineeringDiscipline.COMMIT_DISCIPLINE,
        passed=True,
        rationale="x",
    )
    with pytest.raises(FrozenInstanceError):
        check.passed = False  # type: ignore[misc]
