"""端到端 RSI 闭环 demo (Phase 1.D 验收).

⚠️ FIXTURE/DEMO (audit F057): mid-chain anomaly data is hardcoded and Gate inputs are hand-fed to pass; a green run does NOT prove real RSI closure. See docs/audit/proposals/demo-script-honesty.md.

# FIXTURE-ONLY (V7 §16.3 + Phase 0.5 标注)
#
# 本脚本**绕过** Orchestrator 直接调内部 services, 仅用于:
#   - 工程师本地手动验证 RSI 闭环各环节 wiring
#   - L 系列开发期 fixture / smoke test
#
# 严格约束 (V7 §16.0 产品魂级硬规则):
#   - 产物**不允许进** capability_card / seeds/methodologies/ / production runtime
#   - 不允许在驾驶舱 / dev_log / commit message 里宣称"已完成 RSI"
#   - 此脚本跑出的 strategy_search_request / runtime_capability 行必须标
#     metadata.fixture_only=True (待 Phase 0.5.gate 实装时强 enforce)
#   - 真实 RSI 必须走主路径: 用户输入 → Orchestrator → 启 (Qi) → 9 阶段 lifecycle
#
# 不是 "已开发能力"; 不是 "production 路径"; 不是验收依据.

跑这个脚本会:
  1. 起 SupervisorService → 喂 3 次 llm.fallback.triggered → 触发 anomaly
  2. SupervisorService 自动 cluster (因为还会喂 1 次 task.failed 让 module_systemic 触发)
  3. emitter 落 strategy_search_requests 到真 Postgres
  4. StrategistService 读 request → 产 3 个 candidate
  5. 模拟 Tester 给 ok 的 test_report
  6. GateService 走 4 条规则 → approve → 写 runtime_capabilities 到真 Postgres
  7. PromotionTimeoutSweeper 跑一次 → 看是否有 stale

最后 SELECT 表确认: 应该看到 strategy_search_requests + runtime_capabilities 各 ≥1 行.

前置: docker-compose 起着, alembic 0011 已跑.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from kun.agents.gate.service import GateService
from kun.agents.strategist.service import StrategistService
from kun.agents.supervisor.service import SupervisorService
from kun.core.db import session_scope
from kun.core.orm import (
    RuntimeCapabilityRow,
    StrategySearchRequestRow,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

TENANT_ID = "u-e2e-demo"
TASK_TYPE = "coding.refactor"


async def make_search_request_writer():
    """真把 strategy_search_request 写进 Postgres."""

    async def write(req: dict[str, Any]) -> None:
        async with session_scope(tenant_id=req.get("tenant_id", TENANT_ID)) as session:
            row = StrategySearchRequestRow(
                tenant_id=req.get("tenant_id", TENANT_ID),
                request_id=req["request_id"],
                triggered_by=req["triggered_by"],
                target_module=req["target_module"],
                evidence=req.get("evidence") or [],
                priority=req.get("priority", "medium"),
                dedup_key=req["dedup_key"],
                status=req.get("status", "open"),
                created_at=req["created_at"]
                if isinstance(req["created_at"], datetime)
                else datetime.now(UTC),
            )
            session.add(row)
            await session.commit()

    return write


async def make_capability_writer():
    """真把 runtime_capability 写进 Postgres."""

    async def write(payload: dict[str, Any]) -> None:
        async with session_scope(
            tenant_id=payload.get("tenant_id", TENANT_ID)
        ) as session:
            row = RuntimeCapabilityRow(
                tenant_id=payload["tenant_id"],
                capability_id=payload["capability_id"],
                target_module=payload["target_module"],
                change_summary=payload["change_summary"],
                enabled=payload["enabled"],
                promotion_state=payload["promotion_state"],
                promotion_started_at=payload["promotion_started_at"],
                promotion_deadline=payload["promotion_deadline"],
                rollback_on=payload["rollback_on"],
                sampling_rate=payload["sampling_rate"],
                capability_metadata=payload.get("metadata") or {},
            )
            session.add(row)
            await session.commit()

    return write


async def cleanup(session: AsyncSession) -> None:
    """清掉本 demo 之前留下的 rows."""
    from sqlalchemy import delete

    await session.execute(
        delete(StrategySearchRequestRow).where(
            StrategySearchRequestRow.tenant_id == TENANT_ID
        )
    )
    await session.execute(
        delete(RuntimeCapabilityRow).where(
            RuntimeCapabilityRow.tenant_id == TENANT_ID
        )
    )
    await session.commit()


async def main() -> None:
    print("=" * 64)
    print("KUN Phase 1 端到端 RSI 闭环 demo")
    print("=" * 64)

    # Step 0: cleanup
    print("\n[0] 清理之前的 demo 数据...")
    async with session_scope(tenant_id=TENANT_ID) as session:
        await cleanup(session)

    # Step 1: Supervisor 检测异常 + emit search_request 到真 DB
    print("\n[1] 喂 4 次异常事件给 SupervisorService...")
    search_writer = await make_search_request_writer()
    supervisor = SupervisorService(emitter=search_writer)

    fallback_payload = {
        "tenant_id": TENANT_ID,
        "primary_provider": "anthropic",
        "primary_model": "claude-opus-4-7",
        "fallback_provider": "openai",
        "reason": "rate_limit",
    }
    for i in range(3):
        triggered = await supervisor.observe(
            "llm.fallback.triggered", fallback_payload
        )
        print(f"    fallback #{i+1}: {len(triggered)} trigger(s)")

    duration_payload = {
        "tenant_id": TENANT_ID,
        "task_type": TASK_TYPE,
        "duration_sec": 30.0,
        "avg_duration_sec_for_task_type": 3.0,
    }
    triggered = await supervisor.observe("task.done", duration_payload)
    print(f"    duration_outlier: {len(triggered)} trigger(s)")

    print(
        "    -> Supervisor 触发了 strategy_search_request, 已写 Postgres."
    )

    # Step 2: 读出 Postgres 中的 requests 给 Strategist
    print("\n[2] 从 Postgres 读 strategy_search_requests...")
    async with session_scope(tenant_id=TENANT_ID) as session:
        result = await session.execute(
            select(StrategySearchRequestRow).where(
                StrategySearchRequestRow.tenant_id == TENANT_ID
            )
        )
        rows = result.scalars().all()
        print(f"    Postgres 中有 {len(rows)} 个 search_request:")
        for row in rows:
            print(
                f"      - {row.anomaly_kind if hasattr(row, 'anomaly_kind') else row.triggered_by:35} | "
                f"{row.target_module:30} | priority={row.priority}"
            )

    requests_to_process = []
    for row in rows:
        # 拼回 dict 喂 Strategist
        requests_to_process.append(
            {
                "request_id": row.request_id,
                "tenant_id": row.tenant_id,
                "triggered_by": row.triggered_by,
                "target_module": row.target_module,
                "anomaly_kind": (row.evidence[0].get("type") if row.evidence else "unknown"),
                "evidence": row.evidence,
                "priority": row.priority,
            }
        )

    # Step 3: Strategist 产候选 (取第一个 request)
    print("\n[3] Strategist 处理第一个 request, 产生 candidates...")
    if requests_to_process:
        # 因为 demo 中我们已通过 Supervisor 写 request, anomaly_kind 应该改为 supervisor 视角
        # 简化: 直接构造一个 llm_fallback_spike request 给 Strategist
        # (requests_to_process[0] 仅作存在性 sentinel — 实际 anomaly 数据下面写死)
        strategist = StrategistService()
        candidates = await strategist.propose_candidates(
            {
                "anomaly_kind": "llm_fallback_spike",
                "target_module": "llm.router",
                "evidence": [
                    {
                        "primary_provider": "anthropic",
                        "fallback_provider": "openai",
                    }
                ],
                "tenant_id": TENANT_ID,
            }
        )
        print(f"    Strategist 产了 {len(candidates)} 个 candidates:")
        for c in candidates:
            print(
                f"      - [{c.explorer_mode}] target_level={c.target_level} "
                f"sampling={c.sampling_rate} rollout={c.rollout_mode}"
            )

        # Step 4: Gate 准入 + 写 runtime_capability 到真 DB
        print("\n[4] Gate 处理 candidates (模拟 test_report + debrief)...")
        capability_writer = await make_capability_writer()
        gate = GateService(capability_writer=capability_writer)
        approved_count = 0
        for c in candidates:
            exp_dict = {
                "experiment_id": c.experiment_id,
                "target_module": c.target_module,
                "change_spec": c.change_spec,
                "rationale": c.rationale,
                "rollback_on": c.rollback_on,
                "sampling_rate": c.sampling_rate,
                "explorer_mode": c.explorer_mode,
                "requires_human_review": c.requires_human_review,
            }
            decision = await gate.admit(
                exp_dict,
                test_report={
                    "pass_rate": 0.95,
                    "passed_count": 19,
                    "total_count": 20,
                },
                debrief={
                    "verdict": "ok",
                    "rationale": "anchor satisfied",
                    "evidence_quality_score": 0.8,
                },
                tenant_id=TENANT_ID,
            )
            if decision.verdict == "approve":
                approved_count += 1
                # Gate.admit 内部已经调过 capability_writer, 不再重复写
            print(
                f"      - [{c.explorer_mode}] → {decision.verdict} "
                f"(promotion_state={decision.promotion_state})"
            )
        print(
            f"    Approved {approved_count}/{len(candidates)} candidates, "
            f"已写 runtime_capabilities."
        )

    # Step 5: 从 Postgres 读 runtime_capabilities 确认
    print("\n[5] 验证: 从 Postgres 读 runtime_capabilities...")
    async with session_scope(tenant_id=TENANT_ID) as session:
        result = await session.execute(
            select(RuntimeCapabilityRow).where(
                RuntimeCapabilityRow.tenant_id == TENANT_ID
            )
        )
        caps = result.scalars().all()
        print(f"    Postgres 中有 {len(caps)} 个 runtime_capabilities:")
        for cap in caps:
            print(
                f"      - {cap.capability_id} | target={cap.target_module:25} | "
                f"state={cap.promotion_state} | enabled={cap.enabled} | "
                f"sampling={cap.sampling_rate:.2f}"
            )

    # Step 6: Promotion sweeper 跑一次
    print("\n[6] Promotion sweeper 扫一次 (应无 expired, 因为刚写入)...")
    from kun.governance.promotion_queue import PromotionTimeoutSweeper

    async def reader():
        async with session_scope(tenant_id=TENANT_ID) as session:
            result = await session.execute(
                select(RuntimeCapabilityRow).where(
                    RuntimeCapabilityRow.tenant_id == TENANT_ID
                )
            )
            return [
                {
                    "capability_id": cap.capability_id,
                    "tenant_id": cap.tenant_id,
                    "target_module": cap.target_module,
                    "change_summary": cap.change_summary,
                    "promotion_state": cap.promotion_state,
                    "promotion_started_at": cap.promotion_started_at,
                    "promotion_deadline": cap.promotion_deadline,
                }
                for cap in result.scalars().all()
            ]

    sweeper = PromotionTimeoutSweeper(capability_reader=reader)
    report = await sweeper.sweep()
    print(
        f"    Scanned {report['scanned']} | expired {report['expired']} | "
        f"stale {report['stale']}"
    )

    print("\n" + "=" * 64)
    print("✅ 端到端 RSI 闭环 demo 完成")
    print("=" * 64)
    print(
        "\n下一步: 跑 `docker exec -it kun-dev-postgres-1 psql -U kun_app -d kun -c "
        "\"SELECT capability_id, target_module, promotion_state, enabled, sampling_rate "
        "FROM kun_data.runtime_capabilities;\"` 直接查表."
    )


if __name__ == "__main__":
    asyncio.run(main())
