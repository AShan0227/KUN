"""越狱 prompt 场景。"""

from __future__ import annotations

from importlib import resources
from typing import Literal, cast, get_args

import yaml

from kun.security.red_team.runner import RedTeamCase, Severity

CategorySubtype = Literal[
    "base64_encoded",
    "unicode_obfuscation",
    "role_play",
    "multi_turn",
    "token_smuggling",
]

SUBTYPE_SEVERITY: dict[CategorySubtype, Severity] = {
    "base64_encoded": "high",
    "unicode_obfuscation": "medium",
    "role_play": "high",
    "multi_turn": "critical",
    "token_smuggling": "medium",
}
_VALID_SEVERITIES = set(get_args(Severity))


def load_cases() -> list[RedTeamCase]:
    fixture = resources.files("kun.security.red_team.fixtures").joinpath("jailbreak_corpus.yaml")
    data = yaml.safe_load(fixture.read_text(encoding="utf-8")) or {}
    cases: list[RedTeamCase] = []
    for item in data.get("cases", []):
        subtype = str(item.get("category_subtype", "")).strip()
        coerced_subtype = _coerce_subtype(subtype)
        default_severity = SUBTYPE_SEVERITY[coerced_subtype] if coerced_subtype else "medium"
        cases.append(
            RedTeamCase(
                case_id=str(item["case_id"]),
                category="jailbreak",
                prompt=str(item["prompt"]),
                category_subtype=subtype,
                severity=_coerce_severity(item.get("severity", default_severity)),
                impact=str(item.get("impact", "可能绕过安全策略")),
                recommendation=str(item.get("recommendation", "拒绝越权请求并记录样本")),
            )
        )
    return cases


def _coerce_severity(raw: object) -> Severity:
    if isinstance(raw, str) and raw in _VALID_SEVERITIES:
        return cast(Severity, raw)
    return "medium"


def _coerce_subtype(raw: str) -> CategorySubtype | None:
    if raw in SUBTYPE_SEVERITY:
        return raw
    return None


__all__ = ["SUBTYPE_SEVERITY", "load_cases"]
