"""DiagnoseRunner — 傩修复诊断主管道 (V2.1 §10.6 / T59 + T60).

V2.1.2 修订: 提前到 M3.2 (用户自用必需).

5 步管道:
1. 范围识别 (出问题在哪个子系统)
2. 原因归因 (规则 + LLM 混合)
3. 修复方案生成 (自动可修 vs 需用户确认)
4. 修复执行 (走 §10.4 影响面分档)
5. 修复验证

5 类核心自动可修 (M3.2):
清理 / 加速 / 故障转移 / 网络防护 / 隐私保护
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from kun.core.anchor_expand import AnchorExpandIterator
from kun.core.ids import new_id

logger = logging.getLogger(__name__)


DiagnoseTrigger = Literal[
    "user_health_check_button",
    "watchtower_periodic",
    "anomaly_detection",
    "user_complaint",
    "surprise_persistent_high",
]

Subsystem = Literal[
    "context",
    "router_llm",
    "engineering",
    "watchtower",
    "data",
    "security",
]

ManagerCategory = Literal[
    # V1 §10.5 14 类管家功能 + V2 加 1 类, M3.2 优先 5 类
    "clean",
    "accelerate",
    "failover",
    "network_guard",
    "privacy",
    "software_mgmt",
    "vuln_fix",
    "popup_block",
    "hardware_check",
    "toolbox",
    "disaster_recovery",
    "subscription_mgmt",
    "community_share",
    "admin_policy",
    "security_guard",
    "agent_benchmark",
]


@dataclass
class DiagnoseRequest:
    """诊断触发请求."""

    request_id: str
    trigger: DiagnoseTrigger
    user_id: str
    tenant_id: str
    triggered_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    hint_text: str = ""  # 用户描述 / 异常信号简要


@dataclass
class DiagnoseFinding:
    """诊断发现."""

    finding_id: str
    subsystem: Subsystem
    category: ManagerCategory
    severity: Literal["info", "warn", "error", "critical"]
    description: str
    root_cause: str = ""
    cause_method: Literal["rule", "llm"] = "rule"


@dataclass
class FixPlan:
    """修复方案."""

    plan_id: str
    target_finding_id: str
    fix_kind: Literal["auto", "user_confirm_required"]
    description: str
    estimated_duration_sec: float = 1.0
    confirm_token: str | None = None


@dataclass
class FixOutcome:
    """修复结果."""

    plan_id: str
    success: bool
    verified: bool = False
    notes: str = ""


@dataclass
class DiagnoseReport:
    """单次诊断完整报告."""

    request_id: str
    started_at: datetime
    completed_at: datetime
    findings: list[DiagnoseFinding] = field(default_factory=list)
    plans: list[FixPlan] = field(default_factory=list)
    outcomes: list[FixOutcome] = field(default_factory=list)
    duration_sec: float = 0.0


# 5 类核心自动可修 (M3.2)
AUTO_FIX_CATEGORIES = {"clean", "accelerate", "failover", "network_guard", "privacy"}


# 范围识别规则 (M3.2 简版)
SCOPE_RULES: dict[str, Subsystem] = {
    "memory": "context",
    "asset": "context",
    "skill": "engineering",
    "model": "router_llm",
    "llm": "router_llm",
    "rule": "watchtower",
    "rls": "data",
    "tenant": "data",
    "auth": "security",
    "leak": "security",
}

# 简单原因归因规则 (M3.2 规则版)
RULE_BASED_CAUSES: list[tuple[str, str, ManagerCategory]] = [
    ("过期记忆", "Context tier 短期/长期到期, 触发清理", "clean"),
    ("缓存命中率低", "前缀缓存 key 构造错或 TTL 太短", "accelerate"),
    ("p95 latency 高", "模型 cold start 或网络延迟", "accelerate"),
    ("LLM provider 失败", "主 provider 不可用, 应 fallback", "failover"),
    ("异常调用模式", "网络层异常, 触发限流", "network_guard"),
    ("数据足迹超阈值", "临时缓存堆积, 触发隐私清理", "privacy"),
]


# Plan 执行 hook
FixHandler = Callable[[FixPlan, DiagnoseFinding], Awaitable[FixOutcome]]
LLMReviewer = Callable[[DiagnoseFinding, str], Awaitable[tuple[str, ManagerCategory]]]


class DiagnoseRunner:
    """傩诊断主管道 (V2.1 §10.6 / M3.2 实施)."""

    def __init__(
        self,
        *,
        llm_reviewer: LLMReviewer | None = None,
        fix_handlers: dict[ManagerCategory, FixHandler] | None = None,
    ) -> None:
        self._llm_reviewer = llm_reviewer
        self._fix_handlers = fix_handlers or {}
        self._pending_confirms: dict[str, FixPlan] = {}

    def register_fix_handler(
        self,
        category: ManagerCategory,
        handler: FixHandler,
    ) -> None:
        self._fix_handlers[category] = handler

    async def run(self, request: DiagnoseRequest) -> DiagnoseReport:
        """主管道. 5 步."""
        start = datetime.now(UTC)
        # 1. 范围识别
        findings = await self._scope_identify(request)

        # 2. 原因归因 (规则 + LLM)
        await self._cause_attribute(findings, request)

        # 3. 修复方案生成
        plans = self._generate_fix_plans(findings)

        # 4. 修复执行 (auto 类直接跑, 需用户确认入 pending)
        outcomes: list[FixOutcome] = []
        for plan in plans:
            if plan.fix_kind == "auto":
                outcome = await self._execute(plan, findings)
                outcomes.append(outcome)
            # 需确认的留在 _pending_confirms 等用户

        # 5. 修复验证 (auto 类已 verified, 这里做汇总)
        completed = datetime.now(UTC)
        return DiagnoseReport(
            request_id=request.request_id,
            started_at=start,
            completed_at=completed,
            findings=findings,
            plans=plans,
            outcomes=outcomes,
            duration_sec=(completed - start).total_seconds(),
        )

    async def _scope_identify(
        self,
        request: DiagnoseRequest,
    ) -> list[DiagnoseFinding]:
        """1. 范围识别."""
        findings: list[DiagnoseFinding] = []
        text = request.hint_text.lower()
        for keyword, subsys in SCOPE_RULES.items():
            if keyword in text:
                from kun.core.ids import new_id

                findings.append(
                    DiagnoseFinding(
                        finding_id=new_id("diag"),
                        subsystem=subsys,
                        category="clean",
                        severity="warn",
                        description=f"keyword '{keyword}' 命中 {subsys} 子系统",
                    )
                )
        # 没命中关键词 → 默认 engineering
        if not findings:
            from kun.core.ids import new_id

            findings.append(
                DiagnoseFinding(
                    finding_id=new_id("diag"),
                    subsystem="engineering",
                    category="clean",
                    severity="info",
                    description="范围识别未命中具体子系统, 默认 engineering 全扫",
                )
            )
        return findings

    async def scope_identify_anchor_then_expand(
        self,
        request: DiagnoseRequest,
        *,
        max_rounds: int = 3,
    ) -> AsyncIterator[DiagnoseFinding]:
        """按需返回诊断发现.

        老的 ``_scope_identify`` 一次性返回全部命中范围. 这个新接口先返回最高优先级
        finding, 调用方觉得不够再展开后续命中.

        # TODO: wire by Claude in V2.2
        """
        findings = await self._scope_identify(request)
        ordered = sorted(findings, key=_scope_finding_priority, reverse=True)
        if not ordered:
            return

        async def anchor_fn() -> DiagnoseFinding:
            return ordered[0]

        async def expand_fn(
            _anchor: DiagnoseFinding,
            prior: list[DiagnoseFinding],
        ) -> DiagnoseFinding | None:
            seen = {item.finding_id for item in prior}
            return next((item for item in ordered if item.finding_id not in seen), None)

        async for item in AnchorExpandIterator(
            anchor_fn,
            expand_fn,
            max_rounds=max_rounds,
        ):
            yield item

    async def _cause_attribute(
        self,
        findings: list[DiagnoseFinding],
        request: DiagnoseRequest,
    ) -> None:
        """2. 原因归因 (规则覆盖的直接归; 没覆盖的 LLM 兜底)."""
        for finding in findings:
            attributed = False
            # 规则归因
            for keyword, cause, category in RULE_BASED_CAUSES:
                if keyword in request.hint_text:
                    finding.root_cause = cause
                    finding.category = category
                    finding.cause_method = "rule"
                    attributed = True
                    break
            # LLM 兜底 (M3.3 接 §17.10 模式 B)
            if not attributed and self._llm_reviewer is not None:
                try:
                    cause, category = await self._llm_reviewer(
                        finding,
                        request.hint_text,
                    )
                    finding.root_cause = cause
                    finding.category = category
                    finding.cause_method = "llm"
                except Exception:
                    logger.exception("llm_reviewer failed for finding %s", finding.finding_id)

    def _generate_fix_plans(
        self,
        findings: list[DiagnoseFinding],
    ) -> list[FixPlan]:
        """3. 修复方案生成."""
        plans = []
        for f in findings:
            plans.append(self._build_fix_plan(f))
        return plans

    async def generate_fix_plans_anchor_then_expand(
        self,
        findings: list[DiagnoseFinding],
        *,
        max_rounds: int = 3,
    ) -> AsyncIterator[FixPlan]:
        """按需生成修复方案.

        老的 ``_generate_fix_plans`` 一次性给所有 finding 生成方案. 这个新接口先处理
        最严重/最容易自动修的 finding, 调用方需要更多时再继续 expand.

        # TODO: wire by Claude in V2.2
        """
        ordered = sorted(findings, key=_finding_priority, reverse=True)
        if not ordered:
            return

        async def anchor_fn() -> FixPlan:
            return self._build_fix_plan(ordered[0])

        async def expand_fn(_anchor: FixPlan, prior: list[FixPlan]) -> FixPlan | None:
            used = {p.target_finding_id for p in prior}
            next_finding = next((f for f in ordered if f.finding_id not in used), None)
            if next_finding is None:
                return None
            return self._build_fix_plan(next_finding)

        async for plan in AnchorExpandIterator(
            anchor_fn,
            expand_fn,
            max_rounds=max_rounds,
        ):
            yield plan

    def _build_fix_plan(self, finding: DiagnoseFinding) -> FixPlan:
        """为单个 finding 生成修复方案."""
        if finding.category in AUTO_FIX_CATEGORIES:
            return FixPlan(
                plan_id=new_id("diag"),
                target_finding_id=finding.finding_id,
                fix_kind="auto",
                description=f"自动 {finding.category}: {finding.description}",
                estimated_duration_sec=5.0,
            )

        token = new_id("diag")[-6:].upper()
        plan = FixPlan(
            plan_id=new_id("diag"),
            target_finding_id=finding.finding_id,
            fix_kind="user_confirm_required",
            description=f"需用户确认 {finding.category}: {finding.description}",
            confirm_token=token,
            estimated_duration_sec=10.0,
        )
        self._pending_confirms[token] = plan
        return plan

    async def _execute(
        self,
        plan: FixPlan,
        findings: list[DiagnoseFinding],
    ) -> FixOutcome:
        """4 + 5. 执行 + 验证."""
        finding = next(
            (f for f in findings if f.finding_id == plan.target_finding_id),
            None,
        )
        if finding is None:
            return FixOutcome(
                plan_id=plan.plan_id,
                success=False,
                notes="finding lost",
            )

        handler = self._fix_handlers.get(finding.category)
        if handler is None:
            # 默认: 标记为已尝试但无 handler
            return FixOutcome(
                plan_id=plan.plan_id,
                success=False,
                notes=f"no handler registered for category {finding.category}",
            )

        try:
            outcome = await handler(plan, finding)
            return outcome
        except Exception as e:
            logger.exception("fix handler failed")
            return FixOutcome(
                plan_id=plan.plan_id,
                success=False,
                notes=f"handler error: {e}",
            )

    def confirm_user_fix(self, token: str, accept: bool = True) -> bool:
        """用户确认需确认的 fix."""
        plan = self._pending_confirms.pop(token, None)
        if plan is None:
            return False
        return accept


def _finding_priority(finding: DiagnoseFinding) -> tuple[int, int]:
    severity_order = {"info": 0, "warn": 1, "error": 2, "critical": 3}
    auto_bonus = 1 if finding.category in AUTO_FIX_CATEGORIES else 0
    return (severity_order.get(finding.severity, 0), auto_bonus)


def _scope_finding_priority(finding: DiagnoseFinding) -> tuple[int, int]:
    severity_order = {"info": 0, "warn": 1, "error": 2, "critical": 3}
    subsystem_order = {
        "security": 6,
        "data": 5,
        "watchtower": 4,
        "router_llm": 3,
        "engineering": 2,
        "context": 1,
    }
    return (
        severity_order.get(finding.severity, 0),
        subsystem_order.get(finding.subsystem, 0),
    )


__all__ = [
    "AUTO_FIX_CATEGORIES",
    "RULE_BASED_CAUSES",
    "SCOPE_RULES",
    "DiagnoseFinding",
    "DiagnoseReport",
    "DiagnoseRequest",
    "DiagnoseRunner",
    "DiagnoseTrigger",
    "FixHandler",
    "FixOutcome",
    "FixPlan",
    "LLMReviewer",
    "ManagerCategory",
    "Subsystem",
]
