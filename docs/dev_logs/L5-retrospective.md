# Dev Log: L5 · 自创任务 (RSI 真闭合)

**Date**: 2026-05-27
**Phase / Level**: L5 (Supervisor 异常聚类 / promotion_queue 超时 / Priority Channel / 自创 RSI 请求 / e2e wiring)
**Duration**: ~3 小时（2 轮 /loop 迭代）
**Commits**: 78f79ac + bf87d6d (L5.1) · 7a9d79a (L5.2) · b4df983 (L5.3) · 29e84b3 (L5.4) · 53aaca7 (L5.5) · 本 retrospective + 3 新 methodology seeds (本提交)
**Tests**: 1095 → 1160（+65 new tests across 5 sub-tasks, all green）

---

## Goal

L5 §交付标志 (PROGRESS.md): **监督线发现的系统性问题 → 自动转 Strategist 任务（无需人提）→ 闭环跑完产出 capability → 自动晋级**。

L5.1 → 异常聚类自动产 search_request
L5.2 → promotion timeout 自动 reaudit 卡死的 capability
L5.3 → Priority Channel 让 systemic signal 跳过普通队列
L5.4 → cluster + RCDH 综合产 rich request 喂给 Strategist
L5.5 → e2e wiring: Supervisor + RCDH + Priority Channel 全串联

L5 §交付标志全部达成（service-layer 自创任务基础设施完备）。

---

## Approach

**3 层信号自动转任务的工程化栈**：
1. **聚类层 (L5.1)**：原子 anomaly → cluster (module_systemic / cross_module_pattern / tenant_degradation)
2. **优先级层 (L5.3)**：cluster + timeout → urgent tier，普通 anomaly → 跟随 base priority
3. **enrichment 层 (L5.4)**：cluster + RCDH → hint Strategist 选 target_level + explorer_mode

**全 L5 阶段 0 LLM 调用** (与 L4 一致) — 治理层比执行层更确定。

**Pure functions + dependency injection**：每个新模块（anomaly_cluster, promotion_queue, priority_channel, self_created_request）核心是 pure 函数；side-effect (DB writer / search emitter / diagnostic runner) 全注入。

---

## Key Decisions

1. **Cluster 3 规则按优先级 + Member 不重用 (L5.1)**：module_systemic > cross_module_pattern > tenant_degradation；同 request 不进多个 cluster — 避免重复算账
2. **Cluster 自己的 dedup_key (L5.1)**：`tenant:cluster:kind:module` 让同 cluster 1h 内不重复触发
3. **Cluster 默认 priority=high / severity=strong (L5.1)**：聚类已超单异常基线, 跳 weak/mid
4. **`recent_emitted_requests` 滑动 buffer limit 20 (L5.1)**：太多让 cluster O(n²) 慢; 太少抓不到跨时间
5. **`stale` vs `expired` 双档 (L5.2)**：stale = 推 reaudit 不动 state；expired = 写 state="expired" + 推 reaudit
6. **ready state stale 不算 reaudit (L5.2)**：ready 是等 Gate enable 的合理 state, 单 Gate 慢不是 capability 自己的错
7. **`urgent` tier 不分 cluster vs expired (L5.3)**：两者都"影响 RSI 闭环"一档. 同等紧急
8. **`promotion_stale` 在 `high` 而非 `urgent` (L5.3)**：stale 还可以继续 try, expired 才完全卡死
9. **同 tier 内 FIFO (L5.3)**：防 starvation；同等紧急时, 先到先处理
10. **`target_level_hint` 而非 constraint (L5.4)**：Strategist 看 hint 但可不 follow (有更精确 generator 时)
11. **`recommended_action` → explorer_mode 字典映射 (L5.4)**：redesign→aggressive / activate→conservative — 让 Strategist 推荐合适 Explorer mode
12. **只追加 is_root_cause=True evidence (L5.4)**：noise filter — 让 Strategist 接到的 evidence 不被淹没
13. **不 mutate input dict (L5.4)**：dict 浅拷贝 + 追加. caller 可同时持"原 cluster"和"enriched"
14. **diagnostic_runner 默认 None (L5.5)**：不强制 Supervisor 接 RCDH；测试 / 早期阶段可纯 cluster
15. **diagnostic_runner 异常 fallback (L5.5)**：RCDH 可能 unreliable, 不应阻塞 emit

---

## Constraints Applied

- 单 commit ≤ 1000 行 / ≤ 5 文件改动（L5.1 拆 2 sub-commit）
- pytest + ruff 双绿才 commit
- 每改动 grep verify（L5.1 重用 cluster fan-out / L5.5 是否已有 enrich helper）
- destructive 操作（无）— 全程纯 add + extend
- 每 commit 后追加 progress.md 微日志
- ADR-025 蒸馏卡片单独文件 (本提交 +3 新 seeds)

---

## Patterns Used

| Pattern | Where | Note |
|------|------|------|
| 3 层规则按优先级 + Member 不重用 | L5.1 cluster | 防重复算账 |
| Sliding buffer limit | L5.1 recent_emitted_requests | 同 L4.5/L4.6 deque purge 思路 |
| Pure function + dependency injection | 全 L5 模块 | 单测零 DB / LLM 依赖 |
| 双档 state (stale vs expired) | L5.2 promotion_queue | 区分"卡但活"与"完全卡死" |
| `_X_TO_Y` 字典映射 module-level | L5.4 action → level/explorer | 可单独 grep 改 |
| Hint 而非 constraint | L5.4 target_level_hint | downstream 可自决 |
| Fallback to input on error | L5.5 diagnostic_runner 异常 | graceful degradation |
| 4 档 recommended_action | L5.2 keep/re_evaluate/mark_expired/advance | 详细决策表 |
| ISO 字符串容错 | L5.2 timestamps | 同 L3.3 / L4 |

---

## What Failed

1. **L5.4 验证 `__init__.py` ruff `__all__` 排序**：手动加 build_self_created_request 时插错位, ruff 抓 `RUF022` 字母序不对。autofix 解
2. **L5.5 测试期望 tier 名错**：duration_outlier 的 priority="low" 让 sorted_pairs[-1].tier 是 "low" 不是 "medium" — 测试逻辑修正为接受 low/medium/high
3. **L5.5 测试 created_at 字段缺**：prioritize_requests 接受 None timestamp 但需要测试覆盖. 加 fixture 显式带 created_at
4. **L5.1 _maybe_cluster 调用顺序**：第一版放在 emit 循环之后, 导致 cluster 触发的 emit 不被 cluster 自身的 dedup state 看到. Fix 把 cluster 检查放 emit 循环前

---

## What Worked

1. **3 层处理栈干净**：聚类 → 优先级 → enrichment 各自一层独立 module, 互不依赖具体实现
2. **dependency injection 全栈一致**：diagnostic_runner / capability_reader / search_emitter / state_writer 都用 `Callable[..., Awaitable[None]]` 风格 — 同 L2-L4 经验
3. **Cluster 自带 dedup_key 让 cluster 不会因为新 anomaly 反复触发**
4. **`recommended_action` → `target_level` + `explorer_mode` 双映射字典**：写下来后 Strategist 后续添加新 action 类型只需扩字典
5. **Pure 函数式 priority_channel**：no async, no state, no DB — 100% deterministic, 单测覆盖所有路径
6. **`is_root_cause=True` evidence filter**：让 enriched request 不被 L0/L2/L3 噪音淹没；Strategist 只看到真正命中的层

---

## Heuristics Extracted

1. 聚类规则按优先级 + member 不重用 — 防同 anomaly 算多次
2. Cluster 自己的 dedup_key 避免反复触发
3. 双档 state (stale vs expired) 比单一 timeout 表达力更强
4. ready state 即使 stale 也不算 reaudit — 状态机的合理停留点
5. urgent tier 包含两类信号 (cluster + expired) 因为都"影响 RSI 闭环"
6. 同 tier 内 FIFO 防 starvation
7. Hint 字段而非 constraint 让 downstream 自决, 不绑死决策
8. `_X_TO_Y` 字典 module-level + grep-friendly 比 if/elif 链好维护
9. Pure 函数式 + dependency injection 让单测零外部依赖
10. Fallback to input on optional injection failure (diagnostic_runner)
11. Sliding buffer limit (20) 同时控算法复杂度 + 内存
12. 不 mutate input dict — caller 可同时持 raw 和 enriched

---

## Methodology Card Candidates

以下蒸馏自本阶段（与本 retrospective 同时归档 ≥ 3 份新 seeds）：

1. ✅ **anomaly_clustering_with_member_uniqueness** — 聚类规则按优先级 + Member 不重用（新建 seed）
2. ✅ **promotion_lifecycle_sweeper_with_reaudit_signal** — 4 档 action + reaudit search_request（新建 seed）
3. ✅ **engineering_hints_not_constraints** — Hint 字段让 downstream 自决（新建 seed）

---

## L5 验收清单

- [x] **L5.1** Supervisor 异常聚类 (commits 78f79ac + bf87d6d · +15 tests)
- [x] **L5.2** Promotion Queue 超时规则 (commit 7a9d79a · +16 tests)
- [x] **L5.3** Priority Channel (commit b4df983 · +18 tests)
- [x] **L5.4** 自创 RSI 请求生成 (commit 29e84b3 · +11 tests)
- [x] **L5.5** End-to-end wiring (commit 53aaca7 · +5 tests)
- [x] **L5.6** L5 验收 + retrospective + 3 新 methodology seeds (本提交)

**测试总数**：1095 → 1160（+65）
**Ruff**：clean throughout
**Schema**：未动 (Pool / Quota / Penalty / Cluster / Priority Channel 全 in-memory state)
**LLM 调用**：L5 阶段 0 次 — 全工程化治理（与 L4 一致）
**北极星**：L5 §交付标志达成 — **监督线 → 自动产任务 → enrich → Priority 排序** 端到端工程化基础设施全部就位。**真闭环演练** (Supervisor cluster → Strategist + Pool + Explorer → Gate enable → 启用) 需要 NATS / DB writer 接入, 留给 L6 / 真实工程任务做。

---

*最后更新：2026-05-27*
