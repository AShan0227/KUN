"""ValidationPipeline (ADR-018 §16.2) — 统一所有验证流程.

合并前: 评估触发矩阵 / 辩论 / AB / 红队 / 自我进化验收五种各跑各.
合并后: 所有验证器实现 Validator 接口, 由 ValidationPipeline 按配置编排.

评估档位 (§8.1 矩阵):

                  风险低                    风险高
    复杂度低      档 0: 无评估 (规则通过)     档 2: 多判官投票 (3-5 便宜)
    复杂度高      档 1: 单判官 + 评分表        档 3: 完整评估 (多判官 + 人抽 + 基准)
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from kun.core.logging import get_logger
from kun.core.scoring import ScoreDescriptor
from kun.datamodel.task import TaskMeta
from kun.engineering.multi_judge import jury_evaluate
from kun.interface.llm import LLMMessage, LLMRequest, LLMRouter, get_router

log = get_logger("kun.engineering.validation")

ValidationTier = Literal["tier0", "tier1", "tier2", "tier3"]
ValidatorKind = Literal[
    "single_judge",
    "multi_judge",
    "debate",
    "ab_test",
    "redteam",
    "benchmark",
]


class ValidationResult(BaseModel):
    """A single validator's verdict."""

    model_config = ConfigDict(populate_by_name=True)

    validator_kind: ValidatorKind
    pass_: bool = Field(alias="pass")
    score: ScoreDescriptor
    reason: str = ""
    details: dict[str, Any] = Field(default_factory=dict)


class Validator(Protocol):
    """Minimal validator interface."""

    kind: ValidatorKind

    async def validate(
        self,
        *,
        artifact: str,
        context: dict[str, Any],
    ) -> ValidationResult: ...


def pick_tier(meta: TaskMeta) -> ValidationTier:
    """§8.1 risk × complexity matrix.

    V2.2 §21 wire: ExecutionMode 显式设置时强制覆盖
    (FAST→tier0, MAX/ENSEMBLE→tier3).
    显式标志: mode_override_reason 非空 (说明是 classifier 真算出来的, 不是字段默认值).
    没显式设 (默认 mode=FAST + 空 reason) → 走老 risk × complexity 矩阵.
    """
    # V2.2 mode override (仅在显式设置时生效)
    mode = getattr(meta, "execution_mode", "FAST")
    reason = getattr(meta, "mode_override_reason", "") or ""
    if reason:  # classifier 显式算过
        if mode == "FAST":
            return "tier0"
        if mode in ("MAX", "ENSEMBLE"):
            return "tier3"
        # SMART 走下面老矩阵
    # 老逻辑 (risk × complexity)
    risk_high = meta.risk_level in ("high", "critical")
    complex_high = meta.complexity_score >= 0.5
    if not risk_high and not complex_high:
        return "tier0"
    if not risk_high and complex_high:
        return "tier1"
    if risk_high and not complex_high:
        return "tier2"
    return "tier3"


# =================== Judges ===================


_RUBRIC_SYSTEM = """你是 KUN 的评估判官. 对用户提供的 artifact 按 rubric 打分.
输出 JSON:
{
  "pass": true|false,
  "score": 0.0-1.0,
  "reason": "一句话"
}
"""


class SingleJudge:
    """Tier 1: 一个便宜大模型做 rubric 评分."""

    kind: ValidatorKind = "single_judge"

    def __init__(self, router: LLMRouter | None = None) -> None:
        self._router = router or get_router()

    async def validate(
        self,
        *,
        artifact: str,
        context: dict[str, Any],
    ) -> ValidationResult:
        rubric = context.get("rubric", {})
        user_prompt = (
            f"Rubric: {rubric}\n\nTask goal: {context.get('goal', '')}\n\n"
            f"Artifact to evaluate:\n---\n{artifact[:4000]}\n---"
        )
        response = await self._router.invoke(
            LLMRequest(
                messages=[
                    LLMMessage(role="system", content=_RUBRIC_SYSTEM, cache=True),
                    LLMMessage(role="user", content=user_prompt),
                ],
                temperature=0.1,
                max_tokens=256,
            ),
            purpose="judge",
        )
        parsed = _safe_parse_json(response.content)
        score = float(parsed.get("score", 0.5))
        return ValidationResult(
            validator_kind=self.kind,
            pass_=bool(parsed.get("pass", score >= 0.6)),
            score=ScoreDescriptor(
                kind="rubric", value=score, components={"rubric": score}, weights={"rubric": 1.0}
            ),
            reason=parsed.get("reason", ""),
        )


class MultiJudge:
    """Tier 2/3: N 个便宜判官, 多数票定.

    Default n_judges=3.
    """

    kind: ValidatorKind = "multi_judge"

    def __init__(self, n_judges: int = 3, router: LLMRouter | None = None) -> None:
        self._n = n_judges
        self._router = router or get_router()

    async def validate(
        self,
        *,
        artifact: str,
        context: dict[str, Any],
    ) -> ValidationResult:
        rubric = context.get("rubric", {})
        verdict = await jury_evaluate(
            artifact=artifact,
            rubric=str(rubric),
            judge_models=[f"judge_{i + 1}" for i in range(self._n)],
            router=self._router,
        )
        return ValidationResult(
            validator_kind=self.kind,
            pass_=verdict.pass_,
            score=ScoreDescriptor(
                kind="rubric",
                value=verdict.avg_score,
                components={ballot.judge_id: ballot.score for ballot in verdict.ballots},
                weights={
                    ballot.judge_id: 1.0 / max(1, len(verdict.ballots))
                    for ballot in verdict.ballots
                },
            ),
            reason=verdict.rationale,
            details={
                "spread": verdict.spread,
                "ballots": [ballot.__dict__ for ballot in verdict.ballots],
            },
        )


# =================== Debate mechanism (ADR-015) ===================


_DEBATE_SYSTEM_PROPOSER = """你是 proposer: 为 artifact 提出支持观点, 举出具体理由."""
_DEBATE_SYSTEM_OPPOSER = """你是 opposer: 对 artifact 提出反对观点, 举出具体问题."""
_DEBATE_SYSTEM_MODERATOR = """你是 moderator: 听完 proposer 和 opposer 的观点, 判定最终结论.
输出 JSON: {"pass": true|false, "score": 0-1, "reason": "..."}
"""


class DebateValidator:
    """Tier 3: proposer + opposer + moderator 三角辩论.

    学习曲线 (ADR-015): 同类任务 N 次同结论 → 固化为规则, 后续跳过辩论.
    Walking skeleton 只实现单次辩论; 规则固化由 idle-batch 汇总后写入.
    """

    kind: ValidatorKind = "debate"

    def __init__(self, router: LLMRouter | None = None) -> None:
        self._router = router or get_router()

    async def validate(
        self,
        *,
        artifact: str,
        context: dict[str, Any],
    ) -> ValidationResult:
        goal = context.get("goal", "")
        user_prompt = f"Goal: {goal}\nArtifact:\n---\n{artifact[:3000]}\n---"

        pro = await self._router.invoke(
            LLMRequest(
                messages=[
                    LLMMessage(role="system", content=_DEBATE_SYSTEM_PROPOSER, cache=True),
                    LLMMessage(role="user", content=user_prompt),
                ],
                temperature=0.5,
                max_tokens=400,
            ),
            purpose="judge",
        )
        con = await self._router.invoke(
            LLMRequest(
                messages=[
                    LLMMessage(role="system", content=_DEBATE_SYSTEM_OPPOSER, cache=True),
                    LLMMessage(role="user", content=user_prompt),
                ],
                temperature=0.5,
                max_tokens=400,
            ),
            purpose="judge",
        )
        moderator_prompt = (
            f"Goal: {goal}\n\nPro view: {pro.content}\n\nCon view: {con.content}\n\n"
            "Render a verdict as JSON."
        )
        mod = await self._router.invoke(
            LLMRequest(
                messages=[
                    LLMMessage(role="system", content=_DEBATE_SYSTEM_MODERATOR, cache=True),
                    LLMMessage(role="user", content=moderator_prompt),
                ],
                temperature=0.1,
                max_tokens=256,
            ),
            purpose="judge",
        )
        parsed = _safe_parse_json(mod.content)
        score = float(parsed.get("score", 0.5))
        return ValidationResult(
            validator_kind=self.kind,
            pass_=bool(parsed.get("pass", score >= 0.6)),
            score=ScoreDescriptor(
                kind="rubric", value=score, components={"debate": score}, weights={"debate": 1.0}
            ),
            reason=parsed.get("reason", ""),
            details={
                "proposer": pro.content,
                "opposer": con.content,
                "moderator": mod.content,
            },
        )


# =================== ValidationPipeline ===================


def _safe_parse_json(text: str) -> dict[str, Any]:
    import json
    import re

    try:
        return cast(dict[str, Any], json.loads(text.strip()))
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return cast(dict[str, Any], json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    return {}


class ValidationPipeline:
    """Orchestrate tier-selected validators."""

    def __init__(self, router: LLMRouter | None = None) -> None:
        self._router = router or get_router()

    def build_validators(self, tier: ValidationTier) -> Iterable[Validator]:
        if tier == "tier0":
            return []
        if tier == "tier1":
            return [SingleJudge(self._router)]
        if tier == "tier2":
            return [MultiJudge(n_judges=3, router=self._router)]
        # tier3 full
        return [
            MultiJudge(n_judges=5, router=self._router),
            DebateValidator(router=self._router),
        ]

    async def validate_task(
        self,
        meta: TaskMeta,
        artifact: str,
        *,
        rubric: dict[str, float] | None = None,
        goal: str | None = None,
    ) -> list[ValidationResult]:
        tier = pick_tier(meta)
        validators = list(self.build_validators(tier))
        log.info("validation.tier", task=meta.task_id, tier=tier, n_validators=len(validators))

        context = {
            "goal": goal or meta.success_criteria_short,
            "rubric": rubric or {},
            "task_meta": meta.model_dump(mode="json"),
        }
        results: list[ValidationResult] = []
        for v in validators:
            try:
                results.append(await v.validate(artifact=artifact, context=context))
            except Exception as e:
                log.exception("validator.failed", kind=v.kind, error=str(e))
        return results

    @staticmethod
    def aggregate(results: list[ValidationResult]) -> ValidationResult | None:
        """Aggregate multiple validator outputs — 'all_pass' policy by default."""
        if not results:
            return None
        all_pass = all(r.pass_ for r in results)
        avg = sum(r.score.value for r in results) / len(results)
        return ValidationResult(
            validator_kind=results[0].validator_kind,
            pass_=all_pass,
            score=ScoreDescriptor(
                kind="rubric",
                value=avg,
                components={f"v_{i}": r.score.value for i, r in enumerate(results)},
                weights={f"v_{i}": 1.0 / len(results) for i in range(len(results))},
            ),
            reason="all_pass" if all_pass else "at_least_one_failed",
            details={"validators": [r.model_dump() for r in results]},
        )
