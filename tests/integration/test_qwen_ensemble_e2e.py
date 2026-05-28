"""V7 Phase X.B.QWEN-WIRE — real Qwen + ensemble e2e test.

This is the **e2e 验收层 evidence** the LT-progress had marked TBD. With
Qwen2.5-14B pulled and Ollama 0.24 running, we can finally verify:

  1. LocalLLMProvider really talks to ollama (http://localhost:11434/v1)
     and returns a non-empty response from Qwen.
  2. `build_ensemble_invoker_from_settings` correctly assembles cross-family
     ensemble from (stub-anthropic, qwen-local) when env vars are set.
  3. ensemble_invoke真 runs across the 2 providers (parallel), produces
     consensus + non-zero divergence_score.

Skipped when:
  - Ollama not reachable at localhost:11434 (no daemon running)
  - qwen2.5:14b-instruct-q4_K_M model not pulled

Required real infra (not mocked):
  - Ollama 0.24+ serving on http://localhost:11434
  - Model `qwen2.5:14b-instruct-q4_K_M` available (`ollama pull` done)

NOT mocked:
  - LocalLLMProvider's actual HTTP call to ollama
  - ensemble_invoke's actual parallel dispatch
  - divergence_score computation against real responses
"""

from __future__ import annotations

import httpx
import pytest

QWEN_MODEL = "qwen2.5:14b-instruct-q4_K_M"
OLLAMA_BASE_URL = "http://localhost:11434"


# ============================================================
# Skip-if-no-ollama probe (run once at module level)
# ============================================================


def _ollama_reachable_with_qwen() -> tuple[bool, str]:
    try:
        # Probe with short timeout — don't slow CI
        r = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=2.0)
        if r.status_code != 200:
            return False, f"ollama /api/tags HTTP {r.status_code}"
        models = [m.get("name", "") for m in r.json().get("models", [])]
        if QWEN_MODEL not in models:
            return (
                False,
                f"model {QWEN_MODEL!r} not found in ollama. Available: {models}",
            )
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


@pytest.fixture(scope="module")
def _ollama_ok() -> None:
    ok, reason = _ollama_reachable_with_qwen()
    if not ok:
        pytest.skip(f"Ollama+Qwen unavailable: {reason}")


pytestmark = pytest.mark.asyncio


# ============================================================
# Test 1 — LocalLLMProvider直接调 Qwen, 验证 HTTP path works
# ============================================================


async def test_qwen_via_local_provider_direct(_ollama_ok: None) -> None:
    """Direct LocalLLMProvider.invoke() — proves the OpenAI-compat path真接."""
    from kun.interface.llm.base import LLMMessage, LLMRequest
    from kun.interface.llm.local_provider import LocalLLMProvider

    provider = LocalLLMProvider(
        model_id=QWEN_MODEL,
        base_url=f"{OLLAMA_BASE_URL}/v1",
    )
    req = LLMRequest(
        messages=[
            LLMMessage(
                role="user",
                content="Reply with EXACTLY one short Chinese sentence about Python.",
            )
        ],
        temperature=0.3,
        max_tokens=80,
    )
    response = await provider.invoke(req)

    assert response.content, f"empty response from Qwen: {response}"
    # Sanity: response should be non-trivial (not error message)
    assert len(response.content) > 5, f"too-short response: {response.content!r}"
    # Provider field should be "local"
    assert response.provider == "local"
    # Model field should match what we asked for
    assert response.model == QWEN_MODEL


# ============================================================
# Test 2 — Factory builds cross-family ensemble with Qwen
# ============================================================


async def test_factory_builds_ensemble_with_qwen_local_provider(
    _ollama_ok: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_ensemble_invoker_from_settings: pulls stub-anthropic from router,
    adds qwen-local from env, validates cross-family pair (anthropic vs qwen).
    """
    monkeypatch.setenv("KUN_V7_ENSEMBLE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_TIERS", "top")  # just 1 from router
    monkeypatch.setenv("KUN_V7_ENSEMBLE_LOCAL_MODEL_ID", QWEN_MODEL)
    monkeypatch.setenv("KUN_V7_ENSEMBLE_LOCAL_BASE_URL", f"{OLLAMA_BASE_URL}/v1")

    from kun.integration.ensemble_invoker_factory import (
        build_ensemble_invoker_from_settings,
    )
    from kun.interface.llm.router import LLMRouter
    from kun.interface.llm.stub_provider import StubProvider

    # Build a tiny router with just 1 stub anthropic at top (factory will
    # auto-append qwen-local from env, giving us cross-family)
    router = LLMRouter(
        providers={
            "top": StubProvider(model_id="claude-opus-4-7", tier="top"),  # type: ignore[dict-item]
        }
    )

    invoker = build_ensemble_invoker_from_settings(
        router=router,
        purpose="execution",
    )
    assert invoker is not None, (
        "Factory returned None — cross-family check or env config failed"
    )
    assert callable(invoker)


# ============================================================
# Test 3 — Real ensemble call across stub-anthropic + Qwen真跑
# ============================================================


async def test_real_ensemble_invoke_stub_anthropic_plus_qwen(
    _ollama_ok: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**E2E**: stub anthropic + REAL Qwen → ensemble_invoke真跑, 出 consensus.

    Sets env: KUN_V7_ENSEMBLE_ENABLED + tier=top + local=qwen.
    The invoker, called once, should hit both providers in parallel and
    produce a non-None step response.
    """
    monkeypatch.setenv("KUN_V7_ENSEMBLE_ENABLED", "true")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_TIERS", "top")
    monkeypatch.setenv("KUN_V7_ENSEMBLE_LOCAL_MODEL_ID", QWEN_MODEL)
    monkeypatch.setenv("KUN_V7_ENSEMBLE_LOCAL_BASE_URL", f"{OLLAMA_BASE_URL}/v1")

    from kun.integration.ensemble_invoker_factory import (
        build_ensemble_invoker_from_settings,
    )
    from kun.interface.llm.router import LLMRouter
    from kun.interface.llm.stub_provider import StubProvider

    router = LLMRouter(
        providers={
            "top": StubProvider(model_id="claude-opus-4-7", tier="top"),  # type: ignore[dict-item]
        }
    )

    invoker = build_ensemble_invoker_from_settings(
        router=router, purpose="execution"
    )
    assert invoker is not None

    # Real call — stub returns canned, Qwen actually generates
    step = await invoker(
        [
            {"role": "system", "content": "Reply in one short Chinese sentence."},
            {"role": "user", "content": "What is 2 + 2?"},
        ]
    )

    assert step.content, f"empty step.content: {step}"
    assert isinstance(step.usage_tokens, int)
    assert step.usage_tokens > 0, "usage_tokens not propagated from ensemble"


# ============================================================
# Test 4 — divergence_score should be non-zero across real cross-family
# ============================================================


async def test_qwen_responses_diverge_from_stub(
    _ollama_ok: None,
) -> None:
    """Cross-family responses to the same prompt should have non-zero divergence.

    Stub provider returns canned text; Qwen real-generates. Their token-set
    Jaccard similarity must be < 1.0 → divergence_score > 0.
    """
    from kun.interface.llm.base import LLMMessage, LLMRequest
    from kun.interface.llm.ensemble import (
        ConsensusStrategy,
        ensemble_invoke,
    )
    from kun.interface.llm.local_provider import LocalLLMProvider
    from kun.interface.llm.stub_provider import StubProvider

    stub_provider = StubProvider(model_id="claude-opus-4-7", tier="top")
    qwen_provider = LocalLLMProvider(
        model_id=QWEN_MODEL,
        base_url=f"{OLLAMA_BASE_URL}/v1",
    )

    req = LLMRequest(
        messages=[
            LLMMessage(
                role="user",
                content="Reply with one Chinese sentence about KUN.",
            )
        ],
        temperature=0.3,
        max_tokens=60,
    )
    result = await ensemble_invoke(
        req,
        [stub_provider, qwen_provider],
        consensus_strategy=ConsensusStrategy.MAJORITY_VOTE,
        require_cross_family=True,
    )

    # Both responded (stub doesn't fail, Qwen real-loaded)
    assert len(result.responses) == 2
    # Different families
    assert result.consensus is not None
    # Stub's canned text vs Qwen's real Chinese sentence → must diverge
    assert result.divergence_score > 0, (
        f"Stub and Qwen real responses had ZERO divergence — suspicious. "
        f"Stub: {result.responses[0].content[:120]!r} "
        f"Qwen: {result.responses[1].content[:120]!r}"
    )
    # Should not be perfectly identical
    assert result.divergence_score <= 1.0
