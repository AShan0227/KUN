"""Capability-card calibration task set (ADR-011, 方案附录 C).

6 tasks cover: coding / writing / research / data / reasoning / orchestration.

Each task has:
  - task_id
  - prompt
  - rubric (name → weight)
  - expected_signals (substrings or properties we grade against)
  - estimated_cost_usd + duration_sec

Running the set:
  result = await run_calibration_set(entity_type=..., entity_id=...)

The result updates a CapabilityCard for the given entity.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from kun.core.db import session_scope
from kun.core.logging import get_logger
from kun.core.orm import CapabilityCardRow
from kun.core.tenancy import current_tenant
from kun.datamodel.capability import (
    Boundaries,
    Capability,
    CapabilityCard,
    DecayModel,
    EntityRef,
    EntityType,
    QualityMetrics,
    Stats,
)
from kun.engineering.orchestrator import Orchestrator

log = get_logger("kun.skills.calibration")


# ========= Task definitions =========

CALIBRATION_TASKS: list[dict[str, Any]] = [
    {
        "task_id": "calibration.coding",
        "prompt": (
            "用 Python 写一个函数 fibonacci(n: int) -> int, 带类型提示, "
            "n<0 时抛 ValueError. 只要函数代码, 不要其他解释."
        ),
        "rubric": {"correctness": 0.4, "type_hints": 0.2, "error_handling": 0.2, "style": 0.2},
        "expected_signals": [
            "def fibonacci",
            "-> int",
            "ValueError",
        ],
        "estimated_cost_usd": 0.01,
        "estimated_duration_sec": 10.0,
        "task_type": "coding.python.basic",
    },
    {
        "task_id": "calibration.writing",
        "prompt": (
            "为产品 'KUN 是一个 agent 管家' 写一条 150-200 字的朋友圈文案, "
            "目标人群是技术人. 不要堆砌 emoji."
        ),
        "rubric": {"hook": 0.3, "density": 0.3, "tone": 0.2, "length": 0.2},
        "expected_signals": ["KUN"],
        "estimated_cost_usd": 0.02,
        "estimated_duration_sec": 15.0,
        "task_type": "writing.marketing",
    },
    {
        "task_id": "calibration.research",
        "prompt": (
            "列出 2026 年 AI Agent 领域三个重要趋势, 每个一句话总结加一个权威来源 URL. "
            "结果结构化输出."
        ),
        "rubric": {"accuracy": 0.4, "authority": 0.3, "diversity": 0.15, "clarity": 0.15},
        "expected_signals": ["http", "2026"],
        "estimated_cost_usd": 0.05,
        "estimated_duration_sec": 30.0,
        "task_type": "research.trend_scan",
    },
    {
        "task_id": "calibration.data",
        "prompt": (
            "给定 200 行销售 CSV, 按地区聚合计算总额 + 中位数 + top 5 产品. "
            "说明用什么工具和公式. (CSV 用伪数据也可, 只评估方法)"
        ),
        "rubric": {
            "correctness": 0.5,
            "structured_output": 0.3,
            "tool_choice": 0.2,
        },
        "expected_signals": ["median", "top 5", "group"],
        "estimated_cost_usd": 0.02,
        "estimated_duration_sec": 20.0,
        "task_type": "data.aggregation",
    },
    {
        "task_id": "calibration.reasoning",
        "prompt": (
            "请解出这个 4x4 数独 (给部分已填): "
            "第 1 行 [1, ?, ?, 4], 第 2 行 [?, ?, 3, ?], "
            "第 3 行 [?, 2, ?, ?], 第 4 行 [4, ?, ?, ?]. "
            "输出完整结果并给出推理步骤."
        ),
        "rubric": {"correctness": 0.5, "reasoning_chain": 0.35, "backtracking": 0.15},
        "expected_signals": ["行", "列", "1", "2", "3", "4"],
        "estimated_cost_usd": 0.03,
        "estimated_duration_sec": 30.0,
        "task_type": "reasoning.puzzle",
    },
    {
        "task_id": "calibration.orchestration",
        "prompt": (
            "完成 3 步链式任务: 1) 搜索 '智能体 2026 动态' 最近 3 个月进展, "
            "2) 提炼 3 个要点, 3) 写一封给朋友的介绍邮件 (含这 3 个要点). "
            "每一步显示中间产物再进入下一步."
        ),
        "rubric": {
            "step_completeness": 0.3,
            "mid_artifact_quality": 0.3,
            "final_quality": 0.3,
            "orchestration_efficiency": 0.1,
        },
        "expected_signals": ["步骤 1", "步骤 2", "步骤 3"],
        "estimated_cost_usd": 0.08,
        "estimated_duration_sec": 60.0,
        "task_type": "orchestration.multi_step",
    },
]


# ========= Runner =========


def _grade(content: str, rubric: dict[str, float], expected_signals: list[str]) -> float:
    """Very simple heuristic grader.

    Walking skeleton: we grade by presence of expected signals.
    Later we'll run an LLM judge over rubric dimensions.
    """
    if not content:
        return 0.0
    hits = sum(1 for sig in expected_signals if sig.lower() in content.lower())
    signal_ratio = hits / max(1, len(expected_signals))
    # If we got at least 2/3 of signals, call it pass. Else partial or fail.
    return max(0.0, min(1.0, signal_ratio))


async def run_calibration_set(
    *,
    entity_type: str = "role_template",
    entity_id: str = "rt-default",
) -> list[dict[str, Any]]:
    """Run all calibration tasks against an entity, return scored results."""
    orch = Orchestrator()
    tenant = current_tenant()

    results: list[dict[str, Any]] = []
    capabilities: list[Capability] = []

    for task_def in CALIBRATION_TASKS:
        log.info("calibration.running", task=task_def["task_id"])
        try:
            tr = await orch.run(task_def["prompt"])
            content = tr.answer or ""
            score = _grade(content, task_def["rubric"], task_def["expected_signals"])
            cost = tr.cost_usd_equivalent
            duration = tr.duration_sec
            status = "pass" if score >= 0.66 else ("partial" if score >= 0.33 else "fail")
        except Exception as e:
            log.exception("calibration.task_failed", task=task_def["task_id"], err=str(e))
            score, cost, duration, status = 0.0, 0.0, 0.0, "fail"

        results.append(
            {
                "task_id": task_def["task_id"],
                "status": status,
                "score": score,
                "cost_usd": cost,
                "duration_sec": duration,
                "task_type": task_def["task_type"],
            }
        )

        capabilities.append(
            Capability(
                task_type=task_def["task_type"],
                short_description=task_def["task_id"],
                stats=Stats(
                    total_invocations=1,
                    success_count=1 if status == "pass" else 0,
                    partial_success_count=1 if status == "partial" else 0,
                    failure_count=1 if status == "fail" else 0,
                    avg_cost_usd=cost,
                    avg_duration_sec=duration,
                    duration_p50=duration,
                    duration_p95=duration,
                    duration_p99=duration,
                ),
                quality=QualityMetrics(
                    avg_rubric_score=score * 5.0,  # 0-1 → 0-5
                    consistency_score=0.5,
                    last_benchmark_score=score,
                ),
                decay=DecayModel(half_life_days=30, effective_sample_size=1.0),
                boundaries=Boundaries(),
            )
        )

    # Recompute stats
    for cap in capabilities:
        cap.stats.recompute_rate()

    # Upsert CapabilityCard
    await _upsert_card(tenant.tenant_id, entity_type, entity_id, capabilities)
    return results


async def _upsert_card(
    tenant_id: str,
    entity_type_str: str,
    entity_id: str,
    capabilities: list[Capability],
) -> None:
    # Build fresh card and persist
    entity_type: EntityType = entity_type_str  # type: ignore[assignment]
    card = CapabilityCard(
        entity_ref=EntityRef(entity_type=entity_type, entity_id=entity_id),
        capabilities=capabilities,
    )
    card.recompute_summary()

    async with session_scope() as s:
        existing = (
            await s.execute(
                select(CapabilityCardRow).where(
                    CapabilityCardRow.tenant_id == tenant_id,
                    CapabilityCardRow.entity_type == entity_type_str,
                    CapabilityCardRow.entity_id == entity_id,
                )
            )
        ).scalar_one_or_none()

        if existing is None:
            s.add(
                CapabilityCardRow(
                    card_id=card.card_id,
                    tenant_id=tenant_id,
                    entity_type=entity_type_str,
                    entity_id=entity_id,
                    version=card.version,
                    maturity=card.maturity,
                    overall_reliability=card.overall_reliability,
                    primary_strength=card.primary_strength,
                    primary_weakness=card.primary_weakness,
                    card_json=card.model_dump(mode="json"),
                    created_at=card.created_at,
                    last_updated=card.last_updated,
                )
            )
        else:
            existing.version = (existing.version or 0) + 1
            existing.maturity = card.maturity
            existing.overall_reliability = card.overall_reliability
            existing.primary_strength = card.primary_strength
            existing.primary_weakness = card.primary_weakness
            existing.card_json = card.model_dump(mode="json")
            existing.last_updated = datetime.now(UTC)
