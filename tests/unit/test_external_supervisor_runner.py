"""L2.4 — External Supervisor runner 单测.

只验证 entry / settings gating; 不真启动 ollama.
"""

from __future__ import annotations

import pytest
from kun.core.config import Settings
from kun.external_supervisor import runner


@pytest.mark.asyncio
async def test_main_returns_disabled_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """KUN_EXTERNAL_SUPERVISOR_ENABLED 默认 False → main 返回 disabled marker."""

    def fake_settings() -> Settings:
        return Settings(external_supervisor_enabled=False)

    monkeypatch.setattr(runner, "settings", fake_settings)
    result = await runner.main()
    assert result["started"] is False
    assert "disabled" in result["reason"].lower() or "false" in result["reason"].lower()


@pytest.mark.asyncio
async def test_main_starts_service_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_settings() -> Settings:
        return Settings(external_supervisor_enabled=True)

    monkeypatch.setattr(runner, "settings", fake_settings)
    result = await runner.main()
    assert result["started"] is True
    assert "service" in result
    # service should expose its LLM provider model
    assert result["service"]._llm.model_id  # smoke


@pytest.mark.asyncio
async def test_build_service_uses_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_settings() -> Settings:
        return Settings(
            external_supervisor_enabled=True,
            external_supervisor_model_id="llama3.1:70b",
            external_supervisor_base_url="http://other:8080/v1",
            external_supervisor_max_concurrent=3,
        )

    monkeypatch.setattr(runner, "settings", fake_settings)
    service = await runner.build_service()
    assert service._llm.model_id == "llama3.1:70b"
    assert service._llm._base_url == "http://other:8080/v1"
    assert service._sem._value == 3
