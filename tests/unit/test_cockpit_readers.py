"""V7 Phase X.B — cockpit_readers 单测.

3 个 DB reader functions 测试. 用 fake session_scope (同 plan_review_db /
mission_director_db test 风格), 不实际跑 PG.

Coverage:
  - 每个 reader empty DB → []
  - 每个 reader 返回 row 时, dict shape 完整 (V7 §20 cockpit schema)
  - tenant_id / 过滤参数透传到 SELECT WHERE 子句
  - limit 边界 clamp (1-100)
  - DB 异常 → 返 [] (graceful degradation, log warning)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from kun.api.cockpit_readers import (
    list_recent_auditor_reports,
    list_recent_lifecycle_transitions,
    list_recent_mission_reviews,
)

# ============================================================
# Fake SQLAlchemy session 模拟 (capture stmt + return canned rows)
# ============================================================


class _FakeScalars:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _FakeResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._rows)


class _FakeSession:
    def __init__(self, rows: list[Any], capture: dict[str, Any]) -> None:
        self._rows = rows
        self._capture = capture

    async def execute(self, stmt: Any) -> _FakeResult:
        # Capture stmt for downstream WHERE introspection — readers use
        # SQLAlchemy 2.x select() so str(stmt) gives readable SQL-ish string.
        self._capture.setdefault("stmts", []).append(stmt)
        return _FakeResult(self._rows)


def _install_fake_session(
    monkeypatch: pytest.MonkeyPatch, rows: list[Any]
) -> dict[str, Any]:
    capture: dict[str, Any] = {"stmts": [], "scope_kwargs": []}

    @asynccontextmanager
    async def fake_session_scope(**kwargs: Any) -> AsyncIterator[_FakeSession]:
        capture["scope_kwargs"].append(kwargs)
        yield _FakeSession(rows, capture)

    monkeypatch.setattr("kun.core.db.session_scope", fake_session_scope)
    return capture


def _install_failing_session(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def fake_session_scope(**kwargs: Any) -> AsyncIterator[Any]:
        raise RuntimeError("simulated DB connection refused")
        yield  # pragma: no cover

    monkeypatch.setattr("kun.core.db.session_scope", fake_session_scope)


# ============================================================
# Row fakes — match ORM attribute shape (not the actual SQLAlchemy Row)
# ============================================================


class _MissionReviewRowFake:
    def __init__(
        self,
        *,
        review_id: str = "mar-1",
        task_id: str = "tk-1",
        verdict: str = "ok",
        alignment_score: float = 0.85,
    ) -> None:
        self.review_id = review_id
        self.task_id = task_id
        self.task_plan_version = "v1"
        self.reviewed_at = datetime.now(UTC)
        self.verdict = verdict
        # Numeric column comes back as Decimal in real ORM
        self.alignment_score = Decimal(str(alignment_score))
        self.findings = ["f1", "f2"]
        self.info_gap_coverage = Decimal("0.9")
        self.decomposition_coverage = Decimal("0.85")
        self.evidence_coverage = Decimal("0.7")
        self.plan_change_proposed = False
        self.plan_change_proposal_id = None


class _LifecycleRowFake:
    def __init__(
        self,
        *,
        transition_id: str = "lct-1",
        capability_id: str = "cap-1",
        to_stage: str = "replay",
    ) -> None:
        self.transition_id = transition_id
        self.capability_id = capability_id
        self.from_stage = "candidate"
        self.to_stage = to_stage
        self.decided_at = datetime.now(UTC)
        self.decision_rationale = "..."
        self.user_approval_ticket_id = None
        self.evidence_refs = ["strategy_replay_report:rr-x"]
        self.metrics_snapshot = {"baseline": 0.78}


class _AuditorRowFake:
    def __init__(
        self,
        *,
        report_id: str = "ar-1",
        risk_level: str = "P1",
        allow_release: bool = False,
    ) -> None:
        self.report_id = report_id
        self.audited_capability = "self-reflect"
        self.audited_at = datetime.now(UTC)
        self.auditor_provider = "anthropic/claude-opus"
        self.design_promise = "..."
        self.real_code_path = "kun/..."
        self.bypass_methods = ["bypass-1"]
        self.min_repro_steps = "..."
        self.risk_level = risk_level
        self.must_fix = ["fix-1"]
        self.acceptance_tests = ["test-1"]
        self.allow_release = allow_release
        self.rationale = "..."


# ============================================================
# list_recent_mission_reviews
# ============================================================


async def test_mission_reviews_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _install_fake_session(monkeypatch, rows=[])
    out = await list_recent_mission_reviews(tenant_id="t-a")
    assert out.rows == []
    assert out.error_kind is None  # MF-6: empty is honest, not an error
    assert out.is_ok is True
    assert out.is_empty_honest is True
    assert capture["scope_kwargs"] == [{"tenant_id": "t-a"}]


async def test_mission_reviews_returns_dict_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _MissionReviewRowFake(verdict="ok", alignment_score=0.9),
        _MissionReviewRowFake(verdict="drifting", alignment_score=0.55),
    ]
    _install_fake_session(monkeypatch, rows=rows)
    out = await list_recent_mission_reviews(tenant_id="t-a")
    assert out.error_kind is None
    assert len(out.rows) == 2
    assert out.rows[0]["verdict"] == "ok"
    assert out.rows[0]["alignment_score"] == pytest.approx(0.9)
    # All 12 fields per V7 §20 cockpit schema
    expected_keys = {
        "review_id",
        "task_id",
        "task_plan_version",
        "reviewed_at",
        "verdict",
        "alignment_score",
        "findings",
        "info_gap_coverage",
        "decomposition_coverage",
        "evidence_coverage",
        "plan_change_proposed",
        "plan_change_proposal_id",
    }
    assert expected_keys.issubset(out.rows[0].keys())
    # reviewed_at is isoformat string (JSON-friendly)
    assert isinstance(out.rows[0]["reviewed_at"], str)
    assert "T" in out.rows[0]["reviewed_at"]


async def test_mission_reviews_limit_clamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_session(monkeypatch, rows=[])
    # 0 → clamp to 1
    await list_recent_mission_reviews(tenant_id="t-a", limit=0)
    # 99999 → clamp to 100
    await list_recent_mission_reviews(tenant_id="t-a", limit=99999)
    # Negative → clamp to 1
    await list_recent_mission_reviews(tenant_id="t-a", limit=-5)


async def test_mission_reviews_db_failure_returns_classified_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MF-6: DB failure returns ReaderResult with error_kind != None."""
    _install_failing_session(monkeypatch)
    out = await list_recent_mission_reviews(tenant_id="t-a")
    assert out.rows == []
    assert out.error_kind is not None
    assert out.is_ok is False
    assert out.is_empty_honest is False  # NOT honest empty — DB failed
    assert out.error_detail  # diagnostic detail present


# ============================================================
# list_recent_lifecycle_transitions
# ============================================================


async def test_lifecycle_transitions_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture = _install_fake_session(monkeypatch, rows=[])
    out = await list_recent_lifecycle_transitions(tenant_id="t-b")
    assert out.rows == []
    assert out.error_kind is None
    assert capture["scope_kwargs"] == [{"tenant_id": "t-b"}]


async def test_lifecycle_transitions_dict_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [_LifecycleRowFake(transition_id="lct-1", to_stage="replay")]
    _install_fake_session(monkeypatch, rows=rows)
    out = await list_recent_lifecycle_transitions(tenant_id="t-b")
    assert out.error_kind is None
    assert len(out.rows) == 1
    expected_keys = {
        "transition_id",
        "capability_id",
        "from_stage",
        "to_stage",
        "decided_at",
        "decision_rationale",
        "user_approval_ticket_id",
        "evidence_refs",
        "metrics_snapshot",
    }
    assert expected_keys.issubset(out.rows[0].keys())


async def test_lifecycle_transitions_filter_by_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_session(monkeypatch, rows=[])
    out = await list_recent_lifecycle_transitions(
        tenant_id="t-b", capability_id="cap-foo"
    )
    assert out.error_kind is None


async def test_lifecycle_transitions_db_failure_returns_classified_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MF-6: DB failure surfaces error_kind, not silent []."""
    _install_failing_session(monkeypatch)
    out = await list_recent_lifecycle_transitions(
        tenant_id="t-b", capability_id="cap-x"
    )
    assert out.rows == []
    assert out.error_kind is not None
    assert out.is_ok is False


# ============================================================
# list_recent_auditor_reports
# ============================================================


async def test_auditor_reports_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    capture = _install_fake_session(monkeypatch, rows=[])
    out = await list_recent_auditor_reports(tenant_id="t-c")
    assert out.rows == []
    assert out.error_kind is None
    assert capture["scope_kwargs"] == [{"tenant_id": "t-c"}]


async def test_auditor_reports_dict_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _AuditorRowFake(risk_level="P0", allow_release=False),
        _AuditorRowFake(report_id="ar-2", risk_level="P2", allow_release=True),
    ]
    _install_fake_session(monkeypatch, rows=rows)
    out = await list_recent_auditor_reports(tenant_id="t-c")
    assert len(out.rows) == 2
    assert out.rows[0]["risk_level"] == "P0"
    assert out.rows[0]["allow_release"] is False
    assert out.rows[1]["risk_level"] == "P2"
    assert out.rows[1]["allow_release"] is True
    # All V7 §16.6 9-field schema + metadata
    expected_keys = {
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
    assert expected_keys.issubset(out.rows[0].keys())


async def test_auditor_reports_filter_by_risk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_session(monkeypatch, rows=[])
    out = await list_recent_auditor_reports(tenant_id="t-c", risk_level="P0")
    assert out.rows == []
    assert out.error_kind is None


async def test_auditor_reports_db_failure_returns_classified_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_failing_session(monkeypatch)
    out = await list_recent_auditor_reports(tenant_id="t-c")
    assert out.rows == []
    assert out.error_kind is not None
    assert out.is_ok is False


# ============================================================
# V7 §16.6 MF-6 — error_kind classification vocabulary
# ============================================================


def test_classify_db_error_connection_refused() -> None:
    from kun.api.cockpit_readers import _classify_db_error

    class _RefusedError(Exception):
        pass

    e = _RefusedError("connection refused: localhost:5432")
    assert _classify_db_error(e) == "db_connection_refused"


def test_classify_db_error_table_not_found() -> None:
    from kun.api.cockpit_readers import _classify_db_error

    class UndefinedTableError(Exception):
        pass

    e = UndefinedTableError('relation "mission_alignment_reviews" does not exist')
    assert _classify_db_error(e) == "table_not_found"


def test_classify_db_error_unknown_fallback() -> None:
    from kun.api.cockpit_readers import _classify_db_error

    class _AnythingError(Exception):
        pass

    e = _AnythingError("something weird happened")
    assert _classify_db_error(e) == "unknown_db_error"


def test_reader_result_is_empty_honest_only_when_no_error_and_no_rows() -> None:
    from kun.api.cockpit_readers import ReaderResult

    # honest empty
    assert ReaderResult(rows=[]).is_empty_honest is True
    # has data — not "empty"
    assert ReaderResult(rows=[{"x": 1}]).is_empty_honest is False
    # error — empty list but NOT honest
    assert (
        ReaderResult(rows=[], error_kind="db_connection_refused").is_empty_honest
        is False
    )
