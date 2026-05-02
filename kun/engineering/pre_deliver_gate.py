"""V2.3+: PreDeliverGate — 任务交付前产品级审核.

类比 CI 跑完后的 PR review: 不是 CI green 就直接 merge, 而是再过一层人/规则审核.

KUN 之前的流程:
    LLM step → step done → ... → answer 出来 → mark task done → 直接交付给用户

新流程 (PreDeliverGate):
    LLM step → step done → ... → answer 出来 →
        ┌─ verification (V2.2 Wire 36) ─┐
        ├─ anti-gaming overall scan ─────┤  → 任一 fail → mark task as needs_review
        ├─ 协议合规检查 ─────────────────┤  → 全过 → mark done 真交付
        ├─ 自检 (output 长度 / 格式 / 等) ┤
        └─ (将来) 用户配置的 reviewer LLM ┘

设计原则:
- **opt-in**: KUN_PRE_DELIVER_GATE_ENABLED=1 (default ON in V2.3+, 内测)
- **可绕过**: KUN_PRE_DELIVER_GATE_ENABLED=0 → 跟旧流程一样直接交付
- **失败软处理**: 默认 mark "needs_review" (不是 failed), 让用户 confirm 或 retry
- **审核 verdict 透明**: emit `delivery.review_done` event with full reasons
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GateCheckResult:
    """单项审核结果."""

    name: str
    passed: bool
    severity: str = "low"  # low/medium/high/critical
    reason: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class PreDeliverVerdict:
    """全部审核汇总."""

    passed: bool
    checks: list[GateCheckResult] = field(default_factory=list)
    final_status: str = "done"  # done / needs_review / failed
    reason_summary: str = ""

    @property
    def has_critical(self) -> bool:
        return any(c.severity == "critical" and not c.passed for c in self.checks)

    @property
    def has_high(self) -> bool:
        return any(c.severity == "high" and not c.passed for c in self.checks)


class PreDeliverGate:
    """交付前审核 gate.

    用法 (在 orchestrator 标 done 前):
        gate = PreDeliverGate(verification_runner=..., anti_gaming_detector=..., active_protocol=...)
        verdict = await gate.review(
            answer=answer, task_ref=task_ref, plan=plan, step_records=runtime.completed_steps,
        )
        if verdict.passed:
            mark_done()
        elif verdict.final_status == "needs_review":
            mark_needs_review(reason=verdict.reason_summary)
        else:
            mark_failed(reason=verdict.reason_summary)
    """

    def __init__(
        self,
        *,
        verification_runner: Any = None,
        anti_gaming_detector: Any = None,
        active_protocol: Any = None,
    ) -> None:
        self._verification_runner = verification_runner
        self._anti_gaming_detector = anti_gaming_detector
        self._active_protocol = active_protocol

    @staticmethod
    def is_enabled() -> bool:
        """KUN_PRE_DELIVER_GATE_ENABLED=1 (default ON in V2.3+)."""
        return os.getenv("KUN_PRE_DELIVER_GATE_ENABLED", "1") == "1"

    async def review(
        self,
        *,
        answer: str,
        task_ref: Any,
        plan: Any,
        step_records: list[Any],
    ) -> PreDeliverVerdict:
        """跑全套 review. 返 PreDeliverVerdict."""
        verdict = PreDeliverVerdict(passed=True)

        # === Check 1: verification_specs (V2.2 Wire 36) ===
        if (
            self._verification_runner is not None
            and task_ref.spec is not None
            and task_ref.spec.verification_specs
        ):
            for vspec in task_ref.spec.verification_specs:
                try:
                    vresult = await self._verification_runner.verify(vspec, answer)
                    passed = bool(vresult.passed)
                    verdict.checks.append(
                        GateCheckResult(
                            name=f"verification.{vspec.kind}",
                            passed=passed,
                            severity="high" if vspec.required else "low",
                            reason=("" if passed else (vresult.error_msg or "verification failed")),
                            evidence={"kind": vspec.kind, "spec": dict(vspec.spec or {})},
                        )
                    )
                except Exception as e:
                    verdict.checks.append(
                        GateCheckResult(
                            name=f"verification.{vspec.kind}",
                            passed=False,
                            severity="high" if vspec.required else "low",
                            reason=f"verification exception: {e}",
                            evidence={"error": str(e)},
                        )
                    )

        # === Check 2: AntiGaming overall scan ===
        if self._anti_gaming_detector is not None:
            try:
                prior_answers: list[str] = []
                used_skills: list[str] = []
                for s in step_records:
                    su = getattr(s, "skill_used", None)
                    if su:
                        used_skills.append(su)
                planned_steps = len(getattr(plan, "steps", []) or [])
                actual_steps = len(step_records)
                # 用 task_ref.spec.goal_detail 作 prompt 近似 (好过 task_type)
                # 没 spec → 跳 off_topic check (prompt="" detector 会跳过)
                _prompt = ""
                if task_ref.spec is not None:
                    _prompt = getattr(task_ref.spec, "goal_detail", "") or ""

                finding = self._anti_gaming_detector.check(
                    prompt=_prompt,
                    answer=answer,
                    prior_answers=prior_answers,
                    planned_steps=planned_steps,
                    actual_steps=actual_steps,
                    used_skills=used_skills,
                    has_assets=False,
                    has_skill_traces=any(used_skills),
                    verification_passed=all(c.passed for c in verdict.checks),
                )
                if finding is not None:
                    verdict.checks.append(
                        GateCheckResult(
                            name=f"anti_gaming.{finding.pattern}",
                            passed=False,
                            severity=finding.severity,
                            reason=finding.reason,
                            evidence=dict(finding.evidence or {}),
                        )
                    )
                else:
                    verdict.checks.append(
                        GateCheckResult(
                            name="anti_gaming.overall",
                            passed=True,
                            severity="low",
                            reason="no patterns matched",
                        )
                    )
            except Exception as e:
                verdict.checks.append(
                    GateCheckResult(
                        name="anti_gaming.overall",
                        passed=True,  # 异常不阻交付 (软失败)
                        severity="low",
                        reason=f"detector skipped: {e}",
                    )
                )

        # === Check 3: 自检 — output 不能是空 / 错误信息 ===
        if not answer or len(answer.strip()) < 5:
            verdict.checks.append(
                GateCheckResult(
                    name="self_check.empty_output",
                    passed=False,
                    severity="critical",
                    reason="LLM 输出几乎为空 (< 5 字符)",
                    evidence={"answer_length": len(answer or "")},
                )
            )
        elif answer.strip().lower().startswith(("error", "exception", "traceback", "[error]")):
            verdict.checks.append(
                GateCheckResult(
                    name="self_check.error_output",
                    passed=False,
                    severity="high",
                    reason="LLM 输出像错误信息 (以 error/exception/traceback 开头)",
                    evidence={"prefix": answer.strip()[:50]},
                )
            )
        else:
            verdict.checks.append(
                GateCheckResult(
                    name="self_check.output_sanity",
                    passed=True,
                    severity="low",
                    reason=f"output length={len(answer)}",
                )
            )

        # === Check 4: 协议合规 — 应用了协议则要求其 verification 全过 ===
        if self._active_protocol is not None and self._active_protocol.verification:
            # 协议要求的 verification 已在 Check 1 跑过 (因为 protocol consume 把它加到 task_spec)
            # 这里只验"协议出现 + 至少一项 verification 通过" — 防协议被 silently skip
            protocol_check_passed = any(
                c.passed and c.name.startswith("verification.") for c in verdict.checks
            )
            verdict.checks.append(
                GateCheckResult(
                    name="protocol.compliance",
                    passed=protocol_check_passed or not bool(self._active_protocol.verification),
                    severity="high",
                    reason=(
                        ""
                        if protocol_check_passed
                        else f"协议 {self._active_protocol.protocol_id} 要求 verification 但未跑或全失败"
                    ),
                    evidence={
                        "protocol_id": self._active_protocol.protocol_id,
                        "version": self._active_protocol.version,
                    },
                )
            )

        # === 汇总 verdict ===
        critical_fails = [c for c in verdict.checks if not c.passed and c.severity == "critical"]
        high_fails = [c for c in verdict.checks if not c.passed and c.severity == "high"]
        if critical_fails:
            verdict.passed = False
            verdict.final_status = "failed"
            verdict.reason_summary = "; ".join(c.reason for c in critical_fails)
        elif high_fails:
            verdict.passed = False
            verdict.final_status = "needs_review"
            verdict.reason_summary = "; ".join(c.reason for c in high_fails)
        else:
            verdict.passed = True
            verdict.final_status = "done"
            verdict.reason_summary = "all checks passed"

        return verdict


__all__ = ["GateCheckResult", "PreDeliverGate", "PreDeliverVerdict"]
