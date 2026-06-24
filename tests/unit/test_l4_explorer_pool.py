"""L4.1 — Strategist Explorer Pool 配置化单测."""

from __future__ import annotations

import pytest
from kun.agents.strategist.explorer_pool import (
    ExplorerPoolConfig,
    _parse_modes_env,
    load_explorer_pool_config,
)
from kun.agents.strategist.service import StrategistService

# ---- ExplorerPoolConfig ----


def test_default_pool_has_three_modes() -> None:
    cfg = ExplorerPoolConfig()
    assert cfg.enabled_forward_modes == frozenset(
        ["conservative", "aggressive", "performance"]
    )
    assert cfg.is_enabled("conservative")
    assert cfg.is_enabled("aggressive")
    assert cfg.is_enabled("performance")
    assert cfg.is_enabled("experimental") is False


def test_custom_pool_only_conservative() -> None:
    cfg = ExplorerPoolConfig(enabled_forward_modes=frozenset(["conservative"]))
    assert cfg.is_enabled("conservative")
    assert cfg.is_enabled("aggressive") is False
    assert cfg.is_enabled("performance") is False


def test_filter_candidates_keeps_backward_regardless() -> None:
    """backward 候选不受 Pool config 控制 — 走自己路径."""
    cfg = ExplorerPoolConfig(enabled_forward_modes=frozenset([]))

    class FakeCand:
        def __init__(self, mode: str) -> None:
            self.explorer_mode = mode

    candidates = [FakeCand("conservative"), FakeCand("backward")]
    filtered = cfg.filter_candidates(candidates)
    # forward 都被剔, backward 保留
    assert len(filtered) == 1
    assert filtered[0].explorer_mode == "backward"


# ---- env var parsing ----


def test_parse_modes_env_clean_list() -> None:
    result = _parse_modes_env("conservative,aggressive")
    assert result == frozenset(["conservative", "aggressive"])


def test_parse_modes_env_with_whitespace_and_case() -> None:
    result = _parse_modes_env("  Conservative ,  PERFORMANCE  ")
    assert result == frozenset(["conservative", "performance"])


def test_parse_modes_env_drops_invalid() -> None:
    """非法 mode 名静默丢弃, 不抛."""
    result = _parse_modes_env("conservative,banana,aggressive")
    assert result == frozenset(["conservative", "aggressive"])


def test_parse_modes_env_all_invalid_returns_empty() -> None:
    result = _parse_modes_env("xyz,abc")
    assert result == frozenset()


# ---- load_explorer_pool_config ----


def test_load_no_env_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KUN_STRATEGIST_EXPLORER_MODES", raising=False)
    cfg = load_explorer_pool_config()
    assert cfg.enabled_forward_modes == frozenset(
        ["conservative", "aggressive", "performance"]
    )


def test_load_with_env_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_STRATEGIST_EXPLORER_MODES", "conservative")
    cfg = load_explorer_pool_config()
    assert cfg.enabled_forward_modes == frozenset(["conservative"])


def test_load_with_all_invalid_env_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_STRATEGIST_EXPLORER_MODES", "x,y,z")
    cfg = load_explorer_pool_config()
    # 全非法 → 回默认 3 模式
    assert cfg.enabled_forward_modes == frozenset(
        ["conservative", "aggressive", "performance"]
    )


def test_load_with_empty_env_returns_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUN_STRATEGIST_EXPLORER_MODES", "  ")
    cfg = load_explorer_pool_config()
    assert len(cfg.enabled_forward_modes) == 3


# ---- StrategistService 集成 ----


@pytest.mark.asyncio
async def test_strategist_default_pool_yields_three_forward_candidates() -> None:
    """无显式 config → 3 模式全启用."""
    svc = StrategistService()
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    modes = {c.explorer_mode for c in candidates}
    assert modes == {"conservative", "aggressive", "performance"}


@pytest.mark.asyncio
async def test_strategist_restricted_pool_filters_modes() -> None:
    """显式 config 仅 conservative → 其他 mode 候选被过滤."""
    cfg = ExplorerPoolConfig(enabled_forward_modes=frozenset(["conservative"]))
    svc = StrategistService(explorer_pool_config=cfg)
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    modes = {c.explorer_mode for c in candidates}
    assert modes == {"conservative"}


@pytest.mark.asyncio
async def test_strategist_pool_does_not_filter_backward() -> None:
    """backward 路径不受 Pool 控制 — 即使 forward 全空也能 backward."""
    from datetime import UTC, datetime, timedelta

    cap = {
        "capability_id": "cp-1",
        "promoted_at": datetime.now(UTC) - timedelta(hours=2),
        "enabled": True,
        "change_summary": "x",
    }

    async def history_reader(target_module: str) -> list[dict]:
        return [cap]

    cfg = ExplorerPoolConfig(enabled_forward_modes=frozenset())  # 0 forward modes
    svc = StrategistService(
        explorer_pool_config=cfg,
        capability_history_reader=history_reader,
    )
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    assert len(candidates) == 1
    assert candidates[0].explorer_mode == "backward"


@pytest.mark.asyncio
async def test_strategist_empty_pool_returns_no_forward_candidates() -> None:
    """0 enabled forward mode → forward 路径空返回."""
    cfg = ExplorerPoolConfig(enabled_forward_modes=frozenset())
    svc = StrategistService(explorer_pool_config=cfg)
    candidates = await svc.propose_candidates(
        {
            "anomaly_kind": "llm_fallback_spike",
            "target_module": "llm.router",
            "evidence": [{"fallback_provider": "openai"}],
        }
    )
    assert candidates == []
