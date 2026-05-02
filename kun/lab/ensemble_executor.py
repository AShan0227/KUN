"""ENSEMBLE 模式执行器 (V2.2 §26.3, HEX 启发).

HEX (UCF, ICLR 2026) 启发: 同任务跑 N 路径, 集成胜出. 24.72% → 88.10% (3.56x).
不需要训练, 纯推理时 ensemble.

KUN-Lab ENSEMBLE 行为:
- 每个 step 跑 N 条 LLM 路径 (不同 tier / temperature / system prompt)
- N 个输出 → multi_judge 选最优 (复用 §17.10)
- 记录每条路径的 (config, output, score) 进 ExperimentLog

成本: 比生产 KUN 高 N 倍. 单独走 lab 预算.

应用场景:
- 关键决策 (高 stakes, 用户能等)
- benchmark 评估 (拿同一题跑多 config, 看哪个 config 胜率高)
- 找"有效 recipe" → 推主仓库 (KnowledgePrecipitation)
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


def is_lab_enabled() -> bool:
    """KUN_LAB_MODE=1 才启用 lab. 默认 off."""
    return os.getenv("KUN_LAB_MODE", "0") == "1"


def _emit_ensemble_metrics(result: EnsembleResult, *, task_type: str | None) -> None:
    """Best-effort Prometheus metric emit (Wire 28). 不依赖 prom_client 安装."""
    try:
        from kun.core.metrics import (
            lab_budget_cap_total,
            lab_experiment_cost_usd,
            lab_experiment_latency_seconds,
            lab_experiment_total,
            lab_path_total,
        )

        tt = task_type or "unspecified"
        status = "budget_exceeded" if result.budget_exceeded else "ok"
        lab_experiment_total.labels(task_type=tt, status=status).inc()
        lab_experiment_cost_usd.labels(task_type=tt).inc(result.total_cost_usd)
        lab_experiment_latency_seconds.labels(task_type=tt).observe(result.total_latency_sec)
        if result.budget_exceeded:
            lab_budget_cap_total.labels(task_type=tt).inc()
        for pr in result.path_results:
            path_status = (
                "cancelled"
                if pr.error == "cancelled_budget_exceeded"
                else ("error" if pr.error else "ok")
            )
            lab_path_total.labels(
                strategy=str(pr.config.get("strategy", "unknown")),
                tier=str(pr.config.get("tier", "unknown")),
                status=path_status,
            ).inc()
    except Exception as e:
        logger.debug("lab.metrics_skipped err=%s", e)


PathStrategy = Literal[
    "tier_top_low_temp",  # tier=top + temp=0.1 (保守)
    "tier_strong_mid_temp",  # tier=strong + temp=0.5 (中性)
    "tier_cheap_high_temp",  # tier=cheap + temp=0.7 (探索)
    "chain_of_thought",  # tier=top + CoT prefix
    "diverse_perspective",  # tier=top + 不同 system prompt
]


@dataclass
class PathConfig:
    """单条 ensemble 路径的配置."""

    strategy: PathStrategy
    tier: str  # top / strong / cheap / coding / fallback
    temperature: float
    system_prompt_override: str | None = None
    extra_context: dict[str, Any] = field(default_factory=dict)


class EnsembleConfig(BaseModel):
    """ensemble 实验整体配置."""

    n_paths: int = Field(default=5, ge=2, le=10)
    paths: list[dict[str, Any]] = Field(default_factory=list)  # 实际跑的 PathConfig dump
    selection_method: Literal["best_score", "majority_vote", "judge_picks"] = "best_score"
    timeout_per_path_sec: int = 60
    cost_budget_total_usd: float = 1.0  # lab 预算上限 (高于生产)
    non_best_exploration_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Wire 52: 小流量故意走非最佳路径, 用来发现隐藏更优解. 默认关.",
    )
    exploration_seed: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class EnsemblePathResult(BaseModel):
    """单条路径结果."""

    path_idx: int
    config: dict[str, Any]
    output: str = ""
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    cost_usd: float = 0.0
    latency_sec: float = 0.0
    error: str = ""


class EnsembleResult(BaseModel):
    """ensemble 整体结果."""

    experiment_id: str
    config: EnsembleConfig
    path_results: list[EnsemblePathResult]
    winning_path_idx: int = -1
    winning_output: str = ""
    total_cost_usd: float = 0.0
    total_latency_sec: float = 0.0
    selection_reason: str = ""
    completed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    budget_exceeded: bool = Field(
        default=False,
        description="Wire 27: cost 累积超 cost_budget_total_usd, 剩余 path 被 cancel",
    )
    budget_cancelled_count: int = Field(
        default=0,
        description="Wire 27: 因 budget cap 被 cancel 的 path 数",
    )


# 默认 5 路径 (V2.2 §26.3)
DEFAULT_PATHS: list[PathConfig] = [
    PathConfig(strategy="tier_top_low_temp", tier="top", temperature=0.1),
    PathConfig(strategy="tier_strong_mid_temp", tier="strong", temperature=0.5),
    PathConfig(strategy="tier_cheap_high_temp", tier="cheap", temperature=0.7),
    PathConfig(
        strategy="chain_of_thought",
        tier="top",
        temperature=0.1,
        system_prompt_override="Think step by step. Show reasoning before answer.",
    ),
    PathConfig(
        strategy="diverse_perspective",
        tier="top",
        temperature=0.0,
        system_prompt_override="Take a contrarian view. Challenge default assumptions.",
    ),
]


class EnsembleExecutor:
    """跑 ENSEMBLE 任务. 复用 V2.2 心脏 (LLMRouter / multi_judge).

    用法:
        executor = EnsembleExecutor(llm_invoker=my_llm_call_fn)
        result = await executor.run(
            prompt="Q4 商业方案",
            config=EnsembleConfig(n_paths=5),
            scoring_fn=my_scorer,
        )
        # result.winning_output → 用户可见
        # result.path_results → 进 ExperimentLog 沉淀
    """

    def __init__(
        self,
        llm_invoker: Any,
        *,
        default_paths: list[PathConfig] | None = None,
        event_emitter: Any = None,
        require_lab_mode: bool = True,
    ) -> None:
        """
        Args:
            llm_invoker: async fn(prompt, path_config) → (output_text, cost_usd, latency_sec)
            default_paths: 默认 5 路径 (V2.2 §26.3); 调用方可覆盖
            event_emitter: 可选 async fn(EnsembleResult, *, task_type=None) → 任意.
                Wire 21: 跑完 emit experiment.created 事件给主仓库 events bus.
                None → 不 emit (lab 自治 / 单元测试场景)
            require_lab_mode: 默认 True, 保持 KUN-Lab 隔离语义. 生产 ENSEMBLE
                执行路径会显式传 False, 复用同一个 executor 但不要求
                KUN_LAB_MODE=1.
        """
        self._require_lab_mode = require_lab_mode
        if self._require_lab_mode and not is_lab_enabled():
            logger.warning(
                "EnsembleExecutor created with KUN_LAB_MODE=0; only effective when env enabled"
            )
        self._invoker = llm_invoker
        self._default_paths = default_paths or DEFAULT_PATHS
        self._event_emitter = event_emitter

    async def run(
        self,
        prompt: str,
        config: EnsembleConfig | None = None,
        *,
        scoring_fn: Any = None,
        task_type: str | None = None,
    ) -> EnsembleResult:
        """跑 N 路径并发, 用 scoring_fn 选最优.

        Args:
            prompt: 用户任务 prompt
            config: EnsembleConfig (默认 n_paths=5)
            scoring_fn: async fn(output_text, prompt) → 0..1 分数. None → 等分

        Returns:
            EnsembleResult 含所有路径结果 + 选出的 winner
        """
        if self._require_lab_mode and not is_lab_enabled():
            raise RuntimeError(
                "KUN-Lab disabled. Set KUN_LAB_MODE=1 to enable ensemble experiments."
            )

        from kun.core.ids import new_id

        cfg = config or EnsembleConfig(n_paths=len(self._default_paths))
        paths = self._default_paths[: cfg.n_paths]

        # Wire 27: 并发跑 + cost-cap hard 执行 (累积 cost 超预算 → cancel 剩余)
        path_results, budget_exceeded, cancelled_count = await self._run_paths_with_budget(
            prompt, paths, cfg
        )

        # 评分
        if scoring_fn is not None:
            for pr in path_results:
                if not pr.error and pr.output:
                    try:
                        pr.score = float(await scoring_fn(pr.output, prompt))
                        pr.score = max(0.0, min(1.0, pr.score))
                    except Exception:
                        logger.exception("ensemble scoring_fn failed for path %d", pr.path_idx)

        # 选 winner
        winner_idx, winner_reason = self._select_winner_with_exploration(
            path_results,
            cfg.selection_method,
            exploration_rate=cfg.non_best_exploration_rate,
            seed=cfg.exploration_seed or prompt,
        )
        winning_output = (
            path_results[winner_idx].output if 0 <= winner_idx < len(path_results) else ""
        )

        total_cost = sum(pr.cost_usd for pr in path_results)
        total_latency = max((pr.latency_sec for pr in path_results), default=0.0)

        if budget_exceeded:
            logger.warning(
                "ensemble.budget.cap_triggered cost=%.4f budget=%.4f cancelled=%d",
                total_cost,
                cfg.cost_budget_total_usd,
                cancelled_count,
            )

        result = EnsembleResult(
            experiment_id=new_id("experiment"),
            config=cfg,
            path_results=path_results,
            winning_path_idx=winner_idx,
            winning_output=winning_output,
            total_cost_usd=total_cost,
            total_latency_sec=total_latency,
            selection_reason=winner_reason,
            budget_exceeded=budget_exceeded,
            budget_cancelled_count=cancelled_count,
        )

        # Wire 28: Prometheus metrics (best-effort, 不依赖)
        _emit_ensemble_metrics(result, task_type=task_type)

        # Wire 21: 把实验结果 emit 进 events bus (best-effort, 不阻塞主流程)
        if self._event_emitter is not None:
            try:
                await self._event_emitter(result, task_type=task_type)
            except Exception:
                logger.exception("ensemble.event_emitter_failed exp=%s", result.experiment_id)

        return result

    async def _run_paths_with_budget(
        self,
        prompt: str,
        paths: list[PathConfig],
        cfg: EnsembleConfig,
    ) -> tuple[list[EnsemblePathResult], bool, int]:
        """跑 N 路径并发, 累积 cost 超 budget 立即 cancel 剩余 (Wire 27).

        Returns:
            (path_results 按 idx 排, budget_exceeded, cancelled_count)
        """
        budget = cfg.cost_budget_total_usd
        pending: dict[asyncio.Task[EnsemblePathResult], int] = {}
        for idx, path in enumerate(paths):
            t = asyncio.create_task(
                self._run_one_path(prompt, path, idx, cfg.timeout_per_path_sec),
                name=f"ensemble-path-{idx}",
            )
            pending[t] = idx

        done_results: dict[int, EnsemblePathResult] = {}
        running_cost = 0.0
        budget_exceeded = False

        while pending:
            done, _ = await asyncio.wait(list(pending.keys()), return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                idx = pending.pop(t)
                try:
                    pr = await t
                except asyncio.CancelledError:
                    pr = EnsemblePathResult(
                        path_idx=idx,
                        config=self._dump_path_config(paths[idx]),
                        error="cancelled_budget_exceeded",
                    )
                done_results[idx] = pr
                running_cost += pr.cost_usd

            if running_cost > budget and not budget_exceeded and pending:
                # 累积 cost 超 budget → cancel 剩余 path
                budget_exceeded = True
                cancelled_idxs = list(pending.values())
                for t in pending:
                    t.cancel()
                # 等所有 cancelled task 真结束
                await asyncio.gather(*pending.keys(), return_exceptions=True)
                for idx in cancelled_idxs:
                    done_results[idx] = EnsemblePathResult(
                        path_idx=idx,
                        config=self._dump_path_config(paths[idx]),
                        error="cancelled_budget_exceeded",
                    )
                pending.clear()

        cancelled_count = sum(
            1 for pr in done_results.values() if pr.error == "cancelled_budget_exceeded"
        )
        path_results = [done_results[i] for i in range(len(paths))]
        return path_results, budget_exceeded, cancelled_count

    @staticmethod
    def _dump_path_config(path: PathConfig) -> dict[str, Any]:
        return {
            "strategy": path.strategy,
            "tier": path.tier,
            "temperature": path.temperature,
        }

    async def _run_one_path(
        self,
        prompt: str,
        path: PathConfig,
        idx: int,
        timeout_sec: int,
    ) -> EnsemblePathResult:
        """跑单条路径."""
        config_dump = self._dump_path_config(path)
        try:
            output, cost, latency = await asyncio.wait_for(
                self._invoker(prompt, path), timeout=timeout_sec
            )
            return EnsemblePathResult(
                path_idx=idx,
                config=config_dump,
                output=str(output),
                cost_usd=float(cost),
                latency_sec=float(latency),
            )
        except TimeoutError:
            return EnsemblePathResult(
                path_idx=idx,
                config=config_dump,
                error=f"timeout_after_{timeout_sec}s",
            )
        except Exception as e:
            logger.exception("ensemble path %d failed", idx)
            return EnsemblePathResult(
                path_idx=idx,
                config=config_dump,
                error=f"{type(e).__name__}: {e}",
            )

    @staticmethod
    def _select_winner(
        results: list[EnsemblePathResult],
        method: str,
    ) -> tuple[int, str]:
        """选 winner. 返 (idx, reason)."""
        valid = [r for r in results if not r.error and r.output]
        if not valid:
            return -1, "no_valid_paths"

        if method == "best_score":
            best = max(valid, key=lambda r: r.score)
            return best.path_idx, f"best_score:{best.score:.2f}"

        if method == "majority_vote":
            from collections import Counter

            output_counts = Counter(r.output for r in valid)
            most_common_output, _count = output_counts.most_common(1)[0]
            for r in valid:
                if r.output == most_common_output:
                    return r.path_idx, f"majority_vote_n={_count}"
            return valid[0].path_idx, "majority_vote_fallback"

        # judge_picks: 简化版 — 拿最高 score (实际生产用 multi_judge)
        best = max(valid, key=lambda r: r.score)
        return best.path_idx, "judge_picks_proxy"

    @classmethod
    def _select_winner_with_exploration(
        cls,
        results: list[EnsemblePathResult],
        method: str,
        *,
        exploration_rate: float,
        seed: str,
    ) -> tuple[int, str]:
        """Wire 52: with a tiny opt-in rate, choose a non-best valid path.

        这不是生产默认行为, 默认 rate=0. 用在实验/启窗口里, 给 5% 流量
        机会走"看起来不是第一名但也可用"的路线, 防止系统过早收敛。
        """

        winner_idx, reason = cls._select_winner(results, method)
        if winner_idx < 0 or exploration_rate <= 0:
            return winner_idx, reason

        valid = [r for r in results if not r.error and r.output and r.path_idx != winner_idx]
        if not valid:
            return winner_idx, reason

        rng = random.Random(f"{seed}:{method}:{len(results)}")
        if rng.random() >= exploration_rate:
            return winner_idx, reason

        chosen = rng.choice(valid)
        return (
            chosen.path_idx,
            f"non_best_exploration:original={winner_idx} chosen={chosen.path_idx} "
            f"rate={exploration_rate:.2f} base={reason}",
        )


__all__ = [
    "DEFAULT_PATHS",
    "EnsembleConfig",
    "EnsembleExecutor",
    "EnsemblePathResult",
    "EnsembleResult",
    "PathConfig",
    "PathStrategy",
    "is_lab_enabled",
]
