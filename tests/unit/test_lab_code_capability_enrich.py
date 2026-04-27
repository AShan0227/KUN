"""BATCH8a debugger 接 DiagnoseRunner + BATCH8b reviewer 接 multi_judge (Wire 29C)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest
from kun.skills.code_capability.debugger import CodeDebugger, DebugFinding
from kun.skills.code_capability.reviewer import CodeReviewer

# ---- Wire 29C-debugger: enrich_with_diagnose_runner ----


@pytest.mark.asyncio
async def test_debugger_enrich_appends_diagnose_summary() -> None:
    """DiagnoseRunner 返 plans/outcomes → fix_hint 拼接."""
    finding = DebugFinding(
        category="syntax_error",
        summary="SyntaxError near line 5",
        fix_hint="Open the file and fix syntax.",
        confidence=0.9,
    )

    fake_runner = AsyncMock()
    fake_report = type(
        "Report",
        (),
        {
            "plans": [
                type("P", (), {"description": "Restart subsystem", "category": "clean"})(),
            ],
            "outcomes": [
                type("O", (), {"status": "ok"})(),
            ],
        },
    )()
    fake_runner.run = AsyncMock(return_value=fake_report)

    debugger = CodeDebugger()
    enriched = await debugger.enrich_with_diagnose_runner(
        finding,
        fake_runner,
        output="some output",
        error="some error",
    )

    assert "DiagnoseRunner" in enriched.fix_hint
    assert "Restart subsystem" in enriched.fix_hint
    assert "outcome=ok" in enriched.fix_hint
    # confidence 提升
    assert enriched.confidence >= finding.confidence
    assert enriched.confidence <= 1.0


@pytest.mark.asyncio
async def test_debugger_enrich_passes_diagnose_request_correctly() -> None:
    """DiagnoseRequest 正确构造: hint_text 含 finding category + output/error."""
    finding = DebugFinding(
        category="import_error",
        summary="ModuleNotFoundError: foo",
        fix_hint="Install foo.",
        confidence=0.85,
    )

    captured_request: list[Any] = []

    async def fake_run(request):
        captured_request.append(request)
        return type("R", (), {"plans": [], "outcomes": []})()

    fake_runner = AsyncMock()
    fake_runner.run = fake_run

    debugger = CodeDebugger()
    await debugger.enrich_with_diagnose_runner(
        finding,
        fake_runner,
        output="bar output",
        error="baz error",
        user_id="custom_user",
        tenant_id="custom_tenant",
    )

    assert len(captured_request) == 1
    req = captured_request[0]
    assert req.user_id == "custom_user"
    assert req.tenant_id == "custom_tenant"
    assert req.trigger == "anomaly_detection"
    assert "import_error" in req.hint_text
    assert "bar output" in req.hint_text
    assert "baz error" in req.hint_text


@pytest.mark.asyncio
async def test_debugger_enrich_runner_failure_returns_original_finding() -> None:
    """runner 抛异常 → 静默返原 finding (debugger 不爆)."""
    finding = DebugFinding(
        category="syntax_error",
        summary="x",
        fix_hint="original hint",
        confidence=0.9,
    )

    fake_runner = AsyncMock()
    fake_runner.run = AsyncMock(side_effect=RuntimeError("simulated runner crash"))

    debugger = CodeDebugger()
    result = await debugger.enrich_with_diagnose_runner(finding, fake_runner)

    assert result is finding  # 返原对象 (frozen dataclass)
    assert result.fix_hint == "original hint"


@pytest.mark.asyncio
async def test_debugger_enrich_empty_report_returns_original() -> None:
    """report.plans + outcomes 都空 → 返原 finding (没 enrichment 来源)."""
    finding = DebugFinding(
        category="unknown",
        summary="x",
        fix_hint="original",
        confidence=0.4,
    )

    fake_runner = AsyncMock()
    fake_runner.run = AsyncMock(return_value=type("R", (), {"plans": [], "outcomes": []})())

    debugger = CodeDebugger()
    result = await debugger.enrich_with_diagnose_runner(finding, fake_runner)
    assert result is finding


# ---- Wire 29C-reviewer: review_diff_with_jury ----


@pytest.mark.asyncio
async def test_reviewer_jury_returns_both_static_and_jury() -> None:
    """启发式 ReviewResult + JuryVerdict 都返."""
    from unittest.mock import patch

    diff = "+++ b/foo.py\n@@ -1,3 +1,4 @@\n+import subprocess\n+subprocess.run(cmd, shell=True)\n"

    fake_verdict = type(
        "V",
        (),
        {
            "pass_": False,
            "avg_score": 0.4,
            "spread": 0.1,
            "ballots": [],
            "rationale": "shell=True is unsafe",
        },
    )()

    async def fake_jury_evaluate(*, artifact, rubric, judge_models, router):
        return fake_verdict

    with patch("kun.engineering.multi_judge.jury_evaluate", new=fake_jury_evaluate):
        reviewer = CodeReviewer()
        static_result, jury = await reviewer.review_diff_with_jury(diff, router=AsyncMock())

    # 静态 review 应该捕获 shell=True
    assert static_result.ok is False
    assert any(f.rule == "no-shell-true" for f in static_result.findings)
    # jury 也来了
    assert jury is not None
    assert jury.pass_ is False
    assert "shell=True" in jury.rationale


@pytest.mark.asyncio
async def test_reviewer_jury_failure_returns_static_only() -> None:
    """jury_evaluate 抛异常 → static 仍返, jury=None."""
    from unittest.mock import patch

    diff = "+++ b/x.py\n@@ -1,1 +1,2 @@\n+x = 1\n"

    async def crashing_jury(*args, **kwargs):
        raise RuntimeError("simulated jury crash")

    with patch("kun.engineering.multi_judge.jury_evaluate", new=crashing_jury):
        reviewer = CodeReviewer()
        static_result, jury = await reviewer.review_diff_with_jury(diff, router=AsyncMock())

    assert static_result.ok is True  # 这条 diff 干净
    assert jury is None


@pytest.mark.asyncio
async def test_reviewer_jury_uses_default_judge_models() -> None:
    """没传 judge_models → 用默认 [top, strong, cheap]."""
    from unittest.mock import patch

    captured: list[dict[str, Any]] = []

    async def capture_jury(*, artifact, rubric, judge_models, router):
        captured.append({"models": judge_models, "rubric": rubric})
        return type(
            "V",
            (),
            {"pass_": True, "avg_score": 0.9, "spread": 0.05, "ballots": [], "rationale": "ok"},
        )()

    with patch("kun.engineering.multi_judge.jury_evaluate", new=capture_jury):
        reviewer = CodeReviewer()
        await reviewer.review_diff_with_jury("diff", router=AsyncMock())

    assert captured[0]["models"] == ["top", "strong", "cheap"]
    assert "代码 diff review 标准" in captured[0]["rubric"]


@pytest.mark.asyncio
async def test_reviewer_jury_custom_models_and_rubric() -> None:
    from unittest.mock import patch

    captured: list[dict[str, Any]] = []

    async def capture_jury(*, artifact, rubric, judge_models, router):
        captured.append({"models": judge_models, "rubric": rubric, "artifact": artifact})
        return type(
            "V",
            (),
            {"pass_": True, "avg_score": 1.0, "spread": 0.0, "ballots": [], "rationale": "x"},
        )()

    with patch("kun.engineering.multi_judge.jury_evaluate", new=capture_jury):
        reviewer = CodeReviewer()
        await reviewer.review_diff_with_jury(
            "my_diff_text",
            router=AsyncMock(),
            judge_models=["coding"],
            rubric="custom rubric",
        )

    assert captured[0]["models"] == ["coding"]
    assert captured[0]["rubric"] == "custom rubric"
    assert captured[0]["artifact"] == "my_diff_text"
