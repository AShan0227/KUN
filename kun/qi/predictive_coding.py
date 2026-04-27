"""V2.3 Wire 42 — Predictive Coding 启训练 pipeline.

跟 V2.3 Wire 41 (鲲 hook) 配套. 流程:
    1. 鲲 hook 实时记录 (state, expected, actual, error) → ErrorLog (in-memory)
    2. 启窗口内: PredictionTrainer.train_from_log → 输出 PredictionModel
    3. 鲲 load model.predict(state) → 用作 prediction_provider

设计思路:
    - 不用大 LLM, 用轻量 lookup table (per task_type)
    - 每个 task_type 累积 (cost / duration / tokens) 历史均值 + 95% percentile
    - predict(state) 返该 task_type 的均值
    - 简单直接, 易解释, 容易增量学

进阶 (V2.4): 接 sklearn 训简单 regression 模型. 现 V2.3 用均值就够.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections import defaultdict
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


@dataclass
class PredictionRecord:
    """单次 prediction_error 记录 (鲲 hook 写入)."""

    timestamp: datetime
    task_type: str
    step_id: int
    state: dict[str, Any]
    expected: dict[str, float]
    actual: dict[str, float]
    error: dict[str, float]


@dataclass
class PredictionModel:
    """输出模型 — task_type → mean stats."""

    version: str
    trained_at: datetime
    sample_size: int
    # task_type → metric → mean
    means: dict[str, dict[str, float]] = field(default_factory=dict)
    # task_type → metric → p95
    p95s: dict[str, dict[str, float]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def predict(self, state: dict[str, Any]) -> dict[str, float]:
        """state 里的 task_type → mean.

        没数据 → 返保守默认 (鲲不会因 model 没数据卡住).
        """
        task_type = str(state.get("task_type", ""))
        return self.means.get(task_type, {"cost_usd": 0.05, "duration_sec": 30.0, "tokens": 100})

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "trained_at": self.trained_at.isoformat(),
                "sample_size": self.sample_size,
                "means": self.means,
                "p95s": self.p95s,
                "metadata": self.metadata,
            },
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, data: str) -> PredictionModel:
        d = json.loads(data)
        return cls(
            version=d["version"],
            trained_at=datetime.fromisoformat(d["trained_at"]),
            sample_size=d["sample_size"],
            means=d.get("means", {}),
            p95s=d.get("p95s", {}),
            metadata=d.get("metadata", {}),
        )


class PredictionLog(Protocol):
    """error log storage protocol."""

    async def append(self, record: PredictionRecord) -> None: ...
    async def all(self) -> list[PredictionRecord]: ...
    async def clear(self) -> None: ...


class InMemoryPredictionLog:
    """默认 storage. 单元测试 / 轻量场景."""

    def __init__(self) -> None:
        self._records: list[PredictionRecord] = []

    async def append(self, record: PredictionRecord) -> None:
        self._records.append(record)

    async def all(self) -> list[PredictionRecord]:
        return list(self._records)

    async def clear(self) -> None:
        self._records.clear()


class PredictionLogModelUpdater:
    """Wire 41 model_updater 接口实现 — 把 record 写入 PredictionLog.

    用法:
        log = InMemoryPredictionLog()
        updater = PredictionLogModelUpdater(log)
        orch = Orchestrator(model_updater=updater)
    """

    def __init__(self, log: PredictionLog) -> None:
        self._log = log

    async def record(
        self,
        *,
        step_id: int,
        task_type: str,
        expected: dict[str, float],
        actual: dict[str, float],
        error: dict[str, float],
        state: dict[str, Any] | None = None,
    ) -> None:
        record = PredictionRecord(
            timestamp=datetime.now(UTC),
            task_type=task_type,
            step_id=step_id,
            state=state or {"task_type": task_type},
            expected=expected,
            actual=actual,
            error=error,
        )
        await self._log.append(record)


class PredictionTrainer:
    """启窗口内调用. 拉 PredictionLog → 训 PredictionModel → export.

    用法 (在启的 daily 窗口内):
        log = get_prediction_log_singleton()  # 跨 process 共享
        trainer = PredictionTrainer(log)
        model = await trainer.train()
        model.to_json() → 存 model file → 鲲下次启动 load
    """

    def __init__(self, log: PredictionLog) -> None:
        self._log = log

    async def train(self) -> PredictionModel:
        records = await self._log.all()
        if not records:
            return PredictionModel(
                version=f"v0-{datetime.now(UTC).isoformat()}",
                trained_at=datetime.now(UTC),
                sample_size=0,
                metadata={"reason": "no_data"},
            )

        # 按 task_type 分组累积
        by_type: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
        for r in records:
            for metric, value in r.actual.items():
                by_type[r.task_type][metric].append(value)

        means: dict[str, dict[str, float]] = {}
        p95s: dict[str, dict[str, float]] = {}
        for task_type, metrics in by_type.items():
            means[task_type] = {}
            p95s[task_type] = {}
            for metric, values in metrics.items():
                if not values:
                    continue
                means[task_type][metric] = statistics.mean(values)
                if len(values) >= 5:
                    sorted_vals = sorted(values)
                    p95_idx = int(len(sorted_vals) * 0.95)
                    p95s[task_type][metric] = sorted_vals[min(p95_idx, len(sorted_vals) - 1)]
                else:
                    p95s[task_type][metric] = max(values)

        return PredictionModel(
            version=f"v1-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
            trained_at=datetime.now(UTC),
            sample_size=len(records),
            means=means,
            p95s=p95s,
            metadata={
                "task_types": list(by_type.keys()),
                "trainer": "InMemoryMeanPercentileTrainer",
            },
        )


_log_singleton: InMemoryPredictionLog | None = None


def get_prediction_log() -> InMemoryPredictionLog:
    """单例. 鲲 hook + 启 trainer 共享."""
    global _log_singleton
    if _log_singleton is None:
        _log_singleton = InMemoryPredictionLog()
    return _log_singleton


def reset_prediction_log() -> None:
    global _log_singleton
    _log_singleton = None


# ---- Model file save/load (启 export → 鲲 load) ----


def save_model(model: PredictionModel, path: str | Path) -> None:
    Path(path).write_text(model.to_json(), encoding="utf-8")


def load_model(path: str | Path) -> PredictionModel:
    return PredictionModel.from_json(Path(path).read_text(encoding="utf-8"))


__all__ = [
    "InMemoryPredictionLog",
    "PredictionLog",
    "PredictionLogModelUpdater",
    "PredictionModel",
    "PredictionRecord",
    "PredictionTrainer",
    "get_prediction_log",
    "load_model",
    "reset_prediction_log",
    "save_model",
]


# Awaitable stub
_ = Awaitable
