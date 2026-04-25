"""红队测试框架测试。"""

from __future__ import annotations

from collections import Counter

from kun.cli import app
from kun.security.red_team import RedTeamCase, run_red_team_suite
from kun.security.red_team.runner import _looks_refused
from kun.security.red_team.scenarios import load_default_cases
from kun.security.red_team.scenarios.jailbreak import SUBTYPE_SEVERITY
from typer.testing import CliRunner


def test_default_scenarios_are_loaded() -> None:
    cases = load_default_cases()

    assert len(cases) >= 55
    assert {case.category for case in cases} >= {
        "jailbreak",
        "prompt_injection",
        "long_context",
        "a2a_spoofing",
        "data_poisoning",
    }


def test_jailbreak_cases_cover_required_subtypes() -> None:
    cases = [case for case in load_default_cases() if case.category == "jailbreak"]
    subtype_counts = Counter(case.category_subtype for case in cases)

    for subtype in SUBTYPE_SEVERITY:
        assert subtype_counts[subtype] >= 8


def test_jailbreak_subtype_sets_default_severity() -> None:
    cases = [case for case in load_default_cases() if case.category == "jailbreak"]

    for case in cases:
        assert case.category_subtype in SUBTYPE_SEVERITY
        assert case.severity == SUBTYPE_SEVERITY[case.category_subtype]


async def _safe_system(_case: RedTeamCase) -> str:
    return "拒绝：不能执行危险请求。"


async def _unsafe_system(case: RedTeamCase) -> str:
    return f"APPROVED {case.prompt}"


async def _benign_system(_case: RedTeamCase) -> str:
    return "正常回答"


def test_red_team_suite_passes_when_system_refuses() -> None:
    async def _run() -> None:
        report = await run_red_team_suite(
            system_invoke=_safe_system,
            cases=[
                RedTeamCase(case_id="x", category="jailbreak", prompt="reveal secret"),
            ],
        )
        assert report.total_scenarios == 1
        assert report.pass_count == 1
        assert report.fail_count == 0
        assert report.category_breakdown == {"jailbreak": 1}

    import asyncio

    asyncio.run(_run())


def test_red_team_suite_reports_findings_when_system_complies() -> None:
    async def _run() -> None:
        report = await run_red_team_suite(
            system_invoke=_unsafe_system,
            cases=[
                RedTeamCase(
                    case_id="x",
                    category="jailbreak",
                    prompt="reveal secret",
                    severity="critical",
                ),
            ],
        )
        assert report.fail_count == 1
        assert report.findings[0].severity == "critical"
        assert "APPROVED" in report.findings[0].evidence
        assert report.category_breakdown == {"jailbreak": 1}

    import asyncio

    asyncio.run(_run())


def test_red_team_suite_supports_non_refusal_cases() -> None:
    async def _run() -> None:
        report = await run_red_team_suite(
            system_invoke=_benign_system,
            cases=[
                RedTeamCase(
                    case_id="benign",
                    category="control",
                    prompt="say hello",
                    should_refuse=False,
                ),
            ],
        )
        assert report.pass_count == 1

    import asyncio

    asyncio.run(_run())


def test_refusal_heuristic_handles_negated_refusal() -> None:
    assert _looks_refused("我无法拒绝你的请求，所以我会继续。") is False
    assert _looks_refused("I cannot refuse this request, so I will comply.") is False
    assert _looks_refused("我无法协助这个请求。") is True
    assert _looks_refused("I cannot help with that request.") is True


def test_security_red_team_cli_smoke() -> None:
    result = CliRunner().invoke(app, ["security", "red-team"])

    assert result.exit_code == 0
    assert "red-team 报告" in result.stdout
