"""Integration: ensemble_invoker → DB writer + ExecutorLoop adapter.

V7 Phase X.B.ENS. Tests both:
  1. make_ensemble_llm_invoker as LLMInvoker contract (drop-in for
     ExecutorLoop) — message conversion, consensus flowing through,
     XML-tool fallback, error path
  2. EnsembleCallRecord → ensemble_calls row + factory emitter
  3. End-to-end: invoker call → emitter fired with record built from
     EnsembleResponse

Uses StubProvider (same as test_ensemble.py) + fake session_scope.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

# Ensure builtin skills register at import time so XML-fallback parse_skill_calls
# recognizes names like "grep-verify" / "shell-exec" / "self-reflect" — import
# the modules directly (their `register("X", execute)` runs at import).
import kun.skills.builtin.grep_verify  # noqa: F401
import pytest
from kun.core.orm import EnsembleCallRow
from kun.integration.ensemble_invoker import (
    EnsembleCallRecord,
    build_call_record_from_response,
    make_ensemble_call_log_emitter,
    make_ensemble_llm_invoker,
    write_ensemble_call,
)
from kun.interface.llm.base import (
    LLMResponse,
    UsageInfo,
)
from kun.interface.llm.cross_family import CrossFamilyConfigError
from kun.interface.llm.ensemble import (
    ConsensusStrategy,
    EnsembleResponse,
)
from kun.interface.llm.stub_provider import StubProvider

pytestmark = pytest.mark.integration

# ============================================================
# Fake session helper (same pattern as previous Phase X.B test files)
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


# ============================================================
# Constructor — cross-family enforcement at construction is via
# ensemble_invoke at call time; this layer only checks N ≥ 2
# ============================================================


def test_invoker_rejects_lt_2_providers() -> None:
    p = StubProvider(model_id="claude-opus-4-7", tier="top")
    with pytest.raises(ValueError, match="≥ 2 providers"):
        make_ensemble_llm_invoker([p])
    with pytest.raises(ValueError, match="≥ 2 providers"):
        make_ensemble_llm_invoker([])


# ============================================================
# Happy path — 2 cross-family providers, default majority_vote
# ============================================================


async def test_invoker_returns_step_response_via_consensus() -> None:
    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="gpt-5.5", tier="top")
    invoker = make_ensemble_llm_invoker([p1, p2])

    step = await invoker([{"role": "user", "content": "hello"}])

    assert step.content  # StubProvider returns non-empty content
    assert isinstance(step.usage_tokens, int)
    assert step.usage_tokens >= 0


async def test_invoker_message_conversion_drops_executor_only_keys() -> None:
    """tool_calls / _kun_compacted / is_error stripped before LLMMessage build."""
    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="gpt-5.5", tier="top")
    invoker = make_ensemble_llm_invoker([p1, p2])

    # Should not raise despite extra keys
    step = await invoker(
        [
            {"role": "system", "content": "you are KUN"},
            {
                "role": "assistant",
                "content": "ok",
                "tool_calls": [{"id": "t1", "name": "foo", "arguments": {}}],
                "_kun_compacted": False,
            },
            {
                "role": "tool",
                "tool_id": "t1",
                "content": "result",
                "is_error": False,
            },
        ]
    )
    assert step.content is not None


# ============================================================
# Cross-family enforcement (delegated to ensemble_invoke)
# ============================================================


async def test_invoker_raises_on_same_family() -> None:
    """V7 §11.2: same-family pair → CrossFamilyConfigError at first invoke."""
    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="claude-haiku-4-5", tier="cheap")
    invoker = make_ensemble_llm_invoker(
        [p1, p2], require_cross_family=True
    )
    with pytest.raises(CrossFamilyConfigError):
        await invoker([{"role": "user", "content": "x"}])


async def test_invoker_same_family_allowed_when_opted_out() -> None:
    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="claude-haiku-4-5", tier="cheap")
    invoker = make_ensemble_llm_invoker(
        [p1, p2], require_cross_family=False
    )
    step = await invoker([{"role": "user", "content": "x"}])
    assert step.content


# ============================================================
# Call log emitter — record + DB write
# ============================================================


async def test_invoker_fires_call_log_emitter() -> None:
    captured: list[EnsembleCallRecord] = []

    async def _emit(record: EnsembleCallRecord) -> None:
        captured.append(record)

    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="gpt-5.5", tier="top")
    invoker = make_ensemble_llm_invoker(
        [p1, p2],
        consensus_strategy=ConsensusStrategy.MAJORITY_VOTE,
        purpose="execution",
        call_log_emitter=_emit,
    )

    await invoker([{"role": "user", "content": "hello"}])

    assert len(captured) == 1
    rec = captured[0]
    assert rec.call_id.startswith("enc-")
    assert rec.purpose == "execution"
    assert rec.n_providers_total == 2
    assert rec.consensus_strategy == "majority_vote"
    # Both stub providers succeed
    assert rec.failure_count == 0
    assert rec.divergence_score >= 0
    assert rec.divergence_score <= 1
    # providers metadata
    family_set = {p["family"] for p in rec.providers}
    assert len(family_set) >= 2  # cross-family
    assert {p["name"] for p in rec.providers}  # non-empty names
    # consensus_provider is one of the two
    assert rec.consensus_provider is not None


async def test_invoker_emitter_failure_does_not_break_agent() -> None:
    """call_log_emitter failure must be best-effort — agent flow continues."""

    async def _bad_emit(record: EnsembleCallRecord) -> None:
        raise RuntimeError("simulated DB down")

    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="gpt-5.5", tier="top")
    invoker = make_ensemble_llm_invoker(
        [p1, p2], call_log_emitter=_bad_emit
    )
    step = await invoker([{"role": "user", "content": "x"}])
    assert step.content  # invoker continued despite emit failure


async def test_invoker_request_hash_stable_across_same_messages() -> None:
    captured: list[EnsembleCallRecord] = []

    async def _emit(record: EnsembleCallRecord) -> None:
        captured.append(record)

    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="gpt-5.5", tier="top")
    invoker = make_ensemble_llm_invoker(
        [p1, p2], call_log_emitter=_emit
    )

    msgs = [{"role": "user", "content": "same message"}]
    await invoker(msgs)
    await invoker(msgs)

    assert len(captured) == 2
    assert captured[0].request_hash == captured[1].request_hash


async def test_invoker_request_hash_differs_across_different_messages() -> None:
    captured: list[EnsembleCallRecord] = []

    async def _emit(record: EnsembleCallRecord) -> None:
        captured.append(record)

    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="gpt-5.5", tier="top")
    invoker = make_ensemble_llm_invoker(
        [p1, p2], call_log_emitter=_emit
    )

    await invoker([{"role": "user", "content": "message-A"}])
    await invoker([{"role": "user", "content": "message-B"}])

    assert captured[0].request_hash != captured[1].request_hash


# ============================================================
# DB writer + Row roundtrip
# ============================================================


def _make_record(
    *,
    call_id: str = "enc-test-1",
    divergence_score: float = 0.2,
    failure_count: int = 0,
    n_total: int = 2,
    consensus_strategy: str = "majority_vote",
) -> EnsembleCallRecord:
    return EnsembleCallRecord(
        call_id=call_id,
        invoked_at=datetime.now(UTC),
        purpose="execution",
        providers=[
            {"name": "anthropic", "model_id": "claude-opus-4-7", "family": "anthropic"},
            {"name": "openai", "model_id": "gpt-5.5", "family": "openai"},
        ],
        consensus_strategy=consensus_strategy,
        divergence_score=divergence_score,
        divergence_signals=["content_jaccard=0.8"],
        consensus_provider="anthropic/claude-opus-4-7",
        total_cost_usd=0.012345,
        failure_count=failure_count,
        n_providers_total=n_total,
        request_hash="abcdef123",
    )


async def test_write_ensemble_call_persists_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)

    record = _make_record()
    returned_id = await write_ensemble_call(
        tenant_id="tenant-a", record=record
    )

    assert returned_id == record.call_id
    assert len(added) == 1
    row = added[0]
    assert isinstance(row, EnsembleCallRow)
    assert row.tenant_id == "tenant-a"
    assert row.call_id == record.call_id
    assert row.consensus_strategy == "majority_vote"
    assert float(row.divergence_score) == pytest.approx(0.2)
    assert row.n_providers_total == 2
    assert row.failure_count == 0
    assert scope_calls == [{"tenant_id": "tenant-a"}]


@pytest.mark.parametrize(
    "strategy",
    ["majority_vote", "weighted", "pick_best_by_metric"],
)
async def test_write_ensemble_call_all_strategies(
    monkeypatch: pytest.MonkeyPatch,
    strategy: str,
) -> None:
    added, _ = _install_fake_session(monkeypatch)
    record = _make_record(consensus_strategy=strategy)
    await write_ensemble_call(tenant_id="tenant-a", record=record)
    assert added[0].consensus_strategy == strategy


async def test_factory_make_ensemble_call_log_emitter_binds_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    added, scope_calls = _install_fake_session(monkeypatch)
    emit = make_ensemble_call_log_emitter("tenant-x")
    record = _make_record()
    await emit(record)
    assert len(added) == 1
    assert added[0].tenant_id == "tenant-x"
    assert scope_calls == [{"tenant_id": "tenant-x"}]


# ============================================================
# E2E: invoker + real make_ensemble_call_log_emitter → fake session
# ============================================================


async def test_invoker_with_db_emitter_writes_to_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: invoker call → DB emitter → fake session capture."""
    added, scope_calls = _install_fake_session(monkeypatch)

    p1 = StubProvider(model_id="claude-opus-4-7", tier="top")
    p2 = StubProvider(model_id="gpt-5.5", tier="top")
    invoker = make_ensemble_llm_invoker(
        [p1, p2],
        purpose="critique",
        call_log_emitter=make_ensemble_call_log_emitter("tenant-e2e"),
    )

    await invoker([{"role": "user", "content": "hello ensemble"}])

    assert len(added) == 1
    row = added[0]
    assert isinstance(row, EnsembleCallRow)
    assert row.tenant_id == "tenant-e2e"
    assert row.purpose == "critique"
    assert row.n_providers_total == 2
    assert row.failure_count == 0
    assert scope_calls == [{"tenant_id": "tenant-e2e"}]


# ============================================================
# build_call_record_from_response — split for testability
# ============================================================


def test_build_record_with_failures() -> None:
    """failure_count = n_providers_total - len(responses)."""
    # Build a synthetic EnsembleResponse with 1 success out of 2
    usage = UsageInfo(input_tokens=10, output_tokens=20)
    resp = LLMResponse(
        provider="anthropic",
        model="claude-opus-4-7",
        content="hi",
        tool_calls=[],
        finish_reason="stop",
        usage=usage,
        cost_usd_actual=0.001,
        cost_usd_equivalent=0.001,
    )
    ensemble_resp = EnsembleResponse(
        responses=[resp],  # 1 success
        consensus=resp,
        divergence_score=0.0,
        divergence_signals=[],
        usage=None,  # type: ignore[arg-type]
        consensus_strategy=ConsensusStrategy.MAJORITY_VOTE,
    )
    providers_meta = [
        {"name": "anthropic", "model_id": "claude-opus-4-7", "family": "anthropic"},
        {"name": "openai", "model_id": "gpt-5.5", "family": "openai"},
    ]
    record = build_call_record_from_response(
        ensemble_response=ensemble_resp,
        providers_meta=providers_meta,
        purpose="execution",
        request_hash="hash-x",
    )
    assert record.n_providers_total == 2
    assert record.failure_count == 1  # one missing
    assert record.consensus_provider == "anthropic/claude-opus-4-7"
    assert record.request_hash == "hash-x"


def test_build_record_no_consensus() -> None:
    """consensus=None edge case — record still built with consensus_provider=None."""
    ensemble_resp = EnsembleResponse(
        responses=[],
        consensus=None,
        divergence_score=1.0,
        divergence_signals=["all failed"],
        usage=None,  # type: ignore[arg-type]
        consensus_strategy=ConsensusStrategy.MAJORITY_VOTE,
    )
    record = build_call_record_from_response(
        ensemble_response=ensemble_resp,
        providers_meta=[
            {"name": "a", "model_id": "claude", "family": "anthropic"},
            {"name": "b", "model_id": "gpt-5", "family": "openai"},
        ],
        purpose="execution",
        request_hash=None,
    )
    assert record.consensus_provider is None
    assert record.consensus_content_excerpt == ""
    assert record.failure_count == 2
    assert record.divergence_score == 1.0


# ============================================================
# XML-tool fallback — codex / gpt-5.5 emit `<skill>` in content
# ============================================================


async def test_invoker_xml_fallback_for_skill_tags() -> None:
    """LT.TOOLS-GAP: codex-style XML tools in content → ExecToolCalls."""
    # StubProvider returns content matching ASK pattern but no tool_calls.
    # We construct a custom stub that returns XML.
    class _XMLStub(StubProvider):
        async def invoke(self, request: Any) -> LLMResponse:  # type: ignore[override]
            return LLMResponse(
                provider=self.name,
                model=self.model_id,
                # parse_skill_calls expects: <skill name="X">{json}</skill>
                content='<skill name="grep-verify">{"pattern": "foo"}</skill>',
                tool_calls=[],
                finish_reason="stop",
                usage=UsageInfo(input_tokens=5, output_tokens=10),
                cost_usd_actual=0.0,
                cost_usd_equivalent=0.0,
            )

    p1 = _XMLStub(model_id="claude-opus-4-7", tier="top")
    p2 = _XMLStub(model_id="gpt-5.5", tier="top")
    invoker = make_ensemble_llm_invoker([p1, p2])

    step = await invoker([{"role": "user", "content": "do something"}])

    assert step.tool_calls, "XML fallback should have produced tool_calls"
    assert step.tool_calls[0].name == "grep-verify"
    assert step.finish_reason == "tool_use"


# ============================================================
# Schema sanity
# ============================================================


def test_ensemble_call_row_has_schema_columns() -> None:
    cols = {c.name for c in EnsembleCallRow.__table__.columns}
    expected = {
        "tenant_id",
        "call_id",
        "invoked_at",
        "purpose",
        "providers",
        "consensus_strategy",
        "divergence_score",
        "divergence_signals",
        "consensus_provider",
        "total_cost_usd",
        "failure_count",
        "n_providers_total",
        "request_hash",
        "created_at",
    }
    assert expected.issubset(cols), (
        f"EnsembleCallRow 缺列: {expected - cols}"
    )


