"""V2.3 Wire 44 — AntiGamingDetector: 反作弊套路库 (V2.3 §7).

LLM 容易"耍滑头". 已知套路:
1. 答非所问 — 答案跟 prompt 关键词重合度低
2. 复制 prompt — 把 prompt 当答案
3. 假数据 — 编"看起来对"的数字 (靠 verification + 数据来源声明)
4. 跳 step — 该 4 步走 2 步
5. 抄上轮答案 — 任务变了答案没变
6. 假装答了 — "我已经处理好了" 但没真做 (没 produced asset / 没 skill trace)
7. 超 spec — 用了 protocol 不允许的 skill

跟 jury / verification 的区别:
  - AntiGamingDetector: quick check 已知套路, 命中直接拒, 省 LLM call
  - jury: 综合判断答案质量 (耗 LLM)
  - verification: 跑确定性测试 (e.g. pytest pass)

3 个一起用: AntiGaming 第 1 道, verification 第 2 道, jury 第 3 道.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Literal

logger = logging.getLogger(__name__)


GamingPattern = Literal[
    "answer_off_topic",
    "copy_prompt",
    "fake_data",
    "skip_step",
    "copy_prior_answer",
    "fake_completion",
    "over_spec",
]


@dataclass(frozen=True)
class GamingFinding:
    """检测到一个作弊套路."""

    pattern: GamingPattern
    confidence: float  # 0.0-1.0, 越高越确信是作弊
    reason: str
    severity: Literal["low", "medium", "high"] = "medium"
    evidence: dict[str, Any] = field(default_factory=dict)


# ---- 单个 pattern detector ----


def _word_overlap_ratio(text_a: str, text_b: str) -> float:
    """两段文字关键词重合度. 简化: 字符 4-gram 交集."""
    if not text_a or not text_b:
        return 0.0
    a_low = text_a.lower()
    b_low = text_b.lower()
    a_grams = {a_low[i : i + 4] for i in range(len(a_low) - 3)}
    b_grams = {b_low[i : i + 4] for i in range(len(b_low) - 3)}
    if not a_grams or not b_grams:
        return 0.0
    return len(a_grams & b_grams) / max(len(a_grams), len(b_grams))


def _string_similarity(a: str, b: str) -> float:
    """两个 string 相似度 (SequenceMatcher 0-1)."""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def detect_answer_off_topic(
    *, prompt: str, answer: str, threshold: float = 0.10
) -> GamingFinding | None:
    """答案跟 prompt 关键词重合度低 → 答非所问.

    threshold: 重合度低于此 → 命中.
    """
    if not prompt or not answer:
        return None
    overlap = _word_overlap_ratio(prompt, answer)
    if overlap < threshold and len(answer) > 20:  # 答案有内容但跟问题无关
        return GamingFinding(
            pattern="answer_off_topic",
            confidence=1.0 - overlap / threshold,
            reason=f"answer↔prompt overlap={overlap:.2f} < threshold={threshold}",
            severity="medium",
            evidence={"overlap": overlap, "answer_len": len(answer)},
        )
    return None


def detect_copy_prompt(
    *, prompt: str, answer: str, threshold: float = 0.80
) -> GamingFinding | None:
    """答案跟 prompt 太相似 → 复制 prompt 当答案."""
    if not prompt or not answer:
        return None
    sim = _string_similarity(prompt, answer)
    if sim > threshold:
        return GamingFinding(
            pattern="copy_prompt",
            confidence=sim,
            reason=f"answer 跟 prompt 相似度 {sim:.2f} > {threshold}",
            severity="high",
            evidence={"similarity": sim},
        )
    return None


def detect_copy_prior_answer(
    *, answer: str, prior_answers: list[str], threshold: float = 0.85
) -> GamingFinding | None:
    """答案跟之前 step 答案太像 → 抄上轮."""
    if not answer or not prior_answers:
        return None
    for prior in prior_answers:
        sim = _string_similarity(answer, prior)
        if sim > threshold:
            return GamingFinding(
                pattern="copy_prior_answer",
                confidence=sim,
                reason=f"答案跟某 prior step 相似度 {sim:.2f} > {threshold}",
                severity="high",
                evidence={"similarity": sim, "prior_excerpt": prior[:100]},
            )
    return None


def detect_skip_step(
    *, planned_steps: int, actual_steps: int, threshold_ratio: float = 0.5
) -> GamingFinding | None:
    """实际 step 数 < 计划 step 数 × threshold → 跳 step."""
    if planned_steps <= 0:
        return None
    ratio = actual_steps / planned_steps
    if ratio < threshold_ratio:
        return GamingFinding(
            pattern="skip_step",
            confidence=1.0 - ratio,
            reason=f"actual={actual_steps} planned={planned_steps} ratio={ratio:.2f}",
            severity="high",
            evidence={"planned": planned_steps, "actual": actual_steps},
        )
    return None


_FAKE_COMPLETION_KEYWORDS = [
    "已完成",
    "处理好了",
    "搞定",
    "完成了",
    "没问题",
    "done",
    "completed",
    "finished",
]


def detect_fake_completion(
    *, answer: str, has_assets: bool = False, has_skill_traces: bool = False
) -> GamingFinding | None:
    """答案声称完成但没真 produced asset / 没 skill 调用 trace."""
    if has_assets or has_skill_traces:
        return None
    answer_lower = answer.lower()
    has_completion_word = any(kw in answer_lower for kw in _FAKE_COMPLETION_KEYWORDS)
    if has_completion_word and len(answer) < 200:  # 短答案 + 完成词 + 无证据
        return GamingFinding(
            pattern="fake_completion",
            confidence=0.7,
            reason="答案含完成词但无 asset / skill trace",
            severity="high",
            evidence={"answer_len": len(answer)},
        )
    return None


def detect_over_spec(
    *,
    used_skills: list[str],
    allowed_skills: list[str] | None,
) -> GamingFinding | None:
    """用了 protocol 没允许的 skill (allowed_skills=None → 不检测)."""
    if allowed_skills is None:
        return None
    over = [s for s in used_skills if s not in allowed_skills]
    if over:
        return GamingFinding(
            pattern="over_spec",
            confidence=0.9,
            reason=f"用了不允许的 skill: {over[:3]}",
            severity="medium",
            evidence={"unauthorized_skills": over},
        )
    return None


_SUSPICIOUS_NUMBER_PATTERN = re.compile(
    r"\b(?:1\d{6,}|99\.9+%|100%|exactly|绝对)",
    re.IGNORECASE,
)


def detect_fake_data(*, answer: str, verification_passed: bool = True) -> GamingFinding | None:
    """LLM 编看起来对的数字 — 启发式: 含可疑数字 + verification 没真验证."""
    if verification_passed:
        return None  # verification 真验过, 数据可信
    matches = _SUSPICIOUS_NUMBER_PATTERN.findall(answer)
    if matches:
        return GamingFinding(
            pattern="fake_data",
            confidence=0.6,
            reason=f"含可疑精确数字 {matches[:3]} 但 verification 未通过",
            severity="medium",
            evidence={"suspicious_tokens": matches[:5]},
        )
    return None


# ---- AntiGamingDetector — 整合 7 个 pattern ----


class AntiGamingDetector:
    """整合所有作弊套路检测.

    用法:
        detector = AntiGamingDetector()
        finding = detector.check(
            prompt="...", answer="...", prior_answers=[...],
            planned_steps=4, actual_steps=2,
        )
        if finding:
            # 拒绝 step / mark task failed / emit gaming.detected event
            ...
    """

    def __init__(
        self,
        *,
        off_topic_threshold: float = 0.10,
        copy_prompt_threshold: float = 0.80,
        copy_prior_threshold: float = 0.85,
        skip_step_threshold: float = 0.5,
    ) -> None:
        self.off_topic_threshold = off_topic_threshold
        self.copy_prompt_threshold = copy_prompt_threshold
        self.copy_prior_threshold = copy_prior_threshold
        self.skip_step_threshold = skip_step_threshold

    def check(
        self,
        *,
        prompt: str = "",
        answer: str = "",
        prior_answers: list[str] | None = None,
        planned_steps: int = 0,
        actual_steps: int = 0,
        used_skills: list[str] | None = None,
        allowed_skills: list[str] | None = None,
        has_assets: bool = False,
        has_skill_traces: bool = False,
        verification_passed: bool = True,
    ) -> GamingFinding | None:
        """跑所有 pattern, 返第 1 个命中. None = 没作弊."""
        # 顺序: 高 severity 优先
        checks = [
            lambda: detect_copy_prompt(
                prompt=prompt, answer=answer, threshold=self.copy_prompt_threshold
            ),
            lambda: detect_copy_prior_answer(
                answer=answer,
                prior_answers=prior_answers or [],
                threshold=self.copy_prior_threshold,
            ),
            lambda: detect_fake_completion(
                answer=answer, has_assets=has_assets, has_skill_traces=has_skill_traces
            ),
            lambda: detect_skip_step(
                planned_steps=planned_steps,
                actual_steps=actual_steps,
                threshold_ratio=self.skip_step_threshold,
            ),
            lambda: detect_over_spec(used_skills=used_skills or [], allowed_skills=allowed_skills),
            lambda: detect_answer_off_topic(
                prompt=prompt, answer=answer, threshold=self.off_topic_threshold
            ),
            lambda: detect_fake_data(answer=answer, verification_passed=verification_passed),
        ]
        for check in checks:
            try:
                finding = check()
                if finding is not None:
                    return finding
            except Exception as e:
                logger.debug("anti_gaming.detector_failed err=%s", e)
        return None


__all__ = [
    "AntiGamingDetector",
    "GamingFinding",
    "GamingPattern",
    "detect_answer_off_topic",
    "detect_copy_prior_answer",
    "detect_copy_prompt",
    "detect_fake_completion",
    "detect_fake_data",
    "detect_over_spec",
    "detect_skip_step",
]
