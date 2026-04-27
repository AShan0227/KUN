"""SQL-backed ExperimentLog.

The public ExperimentLog API is intentionally synchronous because the existing
CLI and lab runner call it directly. Internally this wrapper runs async
SQLAlchemy work in a short blocking bridge, preserving the old API while making
`kun lab stats` useful across processes when KUN_LAB_DB_BACKED=1.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from sqlalchemy import delete, select

from kun.core.orm import LabExperimentRow
from kun.lab.cursor_storage import SessionFactory
from kun.lab.ensemble_executor import EnsembleResult
from kun.lab.experiment_log import (
    Experiment,
    ExperimentLog,
    RecipeStats,
    _recipe_stats_from_experiments,
    _top_winning_strategies_from_experiments,
)

T = TypeVar("T")


class SqlExperimentLog(ExperimentLog):
    """DB-backed ExperimentLog with the same public methods as ExperimentLog."""

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        *,
        tenant_id_for_session: str = "kun_lab",
    ) -> None:
        # Do not call super().__init__(): the DB is the source of truth.
        self._session_factory = session_factory
        self._tenant_id_for_session = tenant_id_for_session

    def record(
        self,
        task_type: str,
        ensemble_result: EnsembleResult,
        prompt_hash: str = "",
        notes: str = "",
    ) -> Experiment:
        exp = Experiment(
            experiment_id=ensemble_result.experiment_id,
            task_type=task_type,
            prompt_hash=prompt_hash,
            ensemble_result=ensemble_result,
            notes=notes,
        )
        self._run_sync(lambda: self._record_async(exp))
        return exp

    def list_all(self) -> list[Experiment]:
        return self._run_sync(self._list_all_async)

    def by_task_type(self, task_type: str) -> list[Experiment]:
        return [e for e in self.list_all() if e.task_type == task_type]

    def best_recipe_for(self, task_type: str) -> RecipeStats | None:
        stats = self.recipe_stats(task_type)
        if not stats:
            return None
        return max(stats, key=lambda s: s.win_rate)

    def recipe_stats(self, task_type: str | None = None) -> list[RecipeStats]:
        return _recipe_stats_from_experiments(self.list_all(), task_type)

    def top_winning_strategies(self, top_k: int = 5) -> list[tuple[str, float]]:
        return _top_winning_strategies_from_experiments(self.list_all(), top_k=top_k)

    def total_lab_cost_usd(self) -> float:
        return sum(e.ensemble_result.total_cost_usd for e in self.list_all())

    def reset(self) -> None:
        self._run_sync(self._reset_async)

    async def _open_session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        from kun.core.db import session_scope

        return session_scope(tenant_id=self._tenant_id_for_session)

    async def _record_async(self, exp: Experiment) -> None:
        async with await self._open_session() as session:
            row = LabExperimentRow(
                experiment_id=exp.experiment_id,
                task_type=exp.task_type,
                prompt_hash=exp.prompt_hash,
                ensemble_result=exp.ensemble_result.model_dump(mode="json"),
                created_at=exp.created_at,
            )
            await session.merge(row)

    async def _list_all_async(self) -> list[Experiment]:
        async with await self._open_session() as session:
            result = await session.execute(
                select(LabExperimentRow).order_by(
                    LabExperimentRow.created_at,
                    LabExperimentRow.experiment_id,
                )
            )
            rows = result.scalars().all()
            return [self._row_to_experiment(row) for row in rows]

    async def _reset_async(self) -> None:
        async with await self._open_session() as session:
            await session.execute(delete(LabExperimentRow))

    @staticmethod
    def _row_to_experiment(row: LabExperimentRow) -> Experiment:
        return Experiment(
            experiment_id=row.experiment_id,
            task_type=row.task_type,
            prompt_hash=row.prompt_hash,
            ensemble_result=EnsembleResult.model_validate(row.ensemble_result),
            created_at=row.created_at,
        )

    @staticmethod
    def _run_sync(coro_factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro_factory())

        # Existing public API is sync. If called from an async path, run the DB
        # coroutine in a short-lived thread instead of trying to nest event loops.
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: asyncio.run(coro_factory())).result()


__all__ = ["SqlExperimentLog"]
