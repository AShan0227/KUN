"""Decision-Point Classifier — Executor 主动停下问用户的硬规则判定.

KUN 在长任务跑中, 遇到"需要用户拍板"的决策点 (e.g. "产品方向选哪个垂直 /
Auth 是否启用 / API key 是否暴露 / 是否做 destructive 改动") 应该停下问
用户而非自己拍板.

但 Claude Code 这种"什么时候停下问"的能力, KUN 没有等价品. Gate 是机器自动
判断, 不是问用户. LongTaskInputRouter 是处理 inbound 消息, 不是 outbound
问询. 这一层填补这个缺口.

设计要点:
  - 硬规则 classifier (不调 LLM, 不依赖模型当场判断)
  - 6 类决策点 + no_decision 兜底
  - 输出 frozen dataclass, caller 自己决定是否真触发 AskUserQuestion
  - suggested_question / suggested_options 给 caller 直接用 (硬编码模板)

匹配优先级 (高→低):
  1. destructive_action       (不可逆 + risk >= medium)
  2. auth_posture_change      (安全 posture 切换)
  3. external_api_unlock      (财务: 调真 LLM + risk >= medium)
  4. product_direction        (高度产品决策)
  5. out_of_anchor            (action 触碰 anchor.out_of_scope)
  6. no_decision              (默认: 继续做)

为什么 auth_posture_change 排在 product_direction 前:
  "Auth 升级"既能命中 product_keywords 又能命中 auth_keywords; 但 auth
  是更具体的 posture 切换, 应该走 auth 模板而非泛泛的"产品方向".

后续可扩展: LLM-fallback 当硬规则全 miss 时调一次 cheap LLM 二次判断.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from kun.core.logging import get_logger

log = get_logger("kun.agents.director.decision_point_classifier")


DecisionCategory = Literal[
    "product_direction",
    "destructive_action",
    "external_api_unlock",
    "auth_posture_change",
    "out_of_anchor",
    "no_decision",
]


@dataclass(frozen=True)
class DecisionPointResult:
    """单次决策点判定结果 — pure data, caller 决定是否真触发 AskUserQuestion."""

    category: DecisionCategory
    confidence: float
    matched_signal: str
    suggested_question: str | None = None
    suggested_options: list[str] | None = None


# ---- 硬编码关键词集 (中英文同义都加) ----

_DESTRUCTIVE_KEYWORDS = (
    # 英文
    "drop schema",
    "drop table",
    "rm -rf",
    "git push --force",
    "force push",
    "delete database",
    "truncate",
    "reset --hard",
    # 中文
    "迁移数据库",
    "重写",
    "全部重置",
    "删库",
    "强制推送",
    "回滚生产",
    "清空数据",
)

_PRODUCT_KEYWORDS = (
    # 英文
    "default tenant",
    "new vertical",
    "product direction",
    "phase upgrade",
    # 中文
    "哪个垂直",
    "下一阶段",
    "下个阶段",
    "下一个阶段",
    "phase 升级",
    "新行业",
    "产品方向",
    "选哪个",
    "做哪个",
)

_EXTERNAL_API_KEYWORDS = (
    # 英文
    "production api",
    "openai api key",
    "anthropic api key",
    "real llm",
    "live api",
    # 中文
    "调真 llm",
    "调真llm",
    "真 llm",
    "付费 api",
    "付费api",
    "开 production",
    "开production",
    "生产 api",
    "生产api",
)

_AUTH_KEYWORDS = (
    # 英文
    "kun_auth_enabled=true",
    "kun_auth_enabled",
    "auth posture",
    "rls upgrade",
    "remove default tenant",
    "enable jwt",
    "real jwt",
    # 中文
    "切 jwt",
    "切jwt",
    "切真 jwt",
    "切真jwt",
    "切真生产",
    "rls 升级",
    "auth 升级",
    "启用 auth",
    "启用auth",
    "去掉 default tenant",
    "去掉default tenant",
)


def _has_any(text: str, keywords: tuple[str, ...]) -> str | None:
    """大小写无关 substring 命中, 返回命中的关键词 (供 matched_signal); 无则 None."""
    lowered = text.lower()
    for kw in keywords:
        if kw.lower() in lowered:
            return kw
    return None


def _anchor_out_of_scope_hit(
    action_intent: str,
    task_anchor: dict[str, Any] | None,
) -> str | None:
    """检查 action_intent 是否触碰 anchor.out_of_scope.

    简单 substring 双向匹配: out_of_scope 条目里任何 ≥2 字 / ≥3 字符的关键 token
    出现在 action_intent 里就算命中.

    例: out_of_scope=["重写 Director"], action="重写 Director.intent" → hit.
    """
    if not task_anchor:
        return None
    out_of_scope = task_anchor.get("out_of_scope") or []
    if not out_of_scope:
        return None

    lowered_action = action_intent.lower()
    for item in out_of_scope:
        item_str = str(item).strip()
        if not item_str:
            continue
        # 整条直接匹配
        if item_str.lower() in lowered_action:
            return item_str
        # 拆出"有信号的" token (中文 ≥ 2 字, 英文 ≥ 3 字符), 任一命中即算
        tokens = re.findall(r"[一-鿿]{2,}|[a-zA-Z0-9_]{3,}", item_str.lower())
        for tok in tokens:
            if tok in lowered_action:
                return item_str
    return None


def _risk_is_medium_or_higher(risk: str) -> bool:
    """task_meta.risk_level 是 medium / high / critical 算"足够高"."""
    return risk.lower() in {"medium", "high", "critical"}


# ---- suggested question / options 模板 (硬编码) ----


def _build_template(
    category: DecisionCategory,
    action_intent: str,
) -> tuple[str | None, list[str] | None]:
    """按 category 生成给用户的问询模板. no_decision 返回 (None, None)."""
    snippet = action_intent.strip()
    if len(snippet) > 80:
        snippet = snippet[:77] + "..."

    if category == "destructive_action":
        return (
            f"「{snippet}」是不可逆操作. 真的执行?",
            ["执行", "取消"],
        )
    if category == "auth_posture_change":
        return (
            f"「{snippet}」涉及 auth posture 切换 (安全敏感). 是否继续?",
            ["切换 (我已确认)", "暂缓 (继续旧 posture)"],
        )
    if category == "external_api_unlock":
        return (
            f"「{snippet}」会启用付费外部 API (产生 cost). 是否继续?",
            ["启用并继续", "维持 stub 不调真 API"],
        )
    if category == "product_direction":
        return (
            f"「{snippet}」涉及产品方向决策. 是否继续?",
            ["继续 (我已确认)", "暂停等用户确认"],
        )
    if category == "out_of_anchor":
        return (
            f"「{snippet}」超出当前任务 anchor 范围. 是否扩大 anchor 继续?",
            ["扩大 anchor", "拒绝 (维持当前 anchor)"],
        )
    return (None, None)


def classify_decision_point(
    *,
    action_intent: str,
    task_meta_risk: str = "low",
    task_anchor: dict[str, Any] | None = None,
) -> DecisionPointResult:
    """硬规则决策点 classifier (不调 LLM).

    Args:
        action_intent: 主线 agent 即将执行的动作描述 (中英混排都支持).
        task_meta_risk: TaskMeta.risk_level — low / medium / high / critical.
        task_anchor: 可选 GoalAnchor dict 形式 (含 'out_of_scope' 字段).

    Returns:
        DecisionPointResult — 含 category, confidence, matched_signal,
        suggested_question, suggested_options.

    匹配优先级 (短路):
        1. destructive_keywords ∩ risk >= medium → destructive_action
        2. auth_keywords                          → auth_posture_change
        3. external_api_keywords ∩ risk >= medium → external_api_unlock
        4. product_keywords                       → product_direction
        5. anchor.out_of_scope 命中               → out_of_anchor
        6. 否则                                   → no_decision

    Notes:
        - auth_posture_change 优先于 product_direction (Auth 升级既能命中两组,
          但 auth 更具体, 走 auth 模板).
        - external_api_unlock 需要 risk >= medium — low risk 下"调真 LLM"
          不算财务决策 (可能是 stub / dev 环境).
        - 空 action_intent → no_decision.
    """
    cleaned = action_intent.strip()
    if not cleaned:
        return DecisionPointResult(
            category="no_decision",
            confidence=1.0,
            matched_signal="empty_action_intent",
        )

    risk_high_enough = _risk_is_medium_or_higher(task_meta_risk)

    # 1. destructive_action (不可逆 + risk 高)
    destructive_hit = _has_any(cleaned, _DESTRUCTIVE_KEYWORDS)
    if destructive_hit and risk_high_enough:
        q, opts = _build_template("destructive_action", cleaned)
        log.info(
            "decision_point.classified",
            category="destructive_action",
            matched=destructive_hit,
        )
        return DecisionPointResult(
            category="destructive_action",
            confidence=0.9,
            matched_signal=f"destructive:{destructive_hit}|risk={task_meta_risk}",
            suggested_question=q,
            suggested_options=opts,
        )

    # 2. auth_posture_change (优先于 product, 更具体)
    auth_hit = _has_any(cleaned, _AUTH_KEYWORDS)
    if auth_hit:
        q, opts = _build_template("auth_posture_change", cleaned)
        log.info(
            "decision_point.classified",
            category="auth_posture_change",
            matched=auth_hit,
        )
        return DecisionPointResult(
            category="auth_posture_change",
            confidence=0.85,
            matched_signal=f"auth:{auth_hit}",
            suggested_question=q,
            suggested_options=opts,
        )

    # 3. external_api_unlock (财务: 调真 API + risk 高)
    external_hit = _has_any(cleaned, _EXTERNAL_API_KEYWORDS)
    if external_hit and risk_high_enough:
        q, opts = _build_template("external_api_unlock", cleaned)
        log.info(
            "decision_point.classified",
            category="external_api_unlock",
            matched=external_hit,
        )
        return DecisionPointResult(
            category="external_api_unlock",
            confidence=0.8,
            matched_signal=f"external_api:{external_hit}|risk={task_meta_risk}",
            suggested_question=q,
            suggested_options=opts,
        )

    # 4. product_direction (高度产品决策)
    product_hit = _has_any(cleaned, _PRODUCT_KEYWORDS)
    if product_hit:
        q, opts = _build_template("product_direction", cleaned)
        log.info(
            "decision_point.classified",
            category="product_direction",
            matched=product_hit,
        )
        return DecisionPointResult(
            category="product_direction",
            confidence=0.75,
            matched_signal=f"product:{product_hit}",
            suggested_question=q,
            suggested_options=opts,
        )

    # 5. out_of_anchor (action 触碰 anchor.out_of_scope)
    oos_hit = _anchor_out_of_scope_hit(cleaned, task_anchor)
    if oos_hit:
        q, opts = _build_template("out_of_anchor", cleaned)
        log.info(
            "decision_point.classified",
            category="out_of_anchor",
            matched=oos_hit,
        )
        return DecisionPointResult(
            category="out_of_anchor",
            confidence=0.7,
            matched_signal=f"out_of_anchor:{oos_hit}",
            suggested_question=q,
            suggested_options=opts,
        )

    # 6. 兜底 — 大部分 case 不停下问
    return DecisionPointResult(
        category="no_decision",
        confidence=0.6,
        matched_signal="no_keyword_hit",
    )


__all__ = [
    "DecisionCategory",
    "DecisionPointResult",
    "classify_decision_point",
]
