"""Unit tests for kun.integration.external_supervisor adapter.

Verify ``make_external_supervisor_verify`` correctly wraps
``ExternalSupervisorService.analyze_observation`` into the
``ExternalSupervisorVerify`` callback shape that ``PlanReviewService`` expects.

We stub the service (instead of constructing the real one) because the real
``ExternalSupervisorService`` requires an ``LLMProvider`` and runs network I/O.
The adapter is a thin closure; behaviour we care about is argument forwarding
and closure semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from kun.integration.external_supervisor import make_external_supervisor_verify


class _StubService:
    """Stub of ExternalSupervisorService — captures call args, returns canned obs."""

    def __init__(
        self,
        *,
        verdict: str = "ok",
        rationale: str = "looks fine",
        raise_exc: BaseException | None = None,
    ) -> None:
        self.verdict = verdict
        self.rationale = rationale
        self.raise_exc = raise_exc
        self.calls: list[dict[str, Any]] = []

    async def analyze_observation(
        self,
        *,
        obs_kind: str,
        observation_payload: dict[str, Any],
        anchor: dict[str, Any] | None = None,
        target_task_id: str | None = None,
        target_anchor_id: str | None = None,
    ) -> SimpleNamespace:
        self.calls.append(
            {
                "obs_kind": obs_kind,
                "observation_payload": observation_payload,
                "anchor": anchor,
                "target_task_id": target_task_id,
                "target_anchor_id": target_anchor_id,
            }
        )
        if self.raise_exc is not None:
            raise self.raise_exc
        return SimpleNamespace(
            observation_id="ev_l-stub-1",
            observed_at=datetime.now(UTC),
            obs_kind=obs_kind,
            target_task_id=target_task_id,
            target_anchor_id=target_anchor_id,
            verdict=self.verdict,
            rationale=self.rationale,
            recommended_action=None,
            raw_llm_content="{}",
            model_used="stub",
            extras={},
        )


@pytest.mark.asyncio
async def test_happy_path_returns_observation_with_verdict_ok() -> None:
    """Callable returns observation with verdict='ok' on default stub."""
    service = _StubService(verdict="ok", rationale="all good")
    verify = make_external_supervisor_verify(service)  # type: ignore[arg-type]

    result = await verify({"step": 1}, {"goal_statement": "ship it"})

    assert result.verdict == "ok"
    assert result.rationale == "all good"
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_forwards_self_report_verbatim_to_observation_payload() -> None:
    """self_report dict is passed through unchanged as observation_payload."""
    service = _StubService()
    verify = make_external_supervisor_verify(service)  # type: ignore[arg-type]

    self_report = {
        "what_i_just_did": "ran tests",
        "anchor_alignment_score": 0.8,
        "deviations": [],
        "nested": {"foo": [1, 2, {"bar": "baz"}]},
    }
    await verify(self_report, None)

    assert len(service.calls) == 1
    # Same content AND same identity — we forward verbatim, no copy.
    assert service.calls[0]["observation_payload"] is self_report


@pytest.mark.asyncio
async def test_forwards_anchor_verbatim_to_analyze_observation() -> None:
    """anchor dict is passed through unchanged (including None)."""
    service = _StubService()
    verify = make_external_supervisor_verify(service)  # type: ignore[arg-type]

    anchor = {
        "goal_statement": "build adapter",
        "success_criteria": ["tests pass", "ruff clean"],
        "invariants": ["never modify plan_review_service.py"],
    }
    await verify({"x": 1}, anchor)
    await verify({"x": 2}, None)

    assert service.calls[0]["anchor"] is anchor
    assert service.calls[1]["anchor"] is None


@pytest.mark.asyncio
async def test_obs_kind_defaults_to_drift_check() -> None:
    """When obs_kind not specified at factory, it defaults to 'drift_check'."""
    service = _StubService()
    verify = make_external_supervisor_verify(service)  # type: ignore[arg-type]

    await verify({"step": 1}, None)

    assert service.calls[0]["obs_kind"] == "drift_check"


@pytest.mark.asyncio
async def test_custom_obs_kind_passes_through() -> None:
    """Custom obs_kind given at factory time is forwarded."""
    service = _StubService()
    verify = make_external_supervisor_verify(
        service,  # type: ignore[arg-type]
        obs_kind="task_complete",
    )

    await verify({"step": 1}, None)

    assert service.calls[0]["obs_kind"] == "task_complete"


@pytest.mark.asyncio
async def test_target_ids_close_over_correctly() -> None:
    """target_task_id / target_anchor_id from factory args close over."""
    service = _StubService()
    verify = make_external_supervisor_verify(
        service,  # type: ignore[arg-type]
        target_task_id="task_l-42",
        target_anchor_id="anc_l-7",
    )

    await verify({"step": 1}, None)

    assert service.calls[0]["target_task_id"] == "task_l-42"
    assert service.calls[0]["target_anchor_id"] == "anc_l-7"


@pytest.mark.asyncio
async def test_target_ids_default_to_none_when_not_bound() -> None:
    """When factory called without target ids, callable forwards None."""
    service = _StubService()
    verify = make_external_supervisor_verify(service)  # type: ignore[arg-type]

    await verify({"step": 1}, None)

    assert service.calls[0]["target_task_id"] is None
    assert service.calls[0]["target_anchor_id"] is None


@pytest.mark.asyncio
async def test_analyze_observation_raises_then_callable_raises() -> None:
    """If service raises, callable propagates — no swallowing.

    PlanReviewService already wraps in try/except — adapter must NOT hide errors.
    """
    boom = RuntimeError("local LLM down")
    service = _StubService(raise_exc=boom)
    verify = make_external_supervisor_verify(service)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="local LLM down"):
        await verify({"step": 1}, None)

    # Even on raise, we did record the attempt
    assert len(service.calls) == 1


@pytest.mark.asyncio
async def test_multiple_calls_reuse_factory_closure() -> None:
    """Closure not consumed — same callable handles many submit_self_report calls."""
    service = _StubService(verdict="concerning", rationale="meh")
    verify = make_external_supervisor_verify(
        service,  # type: ignore[arg-type]
        obs_kind="drift_check",
        target_task_id="task_l-stable",
        target_anchor_id="anc_l-stable",
    )

    results = []
    for i in range(5):
        result = await verify({"step": i}, {"goal_statement": "x"})
        results.append(result)

    assert len(service.calls) == 5
    # All five calls bound to the same target IDs from closure
    for call in service.calls:
        assert call["target_task_id"] == "task_l-stable"
        assert call["target_anchor_id"] == "anc_l-stable"
        assert call["obs_kind"] == "drift_check"
    # Per-call payload differs
    assert [c["observation_payload"]["step"] for c in service.calls] == [0, 1, 2, 3, 4]
    # All returns share verdict from stub (closure stable)
    assert all(r.verdict == "concerning" for r in results)


@pytest.mark.asyncio
async def test_returns_observation_object_unchanged() -> None:
    """Returned object is the exact one from analyze_observation (no rewrapping).

    PlanReviewService inspects ``.verdict`` / ``.rationale`` via getattr; we
    must not mutate or wrap.
    """
    service = _StubService(verdict="alarming", rationale="anchor broken")
    verify = make_external_supervisor_verify(service)  # type: ignore[arg-type]

    result = await verify({"step": 1}, None)

    assert result.verdict == "alarming"
    assert result.rationale == "anchor broken"
    # observation_id from stub flows through
    assert getattr(result, "observation_id", None) == "ev_l-stub-1"
    # obs_kind on the returned object matches what we asked for
    assert getattr(result, "obs_kind", None) == "drift_check"
