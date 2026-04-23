"""Shared pytest fixtures."""

from __future__ import annotations

import pytest
from kun.interface.llm import LLMRouter
from kun.interface.llm.router import reset_router, set_router
from kun.interface.llm.stub_provider import StubProvider


@pytest.fixture(autouse=True)
def deterministic_stub_router() -> None:
    """Use stub providers everywhere so tests never hit real APIs."""
    providers = {
        "top": StubProvider(model_id="stub-top", tier="top", latency_ms=0.1),
        "strong": StubProvider(model_id="stub-strong", tier="strong", latency_ms=0.1),
        "cheap": StubProvider(model_id="stub-cheap", tier="cheap", latency_ms=0.1),
        "coding": StubProvider(model_id="stub-coding", tier="coding", latency_ms=0.1),
        "fallback": StubProvider(model_id="stub-fallback", tier="fallback", latency_ms=0.1),
    }
    set_router(LLMRouter(providers))
    yield
    reset_router()
