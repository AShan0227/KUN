"""PanoramaBuilder — 任务全景按需展开 (V2.1.2 §5.8.1).

12 个事前模块每个独立判断 "该不该跑、跑多深", 不绑定档位固定 step.

按需展开矩阵:
- 必跑 (任何任务): task_id + intent_one_sentence
- 按 risk 加跑: 风险预估 / 预冲突 / multi-judge 复审
- 按 complexity 加跑: 拆解 / Context 预热 / 资源预估 / 注意力分配 / 备选路径 / 风险图
- 按 task_type 加跑: 角色实例化

并行优化: 同档位内能并行的并行 (asyncio.gather), 总耗时 = max(单模块).
流式占位: ≥2s 预估的任务先推 placeholder.
缓存复用: 同 fingerprint 复用上次 panorama, 只更新 RuntimeState.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from kun.core.task_panorama import (
    AttentionAllocation,
    ContextPreheat,
    PanoramaTier,
    PreConflictScan,
    RiskAssessment,
    RoleInstance,
    StepPlan,
    TaskPanorama,
)

logger = logging.getLogger(__name__)


# 模块产出回调签名 (返回 partial dict 合并到 panorama 字段)
ModuleRunner = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


def detect_tier(task_meta: dict[str, Any]) -> PanoramaTier:
    """V2.1.2 §5.8.1a 后验档位估算 (按预估事前模块数推断).

    实际生成耗时由 expand 矩阵决定, 这里只是粗略指引.
    """
    has_template = bool(task_meta.get("chosen_template_ref"))
    risk = task_meta.get("risk_level", "low")
    complexity = float(task_meta.get("complexity_score", 0.0))

    if has_template or task_meta.get("cache_hit"):
        return "minimal"
    if risk == "low" and complexity < 0.3:
        return "light"
    if risk in ("low", "medium") and complexity < 0.7:
        return "medium"
    if risk == "critical" or complexity >= 0.9:
        return "full"
    return "heavy"


def _should_run_risk(task_meta: dict[str, Any]) -> bool:
    """风险预估: risk≥medium 或含敏感关键词."""
    risk = task_meta.get("risk_level", "low")
    if risk in ("medium", "high", "critical"):
        return True
    sensitive = ("delete", "drop", "deploy", "支付", "删除", "transfer")
    msg = str(task_meta.get("user_message", "")).lower()
    return any(s in msg for s in sensitive)


def _should_run_preconflict(task_meta: dict[str, Any]) -> bool:
    """预冲突扫描: risk≥high 或资源占用估计高."""
    risk = task_meta.get("risk_level", "low")
    if risk in ("high", "critical"):
        return True
    return bool(task_meta.get("resources_high_contention"))


def _should_run_split(task_meta: dict[str, Any]) -> bool:
    """任务拆解: complexity≥0.3 或 estimated_steps>1."""
    if float(task_meta.get("complexity_score", 0.0)) >= 0.3:
        return True
    return int(task_meta.get("estimated_steps", 1)) > 1


def _should_run_preheat(task_meta: dict[str, Any]) -> bool:
    """Context 预热: complexity≥0.5 或含历史依赖."""
    if float(task_meta.get("complexity_score", 0.0)) >= 0.5:
        return True
    return bool(task_meta.get("has_history_dependency"))


def _should_run_resource_estimate(task_meta: dict[str, Any]) -> bool:
    """资源预估: complexity≥0.5 或预估成本 > 用户阈值."""
    if float(task_meta.get("complexity_score", 0.0)) >= 0.5:
        return True
    est_cost = float(task_meta.get("estimated_cost_usd", 0.0))
    threshold = float(task_meta.get("user_approval_threshold_usd", 1e9))
    return est_cost > threshold


def _should_run_attention(task_meta: dict[str, Any]) -> bool:
    """注意力分配: complexity≥0.7 或 risk≥high."""
    if float(task_meta.get("complexity_score", 0.0)) >= 0.7:
        return True
    return task_meta.get("risk_level") in ("high", "critical")


def _should_run_role(task_meta: dict[str, Any]) -> bool:
    """角色实例化: 涉及外部资源 / 多角色协作."""
    return bool(task_meta.get("involves_external_resources")) or bool(task_meta.get("multi_role"))


def _should_run_multi_judge_review(task_meta: dict[str, Any]) -> bool:
    """multi-judge 复审 Panorama 本身: critical 且 complexity≥0.7."""
    return (
        task_meta.get("risk_level") == "critical"
        and float(task_meta.get("complexity_score", 0.0)) >= 0.7
    )


def _should_run_alternative(task_meta: dict[str, Any]) -> bool:
    """备选路径: critical 或预估失败率>0.3."""
    if task_meta.get("risk_level") == "critical":
        return True
    return float(task_meta.get("estimated_failure_rate", 0.0)) > 0.3


def _should_run_risk_graph(task_meta: dict[str, Any]) -> bool:
    """风险图: critical 或多任务并发."""
    if task_meta.get("risk_level") == "critical":
        return True
    return int(task_meta.get("concurrent_running_tasks", 0)) >= 3


# 模块名 → (是否该跑判断函数, 默认深度)
PREFLIGHT_MODULES: dict[str, tuple[Callable[[dict[str, Any]], bool], str]] = {
    "risk_assessment": (_should_run_risk, "shallow"),
    "pre_conflict_scan": (_should_run_preconflict, "shallow"),
    "task_split": (_should_run_split, "linear"),
    "context_preheat": (_should_run_preheat, "shallow"),
    "resource_estimate": (_should_run_resource_estimate, "shallow"),
    "attention_allocation": (_should_run_attention, "deep"),
    "role_instantiation": (_should_run_role, "single"),
    "multi_judge_review": (_should_run_multi_judge_review, "n=3"),
    "alternative_paths": (_should_run_alternative, "shallow"),
    "risk_graph": (_should_run_risk_graph, "shallow"),
}


class PanoramaBuilder:
    """按需展开任务全景生成器.

    用法:
        builder = PanoramaBuilder(
            risk_runner=async_func,
            preconflict_runner=async_func,
            split_runner=async_func,
            preheat_runner=async_func,
            resource_runner=async_func,
            attention_runner=async_func,
            role_runner=async_func,
            multi_judge_runner=async_func,
            alternative_runner=async_func,
            intent_runner=async_func,
        )
        panorama = await builder.expand(task_meta)
    """

    def __init__(
        self,
        *,
        intent_runner: ModuleRunner | None = None,
        risk_runner: ModuleRunner | None = None,
        preconflict_runner: ModuleRunner | None = None,
        split_runner: ModuleRunner | None = None,
        preheat_runner: ModuleRunner | None = None,
        resource_runner: ModuleRunner | None = None,
        attention_runner: ModuleRunner | None = None,
        role_runner: ModuleRunner | None = None,
        multi_judge_runner: ModuleRunner | None = None,
        alternative_runner: ModuleRunner | None = None,
    ) -> None:
        self.intent_runner = intent_runner
        self._runners: dict[str, ModuleRunner | None] = {
            "risk_assessment": risk_runner,
            "pre_conflict_scan": preconflict_runner,
            "task_split": split_runner,
            "context_preheat": preheat_runner,
            "resource_estimate": resource_runner,
            "attention_allocation": attention_runner,
            "role_instantiation": role_runner,
            "multi_judge_review": multi_judge_runner,
            "alternative_paths": alternative_runner,
            "risk_graph": None,  # 暂留 stub, M4 实装
        }

    async def expand(self, task_meta: dict[str, Any]) -> TaskPanorama:
        """按需展开生成 panorama."""
        start_ns = time.perf_counter_ns()
        task_ref = str(task_meta.get("task_id", "unknown"))

        # ---- 必跑步骤 ----
        intent = task_meta.get("intent_one_sentence", "")
        if not intent and self.intent_runner is not None:
            try:
                intent_out = await self.intent_runner(task_meta)
                intent = intent_out.get("intent_one_sentence", "")
            except Exception:
                logger.exception("intent_runner failed, using fallback")
                intent = task_meta.get("user_message", "")[:80]
        if not intent:
            intent = "(no explicit intent)"

        # ---- 决定档位 + 哪些模块该跑 ----
        tier = detect_tier(task_meta)
        modules_to_run: list[str] = []
        modules_skipped: list[str] = []

        for mod_name, (predicate, _depth) in PREFLIGHT_MODULES.items():
            if predicate(task_meta) and self._runners.get(mod_name) is not None:
                modules_to_run.append(mod_name)
            else:
                modules_skipped.append(mod_name)

        # ---- 并行跑 (能并行的并行) ----
        async def _wrap(name: str) -> tuple[str, dict[str, Any]]:
            runner = self._runners[name]
            assert runner is not None
            try:
                out = await runner(task_meta)
                return (name, out)
            except Exception:
                logger.exception("module %s failed (non-fatal)", name)
                return (name, {})

        results: dict[str, dict[str, Any]] = {}
        if modules_to_run:
            outputs = await asyncio.gather(*(_wrap(n) for n in modules_to_run))
            for name, out in outputs:
                results[name] = out

        # ---- 聚合到 TaskPanorama ----
        panorama = TaskPanorama(
            task_ref=task_ref,
            tier=tier,
            intent_one_sentence=intent,
            audience=task_meta.get("audience", "developer"),
            chosen_template_ref=task_meta.get("chosen_template_ref"),
            modules_run=modules_to_run,
            modules_skipped=modules_skipped,
        )

        if results.get("risk_assessment"):
            r = results["risk_assessment"]
            panorama.risk_assessment = RiskAssessment(
                financial_risk=float(r.get("financial_risk", 0.0)),
                irreversibility_risk=float(r.get("irreversibility_risk", 0.0)),
                complexity_risk=float(r.get("complexity_risk", 0.0)),
                overall_risk_level=r.get("overall_risk_level", "medium"),
            )

        if results.get("pre_conflict_scan"):
            r = results["pre_conflict_scan"]
            panorama.pre_conflict_scan = PreConflictScan(
                conflicts_found=r.get("conflicts_found", []),
                resolution=r.get("resolution", "no_conflict"),
            )

        if results.get("task_split"):
            r = results["task_split"]
            steps_data = r.get("steps", [])
            panorama.execution_plan = [
                StepPlan(
                    step_index=i,
                    skill_id=s.get("skill_id"),
                    role_template_ref=s.get("role_template_ref"),
                    depends_on=s.get("depends_on", []),
                    estimated_cost_usd=float(s.get("estimated_cost_usd", 0.0)),
                    estimated_duration_sec=float(s.get("estimated_duration_sec", 0.0)),
                    intent=s.get("intent", ""),
                )
                for i, s in enumerate(steps_data)
            ]

        if results.get("context_preheat"):
            r = results["context_preheat"]
            panorama.context_preheat = ContextPreheat(
                pinned_assets=r.get("pinned_assets", []),
                semantic_top_k=r.get("semantic_top_k", []),
                methodology_refs=r.get("methodology_refs", []),
                capability_card_snapshot=r.get("capability_card_snapshot", {}),
                depth=r.get("depth", "shallow"),
            )

        if results.get("resource_estimate"):
            r = results["resource_estimate"]
            panorama.estimated_total_cost_usd = float(r.get("total_cost_usd", 0.0))
            panorama.estimated_total_duration_sec = float(r.get("total_duration_sec", 0.0))
            panorama.estimated_total_tokens = int(r.get("total_tokens", 0))

        if results.get("attention_allocation"):
            r = results["attention_allocation"]
            panorama.attention_allocation = AttentionAllocation(
                importance=float(r.get("importance", 0.0)),
                complexity=float(r.get("complexity", 0.0)),
                urgency=float(r.get("urgency", 0.0)),
                surprise=float(r.get("surprise", 0.0)),
                risk=float(r.get("risk", 0.0)),
                overall_score=float(r.get("overall_score", 0.0)),
                chosen_model_tier=r.get("chosen_model_tier", "main"),
                chosen_evaluation_tier=int(r.get("chosen_evaluation_tier", 0)),
                chosen_sandbox_tier=r.get("chosen_sandbox_tier", "硬化容器"),
            )

        if results.get("role_instantiation"):
            r = results["role_instantiation"]
            roles_data = r.get("roles", [])
            panorama.role_instances = [
                RoleInstance(
                    role_template_ref=ri.get("role_template_ref", ""),
                    instance_id=ri.get("instance_id", ""),
                    capability_card_ref=ri.get("capability_card_ref"),
                    assigned_steps=ri.get("assigned_steps", []),
                )
                for ri in roles_data
            ]

        if results.get("multi_judge_review"):
            panorama.multi_judge_review = results["multi_judge_review"]

        if results.get("alternative_paths"):
            r = results["alternative_paths"]
            from kun.core.task_panorama import AlternativePath

            paths_data = r.get("alternatives", [])
            panorama.alternative_paths = [
                AlternativePath(
                    path_id=ap.get("path_id", f"alt-{i}"),
                    description=ap.get("description", ""),
                    estimated_cost_usd=float(ap.get("estimated_cost_usd", 0.0)),
                    estimated_duration_sec=float(ap.get("estimated_duration_sec", 0.0)),
                    rejected_reason=ap.get("rejected_reason"),
                )
                for i, ap in enumerate(paths_data)
            ]

        elapsed_ms = (time.perf_counter_ns() - start_ns) // 1_000_000
        panorama.generated_in_ms = int(elapsed_ms)
        return panorama


__all__ = [
    "PREFLIGHT_MODULES",
    "ModuleRunner",
    "PanoramaBuilder",
    "detect_tier",
]
