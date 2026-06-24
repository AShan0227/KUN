"""V7 §11.4 ensemble_invoke → ExecutorLoop.LLMInvoker adapter (Phase X.B.ENS).

This is the drop-in replacement for ``make_llm_invoker`` (kun/integration/
llm_invoker.py) that calls ``ensemble_invoke`` across ≥ 2 cross-family providers
instead of a single router.invoke().

Key design (V7 §11.4 / §11.2):

  - **Drop-in shape**: returns the same ``LLMInvoker`` callable
    (``Callable[[list[dict]], Awaitable[LLMStepResponse]]``) so it can be
    swapped into ``LongTaskOrchestrator(llm_invoker=...)`` without touching
    the orchestrator surface.
  - **Cross-family enforced**: ``require_cross_family=True`` by default per
    V7 §11.2; raise CrossFamilyConfigError at construction time if violated.
  - **Consensus → LLMStepResponse**: the consensus response (or first
    successful response as fallback) is what flows into ExecutorLoop. The
    other responses + divergence info go to **the call log emitter** for
    cockpit visibility, not into the agent's main flow.
  - **Log emitter callback**: ``call_log_emitter`` (default None) called on
    every ensemble_invoke with a frozen ``EnsembleCallRecord``. Use
    ``make_ensemble_call_log_emitter(tenant_id)`` to wire DB write.

XML-tool fallback (mirrors make_llm_invoker behavior):
  same parse_skill_calls(content) for codex-style providers that emit XML.

Why a separate module instead of extending llm_invoker.py:
  - llm_invoker uses LLMRouter (single-provider routing); ensemble uses
    list[LLMProvider] directly. Different dependency surface.
  - Keeps llm_invoker.py stable for single-LLM path (unaffected).
  - Cleaner test isolation.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kun.agents.executor.exec_loop import (
    LLMInvoker,
    LLMStepResponse,
)
from kun.agents.executor.exec_loop import ToolCall as ExecToolCall
from kun.core.ids import new_id
from kun.core.logging import get_logger

# Reuse the same dict→LLMMessage filter as llm_invoker (consistency).
from kun.integration.llm_invoker import _dict_to_llm_message
from kun.interface.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    TaskProfile,
    ToolSpec,
)
from kun.interface.llm.cross_family import classify_family
from kun.interface.llm.ensemble import (
    ConsensusStrategy,
    EnsembleResponse,
    ensemble_invoke,
)

log = get_logger("kun.integration.ensemble_invoker")


# ============================================================
# EnsembleCallRecord — frozen IO for log emitter
# ============================================================


@dataclass(frozen=True)
class EnsembleCallRecord:
    """One ensemble_invoke call's metadata for logging (V7 §11.4 / X.B.ENS).

    Distinct from EnsembleResponse (which carries full per-provider LLMResponse
    bodies); this is the **metadata-only snapshot** ready to write to
    ensemble_calls table.
    """

    call_id: str
    invoked_at: datetime
    purpose: str
    providers: list[dict[str, str]]  # [{name, model_id, family}, ...]
    consensus_strategy: str
    divergence_score: float
    divergence_signals: list[str]
    consensus_provider: str | None
    total_cost_usd: float
    failure_count: int
    n_providers_total: int
    request_hash: str | None = None
    # Optional: surface the consensus content excerpt for high-divergence triage
    consensus_content_excerpt: str = ""

    def to_row_payload(self, tenant_id: str) -> dict[str, Any]:
        return {
            "tenant_id": tenant_id,
            "call_id": self.call_id,
            "invoked_at": self.invoked_at,
            "purpose": self.purpose,
            "providers": list(self.providers),
            "consensus_strategy": self.consensus_strategy,
            "divergence_score": self.divergence_score,
            "divergence_signals": list(self.divergence_signals),
            "consensus_provider": self.consensus_provider,
            "total_cost_usd": self.total_cost_usd,
            "failure_count": self.failure_count,
            "n_providers_total": self.n_providers_total,
            "request_hash": self.request_hash,
        }


EnsembleCallLogEmitter = Callable[[EnsembleCallRecord], Awaitable[None]]


# ============================================================
# DB writer + factory
# ============================================================


async def write_ensemble_call(
    *,
    tenant_id: str,
    record: EnsembleCallRecord,
) -> str:
    """Persist one EnsembleCallRecord to ensemble_calls table."""
    from kun.core.db import session_scope
    from kun.core.orm import EnsembleCallRow

    payload = record.to_row_payload(tenant_id)
    row = EnsembleCallRow(**payload)

    async with session_scope(tenant_id=tenant_id) as session:
        session.add(row)
        await session.flush()

    log.info(
        "ensemble_call.persisted",
        tenant_id=tenant_id,
        call_id=record.call_id,
        n_providers=record.n_providers_total,
        divergence_score=round(record.divergence_score, 3),
        failure_count=record.failure_count,
        consensus_provider=record.consensus_provider,
    )
    return record.call_id


def make_ensemble_call_log_emitter(tenant_id: str) -> EnsembleCallLogEmitter:
    """Return an emitter that writes ensemble call logs to the DB."""

    async def _emit(record: EnsembleCallRecord) -> None:
        await write_ensemble_call(tenant_id=tenant_id, record=record)

    return _emit


# ============================================================
# helpers — build EnsembleCallRecord from EnsembleResponse
# ============================================================


def _provider_id(name: str, model_id: str) -> str:
    return f"{name}/{model_id}"


def _request_hash(messages: list[LLMMessage]) -> str:
    """Stable hash of request messages for dedup/replay analysis."""
    payload = [
        {"role": m.role, "content": m.content or ""} for m in messages
    ]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def build_call_record_from_response(
    *,
    ensemble_response: EnsembleResponse,
    providers_meta: list[dict[str, str]],
    purpose: str,
    request_hash: str | None,
) -> EnsembleCallRecord:
    """Compute EnsembleCallRecord from EnsembleResponse (split for testability)."""
    total_cost = sum(
        r.cost_usd_equivalent for r in ensemble_response.responses
    )
    consensus_provider = None
    consensus_excerpt = ""
    if ensemble_response.consensus is not None:
        consensus_provider = _provider_id(
            ensemble_response.consensus.provider,
            ensemble_response.consensus.model,
        )
        # Excerpt to surface high-divergence triage without bloating row
        content = ensemble_response.consensus.content or ""
        consensus_excerpt = content[:500]

    n_total = len(providers_meta)
    n_success = len(ensemble_response.responses)
    failure_count = max(0, n_total - n_success)

    return EnsembleCallRecord(
        call_id=new_id("ensemble_call"),
        invoked_at=datetime.now(UTC),
        purpose=purpose,
        providers=list(providers_meta),
        consensus_strategy=ensemble_response.consensus_strategy.value,
        divergence_score=ensemble_response.divergence_score,
        divergence_signals=list(ensemble_response.divergence_signals),
        consensus_provider=consensus_provider,
        total_cost_usd=total_cost,
        failure_count=failure_count,
        n_providers_total=n_total,
        request_hash=request_hash,
        consensus_content_excerpt=consensus_excerpt,
    )


# ============================================================
# Main public API — make_ensemble_llm_invoker (drop-in for make_llm_invoker)
# ============================================================


def make_ensemble_llm_invoker(
    providers: list[LLMProvider],
    *,
    consensus_strategy: ConsensusStrategy = ConsensusStrategy.MAJORITY_VOTE,
    purpose: str = "execution",
    profile: TaskProfile | None = None,
    tools: list[ToolSpec] | None = None,
    temperature: float = 0.7,
    max_tokens: int = 2048,
    require_cross_family: bool = True,
    divergence_threshold: float = 0.5,
    call_log_emitter: EnsembleCallLogEmitter | None = None,
    on_divergence: Callable[[float, list[str]], Awaitable[None]] | None = None,
) -> LLMInvoker:
    """Wrap N providers into an ExecutorLoop-compatible LLMInvoker.

    Drop-in replacement for ``make_llm_invoker`` (single-LLM) when caller wants
    V7 §11.4 multi-LLM ensemble in the main runtime path.

    Args:
        providers: ≥ 2 LLMProvider instances; ≥ 2 distinct family if
            require_cross_family=True (V7 §11.2).
        consensus_strategy: how to pick the response that flows into
            ExecutorLoop. Other responses go to the call_log emitter for
            cockpit visibility but don't reach the main agent.
        purpose: log + cockpit category label ("execution" / "intent" / ...).
        profile: TaskProfile passed to LLMRequest.
        tools: tool specs the LLM may call. Same list sent to every provider.
        temperature / max_tokens: same applied to every provider call.
        require_cross_family: V7 §11.2 strict cross-family check (default True).
        divergence_threshold: > this triggers on_divergence callback +
            higher-priority log emission.
        call_log_emitter: async (EnsembleCallRecord) → None; default None.
            Use make_ensemble_call_log_emitter(tenant_id) for DB persistence.
        on_divergence: async (divergence_score, signals) → None when
            divergence > threshold. Service layer's ensemble_invoke also
            fires this; this layer just lets caller add own handler.

    Returns:
        LLMInvoker compatible with ExecutorLoop. Each call:
          1. Convert messages dicts → LLMMessage (same _dict_to_llm_message
             helper as llm_invoker for consistency)
          2. Build LLMRequest
          3. await ensemble_invoke(...)
          4. Build EnsembleCallRecord + fire call_log_emitter
          5. Convert ensemble.consensus → LLMStepResponse (or first success
             if consensus is None; raise if all failed)
          6. Apply XML-tool fallback (mirror llm_invoker)
    """
    if len(providers) < 2:
        raise ValueError(
            f"make_ensemble_llm_invoker requires ≥ 2 providers, got {len(providers)}"
        )

    # Pre-compute provider metadata (one-time, used in every call's log record)
    providers_meta: list[dict[str, str]] = [
        {
            "name": p.name,
            "model_id": p.model_id,
            "family": classify_family(p.model_id).value,
        }
        for p in providers
    ]

    bound_tools: list[ToolSpec] = list(tools) if tools else []

    async def _invoker(messages: list[dict[str, Any]]) -> LLMStepResponse:
        llm_messages = [_dict_to_llm_message(m) for m in messages]
        request = LLMRequest(
            messages=llm_messages,
            tools=bound_tools,
            temperature=temperature,
            max_tokens=max_tokens,
            profile=profile,
        )
        req_hash = _request_hash(llm_messages)

        ensemble_response: EnsembleResponse = await ensemble_invoke(
            request,
            providers,
            consensus_strategy=consensus_strategy,
            divergence_threshold=divergence_threshold,
            require_cross_family=require_cross_family,
            on_divergence=on_divergence,
        )

        # Build + emit call log (best-effort — emit failure must not break agent)
        if call_log_emitter is not None:
            record = build_call_record_from_response(
                ensemble_response=ensemble_response,
                providers_meta=providers_meta,
                purpose=purpose,
                request_hash=req_hash,
            )
            try:
                await call_log_emitter(record)
            except Exception as e:
                log.warning(
                    "ensemble_invoker.call_log_emit_failed",
                    call_id=record.call_id,
                    error=f"{type(e).__name__}: {e}",
                )

        # Pick the response that flows to ExecutorLoop
        chosen: LLMResponse | None = ensemble_response.consensus
        if chosen is None and ensemble_response.responses:
            chosen = ensemble_response.responses[0]
        if chosen is None:
            # ensemble_invoke raises RuntimeError when all fail, but be defensive
            raise RuntimeError(
                "make_ensemble_llm_invoker: ensemble_response has no consensus "
                "and no successful responses"
            )

        return _llm_response_to_step(chosen)

    return _invoker


def _llm_response_to_step(response: LLMResponse) -> LLMStepResponse:
    """Convert LLMResponse → LLMStepResponse with XML-tool fallback.

    Mirrors the same path llm_invoker uses (LT.TOOLS-GAP fallback for codex
    MCP / gpt-5.5 that emit `<skill>` XML in content instead of tool_calls).
    """
    tool_calls = [
        ExecToolCall(
            tool_id=tc.id,
            name=tc.name,
            arguments=dict(tc.arguments),
        )
        for tc in response.tool_calls
    ]

    if not tool_calls and response.content:
        from kun.engineering.agent_loop import parse_skill_calls

        xml_calls = parse_skill_calls(response.content)
        if xml_calls:
            tool_calls = [
                ExecToolCall(
                    tool_id=f"xml-{i}",
                    name=call.name,
                    arguments=dict(call.params),
                )
                for i, call in enumerate(xml_calls)
            ]

    finish_reason = response.finish_reason
    if tool_calls and not response.tool_calls:
        finish_reason = "tool_use"

    return LLMStepResponse(
        content=response.content,
        tool_calls=tool_calls,
        finish_reason=finish_reason,
        usage_tokens=response.usage.total(),
        cost_usd=response.cost_usd_equivalent,
    )


__all__ = [
    "EnsembleCallLogEmitter",
    "EnsembleCallRecord",
    "build_call_record_from_response",
    "make_ensemble_call_log_emitter",
    "make_ensemble_llm_invoker",
    "write_ensemble_call",
]
