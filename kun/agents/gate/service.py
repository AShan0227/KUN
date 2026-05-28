"""GateService — 主线出口治理门禁 (ADR-020 / ADR-024 step 7-10).

读 evidence: TestReport + DiagnosticRecord (RCDH) + DebriefRecord
(ExternalSupervisor Mode B) + StrategyExperiment (Strategist 输出)
→ 决定是否准入 → 准入则写 runtime_capabilities + 调度 promotion_queue.

工程化门禁规则 (engineering-first, 各条独立可关):
  R1. test_report.pass_rate >= min_pass_rate (默认 0.9)
  R2. is_fix=True 必须带 diagnostic_id (ADR-021 拒绝裸修)
  R3. debrief.verdict != "alarming" (External Supervisor 否决权)
  R4. debrief.evidence_quality_score >= min_evidence_quality (默认 0.5)
  R5. experiment.requires_human_review=True → 不自动启用, 标 awaiting_human_review

晋级状态机 (ADR-024):
  merged → in_replay → in_shadow → in_canary → ready → enabled (或 expired/rejected)

Gate 是"门禁执行者", 不是"产品评委" — 不做体验判断.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kun.core.ids import new_id
from kun.core.logging import get_logger

log = get_logger("kun.agents.gate.service")


_DEFAULT_MIN_PASS_RATE = 0.9
_DEFAULT_MIN_EVIDENCE_QUALITY = 0.5
_DEFAULT_PROMOTION_DEADLINE_DAYS = 14
"""promotion_deadline 默认 14 天 (ADR-024 §runtime_capabilities)."""


@dataclass(frozen=True)
class GateDecision:
    """单次准入决策结果."""

    decision_id: str
    verdict: str  # approve / reject / awaiting_human_review
    reasons: list[str]
    capability_id: str | None
    capability_row_payload: dict[str, Any] | None
    promotion_state: str  # merged / pending / rejected / awaiting_human_review
    rule_results: dict[str, bool] = field(default_factory=dict)
    decided_at: datetime = field(default_factory=lambda: datetime.now(UTC))


CapabilityWriter = Callable[[dict[str, Any]], Awaitable[None]]
"""异步 writer — 真写 runtime_capabilities. 测试用 fake."""


NotificationSender = Callable[[dict[str, Any]], Awaitable[None]]
"""L3.6: NotificationLayer.push 注入 — 让 Gate 在自指拒绝时推送 alert
(ADR-018 §16.3 第 3 个 NotificationLayer caller). 测试用 fake."""


def _check_test_report(
    test_report: dict[str, Any] | None,
    min_pass_rate: float,
) -> tuple[bool, str]:
    if test_report is None:
        return False, "missing_test_report"
    pass_rate = test_report.get("pass_rate")
    if pass_rate is None:
        # 从 passed/total 推算
        passed = test_report.get("passed_count", 0)
        total = test_report.get("total_count", 0)
        pass_rate = passed / total if total > 0 else 0.0
    if pass_rate >= min_pass_rate:
        return True, f"test_pass_rate={pass_rate:.2f}>={min_pass_rate}"
    return False, f"test_pass_rate={pass_rate:.2f}<{min_pass_rate}"


def _check_diagnostic(
    diagnostic_record: dict[str, Any] | None,
    *,
    is_fix: bool,
) -> tuple[bool, str]:
    if not is_fix:
        return True, "not_a_fix"
    if diagnostic_record is None:
        return False, "fix_requires_diagnostic_record"
    if not diagnostic_record.get("diagnostic_id"):
        return False, "diagnostic_record_missing_id"
    return True, f"diagnostic_id={diagnostic_record['diagnostic_id']}"


def _check_debrief(
    debrief: dict[str, Any] | None,
    min_evidence_quality: float,
) -> tuple[bool, str]:
    if debrief is None:
        # 没有 debrief — 不算否决 (有些路径没走 External Supervisor)
        return True, "no_debrief_required"
    verdict = debrief.get("verdict")
    if verdict == "alarming":
        return False, f"debrief_verdict=alarming: {debrief.get('rationale', '')}"
    quality = debrief.get("evidence_quality_score")
    if quality is None:
        return True, "debrief_no_quality_score"
    if quality < min_evidence_quality:
        return False, (
            f"evidence_quality_score={quality:.2f}<{min_evidence_quality}"
        )
    return True, f"debrief_ok quality={quality:.2f}"


def _check_self_referential(experiment: dict[str, Any] | None) -> tuple[bool, str]:
    """L3.5 强化: 两条独立检查 →
      1. experiment.requires_human_review 字段
      2. experiment.target_module 命中监督角色前缀 (即使 caller 忘了标 flag)

    任一命中 → 转 awaiting_human_review.
    """
    from kun.governance.self_referential import is_self_referential

    if experiment is None:
        return True, "no_experiment"
    if experiment.get("requires_human_review"):
        return False, "self_referential_requires_human_review"
    # L3.5 独立 target_module 检查 — 防 caller 漏标 flag
    target_module = experiment.get("target_module")
    if isinstance(target_module, str) and is_self_referential(target_module):
        return False, f"self_referential_target_module={target_module}"
    return True, "auto_admit_ok"


class GateService:
    """主线出口治理门禁.

    入口: admit(experiment, *, test_report, diagnostic_record=None, debrief=None,
                is_fix=False, tenant_id=...) → GateDecision

    若 approve: 准备 RuntimeCapability row payload + 调 capability_writer.
    晋级状态机由 promotion_queue 推进 (后续 service).
    """

    def __init__(
        self,
        *,
        capability_writer: CapabilityWriter | None = None,
        notification_sender: NotificationSender | None = None,
        min_pass_rate: float = _DEFAULT_MIN_PASS_RATE,
        min_evidence_quality: float = _DEFAULT_MIN_EVIDENCE_QUALITY,
        promotion_deadline_days: int = _DEFAULT_PROMOTION_DEADLINE_DAYS,
    ) -> None:
        self._writer = capability_writer
        self._notification_sender = notification_sender
        self._min_pass_rate = min_pass_rate
        self._min_evidence_quality = min_evidence_quality
        self._promotion_deadline_days = promotion_deadline_days

    async def _safe_notify(self, payload: dict[str, Any]) -> None:
        """L3.6: 给 NotificationLayer 推一条; 失败不打挂 admit 主路径."""
        if self._notification_sender is None:
            return
        try:
            await self._notification_sender(payload)
        except Exception as e:
            log.warning(
                "gate.notification_send_failed",
                error=str(e),
                kind=payload.get("kind"),
            )

    async def admit(
        self,
        experiment: dict[str, Any] | None,
        *,
        test_report: dict[str, Any] | None,
        diagnostic_record: dict[str, Any] | None = None,
        debrief: dict[str, Any] | None = None,
        is_fix: bool = False,
        tenant_id: str = "default",
    ) -> GateDecision:
        """走 5 条 engineering 规则 → 准入 / 拒绝 / 标人审."""
        rule_results: dict[str, bool] = {}
        reasons: list[str] = []
        all_passed = True

        ok, reason = _check_test_report(test_report, self._min_pass_rate)
        rule_results["R1_test_report"] = ok
        reasons.append(reason)
        if not ok:
            all_passed = False

        ok, reason = _check_diagnostic(diagnostic_record, is_fix=is_fix)
        rule_results["R2_diagnostic"] = ok
        reasons.append(reason)
        if not ok:
            all_passed = False

        ok, reason = _check_debrief(debrief, self._min_evidence_quality)
        rule_results["R3_debrief"] = ok
        reasons.append(reason)
        if not ok:
            all_passed = False

        ok, reason = _check_self_referential(experiment)
        rule_results["R4_self_referential"] = ok
        reasons.append(reason)
        # R4 不直接 reject; 转人审
        self_referential = not ok

        if not all_passed:
            decision = GateDecision(
                decision_id=new_id("capability_promo"),
                verdict="reject",
                reasons=reasons,
                capability_id=None,
                capability_row_payload=None,
                promotion_state="rejected",
                rule_results=rule_results,
            )
            log.info(
                "gate.rejected",
                decision_id=decision.decision_id,
                reasons=reasons,
            )
            return decision

        # All engineering rules pass — decide auto-admit vs human review
        if self_referential:
            # L3.5 强化: 即使 verdict=awaiting_human_review, 也写一条
            # promotion_state=awaiting_human_review 的 capability row
            # 给 promotion_queue 跟踪 (人审完成后 caller 可 enable_capability)
            capability_id = new_id("capability_promo")
            now = datetime.now(UTC)
            deadline = now + timedelta(days=self._promotion_deadline_days)
            row_payload: dict[str, Any] = {
                "tenant_id": tenant_id,
                "capability_id": capability_id,
                "target_module": (experiment or {}).get("target_module", "unknown"),
                "change_summary": str(
                    (experiment or {}).get("rationale")
                    or "self-referential change awaiting human review"
                ),
                "enabled": False,
                "promotion_state": "awaiting_human_review",
                "promotion_started_at": now,
                "promotion_deadline": deadline,
                "rollback_on": (experiment or {}).get("rollback_on", []),
                "sampling_rate": 0.0,  # 人审前不 sample 任何流量
                "metadata": {
                    "experiment_id": (experiment or {}).get("experiment_id"),
                    "promotion_block_self_referential": True,
                    "self_referential_reason": next(
                        (
                            r
                            for r in reasons
                            if "self_referential" in r
                        ),
                        "self_referential",
                    ),
                },
            }
            decision = GateDecision(
                decision_id=capability_id,
                verdict="awaiting_human_review",
                reasons=reasons,
                capability_id=capability_id,
                capability_row_payload=row_payload,
                promotion_state="awaiting_human_review",
                rule_results=rule_results,
            )
            if self._writer is not None:
                try:
                    await self._writer(row_payload)
                except Exception as e:
                    log.warning(
                        "gate.self_referential_write_failed",
                        error=str(e),
                        capability_id=capability_id,
                    )
            # L3.6: 推 NotificationLayer alert — 让 NUO 看到自指 capability 待人审
            await self._safe_notify(
                {
                    "tenant_id": tenant_id,
                    "kind": "alert",
                    "severity": "warn",
                    "channel": "side",
                    "title": "Self-referential capability awaiting human review",
                    "body": (
                        f"target_module={row_payload['target_module']} "
                        f"capability_id={capability_id}"
                    ),
                    "payload": {
                        "capability_id": capability_id,
                        "target_module": row_payload["target_module"],
                        "decision_id": decision.decision_id,
                        "reasons": reasons,
                    },
                }
            )
            log.info(
                "gate.awaiting_human_review",
                decision_id=decision.decision_id,
            )
            return decision

        # Auto-admit — 构造 runtime_capability row payload
        capability_id = new_id("capability_promo")
        change_summary = (
            (experiment or {}).get("rationale")
            or (experiment or {}).get("change_spec", {}).get("kind", "auto admitted")
        )
        target_module = (experiment or {}).get("target_module", "unknown")
        rollback_on = (experiment or {}).get("rollback_on", [])
        sampling_rate = float((experiment or {}).get("sampling_rate", 0.0))

        now = datetime.now(UTC)
        deadline = now + timedelta(days=self._promotion_deadline_days)

        row_payload: dict[str, Any] = {
            "tenant_id": tenant_id,
            "capability_id": capability_id,
            "target_module": target_module,
            "change_summary": str(change_summary),
            "enabled": False,  # promotion_queue 后续推进
            "promotion_state": "merged",
            "promotion_started_at": now,
            "promotion_deadline": deadline,
            "rollback_on": rollback_on,
            "sampling_rate": sampling_rate,
            "metadata": {
                "experiment_id": (experiment or {}).get("experiment_id"),
                "explorer_mode": (experiment or {}).get("explorer_mode"),
                "test_pass_rate": (test_report or {}).get("pass_rate"),
                "debrief_verdict": (debrief or {}).get("verdict"),
                "evidence_quality_score": (debrief or {}).get(
                    "evidence_quality_score"
                ),
                "diagnostic_id": (diagnostic_record or {}).get("diagnostic_id"),
            },
        }

        decision = GateDecision(
            decision_id=capability_id,
            verdict="approve",
            reasons=reasons,
            capability_id=capability_id,
            capability_row_payload=row_payload,
            promotion_state="merged",
            rule_results=rule_results,
        )

        # 写库
        if self._writer is not None:
            try:
                await self._writer(row_payload)
            except Exception as e:
                log.warning(
                    "gate.capability_write_failed",
                    error=str(e),
                    capability_id=capability_id,
                )

        # V7 Phase X.B.MF-LC-wiring: emit V7 §15 lifecycle transition
        # (OBSERVATION → CANDIDATE) alongside the V6 promotion row.
        # Fire-and-forget; bridge handles its own env opt-out + errors.
        # Audit grep: kun.integration.capability_lifecycle_v7_bridge
        try:
            from kun.integration.capability_lifecycle_v7_bridge import (
                emit_lifecycle_transition_for_gate_decision,
            )

            await emit_lifecycle_transition_for_gate_decision(
                decision=decision, tenant_id=tenant_id
            )
        except Exception:
            # Defense in depth: V6 gate must not break if V7 bridge import / hook fails
            pass

        log.info(
            "gate.approved",
            decision_id=decision.decision_id,
            target_module=target_module,
        )
        return decision

    async def enable_capability(
        self,
        capability_id: str,
        *,
        tenant_id: str = "default",
        capability_state_writer: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        human_approval_token: str | None = None,
        metadata_lookup: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
    ) -> dict[str, Any]:
        """capability 晋级 ready → enabled (promotion_queue 走完时调).

        L3.5 强化: 若 capability metadata 标
        `promotion_block_self_referential=True`, 必须传 human_approval_token
        才允许 flip enabled=True. 没有 token → raise PermissionError.

        metadata_lookup: 异步查询 capability metadata; 测试用 fake.

        返回 update payload (调用方决定怎么落库).
        """
        # L3.5: 自指 capability 强 gate
        if metadata_lookup is not None:
            metadata = await metadata_lookup(capability_id)
            if metadata and metadata.get("promotion_block_self_referential"):
                if not human_approval_token:
                    log.warning(
                        "gate.enable_self_referential_blocked",
                        capability_id=capability_id,
                    )
                    raise PermissionError(
                        f"capability {capability_id} is self-referential — "
                        f"require human_approval_token to enable"
                    )
                log.info(
                    "gate.enable_self_referential_with_human_approval",
                    capability_id=capability_id,
                    token_hint=human_approval_token[:8],
                )

        payload = {
            "tenant_id": tenant_id,
            "capability_id": capability_id,
            "enabled": True,
            "promotion_state": "enabled",
        }
        if capability_state_writer is not None:
            try:
                await capability_state_writer(payload)
            except Exception as e:
                log.warning(
                    "gate.enable_capability_failed",
                    error=str(e),
                    capability_id=capability_id,
                )
                raise
        log.info("gate.capability_enabled", capability_id=capability_id)
        return payload


def decision_as_dict(d: GateDecision) -> dict[str, Any]:
    return asdict(d)


__all__ = [
    "CapabilityWriter",
    "GateDecision",
    "GateService",
    "NotificationSender",
    "decision_as_dict",
]
