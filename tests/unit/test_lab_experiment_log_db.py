from __future__ import annotations

from typing import Any
from unittest.mock import patch

from kun.api.runtime import install_runtime
from kun.lab import (
    EnsembleConfig,
    EnsemblePathResult,
    EnsembleResult,
    ExperimentLog,
    SqlExperimentLog,
    get_experiment_log,
    reset_experiment_log,
)
from kun.lab.experiment_log_db import SqlExperimentLog as DirectSqlExperimentLog
from kun.watchtower.engine import RuleEngine


def _fake_result(experiment_id: str = "exp-1", *, winning_idx: int = 0) -> EnsembleResult:
    return EnsembleResult(
        experiment_id=experiment_id,
        config=EnsembleConfig(n_paths=2),
        path_results=[
            EnsemblePathResult(
                path_idx=0,
                config={"strategy": "tier_top_low_temp"},
                output="winner",
                score=0.9,
                cost_usd=0.05,
            ),
            EnsemblePathResult(
                path_idx=1,
                config={"strategy": "tier_cheap_high_temp"},
                output="other",
                score=0.4,
                cost_usd=0.01,
            ),
        ],
        winning_path_idx=winning_idx,
        winning_output="winner" if winning_idx == 0 else "other",
        total_cost_usd=0.06,
    )


class _ScalarResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def all(self) -> list[Any]:
        return list(self._rows)


class _ExecuteResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._rows)


class _FakeSession:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    async def merge(self, row: Any) -> None:
        self.store[row.experiment_id] = row

    async def execute(self, statement: Any) -> _ExecuteResult:
        if type(statement).__name__ == "Delete":
            self.store.clear()
            return _ExecuteResult([])
        rows = sorted(
            self.store.values(),
            key=lambda row: (row.created_at, row.experiment_id),
        )
        return _ExecuteResult(rows)


class _SessionCM:
    def __init__(self, store: dict[str, Any]) -> None:
        self.store = store

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(self.store)

    async def __aexit__(self, *_exc: Any) -> None:
        return None


def _session_factory(store: dict[str, Any]):
    def factory() -> _SessionCM:
        return _SessionCM(store)

    return factory


def test_sql_experiment_log_round_trip_and_cross_instance_persistence() -> None:
    store: dict[str, Any] = {}
    log1 = DirectSqlExperimentLog(session_factory=_session_factory(store))
    log2 = DirectSqlExperimentLog(session_factory=_session_factory(store))

    recorded = log1.record("ad", _fake_result("exp-db-1"), prompt_hash="hash-1")
    loaded = log2.list_all()

    assert recorded.experiment_id == "exp-db-1"
    assert len(loaded) == 1
    assert loaded[0].experiment_id == "exp-db-1"
    assert loaded[0].prompt_hash == "hash-1"
    assert loaded[0].ensemble_result.winning_output == "winner"


def test_sql_experiment_log_by_task_type_filters_like_in_memory() -> None:
    store: dict[str, Any] = {}
    log = DirectSqlExperimentLog(session_factory=_session_factory(store))

    log.record("ad", _fake_result("exp-ad"))
    log.record("biz", _fake_result("exp-biz"))

    assert [e.experiment_id for e in log.by_task_type("biz")] == ["exp-biz"]


def test_sql_experiment_log_recipe_stats_equivalent_to_in_memory() -> None:
    store: dict[str, Any] = {}
    sql_log = DirectSqlExperimentLog(session_factory=_session_factory(store))
    mem_log = ExperimentLog()
    result = _fake_result("exp-1")

    sql_log.record("ad", result)
    mem_log.record("ad", result)

    sql_stats = [s.model_dump() for s in sql_log.recipe_stats("ad")]
    mem_stats = [s.model_dump() for s in mem_log.recipe_stats("ad")]
    assert sql_stats == mem_stats
    assert sql_log.best_recipe_for("ad") == mem_log.best_recipe_for("ad")
    assert sql_log.total_lab_cost_usd() == mem_log.total_lab_cost_usd()


def test_sql_experiment_log_reset_clears_rows() -> None:
    store: dict[str, Any] = {}
    log = DirectSqlExperimentLog(session_factory=_session_factory(store))

    log.record("ad", _fake_result("exp-reset"))
    assert len(log.list_all()) == 1

    log.reset()
    assert log.list_all() == []


def test_get_experiment_log_uses_db_when_env_enabled() -> None:
    reset_experiment_log()
    with patch.dict("os.environ", {"KUN_LAB_DB_BACKED": "1"}):
        log = get_experiment_log()
    reset_experiment_log()
    assert isinstance(log, SqlExperimentLog)


def test_install_runtime_exposes_db_backed_lab_log_when_enabled() -> None:
    reset_experiment_log()

    class App:
        state = type("State", (), {})()

    with patch.dict("os.environ", {"KUN_LAB_DB_BACKED": "1"}):
        install_runtime(App(), rule_engine=RuleEngine([]))
    reset_experiment_log()

    assert isinstance(App.state.lab_experiment_log, SqlExperimentLog)
