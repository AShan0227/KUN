"""多判官投票评估。

目标：用 3-5 个独立 judge 并发评估同一个产物，多数票定结论。
长期质量目标是和人类评审保持 Spearman 相关系数 0.80+；这个目标由
integration/human-alignment 测试集长期跟踪，本模块只提供运行时机制。
"""

from __future__ import annotations

import asyncio
import json
import math
import random
import re
import uuid
from dataclasses import dataclass
from typing import Any, cast

from kun.core.logging import get_logger
from kun.interface.llm import LLMMessage, LLMRequest, LLMRouter

log = get_logger("kun.engineering.multi_judge")

_JUDGE_SYSTEM = """你是 KUN 的独立评估判官。
请只输出 JSON:
{
  "pass": true|false,
  "score": 0.0-1.0,
  "reason": "一句话说明"
}
"""


@dataclass(frozen=True)
class JudgeBallot:
    """单个判官的一票。"""

    judge_id: str
    pass_: bool
    score: float
    reason: str
    cost_usd_actual: float
    latency_ms: float


@dataclass(frozen=True)
class JuryVerdict:
    """多判官汇总结果。"""

    pass_: bool
    avg_score: float
    spread: float
    ballots: list[JudgeBallot]
    rationale: str


async def jury_evaluate(
    *,
    artifact: str,
    rubric: str,
    judge_models: list[str],
    router: LLMRouter,
) -> JuryVerdict:
    """并发跑多个 judge，并用多数票产出评估结论。

    失败的 judge 不会拖垮整次评估；但有效票少于 3 张时，结论为 inconclusive
    （用 pass_=False 表示不能放行，rationale 里会说明原因）。
    """
    ordered_judges = list(judge_models)
    random.SystemRandom().shuffle(ordered_judges)
    seed = uuid.uuid4().hex[:8]

    tasks = [
        _run_judge(
            artifact=artifact,
            rubric=rubric,
            judge_id=judge_id,
            position=position,
            seed=seed,
            router=router,
        )
        for position, judge_id in enumerate(ordered_judges, start=1)
    ]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)

    ballots: list[JudgeBallot] = []
    failures: list[str] = []
    for judge_id, result in zip(ordered_judges, raw_results, strict=True):
        if isinstance(result, BaseException):
            failures.append(f"{judge_id}: {type(result).__name__}")
            log.warning("multi_judge.judge_failed", judge_id=judge_id, error=str(result))
            continue
        ballots.append(result)

    if len(ballots) < 3:
        return JuryVerdict(
            pass_=False,
            avg_score=_avg_score(ballots),
            spread=_spread(ballots),
            ballots=ballots,
            rationale=(
                f"inconclusive: only {len(ballots)} valid ballots; "
                f"need at least 3; failures={failures}"
            ),
        )

    pass_count = sum(1 for ballot in ballots if ballot.pass_)
    majority = pass_count > len(ballots) / 2
    return JuryVerdict(
        pass_=majority,
        avg_score=_avg_score(ballots),
        spread=_spread(ballots),
        ballots=ballots,
        rationale=(
            f"{pass_count}/{len(ballots)} judges pass; seed={seed}; failures={failures or 'none'}"
        ),
    )


async def _run_judge(
    *,
    artifact: str,
    rubric: str,
    judge_id: str,
    position: int,
    seed: str,
    router: LLMRouter,
) -> JudgeBallot:
    prompt = (
        f"Judge id: {judge_id}\n"
        f"Randomization seed: {seed}\n"
        f"Presentation position: {position}\n\n"
        f"Rubric:\n{rubric}\n\n"
        f"Artifact:\n---\n{artifact[:4000]}\n---"
    )
    response = await router.invoke(
        LLMRequest(
            messages=[
                LLMMessage(role="system", content=_JUDGE_SYSTEM, cache=True),
                LLMMessage(role="user", content=prompt),
            ],
            temperature=0.1,
            max_tokens=256,
        ),
        purpose="judge",
    )
    parsed = _safe_parse_json(response.content)
    score = _clamp01(float(parsed.get("score", 0.5)))
    return JudgeBallot(
        judge_id=judge_id,
        pass_=_parse_pass(parsed.get("pass"), score),
        score=score,
        reason=str(parsed.get("reason", "")),
        cost_usd_actual=response.cost_usd_actual,
        latency_ms=response.latency_ms,
    )


def _avg_score(ballots: list[JudgeBallot]) -> float:
    if not ballots:
        return 0.0
    return sum(ballot.score for ballot in ballots) / len(ballots)


async def jury_evaluate_anchor_then_expand(
    *,
    artifact: str,
    rubric: str,
    judge_models: list[str],
    router: LLMRouter,
    max_rounds: int = 5,
    use_marginal_stop: bool = True,
) -> JuryVerdict:
    """V2.2 §19.3: 按需扩展式 jury_evaluate.

    现在 jury_evaluate 一次并发跑全部 judge_models. 升级后:
    - 第 1 轮: 跑 1 个 judge (anchor)
    - 后续轮次: 顺序跑下一个 judge, 用 marginal_roi 判定一致率提升慢就停
    - 至少跑 min_steps=2 个 judge 才能停 (单个 judge 不能下结论)
    - 最多跑 max_rounds (默认 5, 跟原 jury_evaluate 兼容)

    成本节省: 简单任务可能 2 个 judge 就一致 (省 60% LLM 成本); 难任务跑满 5 个.

    Args:
        max_rounds: 最大 judge 数 (含 anchor). 默认 5.
        use_marginal_stop: True (默认) → 用 ModulePresets.for_multi_judge() 自动停止
    """
    from kun.core.anchor_expand import AnchorExpandIterator
    from kun.engineering.marginal_roi import (
        MarginalROIStopCriterion,
        ModulePresets,
        ValueEstimator,
    )

    ordered_judges = list(judge_models)
    random.SystemRandom().shuffle(ordered_judges)
    seed = uuid.uuid4().hex[:8]

    if not ordered_judges:
        return JuryVerdict(
            pass_=False, avg_score=0.0, spread=0.0, ballots=[], rationale="no judges configured"
        )

    async def anchor_fn() -> JudgeBallot:
        return await _run_judge(
            artifact=artifact,
            rubric=rubric,
            judge_id=ordered_judges[0],
            position=1,
            seed=seed,
            router=router,
        )

    async def expand_fn(_anchor: JudgeBallot, prior: list[JudgeBallot]) -> JudgeBallot | None:
        idx = len(prior)
        if idx >= len(ordered_judges) or idx >= max_rounds:
            return None
        return await _run_judge(
            artifact=artifact,
            rubric=rubric,
            judge_id=ordered_judges[idx],
            position=idx + 1,
            seed=seed,
            router=router,
        )

    criterion: MarginalROIStopCriterion | None = None
    estimator: ValueEstimator | None = None
    if use_marginal_stop:
        # value = 累计一致率 (每加一个 judge, 算 ballots 一致率)
        # 一致率提升 < delta_threshold (3%) 连续 K 步 → 停
        criterion = ModulePresets.for_multi_judge()

        def consensus_estimator(item: JudgeBallot, prior: list[JudgeBallot]) -> float:
            # 重算累计一致率
            all_ballots = [*list(prior), item]
            if len(all_ballots) < 2:
                return float(all_ballots[0].score)
            pass_count = sum(1 for b in all_ballots if b.pass_)
            return pass_count / len(all_ballots)

        estimator = ValueEstimator(custom_fn=consensus_estimator)

    ballots: list[JudgeBallot] = []
    failures: list[str] = []

    iterator = AnchorExpandIterator(
        anchor_fn=anchor_fn,
        expand_fn=expand_fn,
        max_rounds=max_rounds,
        stop_criterion=criterion,
        value_estimator=estimator,
    )
    try:
        async for ballot in iterator:
            ballots.append(ballot)
    except Exception as e:
        log.warning("anchor_expand_jury.iterator_failed", error=str(e))
        failures.append(str(e))

    if len(ballots) < 2:
        return JuryVerdict(
            pass_=False,
            avg_score=_avg_score(ballots),
            spread=_spread(ballots),
            ballots=ballots,
            rationale=(
                f"inconclusive_anchor_expand: only {len(ballots)} ballots; "
                f"need at least 2; stopped_reason={iterator.stats.stopped_reason}"
            ),
        )

    pass_count = sum(1 for ballot in ballots if ballot.pass_)
    majority = pass_count > len(ballots) / 2
    return JuryVerdict(
        pass_=majority,
        avg_score=_avg_score(ballots),
        spread=_spread(ballots),
        ballots=ballots,
        rationale=(
            f"{pass_count}/{len(ballots)} judges pass (anchor_expand mode); "
            f"stopped_reason={iterator.stats.stopped_reason}; "
            f"value_history={iterator.stats.value_history}"
        ),
    )


def _spread(ballots: list[JudgeBallot]) -> float:
    if not ballots:
        return 0.0
    avg = _avg_score(ballots)
    return math.sqrt(sum((ballot.score - avg) ** 2 for ballot in ballots) / len(ballots))


def _safe_parse_json(text: str) -> dict[str, Any]:
    try:
        return cast(dict[str, Any], json.loads(text.strip()))
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            return cast(dict[str, Any], json.loads(match.group(0)))
        except json.JSONDecodeError:
            pass
    return {}


def _parse_pass(value: object, score: float) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "pass", "passed", "1"}:
            return True
        if lowered in {"false", "no", "fail", "failed", "0"}:
            return False
    return score >= 0.6


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


__all__ = [
    "JudgeBallot",
    "JuryVerdict",
    "jury_evaluate",
    "jury_evaluate_anchor_then_expand",
]
