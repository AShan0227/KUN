"""Self-referential guard must cover the RSI judge's real paths (audit F033).

is_self_referential previously matched only role-name prefixes at kun/agents/*,
so the External Supervisor's real home (kun/external_supervisor) and the
governance/watchtower guard code (incl. self_referential.py itself) were NOT
flagged — a candidate could rewrite the judge and auto-promote.
"""

from __future__ import annotations

import pytest
from kun.governance.self_referential import is_self_referential


@pytest.mark.unit
@pytest.mark.parametrize(
    "target",
    [
        "kun/external_supervisor/service",
        "kun/external_supervisor/modes.py",
        "kun.external_supervisor.runner",
        "kun/governance/self_referential.py",
        "kun/governance/capability_lifecycle",
        "kun.governance.rcdh",
        "kun/watchtower/engine.py",
        "kun.watchtower.handlers",
    ],
)
def test_rsi_judge_real_paths_are_self_referential(target: str) -> None:
    assert is_self_referential(target) is True


@pytest.mark.unit
def test_existing_role_prefixes_still_detected() -> None:
    assert is_self_referential("strategist")
    assert is_self_referential("supervisor.service")
    assert is_self_referential("kun/agents/director/intent")
    assert is_self_referential("external_supervisor.modes")


@pytest.mark.unit
@pytest.mark.parametrize(
    "target",
    [
        None,
        "",
        "executor.tool_calling",
        "llm.router",
        "skill.bug_fix",
        # not a guard root — must NOT over-match
        "kun/interface/llm/router.py",
        "kun/engineering/orchestrator.py",
        "kun/governanceX/thing",  # prefix-but-not-a-path-boundary
    ],
)
def test_non_judge_paths_are_not_self_referential(target) -> None:
    assert is_self_referential(target) is False
