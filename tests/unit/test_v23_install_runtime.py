"""V2.3 install_runtime 装上 Wire 38-50 全套."""

from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from kun.api.runtime import install_runtime
from kun.engineering.capability_cache import CapabilityCache
from kun.qi import (
    QiDailyBudget,
    QiWindowConfig,
)
from kun.qi.pheromone import InMemoryPheromoneStorage
from kun.qi.predictive_coding import (
    InMemoryPredictionLog,
    PredictionLogModelUpdater,
)
from kun.qi.protocol import ProtocolRegistry
from kun.watchtower.engine import RuleEngine
from kun.watchtower.rules import GuardRule, RuleTrigger
from starlette.datastructures import State


def _empty_engine() -> RuleEngine:
    return RuleEngine(
        [GuardRule(id="x", kind="guard", trigger=RuleTrigger(event_type="*", when="True"))]
    )


def _fresh_app():
    return SimpleNamespace(state=State())


def test_install_runtime_pc_default_enabled() -> None:
    """KUN_PREDICTIVE_CODING_ENABLED=1 (default) → PC log + updater 装上."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_PREDICTIVE_CODING_ENABLED", None)
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())

    assert isinstance(app.state.predictive_coding_log, InMemoryPredictionLog)
    assert isinstance(app.state.predictive_coding_updater, PredictionLogModelUpdater)
    # provider 默认 None (没 model file)
    assert app.state.predictive_coding_provider is None
    # orchestrator 拿到 updater
    assert app.state.orchestrator.model_updater is app.state.predictive_coding_updater


def test_install_runtime_pc_disabled() -> None:
    with patch.dict(os.environ, {"KUN_PREDICTIVE_CODING_ENABLED": "0"}):
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())
    assert app.state.predictive_coding_updater is None
    assert app.state.orchestrator.model_updater is None


def test_install_runtime_protocol_registry_default_enabled() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_PROTOCOL_REGISTRY_ENABLED", None)
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())
    assert isinstance(app.state.protocol_registry, ProtocolRegistry)


def test_install_runtime_pheromone_default_enabled() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_PHEROMONE_ENABLED", None)
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())
    assert isinstance(app.state.pheromone_storage, InMemoryPheromoneStorage)


def test_install_runtime_capability_cache_default_enabled() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_CAPABILITY_CACHE_ENABLED", None)
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())
    assert isinstance(app.state.capability_cache, CapabilityCache)


def test_install_runtime_qi_default_disabled() -> None:
    """启默认 disabled (KUN_QI_ENABLED=0). 用户 explicit 启用."""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_QI_ENABLED", None)
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())
    # 默认 0 → 不装
    assert not hasattr(app.state, "qi_budget")
    assert not hasattr(app.state, "qi_window_config")


def test_install_runtime_qi_enabled() -> None:
    with patch.dict(os.environ, {"KUN_QI_ENABLED": "1"}):
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())
    assert isinstance(app.state.qi_budget, QiDailyBudget)
    assert isinstance(app.state.qi_window_config, QiWindowConfig)


def test_install_runtime_pheromone_disabled() -> None:
    with patch.dict(os.environ, {"KUN_PHEROMONE_ENABLED": "0"}):
        app = _fresh_app()
        install_runtime(app, rule_engine=_empty_engine())
    assert not hasattr(app.state, "pheromone_storage")
