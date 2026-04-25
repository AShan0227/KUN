"""红队场景集合。"""

from __future__ import annotations

from kun.security.red_team.runner import RedTeamCase
from kun.security.red_team.scenarios import (
    a2a_spoofing,
    data_poisoning,
    jailbreak,
    long_context,
    prompt_injection,
)


def load_default_cases() -> list[RedTeamCase]:
    cases: list[RedTeamCase] = []
    for module in (
        jailbreak,
        prompt_injection,
        long_context,
        a2a_spoofing,
        data_poisoning,
    ):
        cases.extend(module.load_cases())
    return cases


__all__ = ["load_default_cases"]
