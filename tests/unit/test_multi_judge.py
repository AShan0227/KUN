"""多判官投票测试。"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable

import pytest
from kun.engineering.multi_judge import jury_evaluate
from kun.engineering.validation import MultiJudge
from kun.interface.llm import LLMRequest, LLMResponse, LLMRouter
from kun.interface.llm.base import UsageInfo
from kun.interface.llm.stub_provider import StubProvider


class _QueueRouter(LLMRouter):
    def __init__(self, responses: Iterable[LLMResponse | BaseException]) -> None:
        provider = StubProvider()
        super().__init__({"cheap": provider, "fallback": provider})
        self._responses = list(responses)

    async def invoke(self, request: LLMRequest, *, purpose: str = "execution") -> LLMResponse:
        await asyncio.sleep(0)
        if not self._responses:
            raise RuntimeError("no stub response left")
        response = self._responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _response(
    *,
    pass_: bool,
    score: float,
    reason: str = "ok",
    cost: float = 0.01,
    latency: float = 12.0,
) -> LLMResponse:
    return LLMResponse(
        content=f'{{"pass": {str(pass_).lower()}, "score": {score}, "reason": "{reason}"}}',
        usage=UsageInfo(input_tokens=10, output_tokens=5),
        cost_usd_actual=cost,
        latency_ms=latency,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jury_majority_passes() -> None:
    verdict = await jury_evaluate(
        artifact="good result",
        rubric="correctness",
        judge_models=["a", "b", "c"],
        router=_QueueRouter(
            [
                _response(pass_=True, score=0.9),
                _response(pass_=False, score=0.4),
                _response(pass_=True, score=0.8),
            ]
        ),
    )

    assert verdict.pass_ is True
    assert len(verdict.ballots) == 3
    assert verdict.avg_score == pytest.approx(0.7)
    assert verdict.spread > 0
    assert "2/3 judges pass" in verdict.rationale


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jury_failed_judges_do_not_block_when_three_votes_remain() -> None:
    verdict = await jury_evaluate(
        artifact="good result",
        rubric="correctness",
        judge_models=["a", "b", "c", "d"],
        router=_QueueRouter(
            [
                _response(pass_=True, score=0.9),
                RuntimeError("judge down"),
                _response(pass_=True, score=0.8),
                _response(pass_=False, score=0.2),
            ]
        ),
    )

    assert verdict.pass_ is True
    assert len(verdict.ballots) == 3
    assert "failures=" in verdict.rationale


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jury_returns_inconclusive_when_fewer_than_three_votes_survive() -> None:
    verdict = await jury_evaluate(
        artifact="unknown result",
        rubric="correctness",
        judge_models=["a", "b", "c"],
        router=_QueueRouter(
            [
                _response(pass_=True, score=0.9),
                RuntimeError("judge down"),
                RuntimeError("judge down again"),
            ]
        ),
    )

    assert verdict.pass_ is False
    assert len(verdict.ballots) == 1
    assert "inconclusive" in verdict.rationale


@pytest.mark.unit
@pytest.mark.asyncio
async def test_jury_spread_is_zero_when_judges_agree() -> None:
    verdict = await jury_evaluate(
        artifact="same result",
        rubric="correctness",
        judge_models=["a", "b", "c"],
        router=_QueueRouter(
            [
                _response(pass_=True, score=0.7),
                _response(pass_=True, score=0.7),
                _response(pass_=True, score=0.7),
            ]
        ),
    )

    assert verdict.spread == pytest.approx(0.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_validation_multi_judge_wraps_jury_verdict() -> None:
    validator = MultiJudge(
        n_judges=3,
        router=_QueueRouter(
            [
                _response(pass_=True, score=0.9),
                _response(pass_=True, score=0.8),
                _response(pass_=False, score=0.2),
            ]
        ),
    )

    result = await validator.validate(
        artifact="good enough",
        context={"rubric": {"correctness": 1.0}},
    )

    assert result.validator_kind == "multi_judge"
    assert result.pass_ is True
    assert result.score.value == pytest.approx((0.9 + 0.8 + 0.2) / 3)
    assert "spread" in result.details
