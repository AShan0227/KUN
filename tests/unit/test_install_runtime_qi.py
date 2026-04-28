from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from kun.api.runtime import (
    get_capability_card_cache,
    get_pheromone_storage_runtime,
    get_protocol_registry_runtime,
    get_qi_budget_runtime,
    install_runtime,
)
from kun.engineering.capability_cache import CapabilityCardCache
from kun.qi import QiDailyBudget
from kun.watchtower.engine import RuleEngine
from starlette.datastructures import State


def _app():
    return SimpleNamespace(state=State())


def test_install_runtime_qi_disabled_by_default() -> None:
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("KUN_QI_RUNTIME_ENABLED", None)
        app = _app()
        install_runtime(app, rule_engine=RuleEngine([]))

    assert get_protocol_registry_runtime(app) is None
    assert get_pheromone_storage_runtime(app) is None
    assert get_qi_budget_runtime(app) is None


def test_install_runtime_qi_opt_in_installs_state() -> None:
    with patch.dict(
        os.environ,
        {
            "KUN_QI_RUNTIME_ENABLED": "1",
            "KUN_QI_DAILY_BUDGET_USD": "12.5",
            "KUN_CAPABILITY_CACHE_TTL_SEC": "3",
        },
    ):
        app = _app()
        install_runtime(app, rule_engine=RuleEngine([]))

    assert get_protocol_registry_runtime(app) is not None
    assert get_pheromone_storage_runtime(app) is not None
    assert isinstance(get_qi_budget_runtime(app), QiDailyBudget)
    assert get_qi_budget_runtime(app).remaining_budget("u") == 12.5
    assert isinstance(get_capability_card_cache(app), CapabilityCardCache)
