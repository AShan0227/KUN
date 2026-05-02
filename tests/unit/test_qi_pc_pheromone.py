"""Wire 42 + 43: Predictive Coding 训练 + Pheromone 涌现."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from kun.qi import (
    InMemoryPheromoneStorage,
    InMemoryPredictionLog,
    PredictionLogModelUpdater,
    PredictionModel,
    PredictionRecord,
    PredictionTrainer,
    get_pheromone_storage,
    get_prediction_log,
    load_model,
    neighbor_pheromone_score,
    reset_pheromone_storage,
    reset_prediction_log,
    save_model,
)
from kun.qi.pheromone import (
    PHEROMONE_BASE_FACTOR,
    PHEROMONE_DECAY_RATE,
    PHEROMONE_MAX,
    PHEROMONE_REINFORCE_INCREMENT,
)


@pytest.fixture(autouse=True)
def _isolate():
    reset_prediction_log()
    reset_pheromone_storage()
    yield
    reset_prediction_log()
    reset_pheromone_storage()


# ---- Wire 42: Predictive Coding ----


def test_prediction_record_basic() -> None:
    r = PredictionRecord(
        timestamp=datetime.now(UTC),
        task_type="writing.x",
        step_id=1,
        state={"x": 1},
        expected={"cost_usd": 0.1},
        actual={"cost_usd": 0.05},
        error={"cost_usd": -0.05},
    )
    assert r.task_type == "writing.x"


def test_prediction_model_predict_known_type() -> None:
    model = PredictionModel(
        version="v1",
        trained_at=datetime.now(UTC),
        sample_size=10,
        means={"writing.x": {"cost_usd": 0.07, "duration_sec": 12.0}},
    )
    pred = model.predict({"task_type": "writing.x"})
    assert pred["cost_usd"] == 0.07


def test_prediction_model_unknown_type_returns_default() -> None:
    model = PredictionModel(
        version="v1",
        trained_at=datetime.now(UTC),
        sample_size=0,
    )
    pred = model.predict({"task_type": "unseen"})
    assert pred["cost_usd"] == 0.05  # default
    assert pred["duration_sec"] == 30.0


def test_prediction_model_to_from_json() -> None:
    model = PredictionModel(
        version="v2",
        trained_at=datetime.now(UTC),
        sample_size=5,
        means={"x": {"cost_usd": 0.1}},
        p95s={"x": {"cost_usd": 0.2}},
        metadata={"source": "test"},
    )
    s = model.to_json()
    loaded = PredictionModel.from_json(s)
    assert loaded.version == "v2"
    assert loaded.means == {"x": {"cost_usd": 0.1}}
    assert loaded.metadata == {"source": "test"}


@pytest.mark.asyncio
async def test_inmemory_log_append_then_all() -> None:
    log = InMemoryPredictionLog()
    r = PredictionRecord(
        timestamp=datetime.now(UTC),
        task_type="x",
        step_id=1,
        state={},
        expected={"cost_usd": 0.1},
        actual={"cost_usd": 0.05},
        error={"cost_usd": -0.05},
    )
    await log.append(r)
    all_records = await log.all()
    assert len(all_records) == 1


@pytest.mark.asyncio
async def test_log_model_updater_writes_record() -> None:
    log = InMemoryPredictionLog()
    updater = PredictionLogModelUpdater(log)
    await updater.record(
        step_id=1,
        task_type="writing.x",
        expected={"cost_usd": 0.1},
        actual={"cost_usd": 0.08},
        error={"cost_usd": -0.02},
    )
    records = await log.all()
    assert len(records) == 1
    assert records[0].task_type == "writing.x"


@pytest.mark.asyncio
async def test_trainer_no_data_returns_empty_model() -> None:
    log = InMemoryPredictionLog()
    trainer = PredictionTrainer(log)
    model = await trainer.train()
    assert model.sample_size == 0
    assert model.means == {}


@pytest.mark.asyncio
async def test_trainer_with_data_computes_mean() -> None:
    log = InMemoryPredictionLog()
    for cost in [0.1, 0.2, 0.3]:
        await log.append(
            PredictionRecord(
                timestamp=datetime.now(UTC),
                task_type="writing.x",
                step_id=1,
                state={},
                expected={"cost_usd": 0.5},
                actual={"cost_usd": cost},
                error={"cost_usd": cost - 0.5},
            )
        )
    trainer = PredictionTrainer(log)
    model = await trainer.train()
    assert model.sample_size == 3
    assert model.means["writing.x"]["cost_usd"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_trainer_p95_with_enough_data() -> None:
    log = InMemoryPredictionLog()
    for cost in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]:
        await log.append(
            PredictionRecord(
                timestamp=datetime.now(UTC),
                task_type="x",
                step_id=1,
                state={},
                expected={"cost_usd": 0.5},
                actual={"cost_usd": cost},
                error={"cost_usd": cost - 0.5},
            )
        )
    trainer = PredictionTrainer(log)
    model = await trainer.train()
    assert model.p95s["x"]["cost_usd"] >= 0.9


@pytest.mark.asyncio
async def test_save_load_model_roundtrip(tmp_path: Path) -> None:
    model = PredictionModel(
        version="v1",
        trained_at=datetime.now(UTC),
        sample_size=10,
        means={"x": {"cost_usd": 0.5}},
    )
    p = tmp_path / "model.json"
    save_model(model, p)
    loaded = load_model(p)
    assert loaded.version == "v1"
    assert loaded.means["x"]["cost_usd"] == 0.5


def test_prediction_log_singleton() -> None:
    a = get_prediction_log()
    b = get_prediction_log()
    assert a is b


# ---- Wire 43: Pheromone ----


@pytest.mark.asyncio
async def test_pheromone_reinforce_starts_at_increment() -> None:
    storage = InMemoryPheromoneStorage()
    await storage.reinforce(
        "u-test",
        source_kind="skill",
        source_id="reader",
        target_kind="skill",
        target_id="writer",
    )
    p = storage.get_pheromone("u-test", "skill", "reader", "skill", "writer")
    assert p == PHEROMONE_REINFORCE_INCREMENT


@pytest.mark.asyncio
async def test_pheromone_reinforce_accumulates() -> None:
    storage = InMemoryPheromoneStorage()
    for _ in range(5):
        await storage.reinforce(
            "u-test", source_kind="s", source_id="x", target_kind="s", target_id="y"
        )
    p = storage.get_pheromone("u-test", "s", "x", "s", "y")
    assert p == pytest.approx(0.05 * 5)


@pytest.mark.asyncio
async def test_pheromone_capped_at_max() -> None:
    storage = InMemoryPheromoneStorage()
    for _ in range(50):  # 50 × 0.05 = 2.5, 应 cap 在 1.0
        await storage.reinforce(
            "u-test", source_kind="s", source_id="x", target_kind="s", target_id="y"
        )
    p = storage.get_pheromone("u-test", "s", "x", "s", "y")
    assert p == PHEROMONE_MAX


@pytest.mark.asyncio
async def test_pheromone_decay_all() -> None:
    storage = InMemoryPheromoneStorage()
    await storage.reinforce(
        "u-test", source_kind="s", source_id="x", target_kind="s", target_id="y"
    )  # = 0.05
    affected = await storage.decay_all(decay_rate=0.5)
    assert affected == 1
    p = storage.get_pheromone("u-test", "s", "x", "s", "y")
    assert p == pytest.approx(0.025)


@pytest.mark.asyncio
async def test_pheromone_decay_per_tenant() -> None:
    storage = InMemoryPheromoneStorage()
    await storage.reinforce("u-A", source_kind="s", source_id="x", target_kind="s", target_id="y")
    await storage.reinforce("u-B", source_kind="s", source_id="x", target_kind="s", target_id="y")
    affected = await storage.decay_all(decay_rate=0.5, tenant_id="u-A")
    assert affected == 1
    a_p = storage.get_pheromone("u-A", "s", "x", "s", "y")
    b_p = storage.get_pheromone("u-B", "s", "x", "s", "y")
    assert a_p == pytest.approx(0.025)
    assert b_p == pytest.approx(0.05)  # 不受影响


@pytest.mark.asyncio
async def test_pheromone_per_relation_type_isolated() -> None:
    storage = InMemoryPheromoneStorage()
    await storage.reinforce(
        "u-test",
        source_kind="s",
        source_id="x",
        target_kind="s",
        target_id="y",
        relation_type="follows",
    )
    await storage.reinforce(
        "u-test",
        source_kind="s",
        source_id="x",
        target_kind="s",
        target_id="y",
        relation_type="depends_on",
    )
    follows = storage.get_pheromone("u-test", "s", "x", "s", "y", "follows")
    depends = storage.get_pheromone("u-test", "s", "x", "s", "y", "depends_on")
    assert follows == 0.05
    assert depends == 0.05  # 独立


def test_neighbor_pheromone_score_no_pheromone() -> None:
    """无 pheromone → score = confidence × BASE_FACTOR."""
    s = neighbor_pheromone_score(confidence=0.8, pheromone=0.0)
    assert s == pytest.approx(0.8 * PHEROMONE_BASE_FACTOR)


def test_neighbor_pheromone_score_max_pheromone() -> None:
    """max pheromone → score = confidence × (BASE + 1.0) = 1.5x."""
    s = neighbor_pheromone_score(confidence=0.8, pheromone=1.0)
    assert s == pytest.approx(0.8 * (PHEROMONE_BASE_FACTOR + 1.0))


def test_neighbor_pheromone_score_strong_pheromone_beats_high_confidence() -> None:
    """高 pheromone 低 confidence vs 低 pheromone 高 confidence — 看哪个 score 高."""
    high_pheromone_low_conf = neighbor_pheromone_score(0.3, 1.0)  # 0.3 × 1.5 = 0.45
    high_conf_no_pheromone = neighbor_pheromone_score(0.9, 0.0)  # 0.9 × 0.5 = 0.45
    assert high_pheromone_low_conf == pytest.approx(high_conf_no_pheromone)
    # pheromone 真正强时 (0.5+) → 高 confidence 也比不过


def test_pheromone_storage_singleton() -> None:
    a = get_pheromone_storage()
    b = get_pheromone_storage()
    assert a is b


def test_pheromone_decay_rate_default() -> None:
    """默认 0.95 衰减 = 一个月遗忘 (~0.95^30 ≈ 0.21)."""
    val = 1.0
    for _ in range(30):
        val *= PHEROMONE_DECAY_RATE
    assert 0.15 < val < 0.25
