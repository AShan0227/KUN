"""NATS watchtower handler loads real rules (audit F029).

The cross-process (NATS) path built an empty RuleEngine() per event, so no
watchtower rule ever fired off-process. _watchtower_engine() now loads the real
rule set (cached) and watchtower_handler evaluates against it.
"""

from __future__ import annotations

import pytest
from kun.core import nats_subscriber
from kun.core.orm import EventRow


@pytest.fixture(autouse=True)
def _reset_engine_cache():
    nats_subscriber._WATCHTOWER_ENGINE = None
    yield
    nats_subscriber._WATCHTOWER_ENGINE = None


@pytest.mark.unit
def test_engine_loads_real_rules_and_is_cached() -> None:
    engine = nats_subscriber._watchtower_engine()
    # The repo ships rules/anomaly + rules/guard — the engine must not be empty.
    assert len(engine.rules) >= 1
    # Cached: second call returns the same instance (no per-event reload).
    assert nats_subscriber._watchtower_engine() is engine


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watchtower_handler_evaluates_against_loaded_engine(monkeypatch) -> None:
    seen: dict = {}

    class _SpyEngine:
        def __init__(self) -> None:
            self.rules = ["sentinel"]

        async def evaluate(self, event_type, *, namespace):
            seen["event_type"] = event_type
            seen["rule_count"] = len(self.rules)

    monkeypatch.setattr(nats_subscriber, "_WATCHTOWER_ENGINE", _SpyEngine())

    row = EventRow(
        event_id="e1",
        tenant_id="u-1",
        event_type="llm.fallback.triggered",
        subject="kun.llm.fallback.triggered",
        payload={},
    )
    await nats_subscriber.watchtower_handler(row)
    assert seen["event_type"] == "llm.fallback.triggered"
    assert seen["rule_count"] == 1  # evaluated against the (non-empty) engine
