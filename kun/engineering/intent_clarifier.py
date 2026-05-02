"""IntentClarifier — 信息饱和度判断 + 主动反问机制 (V2.1 §5.1.1 + §5.1.2 / T15 + T34).

V1 intent.py 直接结构化, 信息不足也开干.
V2.1 加:
- IntentSaturation: 评估"信息够不够" (TASK.md 必填字段完整度 + 同类历史所需维度覆盖度)
- IntentClarifier: 信息不足时主动反问用户 (优先结构化 A/B/C 选一)

判据 (V2 §17.5 决策点 #3):
- TASK.md 必填字段缺 → 必反问
- risk=critical 但用户无明示偏好 → 必反问
- complexity > 0.7 且 prompt < 20 字 → 必反问
- 含"等等"等省略词 → 必反问 (KUN 主动列可能涵盖项)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

# 含义模糊的省略词
ELLIPSIS_HINTS = [
    "等等",
    "等等等",
    "之类的",
    "之类",
    "对了",
    "另外那个",
    "之前那个",
    "刚才那个",
    "etc",
    "and so on",
    "...",
]

# critical 任务的必填字段
CRITICAL_REQUIRED_FIELDS = (
    "success_criteria_short",
    "deliverable",
    "deadline_iso_or_none",
)

# 一般任务必填字段
NORMAL_REQUIRED_FIELDS = ("intent_one_sentence",)


@dataclass
class SaturationResult:
    """信息饱和度评估结果."""

    saturation_score: float  # 0-1, 1 = 信息完全充分
    missing_fields: list[str] = field(default_factory=list)
    risk_signals: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    force_plan_only: bool = False


@dataclass
class ClarificationQuestion:
    """单个反问问题."""

    field_name: str
    question_text: str
    question_kind: Literal["choice", "fill", "yes_no", "free"] = "choice"
    choices: list[str] = field(default_factory=list)
    default_suggestion: str = ""  # KUN 猜的默认答案 (用户改即可)
    rationale: str = ""


@dataclass
class ClarificationRequest:
    """完整反问请求 (推到 user 的 ask_user 块)."""

    questions: list[ClarificationQuestion]
    summary: str = ""
    suggested_default_action: str = ""  # 用户跳过时的默认行为


class IntentSaturation:
    """信息饱和度判断器 (V2.1 §5.1.1 / T34)."""

    @staticmethod
    def evaluate(
        task_meta: dict[str, object],
        user_prompt: str,
    ) -> SaturationResult:
        """评估信息够不够."""
        missing = []
        risk_signals = []

        # 1. 必填字段检查
        risk = str(task_meta.get("risk_level", "low"))
        required = (
            list(NORMAL_REQUIRED_FIELDS) + list(CRITICAL_REQUIRED_FIELDS)
            if risk == "critical"
            else list(NORMAL_REQUIRED_FIELDS)
        )
        for f in required:
            if not task_meta.get(f):
                missing.append(f)

        # 2. risk=critical 但无用户偏好 (approval_threshold / risk_tolerance)
        if risk == "critical":
            if "approval_threshold_money" not in task_meta:
                risk_signals.append("critical_no_approval_threshold")
            if "risk_tolerance" not in task_meta:
                risk_signals.append("critical_no_risk_tolerance")

        # 3. 复杂任务但 prompt 短 (信息密度太低)
        complexity_raw = task_meta.get("complexity_score", 0.0)
        try:
            complexity = float(complexity_raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            complexity = 0.0
        if complexity > 0.7 and len(user_prompt.strip()) < 20:
            risk_signals.append("high_complexity_short_prompt")

        # 4. 省略词检测
        prompt_lower = user_prompt.lower()
        for hint in ELLIPSIS_HINTS:
            if hint in prompt_lower:
                risk_signals.append(f"ellipsis:{hint}")
                break

        # 5. 计算饱和度分数 (0-1, 越高信息越足)
        score = 1.0
        if missing:
            score -= 0.2 * len(missing)
        if risk_signals:
            score -= 0.15 * len(risk_signals)
        score = max(0.0, min(1.0, score))

        # 6. 判断是否反问
        needs_clarification = bool(missing) or bool(risk_signals) or score < 0.6
        force_plan_only = score < 0.4

        return SaturationResult(
            saturation_score=score,
            missing_fields=missing,
            risk_signals=risk_signals,
            needs_clarification=needs_clarification,
            force_plan_only=force_plan_only,
        )


class IntentClarifier:
    """主动反问机制 (V2.1 §5.1.2 / T15).

    根据 SaturationResult 生成结构化反问问题.
    优先 choice (A/B/C), 次 fill (填字段), 避免开放问题.
    一次问完所有不确定项, 不分多轮.
    """

    @staticmethod
    def build_request(
        saturation: SaturationResult,
        task_meta: dict[str, object],
        user_prompt: str,
    ) -> ClarificationRequest | None:
        """生成反问请求. 不需要反问返 None."""
        if not saturation.needs_clarification:
            return None

        questions: list[ClarificationQuestion] = []

        # 必填字段反问
        for field_name in saturation.missing_fields:
            q = _question_for_field(field_name, task_meta, user_prompt)
            if q is not None:
                questions.append(q)

        # 风险信号反问
        for signal in saturation.risk_signals:
            q = _question_for_risk_signal(signal, task_meta, user_prompt)
            if q is not None:
                questions.append(q)

        if not questions:
            return None

        summary = f"我有 {len(questions)} 个不确定的地方, 一次问清楚, 你选默认或自己改:"
        return ClarificationRequest(
            questions=questions,
            summary=summary,
            suggested_default_action=(
                "(用户跳过) 按我的最佳猜测执行 + 标注 confidence"
                if not saturation.force_plan_only
                else "(用户跳过) 强制 plan-only, 不直接执行"
            ),
        )


def _question_for_field(
    field_name: str,
    task_meta: dict[str, object],
    user_prompt: str,
) -> ClarificationQuestion | None:
    """为缺失字段生成问题."""
    if field_name == "success_criteria_short":
        return ClarificationQuestion(
            field_name="success_criteria_short",
            question_text="这个任务怎么算成功? (一句话)",
            question_kind="fill",
            default_suggestion="按我的理解执行完毕",
        )
    if field_name == "deliverable":
        return ClarificationQuestion(
            field_name="deliverable",
            question_text="想要什么形式的产出?",
            question_kind="choice",
            choices=["代码文件", "Markdown 文档", "只在对话回答", "数据/表格"],
            default_suggestion="只在对话回答",
        )
    if field_name == "deadline_iso_or_none":
        return ClarificationQuestion(
            field_name="deadline",
            question_text="有 deadline 吗?",
            question_kind="choice",
            choices=["越快越好", "今天内", "本周内", "不急"],
            default_suggestion="越快越好",
        )
    if field_name == "intent_one_sentence":
        return ClarificationQuestion(
            field_name="intent_one_sentence",
            question_text="一句话说你想做什么?",
            question_kind="fill",
            default_suggestion=user_prompt[:80] if user_prompt else "(请填)",
        )
    return None


def _question_for_risk_signal(
    signal: str,
    task_meta: dict[str, object],
    user_prompt: str,
) -> ClarificationQuestion | None:
    """为风险信号生成问题."""
    if signal == "critical_no_approval_threshold":
        return ClarificationQuestion(
            field_name="approval_threshold_money",
            question_text="critical 任务, 多少钱以上必须问你?",
            question_kind="choice",
            choices=["$1", "$10", "$100", "$1000", "都问"],
            default_suggestion="$10",
        )
    if signal == "critical_no_risk_tolerance":
        return ClarificationQuestion(
            field_name="risk_tolerance",
            question_text="critical 任务, 风险容忍度?",
            question_kind="choice",
            choices=["低 (保守, 多审一道)", "中 (默认)", "高 (放手干)"],
            default_suggestion="低 (保守, 多审一道)",
        )
    if signal == "high_complexity_short_prompt":
        return ClarificationQuestion(
            field_name="more_context",
            question_text="复杂任务但描述太简, 能多说几句吗?",
            question_kind="free",
            default_suggestion="按当前理解开干 (confidence 标低)",
        )
    if signal.startswith("ellipsis:"):
        # 省略词 → 列可能涵盖项让用户选 (KUN 主动补齐"等等")
        elide = signal.split(":", 1)[1]
        suggestions = _guess_ellipsis_expansion(user_prompt, elide)
        return ClarificationQuestion(
            field_name="ellipsis_expansion",
            question_text=f"你说的 '{elide}' 我猜可能涵盖以下, 你选/补:",
            question_kind="choice",
            choices=suggestions or ["按当前理解执行"],
            default_suggestion=suggestions[0] if suggestions else "按当前理解执行",
            rationale=f"省略词 '{elide}' 命中, KUN 主动补齐(§18 全局视角)",
        )
    return None


def _guess_ellipsis_expansion(prompt: str, elide: str) -> list[str]:
    """猜省略词可能涵盖的具体内容.

    简单启发式: 取省略词前面的几个名词.
    生产: M4 接 LLM 模式 B (§17.10) 给更智能猜测.
    """
    # 简单的中文/英文启发式
    parts = re.split(r"[,，;;。.\n]", prompt)
    nouns = []
    for p in parts:
        p_strip = p.strip()
        if elide in p_strip:
            # 取本句的关键词
            tokens = re.findall(r"[\w一-鿿]+", p_strip)
            for t in tokens:
                if len(t) >= 2 and t != elide:
                    nouns.append(t)
    if nouns:
        return [f"包括 {n}" for n in nouns[:3]] + ["其他, 请我列出"]
    return ["按当前理解扩展", "请你自己列出涵盖项"]


__all__ = [
    "CRITICAL_REQUIRED_FIELDS",
    "ELLIPSIS_HINTS",
    "NORMAL_REQUIRED_FIELDS",
    "ClarificationQuestion",
    "ClarificationRequest",
    "IntentClarifier",
    "IntentSaturation",
    "SaturationResult",
]
