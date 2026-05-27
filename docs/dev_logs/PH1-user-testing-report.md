# Phase 1 USER_TESTING 实跑报告

**Date**: 2026-05-27
**Tester**: Claude (Phase 1 验收手册按 `docs/USER_TESTING.md`)
**前置**: 1550 tests passing · ruff 全绿 · docker-compose 健康 · LT.INT 全部装配完毕

---

## 实验 1 · GoalAnchor pin (Director 长任务防漂移层) ✅

**结果**: anchor 渲染含 4 字段 + 视觉边界 + immutable 标记.

```
═══ GOAL ANCHOR (immutable, pinned, do not override) ═══
GOAL: 把 kun/agents/executor 拆成 5 个独立模块, 不破坏现有调用
SUCCESS CRITERIA:
  - tests/unit 全绿
  - 5 个独立子模块, 每个 <300 行
  - 现有 import kun.agents.executor.xxx 全部不变
OUT OF SCOPE (refuse if user requests these):
  - 顺手重写 Director
  - 改 task 入口契约
INVARIANTS (must hold throughout):
  - 不动 data 脊柱表 schema
  - 不影响监督线接入
════════════════════════════════════════════════════════════
```

**验证项全过**: 视觉边界 ✓ / 4 字段 ✓ / "immutable, do not override" ✓

---

## 实验 2 · Supervisor 异常检测 + emitter ✅

**结果**: 3 个 strategy_search_request (期望 ≥3, 实际 3).

```
触发了 3 个 strategy_search_request
  - task_failure_spike on executor.coding.refactor → priority=high severity=mid triggered_by=anomaly_threshold
  - duration_outlier   on executor.coding.refactor → priority=low  severity=weak triggered_by=anomaly_threshold
  - module_systemic    on executor.coding.refactor → priority=high severity=strong triggered_by=anomaly_cluster
```

**验证项全过**:
- 2 个 `anomaly_threshold` 单异常 (failure_spike + duration_outlier) ✓
- 1 个 `anomaly_cluster` 系统性 (module_systemic) ✓

---

## 实验 3 · Strategist + Explorer Pool 三模式 ✅

**结果**: 3 candidates, 各自 explorer_mode + sampling + rollout 都不同.

```
产了 3 个 candidates:
  - [conservative] target_level=1 sampling=0.3 rollout=canary    | tier_upgrade strong→top
  - [performance]  target_level=1 sampling=1.0 rollout=shadow    | retry_budget 1→3
  - [aggressive]   target_level=1 sampling=0.5 rollout=canary    | primary_swap anthropic→openai
```

**验证项全过**:
- 3 个 candidates ✓
- explorer_mode: conservative / aggressive / performance ✓
- sampling_rate: 0.3 / 0.5 / 1.0 ✓
- rollout_mode: canary / canary / shadow ✓
- 每个有 rationale ✓

---

## 实验 4 · Forward / Backward 自动决策 ✅

**结果**: 24h 内有 enabled capability 命中 target → Strategist 选 backward rollback.

```
Decision: 选了 1 个 candidate(s)
  [backward] capability_rollback — rollout_mode=direct
```

**验证项全过**:
- 1 个 backward candidate (vs 实验 3 的 3 个 forward) ✓
- change_spec.kind == capability_rollback ✓
- rollout_mode = direct ✓

---

## 实验 5 · Gate 4 条准入规则 ✅

**结果**: 3 case 全符合预期, 共写 2 行 capability row (case 1 + case 3).

```
Case 1 (normal):           approve — promotion_state=merged
Case 2 (low pass_rate):    reject  — ['test_pass_rate=0.60<0.9']
Case 3 (self-referential): awaiting_human_review — writes row? True
  metadata.promotion_block_self_referential=True

总共写了 2 行 capability row.
```

**验证项全过**:
- Case 1 approve, promotion_state=merged ✓
- Case 2 reject, reason 含 `test_pass_rate=0.60<0.9` ✓
- Case 3 awaiting_human_review, 仍写一行 row, metadata 标 `promotion_block_self_referential=True` ✓

---

## 实验 6 · 端到端 RSI 闭环 (真接 Postgres) ✅

**结果**: 真接 docker-compose Postgres, 完整 closed-loop 走通.

```
[1] Supervisor emit 异常 → Postgres:
    Postgres 中有 2 个 search_request:
      - anomaly_threshold | anthropic                | priority=medium
      - anomaly_threshold | executor.coding.refactor | priority=low

[3] Strategist 产生 candidates:
      - [conservative] sampling=0.3 rollout=canary
      - [performance]  sampling=1.0 rollout=shadow
      - [aggressive]   sampling=0.5 rollout=canary

[4-5] Gate approve 全部 → runtime_capabilities:
      - cp-...HS4GP | llm.router | state=merged | enabled=False | sampling=0.30
      - cp-...MPWG6 | llm.router | state=merged | enabled=False | sampling=1.00
      - cp-...840VV | llm.router | state=merged | enabled=False | sampling=0.50

[6] Promotion sweeper 扫一次:
    Scanned 3 | expired 0 | stale 0
```

**验证项全过**: 真 Postgres 写 2 search_requests + 3 runtime_capabilities + sweeper 0 expired.

---

## 7 个其他验证点 · 全过 ✅

| 点 | 期望 | 实际 |
|---|---|---|
| 方法论蒸馏 | ~192 novel from 11 sources | **277 novel from 15 sources** (LT 阶段又贡献了 dev logs) |
| Input Classifier ("stop now") | category=interrupt | ✅ `interrupt` |
| RCDH ("feature flag disabled") | L1 activate | ✅ `root_cause_level=1, action=activate` |
| Anomaly Cluster | 见实验 2 | ✅ 实验 2 已覆盖 (`anomaly_cluster` request 命中) |
| Priority Channel (`anomaly_cluster`) | urgent | ✅ `urgent` |
| Exploration Penalty | `test_l4_exploration_penalty.py` 全过 | ✅ (1550 tests 全过含此) |
| External Supervisor 自嗨检测 | `test_external_supervisor_modes.py` 全过 | ✅ (同上) |

---

## 总结

**Phase 1 (L0-L5) 6 个核心实验 + 7 个其他验证点全部通过**.

每个实验:
1. 输入: 已经 / 模拟的真实 KUN 上游数据
2. 调用: 真服务 (零 mock except emitter)
3. 输出: 符合预期 + 等价于设计文档断言

**新发现 (相对 USER_TESTING.md 写的时点)**:
- 方法论蒸馏从 192/11 → **277/15** — LT 阶段的 dev_logs (LT-progress + LT-retrospective + LT.INT-progress) 多贡献 ~85 novel candidates + 4 sources
- 所有实验跑得很快 (除了实验 6 真 PG ~3s, 其他都 <1s)
- 实验 5 写了 **2** 行 capability row (case 1 + case 3), case 2 reject 不写 — 这是符合 Gate 的契约设计

**没发现 bug**.

**结论**: Phase 1 service 层全套真能跑, 跟 USER_TESTING 设计预期完全一致. Phase 1 验收 OK.

---

## 跑法

直接 paste 实验里的 shell 命令到终端 (用 `.venv/bin/python` 替 `uv run python`). 6 个核心 + 7 个其他验证大约 5-10 分钟跑完.

```
# 前置
docker compose -f docker-compose.dev.yml ps | grep postgres   # 确保 Up
.venv/bin/python -m pytest tests/ 2>&1 | tail -1             # 1550 passed

# 6 核心
.venv/bin/python -c "<实验 1 代码>"
.venv/bin/python -c "<实验 2 代码>"
.venv/bin/python -c "<实验 3 代码>"
.venv/bin/python -c "<实验 4 代码>"
.venv/bin/python -c "<实验 5 代码>"
.venv/bin/python scripts/e2e_rsi_demo.py

# 7 其他验证点用同样形式跑 (单行 python -c)
```
