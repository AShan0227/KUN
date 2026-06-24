"""V7 §16.6 MF-AR-LLM — real LLM-driven auditor unit tests.

Tests the LLM audit path without真调 LLM (mocks ensemble invoker).
Real e2e is in tests/integration/test_qwen_ensemble_e2e.py + dogfood v11.

Coverage:
  - _llm_audit_enabled env-var parsing
  - _extract_json_block: fence / bare / nested
  - llm_audit_capability:
    * env-disabled → returns None
    * ensemble factory returns None → returns None
    * LLM raises → returns None (graceful)
    * LLM returns no JSON → returns None
    * LLM returns invalid JSON → returns None
    * Happy path: parses JSON → writes row → returns report_id
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from kun.core.orm import AuditorReportRow
from kun.integration.auditor_report_llm import (
    _extract_json_block,
    _llm_audit_enabled,
    llm_audit_capability,
)

# ============================================================
# env-var parsing
# ============================================================


@pytest.mark.parametrize(
    "value,expected",
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("on", True),
        ("", False),
        ("false", False),
        ("0", False),
        ("no", False),
        ("off", False),
    ],
)
def test_llm_audit_enabled_env(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: bool
) -> None:
    monkeypatch.setenv("KUN_V7_AUDITOR_USE_LLM", value)
    assert _llm_audit_enabled() is expected


def test_llm_audit_enabled_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default OFF — don't surprise users with $."""
    monkeypatch.delenv("KUN_V7_AUDITOR_USE_LLM", raising=False)
    assert _llm_audit_enabled() is False


# ============================================================
# _extract_json_block
# ============================================================


def test_extract_json_with_fence() -> None:
    text = '''Here is the audit:
```json
{"risk_level": "P1", "allow_release": false}
```
End.'''
    block = _extract_json_block(text)
    assert block is not None
    assert '"risk_level": "P1"' in block


def test_extract_json_bare() -> None:
    text = 'prose...{"risk_level": "P2"}...more prose'
    block = _extract_json_block(text)
    assert block is not None
    assert block == '{"risk_level": "P2"}'


def test_extract_json_nested() -> None:
    text = '{"a": {"b": {"c": 1}}, "d": [1, 2]}'
    block = _extract_json_block(text)
    assert block == text


def test_extract_json_no_block() -> None:
    text = "just prose, no curly braces"
    assert _extract_json_block(text) is None


def test_extract_json_unbalanced() -> None:
    text = "{not actually balanced"
    # Should not infinite loop; returns None when unbalanced
    assert _extract_json_block(text) is None


# ============================================================
# llm_audit_capability — env-disabled
# ============================================================


async def test_llm_audit_skipped_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KUN_V7_AUDITOR_USE_LLM", raising=False)
    result = await llm_audit_capability(
        capability_id="cap-x",
        capability_name="x",
        design_promise="...",
        code_paths_to_audit=[],
        test_files_to_audit=[],
    )
    assert result is None


# ============================================================
# llm_audit_capability — factory unavailable
# ============================================================


async def test_llm_audit_skipped_when_no_ensemble(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KUN_V7_AUDITOR_USE_LLM", "true")

    # Factory returns None (e.g. KUN_V7_ENSEMBLE_ENABLED not set)
    monkeypatch.setattr(
        "kun.integration.ensemble_invoker_factory."
        "build_ensemble_invoker_from_settings",
        lambda **kwargs: None,
    )

    result = await llm_audit_capability(
        capability_id="cap-x",
        capability_name="x",
        design_promise="...",
        code_paths_to_audit=[],
        test_files_to_audit=[],
    )
    assert result is None


# ============================================================
# llm_audit_capability — LLM happy path
# ============================================================


class _CaptureSession:
    def __init__(self, sink: list[Any]) -> None:
        self._sink = sink

    def add(self, instance: Any) -> None:
        self._sink.append(instance)

    async def flush(self) -> None:
        return None


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch,
) -> list[Any]:
    added: list[Any] = []

    @asynccontextmanager
    async def fake_scope(**kwargs: Any) -> AsyncIterator[_CaptureSession]:
        yield _CaptureSession(added)

    monkeypatch.setattr("kun.core.db.session_scope", fake_scope)
    return added


async def test_llm_audit_happy_path_writes_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mock invoker returns valid JSON → row written, report_id returned."""
    monkeypatch.setenv("KUN_V7_AUDITOR_USE_LLM", "true")

    added = _install_fake_session(monkeypatch)

    # Fake invoker that returns JSON
    fake_response_content = '''{
  "design_promise": "Verify LLM auditor真 wired",
  "real_code_path": "kun/integration/auditor_report_llm.py",
  "bypass_methods": ["disable env var", "stub the LLM"],
  "min_repro_steps": "set KUN_V7_AUDITOR_USE_LLM=false",
  "risk_level": "P2",
  "must_fix": [],
  "acceptance_tests": ["test_llm_audit_happy_path_writes_row"],
  "allow_release": true,
  "rationale": "MF-AR-LLM wired, fallback chain intact"
}'''

    class _FakeStep:
        content = fake_response_content
        usage_tokens = 100
        cost_usd = 0.05

    async def _fake_invoker(messages: list[Any]) -> Any:
        return _FakeStep()

    def _fake_factory(**kwargs: Any) -> Any:
        return _fake_invoker

    monkeypatch.setattr(
        "kun.integration.ensemble_invoker_factory."
        "build_ensemble_invoker_from_settings",
        _fake_factory,
    )

    report_id = await llm_audit_capability(
        capability_id="cap-llm-happy",
        capability_name="MF-AR-LLM test",
        design_promise="real LLM auditor wired",
        code_paths_to_audit=["kun/integration/auditor_report_llm.py"],
        test_files_to_audit=["tests/unit/test_auditor_report_llm.py"],
        tenant_id="tenant-llm-test",
    )
    assert report_id is not None
    assert report_id.startswith("pr-")
    # Row was added
    ar_rows = [r for r in added if isinstance(r, AuditorReportRow)]
    assert len(ar_rows) == 1
    row = ar_rows[0]
    assert row.auditor_provider == "ensemble/v7-7-angle-attacker"
    assert row.risk_level == "P2"
    assert row.allow_release is True
    assert "MF-AR-LLM" in row.rationale or "auditor" in row.rationale.lower()


async def test_llm_audit_invalid_json_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM returns garbage → graceful None, no row written."""
    monkeypatch.setenv("KUN_V7_AUDITOR_USE_LLM", "true")
    added = _install_fake_session(monkeypatch)

    class _FakeStep:
        content = "I cannot comply with this request because"
        usage_tokens = 20
        cost_usd = 0.01

    async def _fake_invoker(messages: list[Any]) -> Any:
        return _FakeStep()

    monkeypatch.setattr(
        "kun.integration.ensemble_invoker_factory."
        "build_ensemble_invoker_from_settings",
        lambda **kwargs: _fake_invoker,
    )

    report_id = await llm_audit_capability(
        capability_id="cap-bad-json",
        capability_name="x",
        design_promise="...",
        code_paths_to_audit=[],
        test_files_to_audit=[],
    )
    assert report_id is None
    assert added == []  # no row written


async def test_llm_audit_invoker_raises_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM call raises → graceful None."""
    monkeypatch.setenv("KUN_V7_AUDITOR_USE_LLM", "true")

    async def _bad_invoker(messages: list[Any]) -> Any:
        raise RuntimeError("simulated LLM timeout")

    monkeypatch.setattr(
        "kun.integration.ensemble_invoker_factory."
        "build_ensemble_invoker_from_settings",
        lambda **kwargs: _bad_invoker,
    )

    report_id = await llm_audit_capability(
        capability_id="cap-timeout",
        capability_name="x",
        design_promise="...",
        code_paths_to_audit=[],
        test_files_to_audit=[],
    )
    assert report_id is None


async def test_llm_audit_p0_invariant_enforced_at_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLM returns P0 + allow_release=true → AuditorReport __post_init__ raises
    → graceful None."""
    monkeypatch.setenv("KUN_V7_AUDITOR_USE_LLM", "true")
    added = _install_fake_session(monkeypatch)

    fake_bad = '{"design_promise":"x","real_code_path":"y","risk_level":"P0","allow_release":true,"rationale":"violates V7 §16.6 invariant"}'

    class _FakeStep:
        content = fake_bad
        usage_tokens = 30
        cost_usd = 0.01

    async def _fake_invoker(messages: list[Any]) -> Any:
        return _FakeStep()

    monkeypatch.setattr(
        "kun.integration.ensemble_invoker_factory."
        "build_ensemble_invoker_from_settings",
        lambda **kwargs: _fake_invoker,
    )

    # Should NOT crash — AuditorReport.__post_init__ raises, llm_audit catches
    report_id = await llm_audit_capability(
        capability_id="cap-bad-p0",
        capability_name="x",
        design_promise="...",
        code_paths_to_audit=[],
        test_files_to_audit=[],
    )
    assert report_id is None
    assert added == []  # no row written
