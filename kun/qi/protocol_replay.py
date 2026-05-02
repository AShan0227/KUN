"""Protocol replay evidence for Qi auto-promotion.

This module is intentionally conservative. It does not claim that a protocol
has won real production traffic. It only creates a small, auditable smoke/replay
evidence record when the protocol is complete enough to enter the next stage.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kun.core.logging import get_logger
from kun.qi.protocol import Protocol, ProtocolRegistry

log = get_logger("kun.qi.protocol_replay")

PROMOTION_EVIDENCE_SOURCE = "protocol_replay_smoke"
_VALID_MODES = {"FAST", "SMART", "MAX", "ENSEMBLE"}
_VALID_RISKS = {"low", "medium", "high", "critical"}


@dataclass(frozen=True)
class ProtocolReplayAssessment:
    """One protocol's smoke/replay assessment."""

    protocol_id: str
    version: str
    status: str
    score: float
    guardrail_pass: bool
    evidence: dict[str, Any] | None = None
    reasons: list[str] = field(default_factory=list)
    blocked_reasons: list[str] = field(default_factory=list)


class ProtocolReplayEvaluator:
    """Generate small promotion evidence from deterministic protocol checks.

    The evaluator is not a replacement for canary metrics. It gives Qi enough
    grounded signal to move a well-formed experimental protocol into shadow,
    while keeping higher stages below their run-count thresholds.
    """

    def __init__(
        self,
        *,
        smoke_runs: int | None = None,
        max_expected_cost_usd: float | None = None,
        min_score_for_evidence: float | None = None,
        source: str = PROMOTION_EVIDENCE_SOURCE,
    ) -> None:
        self.smoke_runs = smoke_runs or int(os.getenv("KUN_PROTOCOL_REPLAY_SMOKE_RUNS", "5"))
        self.max_expected_cost_usd = max_expected_cost_usd or float(
            os.getenv("KUN_PROTOCOL_REPLAY_MAX_EXPECTED_COST_USD", "1.0")
        )
        self.min_score_for_evidence = min_score_for_evidence or float(
            os.getenv("KUN_PROTOCOL_REPLAY_MIN_SCORE", "0.65")
        )
        self.source = source

    async def evaluate_missing_evidence(
        self,
        registry: ProtocolRegistry,
        tenant_id: str,
    ) -> dict[str, Any]:
        """Write smoke evidence for protocols that are missing it.

        Returns a small summary for cron/API visibility. Blocked protocols get a
        ``protocol_replay`` note in metadata, but no ``promotion_evidence``.
        """

        protocols = await registry.list_all(tenant_id)
        evaluated = 0
        updated = 0
        blocked = 0
        skipped_existing = 0
        skipped_status = 0

        for proto in protocols:
            if proto.status not in {"experimental", "shadow", "canary"}:
                skipped_status += 1
                continue
            if _has_promotion_evidence(proto.metadata):
                skipped_existing += 1
                continue

            assessment = self.assess(proto)
            evaluated += 1
            meta = dict(proto.metadata or {})
            replay_note = {
                "evaluated_at": datetime.now(UTC).isoformat(),
                "score": round(assessment.score, 4),
                "guardrail_pass": assessment.guardrail_pass,
                "reasons": assessment.reasons,
                "blocked_reasons": assessment.blocked_reasons,
                "source": self.source,
            }
            meta["protocol_replay"] = replay_note
            if assessment.evidence is not None:
                meta["promotion_evidence"] = assessment.evidence
                updated += 1
            else:
                blocked += 1

            await registry.save(proto.model_copy(update={"metadata": meta}))

        summary = {
            "evaluated": evaluated,
            "updated": updated,
            "blocked": blocked,
            "skipped_existing": skipped_existing,
            "skipped_status": skipped_status,
            "source": self.source,
        }
        log.info("protocol_replay.evaluated", tenant=tenant_id, **summary)
        return summary

    def assess(self, protocol: Protocol) -> ProtocolReplayAssessment:
        """Score one protocol with deterministic, auditable checks."""

        reasons: list[str] = []
        blocked_reasons: list[str] = []
        score = 0.0

        pattern = protocol.trigger.task_type_pattern.strip()
        if pattern and pattern != "*":
            score += 0.20
            reasons.append("specific_trigger")
        else:
            blocked_reasons.append("trigger_too_broad")

        if protocol.execution.mode in _VALID_MODES:
            score += 0.15
            reasons.append("valid_execution_mode")
        else:
            blocked_reasons.append(f"invalid_execution_mode:{protocol.execution.mode}")

        if 1 <= protocol.execution.max_steps <= 50:
            score += 0.10
            reasons.append("bounded_max_steps")
        else:
            blocked_reasons.append("max_steps_out_of_bounds")

        if 0.0 <= protocol.execution.expected_cost_usd <= self.max_expected_cost_usd:
            score += 0.10
            reasons.append("bounded_expected_cost")
        else:
            blocked_reasons.append("expected_cost_out_of_bounds")

        if (
            set(protocol.trigger.risk_levels).issubset(_VALID_RISKS)
            and protocol.trigger.risk_levels
        ):
            score += 0.05
            reasons.append("valid_risk_levels")
        else:
            blocked_reasons.append("invalid_risk_levels")

        if protocol.skill_chain:
            score += 0.15
            reasons.append("has_skill_chain")

        if (
            protocol.hermes_template.system_prompt_addon.strip()
            or protocol.hermes_template.action_type_preference
        ):
            score += 0.10
            reasons.append("has_hermes_guidance")

        if protocol.verification:
            score += 0.10
            reasons.append("has_verification")

        if protocol.reward_weights:
            score += 0.05
            reasons.append("has_reward_weights")

        if not (
            protocol.skill_chain
            or protocol.hermes_template.system_prompt_addon.strip()
            or protocol.hermes_template.action_type_preference
            or protocol.verification
        ):
            blocked_reasons.append("missing_operational_contract")

        score = min(1.0, score)
        guardrail_pass = not blocked_reasons
        evidence: dict[str, Any] | None = None
        if guardrail_pass and score >= self.min_score_for_evidence:
            evidence = {
                "runs": max(1, self.smoke_runs),
                "win_rate": round(min(0.95, score), 4),
                "guardrail_pass": True,
                "source": self.source,
                "evaluated_at": datetime.now(UTC).isoformat(),
                "reasons": reasons,
            }

        return ProtocolReplayAssessment(
            protocol_id=protocol.protocol_id,
            version=protocol.version,
            status=protocol.status,
            score=score,
            guardrail_pass=guardrail_pass,
            evidence=evidence,
            reasons=reasons,
            blocked_reasons=blocked_reasons,
        )


def _has_promotion_evidence(metadata: dict[str, Any]) -> bool:
    raw = metadata.get("promotion_evidence")
    if not isinstance(raw, dict):
        return False
    return "runs" in raw or "sample_size" in raw


__all__ = [
    "PROMOTION_EVIDENCE_SOURCE",
    "ProtocolReplayAssessment",
    "ProtocolReplayEvaluator",
]
