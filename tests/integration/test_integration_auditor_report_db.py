"""Integration: AuditorReport → DB writer (V7 §16.6 Phase X.B).

Validates AuditorReport frozen IO + parse_auditor_json + DB writer via fake
session_scope (same pattern as previous Phase X.B test files).

Coverage:
  - AuditorReport dataclass __post_init__ invariants (P0 ⇒ no release, valid
    risk_level enum)
  - parse_auditor_json from LLM JSON
  - write_auditor_report for each risk_level (P0/P1/P2)
  - Factory tenant_id binding
  - to_row_payload roundtrip
  - Schema sanity: V7 §16.6 9-field schema present
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest
from kun.core.orm import AuditorReportRow
from kun.integration.auditor_report_db import (
    AuditorReport,
    make_auditor_report_emitter,
    parse_auditor_json,
    write_auditor_report,
)

# ============================================================
# Fake session helper
# ============================================================


class _CaptureSession:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def add(self, instance: Any) -> None:
        self._sink.append(instance)

    async def flush(self) -> None:  # pragma: no cover - trivial
        return None


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[Any], list[dict[str, Any]]]:
    added: list[Any] = []
    scope_calls: list[dict[str, Any]] = []

    @asynccontextmanager
    async def fake_session_scope(**kwargs: Any) -> AsyncIterator[_CaptureSession]:
        scope_calls.append(kwargs)
        yield _CaptureSession(added)

    monkeypatch.setattr("kun.core.db.session_scope", fake_session_scope)
    return added, scope_calls


def _make_report(
    *,
    report_id: str = "ar-test-1",
    audited_capability: str = "self-reflect skill",
    risk_level: str = "P2",
    allow_release: bool = True,
    bypass_methods: list[str] | None = None,
    must_fix: list[str] | None = None,
) -> AuditorReport:
    return AuditorReport(
        report_id=report_id,
        audited_capability=audited_capability,
        audited_at=datetime.now(UTC),
        auditor_provider="anthropic/claude-opus",
        design_promise="该能力承诺...",
        real_code_path="kun/skills/builtin/self_reflect.py:42-89",
        bypass_methods=list(bypass_methods or ["可以通过直接 import 绕过"]),
        min_repro_steps="from kun.skills.builtin.self_reflect import _internal; ...",
        risk_level=risk_level,
        must_fix=list(must_fix or ["加 audit gate 拦截 _internal 直调"]),
        acceptance_tests=["test_self_reflect_no_bypass"],
        allow_release=allow_release,
        rationale="bypass 1 处, 不许发布前先修",
    )


# ============================================================
# AuditorReport dataclass invariants
# ============================================================


def test_p0_with_allow_release_raises() -> None:
    """V7 §16.6 不变量: P0 必不许发布."""
    with pytest.raises(ValueError, match="P0"):
        AuditorReport(
            report_id="ar-bad",
            audited_capability="x",
            audited_at=datetime.now(UTC),
            auditor_provider="anthropic/claude-opus",
            design_promise="...",
            real_code_path="...",
            risk_level="P0",
            allow_release=True,  # 违反 V7 §16.6
        )


def test_p0_with_block_release_ok() -> None:
    """P0 + allow_release=False 是允许的."""
    report = AuditorReport(
        report_id="ar-good-p0",
        audited_capability="x",
        audited_at=datetime.now(UTC),
        auditor_provider="anthropic/claude-opus",
        design_promise="...",
        real_code_path="...",
        risk_level="P0",
        allow_release=False,
    )
    assert report.risk_level == "P0"
    assert report.allow_release is False


def test_invalid_risk_level_raises() -> None:
    with pytest.raises(ValueError, match="risk_level"):
        AuditorReport(
            report_id="ar-bad-risk",
            audited_capability="x",
            audited_at=datetime.now(UTC),
            auditor_provider="anthropic/claude-opus",
            design_promise="...",
            real_code_path="...",
            risk_level="P3",  # 非法
        )


def test_auditor_report_is_frozen() -> None:
    from dataclasses import FrozenInstanceError

    report = _make_report()
    with pytest.raises(FrozenInstanceError):
        report.risk_level = "P0"  # type: ignore[misc]


# ============================================================
# parse_auditor_json
# ============================================================


def test_parse_auditor_json_full_payload() -> None:
    raw = {
        "design_promise": "self-reflect skill 必经主 runtime path",
        "real_code_path": "kun/skills/builtin/self_reflect.py",
        "bypass_methods": ["直接 import 绕过 audit"],
        "min_repro_steps": "from ... import _internal",
        "risk_level": "P1",
        "must_fix": ["加 audit gate"],
        "acceptance_tests": ["test_no_bypass"],
        "allow_release": False,
        "rationale": "有 1 处 bypass",
    }
    report = parse_auditor_json(
        raw_json=raw,
        report_id="ar-parsed-1",
        audited_capability="self-reflect skill",
        auditor_provider="anthropic/claude-opus",
    )
    assert report.report_id == "ar-parsed-1"
    assert report.audited_capability == "self-reflect skill"
    assert report.risk_level == "P1"
    assert report.bypass_methods == ["直接 import 绕过 audit"]
    assert report.must_fix == ["加 audit gate"]
    assert report.allow_release is False


def test_parse_auditor_json_missing_optional_fields_uses_defaults() -> None:
    raw = {
        "design_promise": "x",
        "real_code_path": "y",
        "risk_level": "P2",
        # bypass_methods / must_fix / acceptance_tests 缺
    }
    report = parse_auditor_json(
        raw_json=raw,
        report_id="ar-min",
        audited_capability="x",
        auditor_provider="anthropic/claude-opus",
    )
    assert report.bypass_methods == []
    assert report.must_fix == []
    assert report.acceptance_tests == []
    assert report.allow_release is True  # default


def test_parse_auditor_json_p0_invariant_enforced() -> None:
    raw = {
        "design_promise": "x",
        "real_code_path": "y",
        "risk_level": "P0",
        "allow_release": True,
    }
    with pytest.raises(ValueError, match="P0"):
        parse_auditor_json(
            raw_json=raw,
            report_id="ar-bad",
            audited_capability="x",
            auditor_provider="anthropic/claude-opus",
        )


# ============================================================
# Writer — risk_level matrix
# ============================================================


@pytest.mark.parametrize(
    "risk_level,allow_release",
    [
        ("P0", False),  # P0 不许发布
        ("P1", True),
        ("P1", False),
        ("P2", True),
    ],
)
async def test_write_auditor_report_risk_matrix(
    monkeypatch: pytest.MonkeyPatch,
    risk_level: str,
    allow_release: bool,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)

    report = _make_report(
        risk_level=risk_level, allow_release=allow_release
    )
    returned_id = await write_auditor_report(
        tenant_id="tenant-a", report=report
    )

    assert returned_id == report.report_id
    assert len(added) == 1
    row = added[0]
    assert isinstance(row, AuditorReportRow)
    assert row.tenant_id == "tenant-a"
    assert row.risk_level == risk_level
    assert row.allow_release is allow_release
    assert scope_calls == [{"tenant_id": "tenant-a"}]


async def test_write_persists_full_9_field_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, _ = _install_fake_session(monkeypatch)

    report = AuditorReport(
        report_id="ar-full",
        audited_capability="Mission Director runner",
        audited_at=datetime.now(UTC),
        auditor_provider="openai/gpt-5.5",
        design_promise="任务级方案监督",
        real_code_path="kun/agents/mission_director/service.py:129-296",
        bypass_methods=["从 LongTaskOrch 直接调 Executor 跳过 review"],
        min_repro_steps="orch.execute(task) without service.review_mission()",
        risk_level="P1",
        must_fix=["LongTaskOrch 启动时强制 mount Mission Director runner"],
        acceptance_tests=["test_long_task_requires_mission_director"],
        allow_release=False,
        rationale="1 个 P1 绕过, 修了再发",
    )

    await write_auditor_report(tenant_id="tenant-full", report=report)

    row = added[0]
    assert row.audited_capability == "Mission Director runner"
    assert row.auditor_provider == "openai/gpt-5.5"
    assert row.design_promise == "任务级方案监督"
    assert "service.py" in row.real_code_path
    assert row.bypass_methods == [
        "从 LongTaskOrch 直接调 Executor 跳过 review"
    ]
    assert "service.review_mission" in row.min_repro_steps
    assert row.risk_level == "P1"
    assert "Mission Director runner" in row.must_fix[0]
    assert row.acceptance_tests == [
        "test_long_task_requires_mission_director"
    ]
    assert row.allow_release is False
    assert row.rationale == "1 个 P1 绕过, 修了再发"


# ============================================================
# Factory — tenant binding
# ============================================================


async def test_make_auditor_report_emitter_binds_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)

    emitter = make_auditor_report_emitter("tenant-emitter-ar")
    report = _make_report()
    await emitter(report)

    assert len(added) == 1
    assert scope_calls == [{"tenant_id": "tenant-emitter-ar"}]


# ============================================================
# Schema sanity
# ============================================================


def test_auditor_report_row_has_v7_schema_columns() -> None:
    cols = {c.name for c in AuditorReportRow.__table__.columns}
    expected = {
        "tenant_id",
        "report_id",
        "audited_capability",
        "audited_at",
        "auditor_provider",
        "design_promise",
        "real_code_path",
        "bypass_methods",
        "min_repro_steps",
        "risk_level",
        "must_fix",
        "acceptance_tests",
        "allow_release",
        "rationale",
        "created_at",
    }
    assert expected.issubset(cols), (
        f"AuditorReportRow 缺列: {expected - cols}"
    )


def test_to_row_payload_roundtrip() -> None:
    report = _make_report()
    payload = report.to_row_payload("tenant-x")
    assert payload["tenant_id"] == "tenant-x"
    assert payload["report_id"] == report.report_id
    assert payload["audited_capability"] == report.audited_capability
    assert payload["risk_level"] == report.risk_level
    assert payload["bypass_methods"] == list(report.bypass_methods)
    # 全 14 字段都在 (含 tenant_id)
    assert set(payload.keys()) == {
        "tenant_id",
        "report_id",
        "audited_capability",
        "audited_at",
        "auditor_provider",
        "design_promise",
        "real_code_path",
        "bypass_methods",
        "min_repro_steps",
        "risk_level",
        "must_fix",
        "acceptance_tests",
        "allow_release",
        "rationale",
    }
