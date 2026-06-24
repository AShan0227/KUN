"""Prompt A/B test infrastructure — reuse RSI loop for prompt templates.

KUN 已有完整 RSI 闭环 (ADR-024) — Strategist 产候选 / Gate 准入 /
CapabilityRouter 选 provider tier. 本模块把同样基础设施扩展到 prompt 模板:

  StrategistService.propose_candidates  → 3 mode 候选 (conservative / aggressive
                                          / performance, sampling 0.3/0.5/1.0)
  GateService.admit                     → pass_rate ≥ 0.9 → 写 runtime_capabilities
  pick_active_variant                   → 主路径用; 读 capability rows, 选 sampling_rate
                                          最高的 enabled 行 (capability_score-driven)

Anomaly wrapping: ``anomaly_kind = "prompt_template_under_review"`` —
Strategist 当作未知 kind 不会自带 generator, 所以本模块自己 transform
Strategist 的 (anomaly_kind, target_module, evidence) → 3 个 PromptVariant.

DI:
  - StrategistService (Explorer Pool 3 模式) — propose flow
  - GateService — admit_variant_after_eval
  - capability_writer / capability_reader — 测试用 in-memory fake;
    生产用 kun.core.db session + RuntimeCapabilityRow
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from kun.agents.gate.service import GateDecision, GateService
from kun.agents.strategist.service import StrategistService, StrategyExperiment
from kun.core.ids import new_id
from kun.core.logging import get_logger

log = get_logger("kun.integration.prompt_ab")


# ---- Domain types ----


@dataclass(frozen=True)
class PromptTemplate:
    """A versioned prompt template for some purpose."""

    template_id: str  # 'pt-...'
    purpose: str  # 'intent' / 'planning' / 'execution' / 'critique' / etc
    system_prompt: str  # the actual template (含 {var} 占位符)
    variables: list[str] = field(default_factory=list)  # 占位符名
    rationale: str = ""  # 为什么这么写
    confidence: float = 0.5


@dataclass(frozen=True)
class PromptVariant:
    """Strategist 产的一个 variant.

    explorer_mode 决定 sampling_rate (与 StrategistService 第一条 RSI 实例对齐):
      conservative → 0.3 (canary 30%)
      aggressive   → 0.5 (canary 50%, 风险更大)
      performance  → 1.0 (shadow 100%, 不影响生产)
    """

    variant_id: str
    base_template_id: str
    purpose: str
    explorer_mode: Literal["conservative", "aggressive", "performance"]
    sampling_rate: float
    rollout_mode: Literal["canary", "shadow"]
    delta_description: str  # 与 base 的差异 (e.g. "加 anti-sycophancy 段")
    new_system_prompt: str
    rationale: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


CapabilityWriter = Callable[[dict[str, Any]], Awaitable[None]]
"""异步 writer — 写 runtime_capabilities row. 测试用 in-memory fake."""

CapabilityReader = Callable[[str], Awaitable[list[dict[str, Any]]]]
"""异步 reader — 按 target_module 查 runtime_capabilities. 测试用 in-memory fake.

返回 dict shape (mirror RuntimeCapabilityRow):
    {
        "capability_id": str,
        "target_module": str,
        "enabled": bool,
        "promotion_state": str,
        "sampling_rate": float,
        "change_summary": str,
        "metadata": dict (含 'new_system_prompt' / 'base_template_id' / 'purpose'),
    }
"""


# ---- Variant transforms (Conservative / Aggressive / Performance) ----


# Strategist's first RSI instance uses these sampling rates per explorer mode.
# We mirror that for prompt variants to keep the data-spine semantics aligned.
_MODE_SAMPLING: dict[str, float] = {
    "conservative": 0.3,
    "aggressive": 0.5,
    "performance": 1.0,
}

# Modes that ship as live canary traffic vs. shadow-only (no user-visible
# behavior change). Aligned with StrategistService — performance mode is
# always shadow because high sampling + uncertain delta is risky in production.
_MODE_ROLLOUT: dict[str, Literal["canary", "shadow"]] = {
    "conservative": "canary",
    "aggressive": "canary",
    "performance": "shadow",
}


def _conservative_delta(base: PromptTemplate) -> tuple[str, str]:
    """微调 — 加 1 段 anti-sycophancy footer."""
    suffix = (
        "\n\n[constraint] Stay objective. If the user is wrong, say so plainly. "
        "Do not flatter."
    )
    return (
        "add anti-sycophancy footer (1 段)",
        base.system_prompt.rstrip() + suffix,
    )


def _performance_delta(base: PromptTemplate) -> tuple[str, str]:
    """保守 + 加 reasoning step / 重试 hint."""
    suffix = (
        "\n\n[reasoning] Before answering, list 2-3 alternative interpretations "
        "of the user's intent. Pick the most likely; explicitly note the second-best "
        "so the user can correct."
    )
    return (
        "add reasoning-step preamble (列 2-3 个 alt interpretation)",
        base.system_prompt.rstrip() + suffix,
    )


def _aggressive_delta(base: PromptTemplate) -> tuple[str, str]:
    """重写关键段 — system prompt 顶部完全换 (保留变量段)."""
    # Keep variable references intact so the template stays valid for the loader.
    var_refs = " ".join(f"{{{v}}}" for v in base.variables) if base.variables else ""
    rewritten = (
        f"You are a precision-first assistant for purpose={base.purpose}. "
        f"Your job: produce the smallest correct answer. "
        f"No filler, no apologies, no '可能' hedges unless evidence is genuinely "
        f"missing. {var_refs}".rstrip()
    )
    return (
        "rewrite top section (precision-first, drop hedges)",
        rewritten,
    )


_MODE_TRANSFORMS: dict[
    str, Callable[[PromptTemplate], tuple[str, str]]
] = {
    "conservative": _conservative_delta,
    "performance": _performance_delta,
    "aggressive": _aggressive_delta,
}


# ---- Capability row helpers ----


def _capability_score(row: dict[str, Any]) -> float:
    """Order key for pick_active_variant.

    Mirrors capability_router.py's intuition: sampling_rate is a proxy for
    'how much production traffic the variant has earned'. A variant that
    survived to higher sampling_rate (e.g. 1.0 shadow / 0.5 canary) has more
    evidence behind it. We additionally read metadata.capability_score when
    set explicitly by upstream (capability_writeback math).
    """
    md = row.get("metadata") or {}
    explicit = md.get("capability_score")
    if isinstance(explicit, (int, float)):
        return float(explicit)
    return float(row.get("sampling_rate") or 0.0)


def _module_for_purpose(purpose: str) -> str:
    """target_module convention: 'prompt.<purpose>' — 与 Strategist
    self-referential check 不冲突 (purpose 不会以 strategist/director/supervisor/
    gate/external_supervisor 开头, caller 调 propose_variants 时如果 purpose
    误用监督前缀, Strategist 会自动转 human-review, 这是想要的行为).
    """
    return f"prompt.{purpose}"


# ---- Service ----


class PromptABService:
    """Drive prompt variants through the RSI loop (propose → eval → admit → pick)."""

    def __init__(
        self,
        *,
        strategist: StrategistService | None = None,
        gate: GateService | None = None,
        capability_writer: CapabilityWriter | None = None,
        capability_reader: CapabilityReader | None = None,
    ) -> None:
        self._strategist = strategist or StrategistService()
        self._gate = gate or GateService()
        self._capability_writer = capability_writer
        self._capability_reader = capability_reader

    # ---- step 1: propose ----

    async def propose_variants(
        self,
        base: PromptTemplate,
        anomaly_hint: dict[str, Any] | None = None,
    ) -> list[PromptVariant]:
        """Use Strategist Explorer Pool 3 modes to propose 3 variants.

        conservative: 微调 (加 1 段 anti-sycophancy footer)
        performance:  保守 + 加 reasoning step (列 alt interpretations)
        aggressive:   重写关键段 (system prompt 顶部精简版)

        Flow:
          1. Build a wrapped strategy_search_request with
             ``anomaly_kind = "prompt_template_under_review"``,
             ``target_module = prompt.<purpose>``, and (optionally) an
             ``anomaly_hint`` payload that downstream debriefs can inspect.
          2. Call ``StrategistService.propose_candidates(request)``. Stubs in
             tests return 3 mode shells directly; production deployments add a
             ``_CANDIDATE_GENERATORS["prompt_template_under_review"]`` entry
             so the live Strategist participates.
          3. Local fallback — if Strategist returns nothing (no generator
             registered yet), synthesize the 3 shells in-process and route
             them back through ``Strategist._emit_and_adjust`` so quotas /
             penalties / self-referential rewrites still apply. This keeps
             the public contract honest: Strategist owns the shaping, we own
             the prompt-specific transform.
          4. Transform StrategyExperiment → PromptVariant by applying the
             mode-specific text delta to ``base.system_prompt``.
        """
        target_module = _module_for_purpose(base.purpose)
        request: dict[str, Any] = {
            "anomaly_kind": "prompt_template_under_review",
            "target_module": target_module,
            "evidence": [
                {
                    "type": "prompt_a_b_request",
                    "base_template_id": base.template_id,
                    "purpose": base.purpose,
                },
            ],
        }
        if anomaly_hint:
            request["anomaly_hint"] = dict(anomaly_hint)
            # Also fold hint into evidence so a future generator can read it
            # via the standard evidence channel.
            request["evidence"].append({"type": "anomaly_hint", **anomaly_hint})

        adjusted = await self._strategist.propose_candidates(request)

        # Fallback path: Strategist has no generator for this anomaly_kind yet
        # (the standard case during rollout). Synthesize 3 shells and pipe
        # them through Strategist's adjust pipeline so quotas + self-ref
        # checks still run. We avoid duplicating the live RSI loop by only
        # taking this branch when adjusted is empty.
        if not adjusted:
            synthetic_experiments: list[StrategyExperiment] = []
            for mode in ("conservative", "aggressive", "performance"):
                sampling = _MODE_SAMPLING[mode]
                rollout = _MODE_ROLLOUT[mode]
                delta_desc, _new_prompt = _MODE_TRANSFORMS[mode](base)
                synthetic_experiments.append(
                    StrategyExperiment(
                        experiment_id=new_id("experiment_run"),
                        target_module=target_module,
                        target_level=2,
                        change_spec={
                            "kind": "prompt_variant",
                            "base_template_id": base.template_id,
                            "purpose": base.purpose,
                            "delta_description": delta_desc,
                        },
                        rollout_mode=rollout,
                        sampling_rate=sampling,
                        success_metric="prompt_pass_rate",
                        acceptance_threshold=0.9,
                        rollback_on=[
                            {
                                "metric": "task_failure_rate",
                                "operator": ">",
                                "value": 0.2,
                            },
                        ],
                        explorer_mode=mode,
                        rationale=(
                            f"prompt A/B for purpose={base.purpose} "
                            f"({mode}: {delta_desc})"
                        ),
                    )
                )
            # _emit_and_adjust is the shared private helper Strategist uses
            # internally; we reuse it intentionally so prompt A/B applies the
            # same self-referential / quota / penalty rules as production RSI.
            adjusted = await self._strategist._emit_and_adjust(
                synthetic_experiments,
                anomaly_kind="prompt_template_under_review",
            )

        # Transform StrategyExperiment → PromptVariant
        variants: list[PromptVariant] = []
        for exp in adjusted:
            if exp.explorer_mode not in _MODE_TRANSFORMS:
                # 'backward' rollback or unknown mode — skip (prompt A/B has
                # no rollback equivalent at this stage).
                continue
            delta_desc, new_prompt = _MODE_TRANSFORMS[exp.explorer_mode](base)
            md: dict[str, Any] = {
                "experiment_id": exp.experiment_id,
                "purpose": base.purpose,
                "base_template_id": base.template_id,
            }
            if anomaly_hint:
                md["anomaly_hint"] = dict(anomaly_hint)
            variants.append(
                PromptVariant(
                    variant_id=new_id("experiment_run"),
                    base_template_id=base.template_id,
                    purpose=base.purpose,
                    explorer_mode=exp.explorer_mode,  # type: ignore[arg-type]
                    sampling_rate=exp.sampling_rate,
                    rollout_mode=exp.rollout_mode,  # type: ignore[arg-type]
                    delta_description=delta_desc,
                    new_system_prompt=new_prompt,
                    rationale=exp.rationale,
                    metadata=md,
                )
            )

        log.info(
            "prompt_ab.variants_proposed",
            purpose=base.purpose,
            count=len(variants),
            modes=[v.explorer_mode for v in variants],
        )
        return variants

    # ---- step 2: gate admit after evaluation ----

    async def admit_variant_after_eval(
        self,
        variant: PromptVariant,
        eval_report: dict[str, Any],
        *,
        tenant_id: str = "default",
    ) -> GateDecision:
        """Run the eval_report through GateService.

        eval_report shape (caller responsibility):
          - pass_rate: float ∈ [0,1]
          - passed_count / total_count: optional ints
          - avg_quality_score: float ∈ [0,1] (mapped to debrief.evidence_quality_score)
          - cost_uplift: optional float (informational metadata)

        Gate decides:
          - approve   if pass_rate ≥ min_pass_rate (default 0.9) and quality OK
          - reject    otherwise
        On approve, the capability row payload carries the variant's new prompt
        in metadata so pick_active_variant can rehydrate it.
        """
        target_module = _module_for_purpose(variant.purpose)

        # Translate eval_report → Gate's expected (test_report, debrief).
        pass_rate = float(eval_report.get("pass_rate") or 0.0)
        total = int(
            eval_report.get("total_count")
            or eval_report.get("total")
            or 0
        )
        passed = int(
            eval_report.get("passed_count")
            or eval_report.get("passed")
            or round(pass_rate * total)
        )
        test_report = {
            "pass_rate": pass_rate,
            "passed_count": passed,
            "total_count": total,
        }
        avg_quality = float(eval_report.get("avg_quality_score") or 0.0)
        debrief = {
            "verdict": "ok" if pass_rate >= 0.9 else "concerning",
            "rationale": (
                f"prompt variant {variant.variant_id} pass_rate={pass_rate:.2f}"
            ),
            "evidence_quality_score": avg_quality,
        }

        # Build the experiment-shaped dict Gate consumes. We carry the new prompt
        # in change_spec so it lands in metadata.experiment_id-linked rows.
        experiment_for_gate: dict[str, Any] = {
            "experiment_id": variant.metadata.get("experiment_id")
            or variant.variant_id,
            "target_module": target_module,
            "change_spec": {
                "kind": "prompt_variant",
                "base_template_id": variant.base_template_id,
                "purpose": variant.purpose,
                "new_system_prompt": variant.new_system_prompt,
                "delta_description": variant.delta_description,
            },
            "rationale": variant.rationale or variant.delta_description,
            "explorer_mode": variant.explorer_mode,
            "sampling_rate": variant.sampling_rate,
            "rollback_on": [
                {"metric": "task_failure_rate", "operator": ">", "value": 0.2},
            ],
        }

        decision = await self._gate.admit(
            experiment_for_gate,
            test_report=test_report,
            debrief=debrief,
            tenant_id=tenant_id,
        )

        # On approve, persist the variant's full prompt + delta into the
        # capability row (Gate only writes its own framing). We layer our own
        # write on top so pick_active_variant can rehydrate. We do this only
        # when capability_writer is set AND Gate approved.
        if (
            decision.verdict == "approve"
            and self._capability_writer is not None
            and decision.capability_row_payload is not None
        ):
            row = dict(decision.capability_row_payload)
            md = dict(row.get("metadata") or {})
            md.update(
                {
                    "variant_id": variant.variant_id,
                    "base_template_id": variant.base_template_id,
                    "purpose": variant.purpose,
                    "new_system_prompt": variant.new_system_prompt,
                    "delta_description": variant.delta_description,
                    "explorer_mode": variant.explorer_mode,
                    "avg_quality_score": avg_quality,
                    "cost_uplift": eval_report.get("cost_uplift"),
                }
            )
            row["metadata"] = md
            # Enable immediately on approve — prompt A/B does not need the
            # full promotion_queue dance (no replay/shadow staging here;
            # caller's eval already took the role of replay+shadow tests).
            row["enabled"] = True
            row["promotion_state"] = "enabled"
            try:
                await self._capability_writer(row)
            except Exception as e:
                log.warning(
                    "prompt_ab.capability_write_failed",
                    error=str(e),
                    variant_id=variant.variant_id,
                )

        log.info(
            "prompt_ab.variant_admit_decided",
            variant_id=variant.variant_id,
            verdict=decision.verdict,
            pass_rate=pass_rate,
        )
        return decision

    # ---- step 3: pick active winner ----

    async def pick_active_variant(
        self,
        purpose: str,
        capability_reader: CapabilityReader | None = None,
    ) -> PromptTemplate | None:
        """Return the active winning variant for purpose, or None.

        Caller can pass a one-shot reader; otherwise we use the one injected at
        construction. We filter to (target_module = prompt.<purpose>, enabled=True)
        and pick the row with the highest capability_score (sampling_rate as
        proxy; metadata.capability_score overrides if set).
        """
        reader = capability_reader or self._capability_reader
        if reader is None:
            return None

        target_module = _module_for_purpose(purpose)
        try:
            rows = await reader(target_module)
        except Exception as e:
            log.warning(
                "prompt_ab.capability_read_failed",
                error=str(e),
                target_module=target_module,
            )
            return None

        enabled = [
            r
            for r in rows
            if r.get("enabled")
            and r.get("target_module") == target_module
        ]
        if not enabled:
            return None

        winner = max(enabled, key=_capability_score)
        md = winner.get("metadata") or {}
        new_prompt = md.get("new_system_prompt")
        if not isinstance(new_prompt, str):
            log.warning(
                "prompt_ab.winner_missing_prompt",
                capability_id=winner.get("capability_id"),
            )
            return None

        return PromptTemplate(
            template_id=str(md.get("variant_id") or winner.get("capability_id")),
            purpose=purpose,
            system_prompt=new_prompt,
            variables=list(md.get("variables") or []),
            rationale=str(md.get("delta_description") or winner.get("change_summary") or ""),
            confidence=float(_capability_score(winner)),
        )


# Re-export for symmetry with strategist/gate modules
def variant_as_dict(v: PromptVariant) -> dict[str, Any]:
    """Plain-dict view of a PromptVariant (for logging / persistence)."""
    return {
        "variant_id": v.variant_id,
        "base_template_id": v.base_template_id,
        "purpose": v.purpose,
        "explorer_mode": v.explorer_mode,
        "sampling_rate": v.sampling_rate,
        "rollout_mode": v.rollout_mode,
        "delta_description": v.delta_description,
        "new_system_prompt": v.new_system_prompt,
        "rationale": v.rationale,
        "metadata": dict(v.metadata),
    }


def template_with_rationale(
    base: PromptTemplate, rationale: str
) -> PromptTemplate:
    """Helper: tweak rationale without rebuilding the dataclass by hand."""
    return replace(base, rationale=rationale)


__all__ = [
    "CapabilityReader",
    "CapabilityWriter",
    "PromptABService",
    "PromptTemplate",
    "PromptVariant",
    "template_with_rationale",
    "variant_as_dict",
]
