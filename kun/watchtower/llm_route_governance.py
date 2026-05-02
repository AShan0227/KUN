"""守望驱动的 LLM 路由治理 (BATCH4 C6 / T22).

这里只提供咨询层; router wire 由 Claude 主线接入.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, Literal

from kun.datamodel.decision_ticket import DecisionTicket, ticket_from_llm_route_governance
from kun.watchtower.engine import RuleEngine

RouteChangePhase = Literal["shadow", "canary", "stable", "rolled_back"]


@dataclass(frozen=True)
class ModelScore:
    model_id: str
    score: float
    reason: str = "capability_card"


@dataclass
class RouteChangeProposal:
    task_type: str
    from_model: str
    to_model: str
    reason: str
    phase: RouteChangePhase = "shadow"
    rollout_percent: float = 0.0
    fired_rules: list[str] = field(default_factory=list)


class LLMRouteGovernanceError(RuntimeError):
    """Base error for LLM route governance blocks."""


class CostExceededError(LLMRouteGovernanceError):
    """Raised when model selection would exceed the task cost ceiling."""


class ModelTrustError(LLMRouteGovernanceError):
    """Raised when no candidate model passes trust policy."""


class LLMRouteGovernor:
    """RuleEngine + capability_router 组合出的模型选择治理层.

    三层硬护栏在这里兜底:
    - privacy: 发给 RuleEngine 的 task_meta 先脱敏
    - cost: 超预算直接阻断
    - trust: 不信任模型从候选里剔除
    """

    def __init__(self, rule_engine: RuleEngine, capability_router: Any) -> None:
        self.rule_engine = rule_engine
        self.capability_router = capability_router
        self.route_change_proposals: list[RouteChangeProposal] = []

    async def consult_for_model_select(
        self,
        task_meta: dict[str, Any],
        candidate_models: list[str],
    ) -> str:
        """守望咨询: 在候选中按 capability_card 历史成功率重排."""

        if not candidate_models:
            raise ValueError("candidate_models must not be empty")

        await self._enforce_cost_ceiling(task_meta)
        trusted_candidates = await self._filter_trusted_models(task_meta, candidate_models)
        task_type = str(task_meta.get("task_type") or "unknown")
        scores = await self._score_candidates(task_type, trusted_candidates)
        best = _pick_best(scores, trusted_candidates)
        ticket = ticket_from_llm_route_governance(
            tenant_id=_tenant_id(task_meta),
            task_id=_task_id(task_meta),
            mission_id=_mission_id(task_meta),
            task_type=task_type,
            selected_model=best.model_id,
            candidate_models=trusted_candidates,
            original_candidate_models=candidate_models,
            status="selected",
            reason="Watchtower selected a trusted model by capability score.",
            risk_level=_risk_level(task_meta),
            estimated_cost_usd=_estimated_cost(task_meta),
            selected_score=best.score,
            score_reason=best.reason,
            evidence={"task_meta": _redact_for_event(task_meta)},
        )
        await self.rule_engine.evaluate(
            "llm.model_select.consulted",
            namespace={
                "task_type": task_type,
                "task_meta": _redact_for_event(task_meta),
                "candidate_models": trusted_candidates,
                "original_candidate_models": candidate_models,
                "selected_model": best.model_id,
                "selected_score": best.score,
                "score_reason": best.reason,
                "tenant_id": str(
                    task_meta.get("tenant_id") or task_meta.get("owner_tenant_id") or "unknown"
                ),
                "decision_ticket": ticket.event_payload(),
            },
        )
        return best.model_id

    async def _enforce_cost_ceiling(self, task_meta: dict[str, Any]) -> None:
        estimated = _first_float(
            task_meta,
            ("estimated_cost_usd", "cost_usd_estimate", "predicted_cost_usd", "estimated_cost"),
        )
        ceiling = _first_float(task_meta, ("cost_ceiling_usd", "max_cost_usd", "budget_usd"))
        if estimated is None or ceiling is None or estimated <= ceiling:
            return

        ticket = _blocked_route_ticket(
            task_meta=task_meta,
            reason="cost_ceiling",
            candidate_models=[],
            constraints=[f"estimated_cost_usd <= {ceiling}"],
            evidence={
                "estimated_cost_usd": estimated,
                "cost_ceiling_usd": ceiling,
            },
        )
        await self.rule_engine.evaluate(
            "llm.model_select.blocked",
            namespace={
                "reason": "cost_ceiling",
                "estimated_cost_usd": estimated,
                "cost_ceiling_usd": ceiling,
                "task_meta": _redact_for_event(task_meta),
                "tenant_id": str(
                    task_meta.get("tenant_id") or task_meta.get("owner_tenant_id") or "unknown"
                ),
                "decision_ticket": ticket.event_payload(),
            },
        )
        raise CostExceededError(f"estimated LLM cost {estimated} exceeds ceiling {ceiling}")

    async def _filter_trusted_models(
        self,
        task_meta: dict[str, Any],
        candidate_models: list[str],
    ) -> list[str]:
        distrusted = _string_set(task_meta.get("distrusted_models")) | _string_set(
            task_meta.get("blocked_models")
        )
        allowed = _string_set(task_meta.get("trusted_models") or task_meta.get("allowed_models"))
        model_trust = task_meta.get("model_trust")
        if isinstance(model_trust, dict):
            distrusted |= {str(model) for model, trusted in model_trust.items() if trusted is False}

        trusted = [
            model
            for model in candidate_models
            if model not in distrusted and (not allowed or model in allowed)
        ]
        if trusted:
            return trusted

        ticket = _blocked_route_ticket(
            task_meta=task_meta,
            reason="model_trust",
            candidate_models=candidate_models,
            constraints=["candidate model must pass trust policy"],
            evidence={
                "candidate_models": candidate_models,
                "distrusted_models": sorted(distrusted),
                "trusted_models": sorted(allowed),
            },
        )
        await self.rule_engine.evaluate(
            "llm.model_select.blocked",
            namespace={
                "reason": "model_trust",
                "candidate_models": candidate_models,
                "distrusted_models": sorted(distrusted),
                "trusted_models": sorted(allowed),
                "task_meta": _redact_for_event(task_meta),
                "tenant_id": str(
                    task_meta.get("tenant_id") or task_meta.get("owner_tenant_id") or "unknown"
                ),
                "decision_ticket": ticket.event_payload(),
            },
        )
        raise ModelTrustError("all candidate models are blocked by trust policy")

    async def trigger_route_change(
        self,
        task_type: str,
        from_model: str,
        to_model: str,
        reason: str,
    ) -> None:
        """规则触发换默认模型: 先进入 shadow, 后续由进化系统放量."""

        proposal = RouteChangeProposal(
            task_type=task_type,
            from_model=from_model,
            to_model=to_model,
            reason=reason,
            phase="shadow",
            rollout_percent=0.0,
        )
        ticket = ticket_from_llm_route_governance(
            tenant_id="system",
            task_id=f"route-policy:{task_type}",
            task_type=task_type,
            selected_model=to_model,
            candidate_models=[from_model, to_model],
            status="needs_review",
            reason=reason,
            from_model=from_model,
            to_model=to_model,
            rollout_phase=proposal.phase,
            constraints=["shadow-first rollout; no production switch without review"],
        )
        fired = await self.rule_engine.evaluate(
            "llm.route_change.proposed",
            namespace={
                "task_type": task_type,
                "from_model": from_model,
                "to_model": to_model,
                "reason": reason,
                "phase": proposal.phase,
                "decision_ticket": ticket.event_payload(),
            },
        )
        proposal.fired_rules = fired
        self.route_change_proposals.append(proposal)

    async def _score_candidates(
        self, task_type: str, candidate_models: list[str]
    ) -> list[ModelScore]:
        raw_scores = await _maybe_model_scores(self.capability_router, task_type, candidate_models)
        if raw_scores:
            return [
                ModelScore(model_id=model, score=float(raw_scores.get(model, 0.0)))
                for model in candidate_models
            ]

        scores: list[ModelScore] = []
        for model in candidate_models:
            score = await _maybe_score_model(self.capability_router, task_type, model)
            scores.append(
                ModelScore(
                    model_id=model,
                    score=score if score is not None else 0.0,
                    reason="capability_card" if score is not None else "no_history",
                ),
            )
        return scores


def _pick_best(scores: list[ModelScore], candidate_models: list[str]) -> ModelScore:
    order = {model: idx for idx, model in enumerate(candidate_models)}
    return max(scores, key=lambda item: (item.score, -order.get(item.model_id, 0)))


async def _maybe_model_scores(
    capability_router: Any,
    task_type: str,
    candidate_models: list[str],
) -> dict[str, float] | None:
    method = getattr(capability_router, "model_scores", None)
    if method is None:
        return None
    result = method(task_type, candidate_models)
    if isinstance(result, Awaitable):
        result = await result
    if isinstance(result, dict):
        return {str(key): float(value) for key, value in result.items()}
    return None


async def _maybe_score_model(
    capability_router: Any,
    task_type: str,
    model_id: str,
) -> float | None:
    method = getattr(capability_router, "score_model", None)
    if method is None:
        return None
    result = method(task_type, model_id)
    if isinstance(result, Awaitable):
        result = await result
    if result is None:
        return None
    return float(result)


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
_TOKEN_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*[A-Za-z0-9_\-./+=]{8,}"
)


def _redact_for_event(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _redact_for_event(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_for_event(item) for item in value]
    if isinstance(value, str):
        redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", value)
        return _TOKEN_RE.sub(lambda match: f"{match.group(1)}=[REDACTED_SECRET]", redacted)
    return value


def _blocked_route_ticket(
    *,
    task_meta: dict[str, Any],
    reason: str,
    candidate_models: list[str],
    constraints: list[str],
    evidence: dict[str, Any],
) -> DecisionTicket:
    return ticket_from_llm_route_governance(
        tenant_id=_tenant_id(task_meta),
        task_id=_task_id(task_meta),
        mission_id=_mission_id(task_meta),
        task_type=str(task_meta.get("task_type") or "unknown"),
        selected_model="blocked",
        candidate_models=candidate_models,
        status="blocked",
        reason=f"Watchtower blocked LLM model selection: {reason}",
        risk_level=_risk_level(task_meta),
        estimated_cost_usd=_estimated_cost(task_meta),
        constraints=constraints,
        evidence={"task_meta": _redact_for_event(task_meta), **evidence},
        metadata={"block_reason": reason},
    )


def _tenant_id(task_meta: dict[str, Any]) -> str:
    return str(task_meta.get("tenant_id") or task_meta.get("owner_tenant_id") or "unknown")


def _task_id(task_meta: dict[str, Any]) -> str:
    return str(
        task_meta.get("task_id")
        or task_meta.get("task_ref")
        or task_meta.get("id")
        or f"route:{task_meta.get('task_type') or 'unknown'}"
    )


def _mission_id(task_meta: dict[str, Any]) -> str | None:
    mission_id = task_meta.get("mission_id")
    return str(mission_id) if mission_id else None


def _risk_level(task_meta: dict[str, Any]) -> str:
    return str(task_meta.get("risk_level") or task_meta.get("risk") or "low")


def _estimated_cost(task_meta: dict[str, Any]) -> float | None:
    return _first_float(
        task_meta,
        ("estimated_cost_usd", "cost_usd_estimate", "predicted_cost_usd", "estimated_cost"),
    )


def _first_float(task_meta: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = task_meta.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _string_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, list | tuple | set):
        return {str(item) for item in value}
    return set()


__all__ = [
    "CostExceededError",
    "LLMRouteGovernanceError",
    "LLMRouteGovernor",
    "ModelScore",
    "ModelTrustError",
    "RouteChangePhase",
    "RouteChangeProposal",
]
