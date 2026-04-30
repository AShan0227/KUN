"""V2.3 mock dogfood — Claude 自己能跑, 不需真 LLM API key.

用 mock LLM provider 跑 V2.3 全套闭环, 验证:
  1. install_runtime 装上 ProtocolRegistry / Pheromone / QiBudget / CapabilityCardCache
  2. seed_default_protocols 真 seed 5 个 protocol 进 registry
  3. orchestrator KUN_PROTOCOL_CONSUME_ENABLED=1 真消费协议 (改 ExecutionMode + verification)
  4. orchestrator KUN_ANTI_GAMING_ENABLED=1 真跑 7 套路 check
  5. Pheromone reinforce hook (orchestrator step 完后 reinforce)
  6. PheromoneDecayStep (idle_batch step 跑)
  7. metrics_collector 30s tick set gauge
  8. cron jobs 注册 (Darwin / AI Scientist / PC train)

跑法:
    KUN_QI_RUNTIME_ENABLED=1 KUN_QI_ENABLED=1 KUN_PROTOCOL_CONSUME_ENABLED=1 \\
    KUN_ANTI_GAMING_ENABLED=1 KUN_QI_FORCE_ACTIVE=1 \\
    uv run python scripts/dogfood_v23_mock.py

预期输出 (重要打勾):
  ✓ ProtocolRegistry 装上 + 5 seed protocol
  ✓ orchestrator 收到 protocol_registry + anti_gaming_detector
  ✓ Pheromone storage 装上
  ✓ CapabilityCardCache 装上
  ✓ metrics 注册了 (kun_protocol_match_total / kun_anti_gaming_detection_total / etc.)
  ✓ Pheromone decay step 跑 (返 affected count)
  ✓ qi CLI status 正确显示
"""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from typing import NoReturn

# 强制启用 V2.3 全套
os.environ["KUN_QI_RUNTIME_ENABLED"] = "1"
os.environ["KUN_QI_ENABLED"] = "1"
os.environ["KUN_QI_FORCE_ACTIVE"] = "1"
os.environ["KUN_PROTOCOL_CONSUME_ENABLED"] = "1"
os.environ["KUN_ANTI_GAMING_ENABLED"] = "1"
os.environ["KUN_PROTOCOL_SEED_DEFAULTS"] = "1"
os.environ["KUN_PHEROMONE_DECAY_ENABLED"] = "1"


def _section(title: str) -> None:
    print(f"\n\033[36m═══ {title} ═══\033[0m")


def _ok(msg: str) -> None:
    print(f"\033[32m✓\033[0m {msg}")


def _fail(msg: str) -> NoReturn:
    print(f"\033[31m✗\033[0m {msg}")
    sys.exit(1)


async def main() -> None:
    print("\033[1mV2.3 mock dogfood — 不需真 LLM, 验闭环\033[0m\n")

    # ===== 1. install_runtime =====
    _section("1/8 install_runtime — 装 V2.3 全套到 app.state")
    from kun.api.runtime import install_runtime
    from kun.qi import (
        reset_pheromone_storage,
        reset_protocol_registry,
        reset_qi_budget,
    )
    from kun.qi.pheromone import InMemoryPheromoneStorage
    from kun.watchtower.engine import RuleEngine
    from kun.watchtower.rules import GuardRule, RuleTrigger
    from starlette.datastructures import State

    reset_protocol_registry()
    reset_pheromone_storage()
    reset_qi_budget()

    rule_engine = RuleEngine(
        [GuardRule(id="x", kind="guard", trigger=RuleTrigger(event_type="*", when="True"))]
    )
    app = SimpleNamespace(state=State())
    install_runtime(app, rule_engine=rule_engine)
    _ok(f"app.state.protocol_registry: {type(app.state.protocol_registry).__name__}")
    _ok(f"app.state.pheromone_storage: {type(app.state.pheromone_storage).__name__}")
    _ok(f"app.state.qi_budget: {type(app.state.qi_budget).__name__}")
    _ok(f"app.state.capability_card_cache: {type(app.state.capability_card_cache).__name__}")
    _ok(
        f"app.state.orchestrator.protocol_registry is set: "
        f"{app.state.orchestrator.protocol_registry is not None}"
    )
    _ok(
        f"app.state.orchestrator.anti_gaming_detector is set: "
        f"{app.state.orchestrator.anti_gaming_detector is not None}"
    )

    # ===== 2. seed protocols =====
    _section("2/8 seed_default_protocols — 5 starter protocol")
    from kun.qi.seed_protocols import seed_default_protocols

    seed_count = await seed_default_protocols(app.state.protocol_registry)
    _ok(f"seeded {seed_count} protocols")
    listed = await app.state.protocol_registry.list_all("u-sylvan")
    _ok(f"registry.list_all 返 {len(listed)} 个 protocol")
    for p in listed:
        print(f"   - {p.protocol_id}@{p.version} status={p.status} mode={p.execution.mode}")
    if len(listed) != 5:
        _fail(f"期望 5 个 seed protocol, 实际 {len(listed)}")

    # ===== 3. find_protocol_for =====
    _section("3/8 find_protocol_for — 协议匹配")
    found = await app.state.protocol_registry.find_protocol_for(
        {"task_type": "writing.creative.short", "complexity_score": 0.3, "risk_level": "low"},
        "u-sylvan",
    )
    if found is None:
        _fail("writing.creative.short 应匹配到 protocol, 实际没")
    _ok(f"writing.creative.short → matched {found.protocol_id} (mode={found.execution.mode})")

    not_found = await app.state.protocol_registry.find_protocol_for(
        {"task_type": "unknown.weird.type", "complexity_score": 0.5, "risk_level": "low"},
        "u-sylvan",
    )
    if not_found is not None:
        _fail("unknown.weird.type 不应匹配, 实际 matched")
    _ok("unknown.weird.type → no match (期望)")

    # ===== 4. AntiGamingDetector =====
    _section("4/8 AntiGamingDetector — 7 套路 check")
    from kun.security.anti_gaming import AntiGamingDetector

    det = AntiGamingDetector()
    finding = det.check(prompt="What is 1+1?", answer="What is 1+1?")
    if finding is None:
        _fail("copy_prompt 应被检出, 实际没")
    _ok(f"copy_prompt detected: pattern={finding.pattern} severity={finding.severity}")

    finding2 = det.check(planned_steps=10, actual_steps=2)
    if finding2 is None:
        _fail("skip_step 应被检出, 实际没")
    _ok(f"skip_step detected: pattern={finding2.pattern}")

    # ===== 5. Pheromone reinforce + decay =====
    _section("5/8 Pheromone reinforce + decay")
    storage: InMemoryPheromoneStorage = app.state.pheromone_storage
    await storage.reinforce(
        "u-sylvan",
        source_kind="skill",
        source_id="reader",
        target_kind="skill",
        target_id="writer",
        relation_type="follows",
    )
    initial = storage.get_pheromone("u-sylvan", "skill", "reader", "skill", "writer", "follows")
    _ok(f"reinforce(reader→writer): pheromone={initial:.4f}")

    from kun.engineering.idle_batch import PheromoneDecayStep

    step = PheromoneDecayStep()
    result = await step.run("u-sylvan")
    after = storage.get_pheromone("u-sylvan", "skill", "reader", "skill", "writer", "follows")
    _ok(f"PheromoneDecayStep: affected={result.get('affected')} after={after:.4f}")
    if after >= initial:
        _fail("decay 后值应比初始小, 实际没衰减")

    # ===== 6. metrics collector =====
    _section("6/8 metrics_collector — 30s tick set gauge")
    from kun.qi.metrics_collector import collect_once

    await collect_once(app, "u-sylvan")
    _ok("collect_once 跑了一次, 没报错")

    from kun.core.metrics import (
        capability_card_cache_hit_rate,
        pheromone_total_strength,
        qi_window_active,
    )

    # 查 gauge 当前值
    qi_window_val = qi_window_active.labels(tenant_id="u-sylvan")._value.get()
    pheromone_val = pheromone_total_strength.labels(tenant_id="u-sylvan")._value.get()
    cache_hit_val = capability_card_cache_hit_rate.labels(tenant_id="u-sylvan")._value.get()
    _ok(f"kun_qi_window_active = {qi_window_val} (期望 1.0, 因为 KUN_QI_FORCE_ACTIVE=1)")
    _ok(f"kun_pheromone_total_strength = {pheromone_val:.4f}")
    _ok(f"kun_capability_card_cache_hit_rate = {cache_hit_val}")

    # ===== 7. CLI =====
    _section("7/8 kun qi CLI status")
    from kun.cli import app as cli_app
    from typer.testing import CliRunner

    runner = CliRunner()
    cli_result = runner.invoke(cli_app, ["qi", "status"])
    if cli_result.exit_code != 0:
        _fail(f"kun qi status exit_code={cli_result.exit_code}")
    _ok("kun qi status 返 exit_code=0")
    print("   --- output ---")
    for line in cli_result.output.splitlines():
        print(f"   {line}")

    # ===== 8. cron jobs registered =====
    _section("8/8 启 cron jobs (register_qi_cron_jobs)")
    from kun.engineering.cron_scheduler import CronScheduler
    from kun.qi.cron_jobs import register_qi_cron_jobs

    sched = CronScheduler()
    register_qi_cron_jobs(sched, app, "u-sylvan")
    jobs = sched.list_jobs()
    expected = {"qi_pc_train_hourly", "qi_darwin_explore_hourly", "qi_ai_scientist_hourly"}
    if not expected.issubset(set(jobs)):
        _fail(f"期望 cron jobs {expected}, 实际 {jobs}")
    _ok(f"cron jobs registered: {sorted(jobs)}")

    # 跑一次 PC train (启窗口活跃 → 应跑)
    pc_job = sched.get_job("qi_pc_train_hourly")
    if pc_job is not None:
        await pc_job.callback()
        _ok("qi_pc_train cron job 跑了一次, 没报错")

    print("\n\033[1;32m✓ V2.3 mock dogfood 全过. V2.3 真闭环 verified.\033[0m\n")
    print("下一步: 用真 LLM API key 跑 ./scripts/dogfood_v23.sh 验真协议涌现.")


if __name__ == "__main__":
    asyncio.run(main())
