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


__all__ = ["JudgeBallot", "JuryVerdict", "jury_evaluate"]
