# Dev Log: L4 · 多实例资源治理

**Date**: 2026-05-27
**Phase / Level**: L4 (Explorer Pool / Supervisor Pool / External Supervisor Pool / 合议层 / Resource Quota / Exploration Penalty)
**Duration**: ~3 小时（2 轮 /loop 迭代）
**Commits**: b6c560c + 65180f2 (L4.1) · 173d3c1 (L4.2) · a472331 (L4.3) · 64cbe60 + 751f8c2 (L4.4) · 8431427 + 7d171dc (L4.5) · fcf1241 + 595b050 (L4.6) · 本 retrospective + 3 新 methodology seeds (本提交)
**Tests**: 1003 → 1095（+92 new tests across 6 sub-tasks, all green）

---

## Goal

L4 阶段 PROGRESS.md §交付标志:
1. **Strategist Explorer Pool / Supervisor Pool / External Supervisor Pool 多实例并行**
2. **合议层处理 dedup / cluster / 优先级排序** — 跨实例并发产候选时不爆量
3. **资源不爆** — token / experiment / dedup_key cooldown 工程化限流
4. **探索惩罚** — 失败 signature 短期内不重复 + 相似策略合并

L4 §交付标志 4 条全部达成（service-layer 资源治理基础设施完备）。

---

## Approach

**Pool 抽象统一**：
- 3 个 Pool (Strategist Explorer / Supervisor / External Supervisor) 各自独立 Config + lazy-init instance + 依赖注入
- **Pool config 默认 3 模式/维度/mode**, 与 ADR 一致
- 所有 Pool 都允许 caller 覆盖 config，env var 容错优先

**单 commit ≤ 5 文件严守**：
- L4.1/L4.4/L4.5/L4.6 各拆 2 sub-commit (code + docs)
- L4.2/L4.3 单 commit (5 文件刚好)
- 总 11 commits 全部 < 5 文件

**dependency injection 一致**：
- `notification_sender` / `capability_writer` / `capability_history_reader` / `resource_quota` / `exploration_penalty` 都用 `Callable[..., Awaitable[None]]` 或 service object 注入
- service 单测零外部依赖，prod 接真 DB writer

**engineering-first across L4**：
- Jaccard 相似度替代 embedding (L4.4)
- 工程化 token + experiment budget 替代 LLM cost predictor (L4.5)
- 工程化 signature 字符串替代 embedding similarity (L4.6)
- 全 L4 阶段 0 个 LLM 直接调用 — 治理层应该比执行层更确定

---

## Key Decisions

1. **生成器与过滤层分离 (L4.1)**: 候选生成器输出全 3 模式，filter 是 Service 端的资源/策略关卡。让 L4+ 加新 mode 时只改 generator + config 不改主路径
2. **`backward` 不受 Pool 控制 (L4.1)**: forward / backward 是两条独立路径; Explorer Pool config 只过滤 forward modes
3. **Pool fan-out 而非分配 (L4.2)**: `task.done` 被 latency_audit + safety_audit 同时消费，单事件被多 dim 看不互相争抢
4. **dedup_key 通过 instance 隔离自然实现 (L4.2)**: 不需在 key 字符串里加 dim 前缀；两 dim 各持 SupervisorService instance，state 字典是独立 dict
5. **`return_exceptions=True` + log 异常 (L4.2)**: 单 dim 失败不让整个 Pool observe 挂掉；监督是旁路，不能把自己挂掉
6. **每 mode 独立 budget (L4.3)**: gate_review (低温) / debrief (允许发散) / self_aggrandizement (高频简短) — 单一 SupervisorService 用同一 semaphore 会让 3 mode 互相争抢
7. **`provider_factory` 而非每 mode 必传 provider (L4.3)**: 默认 caller 只需传一个 shared `llm_provider`，需要细分时再传 factory，降低 90% 用例的配置负担
8. **不用 embedding 做 dedup (L4.4)**: embedding 引入依赖 (sentence-transformers / OpenAI)，L4 阶段为 engineering-first 不上 LLM。Jaccard on token sets 工程化覆盖大多数候选对比场景
9. **threshold=0.7 vs ADR 写的 0.85 (L4.4)**: embedding cosine 0.85 vs token Jaccard 0.7 — **换 metric 必须换 threshold，不能照搬数字**
10. **双预算 token+experiment 而非单一 (L4.5)**: 两者解耦; 高 token 任务可能只 1 experiment 但消耗大，高 experiment 任务可能 token 少但实验密
11. **预扣 estimated_tokens (L4.5)**: 不预扣会让 burst 短时间内多个 experiment 通过 check 然后集体烧 token; reservation pattern 防 race
12. **`record_success` 清整个 signature 失败历史 (L4.6)**: 一次成功 = 该 signature 又可探索; 递减让"3 次失败再 1 次成功"还有 2 次 buffer，不符合"成功 reset" 直觉
13. **filter 在 quota 之前 (L4.6)**: 失败 signature 先剔，剩下的才占 quota budget。反过来让"3 次失败 candidate"还占 budget 直到被 penalty 拒
14. **只取 scalar 字段算 signature (L4.6)**: 嵌套 dict/list 序列化不稳定 (顺序 / 缩进)，让 signature 不 stable

---

## Constraints Applied

- 单 commit ≤ 1000 行 / ≤ 5 文件改动（L4.1/4.4/4.5/4.6 各拆 2 sub-commit）
- pytest + ruff 双绿才 commit
- 每改动 grep verify（L4.1 是否还有别处 hard-code 3 mode / L4.4 现有 dedup 代码 / L4.6 signature 函数是否已存在）
- destructive 操作（无）
- 每 commit 后追加 progress.md 微日志
- ADR-025 蒸馏卡片单独文件（本提交 +3 新 seeds）

---

## Patterns Used

| Pattern | Where | Note |
|------|------|------|
| Pool config 默认 3 + lazy-init instance | 全 3 个 Pool | 缩到 1 也扩到 N 都行 |
| Filter layer ≠ Generator layer | L4.1 explorer_pool / L4.6 penalty | 关注点分离 |
| Fan-out 不分配 | L4.2 task.done | 单事件多 dim 共享 |
| `return_exceptions=True` | L4.2 supervisor pool | 旁路系统不打挂 |
| Frozen dataclass IO contract | 全 L4 输出 | 同 L2/L3 经验 |
| Dependency injection (writer/sender/quota/penalty) | 全 L4 集成 | 单测零外部依赖 |
| 4 维 sort key tuple | L4.4 rank_candidates | 稳定排序 + 多维优先级 |
| Engineering signature string | L4.6 candidate_signature_key | 与 Jaccard set 互补 |
| Per-tenant state + asyncio.Lock | L4.5 / L4.6 | 高 tenant 不影响低 tenant |
| Sliding-window purge on check | L4.5 / L4.6 | deque + popleft |
| Sub-commit 拆分 | L4.1/4.4/4.5/4.6 | 5 文件硬约束驱动 |

---

## What Failed

1. **L4.2 self-referential 集成测试 target_module 落点错估**: `_check_failure_spike` 把 target 拼成 `executor.{task_type}` 而非 task_type 本身，所以 task_type=supervisor.audit 时 target=executor.supervisor.audit 不命中前缀。教训：grep target_module 构造逻辑，不假设
2. **L4.2 repeat_count 测试逻辑错算**: 多事件循环 + dedup_ttl=0 时每事件都触发，repeat_count 早超过 2。重写期望让测试反映真实计数语义
3. **L4.2 isolation test 不需 multi-event**: 单 task.done 已能让两 dim 各自独立触发（score 0.7 + ratio 10），后续多事件 dedup 干扰测试。简化为单事件 + 比对 state instance identity
4. **L4.4 ruff TYPE_CHECKING quote**: 用了 `"StrategyExperiment"` 在 TYPE_CHECKING block 后还加 quote — 不需要 quote 了。autofix 处理
5. **L4.5 ruff Imports**: 测试文件 import 顺序错；ruff autofix 修
6. **L4.6 ruff Quote + TYPE_CHECKING**: 同 L4.4 — `"StrategyExperiment"` 字符串引用过多。统一删

---

## What Worked

1. **Pool 3 模式统一架构**: Strategist Explorer Pool / Supervisor Pool / External Supervisor Pool 都用 `default_config + lazy-init + dependency injection` 模式 — **写第二个 Pool 是 copy-paste，第三个 Pool 是肌肉记忆**
2. **依赖注入接口一致**: notification_sender / capability_writer / resource_quota / exploration_penalty 都用 Callable 或 service object 注入 — **生产换 fake 测试零修改**
3. **engineering signature 双形态 (set + string) 互补**: L4.4 deliberation 用 set (Jaccard 模糊匹配同批合并)，L4.6 penalty 用 string (精确 lookup 跨时间避免) — 两层各司其职
4. **`record_failure` 显式而非自动**: caller 显式记 (Tester / Gate 在 rollback 触发后调) — ExplorationPenalty 不耦合具体业务流
5. **滑动窗口自动 purge**: deque + popleft 在 check 路径上 incremental purge — 不需要 background sweep
6. **整 L4 阶段 0 LLM 调用**: 治理层完全工程化，让 RSI 闭环成本可控、决定可测、热路径不卡

---

## Heuristics Extracted

1. Pool 设计三件套: default config + lazy-init instance + dependency injection
2. 过滤层应在生成层后, 资源限制层应在 emit 前
3. Fan-out 不分配 — 多消费者共享一事件; 单消费者争抢是反模式
4. 多实例 audit: per-dim 独立 instance 让 dedup key namespace 自然不冲突
5. `provider_factory` 而非每 mode 必传 — 默认 shared, 需要细分时显式注入
6. 不用 embedding 做工程层 dedup — Jaccard on token sets 已足够; embedding 留 L5+
7. 换 metric 必换 threshold — 不能照搬 0.85 到不同算法
8. 双预算 (token + experiment) 解耦 LLM cost vs 探索频率
9. 预扣 estimated_tokens 防 burst race
10. signature string for 精确 lookup, signature set for 模糊匹配 — 双形态互补
11. `record_success` 清整个 signature 而非递减 — "成功 reset" 直觉一致
12. 滑动窗口用 deque + popleft + check-time purge — 不需要 background sweep
13. asyncio.Lock 全局而非 per-tenant — 锁内仅微秒, 全局锁不卡
14. Pool / 治理模块 0 LLM 调用 — 治理层比执行层更确定

---

## Methodology Card Candidates

以下蒸馏自本阶段（与本 retrospective 同时归档 ≥ 3 份新 seeds）：

1. ✅ **pool_with_lazy_init_and_dependency_injection** — Pool 设计三件套通用模式（新建 seed）
2. ✅ **sliding_window_with_deque_purge** — 时间窗口工程化实现（新建 seed）
3. ✅ **signature_string_vs_signature_set** — 精确 lookup 与模糊匹配双形态（新建 seed）
4. **engineering_layer_zero_llm**（candidate, 与 engineering_first_with_llm_fallback 部分重叠）
5. **fan_out_not_assign**（candidate）
6. **reservation_pattern_prevent_race**（candidate）

---

## L4 验收清单

- [x] **L4.1** Strategist Explorer Pool 配置化（commits b6c560c + 65180f2 · +15 tests）
- [x] **L4.2** Supervisor Pool 多实例（commit 173d3c1 · +13 tests）
- [x] **L4.3** External Supervisor Pool 配置化（commit a472331 · +11 tests）
- [x] **L4.4** 合议层 dedup + cluster + rank（commits 64cbe60 + 751f8c2 · +23 tests）
- [x] **L4.5** Resource quota（commits 8431427 + 7d171dc · +13 tests）
- [x] **L4.6** Exploration penalty（commits fcf1241 + 595b050 · +17 tests）
- [x] **L4.7** L4 验收 + retrospective + 3 新 methodology seeds（本提交）

**测试总数**：1003 → 1095（+92）
**Ruff**：clean throughout
**Schema**：未动（Pool / Quota / Penalty 全 in-memory state, prod Redis 替换）
**LLM 调用**：L4 阶段 0 次 — 全工程化治理
**北极星**：L4 §交付标志 4 条全部达成。Pool 多实例可并行，合议层去重排序，资源 quota 防爆，探索惩罚防 dead-end。**端到端 RSI 闭环演练**（Supervisor Pool → Strategist + Explorer Pool → deliberate → quota → penalty → emit → Gate → 启用）需要把这些 service 接到真 NATS / 真 DB writer，留给 L5 启动时做。

---

*最后更新：2026-05-27*
