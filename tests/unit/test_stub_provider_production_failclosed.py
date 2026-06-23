"""StubProvider fails closed under KUN_ENV=production (audit F045).

The router wires StubProvider only when no real provider has credentials. Its
invoke() returns a fabricated successful response — fine for tests/dev, but in
production that silently served fake "successful" LLM output (caused an incident).
Now it raises in production instead of faking success.
"""

from __future__ import annotations

import pytest
from kun.interface.llm.base import LLMMessage, LLMRequest
from kun.interface.llm.stub_provider import StubProvider


def _req() -> LLMRequest:
    return LLMRequest(messages=[LLMMessage(role="user", content="hi")], max_tokens=16)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raises_in_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_ENV", "production")
    with pytest.raises(RuntimeError, match="production"):
        await StubProvider(model_id="stub-x", tier="top").invoke(_req())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_case_insensitive_production(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_ENV", "Production")
    with pytest.raises(RuntimeError):
        await StubProvider().invoke(_req())


@pytest.mark.unit
@pytest.mark.asyncio
async def test_serves_normally_in_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUN_ENV", raising=False)
    resp = await StubProvider(model_id="stub-x", tier="top").invoke(_req())
    # Dev/test path unchanged: returns a (fabricated) response, no raise.
    assert resp.provider == "stub"
    assert resp.model == "stub-x"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dev_explicitly_is_fine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_ENV", "dev")
    resp = await StubProvider().invoke(_req())
    assert resp.provider == "stub"
