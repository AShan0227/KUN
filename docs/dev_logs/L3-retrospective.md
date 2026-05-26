# Dev Log: L3 · 闭环扩散 + Forward/Backward 双策略

**Date**: 2026-05-27
**Phase / Level**: L3 (第 2-3 条 RSI 实例 + Forward/Backward + 三级阈值 + 自指限制强化 + ADR-018 半合并)
**Duration**: ~4 小时（含 3 个 /loop 自主迭代）
**Commits**: f03a5bf (L3.1) · 0476315 (L3.2) · 5276801 (L3.3) · 0c8bc9a (L3.4) · a496a9f + 5d30395 (L3.5 双 sub-commit) · 84308fa + 98c890a (L3.6 双 sub-commit) · 本 retrospective + 3 新 methodology seeds（本提交）
**Tests**: 928 → 1003+（+75 new tests across 6 sub-tasks, all green）

---

## Goal

L3 阶段 PROGRESS.md §交付标志:
1. **3 条 RSI 实例并行跑** —— 已有 L2 LLM 路由 + L3.1 context 压缩 + L3.2 skill 选择共 3 条
2. **Forward/Backward 双策略 auto-select** —— Strategist 根据 capability_history 决定 forward 新探索 vs backward rollback
3. **自指限制生效** —— governance source of truth + Strategist 强制 target_level=0 + Gate 双层独立检查 + enable_capability 强 gate
4. **ADR-018 半合并补齐到 ≥ 3 调用方** —— NotificationLayer 升 3 caller，其他 3 项做完整审计 + 决策

L3 §交付标志全部达成（service-layer 基础设施层面）。

---

## Approach

**1 commit ≤ 5 文件 严守纪律**：
- L3.5 拆 2 sub-commit（governance/strategist refactor + Gate strengthening）
- L3.6 拆 2 sub-commit（code wiring + audit doc）
- 单 commit ≤ 1000 行约束自然遵守

**测试驱动 + paired commit 节奏**：
- 每个子任务前 grep 既有约定 → 改造 → tests/unit/test_l3_*.py 一对一 → pytest + ruff 双绿
- L3.6 audit 反对"为达 ≥3 caller 而 KPI 化"思维 — 实事求是审计 4 项

**3 条 RSI 实例同形态**：
- L2 LLM 路由 / L3.1 context 压缩 / L3.2 skill 选择 都遵循同样的 (Supervisor 异常 → Strategist 3 Explorer 候选) 模式
- 候选数据结构 (StrategyExperiment) + emitter 模式让新 RSI 实例只需 +1 异常 check + 1 generator function

---

## Key Decisions

1. **`select_repair_direction` 是 pure module function 而非 method**：让 LLM Strategist (L3+) 或新 caller 都可复用同一决策逻辑
2. **24h 是 backward lookback 默认**：覆盖大多数 promotion 周期；太短漏掉缓慢恶化，太长误判
3. **backward rollback rollout_mode="direct"**：回滚是已知态，不 canary。canary 在 forward 探索时合理；rollback 直接关
4. **`compute_severity` 用 priority + repeat + failure_rate 组合，不仅 priority**：单 priority 太粗，组合能区分"高优单次"(mid) vs "高优复发"(strong)
5. **repeat_count 跨 dedup_ttl 累计而非每 TTL 重置**：dedup 是窗口去重，repeat 是长期跟踪。让 severity 自然升级
6. **`kun/governance/self_referential.py` 集中 source of truth**：之前 prefix 列表在 strategist + escalation 各一份，未来再加 caller 就 3 份漂移
7. **Strategist 自指 candidate 强制 target_level=0**：改自己 = 设计层决策，不能伪装成 module-level fix 让 Gate 误判
8. **Gate 自指仍写 capability row + sampling=0**：让 promotion_queue 跟踪人审进度，row 不存在 = 看不见
9. **`enable_capability` self-referential gate 强 raise PermissionError**：状态机推进必须可见，不让 promote 自指 capability 静默成功
10. **ADR-018 §16 "≥3 caller" 是护栏不是 KPI**：反对硬塞 caller。GuardPolicy 无自然 caller → 永久退役；ValidationPipeline 接口已成熟 → by interface 真合并
11. **NotificationSender 用 dict-shape 而非 Notification 对象**：Service 不 import datamodel，降低耦合

---

## Constraints Applied

- 单 commit ≤ 1000 行 / ≤ 5 文件改动（L3.5 + L3.6 各拆 2 sub-commit）
- pytest + ruff 双绿才 commit
- 每改动 grep verify（L3.5 `_SELF_REFERENTIAL_PREFIXES` 散点 / L3.6 NotificationLayer 现有 caller）
- destructive 操作（无）
- 每 commit 后追加 progress.md 微日志
- ADR-025 蒸馏卡片单独文件 + index 注册（本提交 +3 新 seeds）

---

## Patterns Used

| Pattern | Where | Note |
|------|------|------|
| Explorer Pool 3 模式 | L3.1 / L3.2 RSI 实例 | Conservative / Aggressive / Performance |
| Engineering rules first, LLM tier 2 | 全 6 子任务 | 同 L2 经验扩展 |
| Frozen dataclass + emitter callback | StrategyExperiment / EscalationDecision | 同 L2 |
| 依赖注入 capability_history_reader / notification_sender | L3.3 / L3.6 | Service 不 import DB / datamodel |
| Pure module-level decision functions | select_repair_direction / decide_escalation / is_self_referential | 让多 caller 复用 |
| Source of truth 集中 | kun/governance/self_referential.py | L3.5 抽 |
| 1 sub-commit ≤ 5 文件 | L3.5 / L3.6 拆分 | paired_commit methodology 操作 |
| 工程化规则 + 词边界 + 命名形式覆盖 | _is_self_referential 4 形式 + escalation keyword | 同 L2.6 NL keyword 经验 |
| Backward 路径 ≠ Forward 路径 | L3.3 backward 单候选, rollout=direct | 区分语义 |

---

## What Failed

1. **L3.4 SupervisorService 集成测试 target_module 落点错估**：第一版测试用 `task_type="supervisor.audit"` 期望 target_module 命中前缀，但实际 `_check_failure_spike` 把 `target_module = f"executor.{task_type}"` 拼成 `"executor.supervisor.audit"` —— 不命中。教训：grep 先看 target_module 是如何构造的而非猜测
2. **L3.4 repeat_count 测试逻辑错算**：第一版假设 dedup_ttl=0 让每事件触发，但忽略每事件都会增 repeat_count，所以 8 事件后 repeat=5 不是 2。重写期望让测试反映真行为
3. **L3.5 旧 L2.8 test 假设过时**：`capability_row_payload is None` 在 L3.5 改 self-ref 写 row 后不再成立。更新断言而非回退新行为
4. **L3.6 staged 6 文件超 5 限**：第一次 commit 暂存 6 文件，发现超限主动拆 2 sub-commit。教训：commit 前 `git diff --stat --cached` 是必备节流步

---

## What Worked

1. **3 RSI 实例同形态扩展**：L3.1 context 压缩 + L3.2 skill 选择沿用 L2 LLM 路由 (Supervisor + 候选生成器 + emitter) 完全相同结构。**写新 RSI 实例 = 1 个 _check_X + 1 个 _candidates_for_X**
2. **Forward/Backward auto-select 干净不分裂主路径**：`_emit_and_adjust` 共用 helper 让 backward 也走自指限制检查 + emit，主路径只 +1 决策分支
3. **集中 source of truth 立刻见效**：L3.5 抽 `kun/governance/self_referential.py` 后，escalation + strategist 改 wrapper 都减少代码，且 Gate L3.5 引入新 caller 直接 import 不需重复定义
4. **审计先于改造**：L3.6 先用 grep 实际数 caller，然后做"维持/补齐/退役"三档决策。比无脑"加 caller 凑数"清晰得多
5. **每个子任务后追加 progress.md**：dev_log 累积到 L3.7 retrospective 时直接复制粘贴章节，蒸馏不需要回忆
6. **测试覆盖反向场景**：每个新功能至少 1 个 "异常 / 缺字段 / sender 失败" 测试 — 保 caller 失败不打挂主路径

---

## Heuristics Extracted

1. RSI 实例扩展遵循 (新 anomaly check + 新 candidate generator) 单一模式；新增类型不动主路径
2. Strategist 提候选时 Explorer Pool 3 模式默认 (Conservative+Aggressive+Performance) — 各自 sampling/rollout/acceptance 分级
3. Forward vs Backward auto-select 看 capability_history 24h 窗口 — 不看更早历史
4. Severity 决策用 priority × repeat × failure_rate 组合，单维度太粗
5. Escalation path 累加而非替代 (mid 不替代 role 而是 role+task)，self-ref 追加 human 独立
6. 自指限制要 4 种命名形式覆盖；集中 source of truth 避免漂移
7. "改自己" target_level 强制 0 (设计层)；module-level fix 是 level 2 的语义
8. Gate self-referential 写 row 但 enabled=False + sampling=0 — 让 promotion_queue 看见, 不让流量沾
9. enable_capability self-ref 必 raise PermissionError 而非 silent skip — 状态机推进可见性必备
10. "≥3 caller" 是抽象护栏不是 KPI；GuardPolicy 无 caller → 退役，不假装实施
11. NotificationSender 用 dict shape — Service 不 import datamodel，降耦合
12. 单 commit ≤ 5 文件强约束驱动 sub-commit 拆分；先 `git diff --stat --cached` 看清
13. Backward rollback rollout_mode=direct (不 canary) — 已知态不再探索

---

## Methodology Card Candidates

以下蒸馏自本阶段（与本 retrospective 同时归档 ≥ 3 份新 seeds）：

1. ✅ **rsi_explorer_pool_three_modes** — 工程化 RSI 候选生成统一 Conservative/Aggressive/Performance 三模式（新建 seed）
2. ✅ **forward_backward_repair_auto_select** — 24h 内有 enabled capability → backward, 否则 forward（新建 seed）
3. ✅ **caller_count_is_a_guardrail_not_a_kpi** — ADR-018 §16 "≥3 caller" 反 KPI 思维（新建 seed）
4. **dependency_injection_for_db_decoupling**（candidate，本阶段不入库，与 frozen_dataclass_agent_io_contract 部分重叠）
5. **escalation_path_accumulate_not_replace**（candidate）
6. **source_of_truth_centralization_for_shared_predicates**（candidate）

---

## L3 验收清单

- [x] **L3.1** 第 2 条 RSI 实例：context 压缩策略（commit f03a5bf · +10 tests）
- [x] **L3.2** 第 3 条 RSI 实例：skill 选择启发式（commit 0476315 · +9 tests）
- [x] **L3.3** Forward / Backward 双修复策略（commit 5276801 · +14 tests）
- [x] **L3.4** 监督线三级阈值 + 4 级升级路径（commit 0c8bc9a · +22 tests）
- [x] **L3.5** 自指限制强化（commits a496a9f + 5d30395 · +13 tests）
- [x] **L3.6** ADR-018 半合并补齐（commits 84308fa + 98c890a · +7 tests）
- [x] **L3.7** L3 验收 + retrospective + 3 新 methodology seeds（本提交）

**测试总数**：928 → 1003（+75）
**Ruff**：clean throughout
**Schema**：未动（L1.3 已建好的 7 张表 service-layer 写入路径就位 + L3 用到 capability_history + notification 都是 sender 注入接口）
**北极星**：L3 §交付标志 4 条 — 3 RSI 并行 + Forward/Backward + 自指生效 + ADR-018 半合并补齐 全部满足。**第一条 RSI 真闭环演练**（Supervisor→Strategist→Gate→Capability enable）需要把这些 service 接到 NATS / 真 DB writer + 跑一次端到端实验 —— 留给 L4 启动时做。

---

*最后更新：2026-05-27*
