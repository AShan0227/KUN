"""V2.4: Verification 模板自动生成 (基于 dogfood / task 真数据).

观察某个 task_type 的成功 task → 提取共同特征 → 自动建议 verification spec.

简化: 现在按 task_type 收集 done/failed 的 (cost / duration / answer_length)
分布 → 当样本 ≥ 阈值时建议一组 default verification.

KUN_VERIFICATION_AUTO_GEN_ENABLED=1 (default ON 内测).
"""

from __future__ import annotations

import os
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TaskOutcomeSample:
    task_type: str
    answer_length: int
    duration_sec: float
    cost_usd: float
    success: bool


@dataclass
class AutoVerifyTemplate:
    task_type: str
    sample_size: int
    suggested: list[dict[str, Any]] = field(default_factory=list)


class VerificationAutoGen:
    """收集 task outcome → 建议 verification 模板."""

    def __init__(self) -> None:
        self._samples: dict[str, list[TaskOutcomeSample]] = defaultdict(list)

    def record(self, sample: TaskOutcomeSample) -> None:
        if not sample.success:
            return  # 只学成功 task 的特征
        self._samples[sample.task_type].append(sample)
        # 限内存: 每 task_type 最多 200 sample
        if len(self._samples[sample.task_type]) > 200:
            self._samples[sample.task_type] = self._samples[sample.task_type][-200:]

    def suggest(self, task_type: str, *, min_samples: int = 5) -> AutoVerifyTemplate:
        """返自动 build 的 verification template. 样本不足 → 空 list."""
        if os.getenv("KUN_VERIFICATION_AUTO_GEN_ENABLED", "1") != "1":
            return AutoVerifyTemplate(task_type=task_type, sample_size=0)

        samples = self._samples.get(task_type, [])
        if len(samples) < min_samples:
            return AutoVerifyTemplate(task_type=task_type, sample_size=len(samples))

        lengths = [s.answer_length for s in samples]
        # 用 p10 / p90 作 min/max 边界 (排除极端)
        sorted_lengths = sorted(lengths)
        p10_idx = max(0, int(len(sorted_lengths) * 0.10))
        p90_idx = min(len(sorted_lengths) - 1, int(len(sorted_lengths) * 0.90))
        suggested = [
            {
                "kind": "exact_output",
                "spec": {
                    "min_length_chars": sorted_lengths[p10_idx],
                    "max_length_chars": sorted_lengths[p90_idx] * 2,  # 上限给宽
                },
                "required": False,  # auto-gen 默认 optional, 用户 confirm 后转 required
            },
        ]
        try:
            avg_cost = statistics.mean(s.cost_usd for s in samples)
            if avg_cost > 0:
                suggested.append(
                    {
                        "kind": "custom",
                        "spec": {
                            "rule": "cost_within_3x_avg",
                            "avg_cost_usd": round(avg_cost, 4),
                        },
                        "required": False,
                    }
                )
        except statistics.StatisticsError:
            pass

        return AutoVerifyTemplate(
            task_type=task_type,
            sample_size=len(samples),
            suggested=suggested,
        )

    def reset(self) -> None:
        self._samples.clear()


_singleton: VerificationAutoGen | None = None


def get_verification_auto_gen() -> VerificationAutoGen:
    global _singleton
    if _singleton is None:
        _singleton = VerificationAutoGen()
    return _singleton


def reset_verification_auto_gen() -> None:
    global _singleton
    _singleton = None


__all__ = [
    "AutoVerifyTemplate",
    "TaskOutcomeSample",
    "VerificationAutoGen",
    "get_verification_auto_gen",
    "reset_verification_auto_gen",
]
