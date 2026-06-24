"""Prompt A/B 测试基础设施单测 (kun.integration.prompt_ab).

Coverage:
  - propose_variants → 3 mode shells (conservative / aggressive / performance)
  - sampling_rate 与 Strategist 第一条 RSI 实例对齐 (0.3 / 0.5 / 1.0)
  - new_system_prompt 与 base.system_prompt 真实不同 (每个 mode 都有 delta)
  - admit_variant high pass_rate → approved + 写 capability row
  - admit_variant low pass_rate (< 0.9) → rejected
  - pick_active_variant → 取 sampling_rate 最高的 enabled 行
  - pick_active_variant → 没 enabled 行 → None
  - pick_active_variant 按 purpose filter (其他 purpose 的 row 不会选错)
  - propose_with_anomaly_hint 把 hint 透传到 variant.metadata
  - end-to-end: propose → admit → pick → 拿到新 variant
"""

from __future__ import annotations

from typing import Any

import pytest
from kun.agents.gate.service import GateService
from kun.agents.strategist.service import StrategistService, StrategyExperiment
from kun.core.ids import new_id
from kun.integration.prompt_ab import (
    PromptABService,
    PromptTemplate,
    PromptVariant,
    variant_as_dict,
)

# ---- fixtures / helpers ----


def _base_template(purpose: str = "intent") -> PromptTemplate:
    return PromptTemplate(
        template_id="pt-base-001",
        purpose=purpose,
        system_prompt=(
            "You are KUN's intent classifier. Read the user message and pick "
            "the best matching intent code. {context}"
        ),
        variables=["context"],
        rationale="baseline",
        confidence=0.5,
    )


class _FakeCapabilityStore:
    """In-memory fake for capability_writer + capability_reader.

    Mirrors the dict shape PromptABService writes via Gate; reader returns rows
    filtered by target_module (the same query PromptABService issues).
    """

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def writer(self, payload: dict[str, Any]) -> None:
        # Replace if same capability_id (simulate UPSERT)
        self.rows = [
            r for r in self.rows if r.get("capability_id") != payload.get("capability_id")
        ]
        self.rows.append(dict(payload))

    async def reader(self, target_module: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self.rows
            if r.get("target_module") == target_module
        ]


class _StubStrategist(StrategistService):
    """Stub that returns N synthesized StrategyExperiment shells regardless of
    request — keeps propose_variants deterministic in unit tests."""

    def __init__(self, *, modes: tuple[str, ...] | None = None) -> None:
        super().__init__()
        self._modes = modes or ("conservative", "aggressive", "performance")
        self.last_request: dict[str, Any] | None = None

    async def propose_candidates(
        self, request: dict[str, Any]
    ) -> list[StrategyExperiment]:
        self.last_request = dict(request)
        sampling_map = {"conservative": 0.3, "aggressive": 0.5, "performance": 1.0}
        rollout_map = {"conservative": "canary", "aggressive": "canary", "performance": "shadow"}
        target_module = str(request.get("target_module") or "prompt.unknown")
        return [
            StrategyExperiment(
                experiment_id=new_id("experiment_run"),
                target_module=target_module,
                target_level=2,
                change_spec={"kind": "prompt_variant"},
                rollout_mode=rollout_map[m],
                sampling_rate=sampling_map[m],
                success_metric="prompt_pass_rate",
                acceptance_threshold=0.9,
                rollback_on=[],
                explorer_mode=m,
                rationale=f"stub {m}",
            )
            for m in self._modes
        ]


# ---- propose_variants ----


async def test_propose_variants_returns_three_modes() -> None:
    svc = PromptABService(strategist=_StubStrategist())
    variants = await svc.propose_variants(_base_template())
    assert len(variants) == 3
    modes = {v.explorer_mode for v in variants}
    assert modes == {"conservative", "aggressive", "performance"}
    for v in variants:
        assert isinstance(v, PromptVariant)
        assert v.base_template_id == "pt-base-001"
        assert v.purpose == "intent"


async def test_propose_variants_sampling_rates_match_strategist_modes() -> None:
    """0.3 / 0.5 / 1.0 — same shape as StrategistService's first RSI instance."""
    svc = PromptABService(strategist=_StubStrategist())
    variants = await svc.propose_variants(_base_template())
    by_mode = {v.explorer_mode: v for v in variants}
    assert by_mode["conservative"].sampling_rate == pytest.approx(0.3)
    assert by_mode["aggressive"].sampling_rate == pytest.approx(0.5)
    assert by_mode["performance"].sampling_rate == pytest.approx(1.0)


async def test_propose_variants_rollout_mode_aligned_with_risk() -> None:
    """conservative/aggressive → canary, performance → shadow (matches RSI)."""
    svc = PromptABService(strategist=_StubStrategist())
    variants = await svc.propose_variants(_base_template())
    by_mode = {v.explorer_mode: v for v in variants}
    assert by_mode["conservative"].rollout_mode == "canary"
    assert by_mode["aggressive"].rollout_mode == "canary"
    assert by_mode["performance"].rollout_mode == "shadow"


async def test_propose_variants_new_prompt_differs_from_base() -> None:
    base = _base_template()
    svc = PromptABService(strategist=_StubStrategist())
    variants = await svc.propose_variants(base)
    for v in variants:
        assert v.new_system_prompt != base.system_prompt
        assert v.delta_description, "delta_description must explain the diff"


async def test_propose_variants_each_mode_has_distinct_delta() -> None:
    """Three modes must yield three distinct system prompts — otherwise we're
    not actually exploring."""
    svc = PromptABService(strategist=_StubStrategist())
    variants = await svc.propose_variants(_base_template())
    prompts = {v.explorer_mode: v.new_system_prompt for v in variants}
    assert len(set(prompts.values())) == 3


async def test_propose_variants_anomaly_hint_passes_through() -> None:
    svc = PromptABService(strategist=_StubStrategist())
    hint = {"hint_kind": "sycophancy_complaint", "rate": 0.18}
    variants = await svc.propose_variants(_base_template(), anomaly_hint=hint)
    assert all(v.metadata.get("anomaly_hint") == hint for v in variants)


async def test_propose_variants_wraps_request_for_strategist() -> None:
    stub = _StubStrategist()
    svc = PromptABService(strategist=stub)
    await svc.propose_variants(_base_template("planning"))
    assert stub.last_request is not None
    assert stub.last_request["anomaly_kind"] == "prompt_template_under_review"
    assert stub.last_request["target_module"] == "prompt.planning"


async def test_propose_variants_fallback_when_strategist_empty() -> None:
    """If Strategist has no generator for prompt_template_under_review,
    we still synthesize 3 shells locally so the A/B can start."""
    # Use a real StrategistService with no custom generator registered —
    # propose_candidates returns [] for unknown anomaly_kind, and our
    # fallback path takes over.
    svc = PromptABService(strategist=StrategistService())
    variants = await svc.propose_variants(_base_template())
    assert len(variants) == 3
    modes = {v.explorer_mode for v in variants}
    assert modes == {"conservative", "aggressive", "performance"}


# ---- admit_variant_after_eval ----


def _variant(mode: str = "conservative") -> PromptVariant:
    return PromptVariant(
        variant_id="er-variant-1",
        base_template_id="pt-base-001",
        purpose="intent",
        explorer_mode=mode,  # type: ignore[arg-type]
        sampling_rate=0.3,
        rollout_mode="canary",
        delta_description="add anti-sycophancy footer",
        new_system_prompt="You are KUN. [constraint] no flattery.",
        rationale="stub",
        metadata={"experiment_id": "er-variant-1"},
    )


async def test_admit_variant_high_pass_rate_approves_and_writes_row() -> None:
    store = _FakeCapabilityStore()
    svc = PromptABService(
        strategist=_StubStrategist(),
        gate=GateService(capability_writer=store.writer),
        capability_writer=store.writer,
    )
    variant = _variant()
    eval_report = {
        "pass_rate": 0.95,
        "passed_count": 19,
        "total_count": 20,
        "avg_quality_score": 0.85,
        "cost_uplift": 0.02,
    }
    decision = await svc.admit_variant_after_eval(variant, eval_report)
    assert decision.verdict == "approve"

    # Two writes: Gate's own (enabled=False, promotion_state=merged) +
    # our overlay (enabled=True with full metadata). Since the fake store
    # UPSERTs on capability_id, only the overlay remains.
    rows = [r for r in store.rows if r["target_module"] == "prompt.intent"]
    assert len(rows) == 1
    row = rows[0]
    assert row["enabled"] is True
    md = row["metadata"]
    assert md["variant_id"] == "er-variant-1"
    assert md["new_system_prompt"] == variant.new_system_prompt
    assert md["delta_description"] == "add anti-sycophancy footer"
    assert md["avg_quality_score"] == pytest.approx(0.85)
    assert md["cost_uplift"] == pytest.approx(0.02)


async def test_admit_variant_low_pass_rate_rejects() -> None:
    store = _FakeCapabilityStore()
    svc = PromptABService(
        strategist=_StubStrategist(),
        gate=GateService(capability_writer=store.writer),
        capability_writer=store.writer,
    )
    variant = _variant()
    eval_report = {
        "pass_rate": 0.70,  # below 0.9
        "passed_count": 7,
        "total_count": 10,
        "avg_quality_score": 0.6,
    }
    decision = await svc.admit_variant_after_eval(variant, eval_report)
    assert decision.verdict == "reject"
    # No enabled row should be written
    enabled_rows = [r for r in store.rows if r.get("enabled")]
    assert enabled_rows == []


async def test_admit_variant_borderline_passes_at_threshold() -> None:
    """pass_rate exactly 0.9 → approve (Gate uses >=)."""
    store = _FakeCapabilityStore()
    svc = PromptABService(
        strategist=_StubStrategist(),
        gate=GateService(capability_writer=store.writer),
        capability_writer=store.writer,
    )
    decision = await svc.admit_variant_after_eval(
        _variant(),
        {"pass_rate": 0.9, "passed_count": 9, "total_count": 10, "avg_quality_score": 0.6},
    )
    assert decision.verdict == "approve"


# ---- pick_active_variant ----


async def test_pick_active_variant_returns_highest_score() -> None:
    store = _FakeCapabilityStore()
    # Two enabled rows, different sampling_rate → highest wins
    await store.writer(
        {
            "tenant_id": "default",
            "capability_id": "cp-low",
            "target_module": "prompt.intent",
            "change_summary": "low",
            "enabled": True,
            "promotion_state": "enabled",
            "sampling_rate": 0.3,
            "rollback_on": [],
            "metadata": {
                "variant_id": "er-low",
                "new_system_prompt": "low-version-prompt",
                "purpose": "intent",
            },
        }
    )
    await store.writer(
        {
            "tenant_id": "default",
            "capability_id": "cp-high",
            "target_module": "prompt.intent",
            "change_summary": "high",
            "enabled": True,
            "promotion_state": "enabled",
            "sampling_rate": 1.0,
            "rollback_on": [],
            "metadata": {
                "variant_id": "er-high",
                "new_system_prompt": "high-version-prompt",
                "purpose": "intent",
            },
        }
    )

    svc = PromptABService(capability_reader=store.reader)
    winner = await svc.pick_active_variant("intent")
    assert winner is not None
    assert winner.template_id == "er-high"
    assert winner.system_prompt == "high-version-prompt"
    assert winner.purpose == "intent"


async def test_pick_active_variant_returns_none_when_no_enabled_row() -> None:
    store = _FakeCapabilityStore()
    # All rows disabled
    await store.writer(
        {
            "tenant_id": "default",
            "capability_id": "cp-disabled",
            "target_module": "prompt.intent",
            "change_summary": "off",
            "enabled": False,
            "promotion_state": "merged",
            "sampling_rate": 0.5,
            "rollback_on": [],
            "metadata": {"new_system_prompt": "x", "purpose": "intent"},
        }
    )
    svc = PromptABService(capability_reader=store.reader)
    winner = await svc.pick_active_variant("intent")
    assert winner is None


async def test_pick_active_variant_filters_by_purpose() -> None:
    """A row under prompt.planning must not leak into pick('intent')."""
    store = _FakeCapabilityStore()
    await store.writer(
        {
            "tenant_id": "default",
            "capability_id": "cp-planning",
            "target_module": "prompt.planning",
            "change_summary": "planning",
            "enabled": True,
            "promotion_state": "enabled",
            "sampling_rate": 1.0,
            "rollback_on": [],
            "metadata": {
                "variant_id": "er-planning",
                "new_system_prompt": "planning-prompt",
                "purpose": "planning",
            },
        }
    )

    svc = PromptABService(capability_reader=store.reader)
    assert await svc.pick_active_variant("intent") is None
    winner = await svc.pick_active_variant("planning")
    assert winner is not None
    assert winner.template_id == "er-planning"


async def test_pick_active_variant_no_reader_returns_none() -> None:
    svc = PromptABService()
    assert await svc.pick_active_variant("intent") is None


async def test_pick_active_variant_uses_explicit_capability_score_when_set() -> None:
    """metadata.capability_score should override sampling_rate ordering."""
    store = _FakeCapabilityStore()
    await store.writer(
        {
            "tenant_id": "default",
            "capability_id": "cp-A",
            "target_module": "prompt.intent",
            "change_summary": "A",
            "enabled": True,
            "promotion_state": "enabled",
            "sampling_rate": 1.0,  # higher sampling
            "rollback_on": [],
            "metadata": {
                "variant_id": "er-A",
                "new_system_prompt": "A-prompt",
                "capability_score": 0.4,  # but lower explicit score
                "purpose": "intent",
            },
        }
    )
    await store.writer(
        {
            "tenant_id": "default",
            "capability_id": "cp-B",
            "target_module": "prompt.intent",
            "change_summary": "B",
            "enabled": True,
            "promotion_state": "enabled",
            "sampling_rate": 0.3,  # lower sampling
            "rollback_on": [],
            "metadata": {
                "variant_id": "er-B",
                "new_system_prompt": "B-prompt",
                "capability_score": 0.9,  # but higher explicit score
                "purpose": "intent",
            },
        }
    )
    svc = PromptABService(capability_reader=store.reader)
    winner = await svc.pick_active_variant("intent")
    assert winner is not None
    assert winner.template_id == "er-B"


# ---- end-to-end ----


async def test_end_to_end_propose_admit_pick_returns_new_variant() -> None:
    """Full RSI loop: propose 3 → eval each → admit best → pick brings back winner."""
    store = _FakeCapabilityStore()
    svc = PromptABService(
        strategist=_StubStrategist(),
        gate=GateService(capability_writer=store.writer),
        capability_writer=store.writer,
        capability_reader=store.reader,
    )
    base = _base_template("intent")

    # Step 1: propose
    variants = await svc.propose_variants(base)
    assert len(variants) == 3

    # Step 2: admit each variant with different pass_rates.
    # Aggressive gets the highest pass_rate (0.97), conservative 0.91,
    # performance 0.93.
    eval_reports = {
        "conservative": {"pass_rate": 0.91, "passed_count": 91, "total_count": 100, "avg_quality_score": 0.7},
        "performance": {"pass_rate": 0.93, "passed_count": 93, "total_count": 100, "avg_quality_score": 0.8},
        "aggressive": {"pass_rate": 0.97, "passed_count": 97, "total_count": 100, "avg_quality_score": 0.85},
    }
    for variant in variants:
        decision = await svc.admit_variant_after_eval(
            variant, eval_reports[variant.explorer_mode]
        )
        assert decision.verdict == "approve"

    # Step 3: pick the winner — sampling_rate based, aggressive=0.5 is below
    # performance=1.0. Performance wins on score.
    winner = await svc.pick_active_variant("intent")
    assert winner is not None
    # The winning variant's system_prompt must be DIFFERENT from base
    assert winner.system_prompt != base.system_prompt
    # And it must come from one of the proposed variants
    proposed_prompts = {v.new_system_prompt for v in variants}
    assert winner.system_prompt in proposed_prompts


# ---- variant_as_dict round-trip ----


def test_variant_as_dict_round_trip() -> None:
    v = _variant("performance")
    d = variant_as_dict(v)
    assert d["variant_id"] == v.variant_id
    assert d["explorer_mode"] == "performance"
    assert d["sampling_rate"] == 0.3
    assert d["new_system_prompt"] == v.new_system_prompt
    # metadata is a dict copy, not aliasing
    assert d["metadata"] == v.metadata
    assert d["metadata"] is not v.metadata
