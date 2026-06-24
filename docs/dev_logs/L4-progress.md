# L4 · 进度微日志（追加式）

> 完整回顾在 `L4-retrospective.md`（L4 全 7 项完成时写）。
> 本文件每完成一个子任务追加 3-5 句。

---

## L4.1 · Strategist Explorer Pool 配置化

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/strategist/explorer_pool.py`：`ExplorerPoolConfig` (frozen dataclass) + `ExplorerMode` Literal + `load_explorer_pool_config()` 从 env var 读取
- 默认 3 模式 `(conservative, aggressive, performance)`；支持 5 个已知 mode（含 `backward` 与 `experimental` 预留）；非法 mode 名静默丢弃
- `StrategistService.__init__` 增 `explorer_pool_config: ExplorerPoolConfig | None` 参数，默认 `load_explorer_pool_config()` 读 `KUN_STRATEGIST_EXPLORER_MODES` env
- `propose_candidates` 在 forward 路径调 `_explorer_pool.filter_candidates(candidates)` —— 仅保留 enabled forward mode；**backward 候选不过滤**（走自己的 forward/backward 决策路径）
- 候选生成器仍输出全 3 模式，**过滤是 Service 责任**（生成器不感知 config）

**关键决策**：
- **过滤层 ≠ 生成层**：生成器输出"已知最佳 N 模式"，filter 是 Service 端的资源/策略关卡。让 L4+ 加新 mode 时只改 generator + config 不改主路径
- **`backward` 不在 forward modes 列表里**：Pool config 只控 forward。backward 走 `select_repair_direction` 独立决策；即使 enabled_forward_modes=`frozenset()`，backward 仍能触发
- **`experimental` 预留 mode 不在默认 enabled**：必须显式启用，避免未来引入新 mode 时静默改变行为
- **非法 mode 名静默丢弃 + 全非法时回默认**：env var 容错优先于严格 — 错配置应"degraded but functional"，不应让 Strategist 拒绝启动
- **frozenset 作 config value**：immutable + hashable，多 service 共享同一 config 无副作用
- **TYPE_CHECKING import**：避免 strategist/service.py 主路径 import explorer_pool（防循环依赖）；运行时才在 `__init__` 内 lazy import

**15 个新单测**覆盖：默认/自定义/空 pool / filter backward 保留 / env 解析 4 case / load_explorer_pool_config 4 case / 集成 3 + 4 cases。1018/1018 unit tests pass，ruff clean。

**为下一步**：L4.2 Supervisor Pool 多实例 —— 不同 audit 维度 (latency / cost / safety) 各自独立 SupervisorService 实例，事件分流到对应维度。

---

## L4.2 · Supervisor Pool 多实例（按 audit 维度分流）

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/supervisor/pool.py`：`SupervisorPool` + `SupervisorPoolConfig`
- 默认 3 audit 维度 + 各自订阅的 event_type:
  - `latency_audit` ← `task.done` (duration_outlier)
  - `cost_audit` ← `llm.fallback.triggered`, `llm.invoke.completed` (context_oversized)
  - `safety_audit` ← `task.failed`, `skill.invocation.completed`, `task.done` (task_anomaly_score)
- 同事件可被多 dim 消费 (e.g. `task.done` 同时进 latency + safety) — **fan-out 不丢信号**
- `instance_for(dim)` lazy-init 每 dim 独立 `SupervisorService` instance, dedup_key 命名空间天然隔离（不同 instance 各有自己的 `state.recent_search_requests`）
- `observe(event_type, payload)` 路由 fan-out + 并行 `asyncio.gather`，单 dim 异常 `return_exceptions=True` 吞掉不打挂其他 dim
- 共用的 `emitter` / `notification_sender` 注入到所有 instance — 一处配齐多 dim 都用

**关键决策**：
- **fan-out 而非分配**：`task.done` 被两 dim 同时消费（latency 检查 duration_outlier，safety 检查 task_anomaly_score）。**单事件被多 dim 看，不互相争抢**。如果改成"先到先得"会让 audit 维度互相覆盖
- **dedup_key 命名空间通过 instance 隔离自然实现**：不需要在 key 字符串里加 dim 前缀。两 dim 各持 `SupervisorService` instance，state 字典是独立 dict，dedup 自动不冲突
- **`return_exceptions=True` + log 异常**：单 dim 异常不让整个 Pool observe 挂掉。Supervisor 是旁路监督，不能把自己挂掉影响主线
- **lazy-init instance**：Pool 启动时不预创建所有 dim — 第一次该 dim 收到事件才 init。省内存，适合 dim 配置可能变化的场景
- **未知 event_type 不创建 instance**：`dimensions_for_event` 返回空时 observe 直接 `return {}`，不污染 Pool state
- **Pool 不持锁等 emitter / LLM 调用**：`async with self._lock` 仅包裹 `instance_for` 创建步骤，`asyncio.gather` 在锁外跑，避免单 dim 卡住其他 dim

**13 个新单测**覆盖：默认 3 dim / fan-out task.done / 唯一 cost_audit / 未知 event 空 / 自定义 config / lazy init / 不匹配 event 空返回 / 路由单 dim / 两 dim 并行 fan-out / state isolation / all_dimensions 列表 / 共享 emitter / 单 dim 异常吞。1031/1031 unit tests pass，ruff clean。

**为下一步**：L4.3 External Supervisor Pool 配置化 —— 按 audit mode 分实例（gate_review / task_debrief / self_aggrandizement 各自独立 ExternalSupervisorService 实例 + 本地模型 concurrency budget）。

---

## L4.3 · External Supervisor Pool 配置化（按 audit mode 分实例）

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/external_supervisor/pool.py`：`ExternalSupervisorPool` + `ExternalSupervisorPoolConfig` + `ExternalSupervisorPoolEntry`
- 3 个默认 mode + 各自 budget:
  - `gate_review`: max_concurrent=2, temperature=0.1, max_tokens=512 — 严肃决策低温
  - `task_debrief`: max_concurrent=1, temperature=0.3, max_tokens=1024 — 允许发散输出更长
  - `self_aggrandizement`: max_concurrent=3, temperature=0.1, max_tokens=256 — 高频简短
- `instance_for(mode)` lazy-init；未知 mode 用默认 entry（容错优先，与 Supervisor Pool 同思路）
- `dispatch(mode, ...)` 路由到对应 instance + 调 `analyze_observation`
- `provider_factory` 可选注入 — 每 mode 独立 LLM provider（e.g. gate_review 用快模型，debrief 用深推理）；factory 异常 fallback 到 shared provider
- 共享或独立两种 provider 注入方式同源支持

**关键决策**：
- **每 mode 独立 budget 是核心**：gate_review 等级紧迫（Gate 决策不能等），debrief 可慢可串行，self_aggrandizement 高频高并发。单一 SupervisorService 用同一 semaphore 会让 3 个 mode 互相争抢
- **`temperature` per mode**：gate decisions deserve low temperature 一致性；debrief 允许更宽 verdict 反映"真复盘"；self_aggrandizement 是判定题不是 essay → 低温短输出
- **provider_factory 而非每 mode 必传 provider**：默认 caller 只需传一个 `llm_provider`（共享），需要细分时再传 factory。**降低 90% 用例的配置负担**
- **factory 失败 fallback 到 shared 而非 raise**：本地模型容易"今天慢明天好"，pool 不应该让单个 mode 的 factory 失败阻止其他 mode 工作
- **未知 mode 用默认 entry**：与 Supervisor Pool 同思路，容错优先；让 L4+ 加新 mode 不需先改 config
- **不重写 Mode A/B 的 wrapper**：L2.5 `mode_a_gate_review` / `mode_b_task_debrief` / `check_self_aggrandizement` 已经接受 `ExternalSupervisorService`，**调用方可以传 `pool.instance_for(mode)` 替换原单实例**。Pool 是 service 的横向扩展，不破坏现有 API

**11 个新单测**覆盖：默认 3 mode + budget / has_mode + all_modes / 自定义 entries / lazy-init + 跨 mode 不同 instance / 未知 mode 默认 entry / max_concurrent 匹配 entry / dispatch 路由 / provider_factory 注入 / factory 失败 fallback / all_modes 列表。1042/1042 unit tests pass，ruff clean。

**为下一步**：L4.4 合议层 dedup（Jaccard 相似度 ≥ 0.85 合并）+ cluster + 优先级排序 —— Pool 跨实例并发产候选时去重 + 聚类。

---

## L4.4 · 合议层 dedup + cluster + rank

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/agents/strategist/deliberation.py`：
  - `_candidate_signature` 计算 signature set（target / level / mode / rollout / kind / spec_keys）
  - `jaccard_similarity(a, b)` pure Jaccard
  - `deduplicate(candidates, threshold)` 两两比对 Jaccard ≥ threshold → 合并；保留 `_candidate_score` 更高的（acceptance + explorer_mode bonus）
  - `cluster_by_kind` 按 `(target_module, change_kind)` 聚类 → `CandidateCluster` frozen dataclass
  - `rank_candidates` 4 维 sort key: `explorer_mode rank (backward=0 < conservative=1 < performance=2 < aggressive=3)` → `requires_human_review` → `sampling_rate` 低优先 → `acceptance_threshold` 严格优先
  - `deliberate(candidates)` 主入口: dedup → rank（cluster 留给上层 UI/Gate 展示用）
- `StrategistService.propose_candidates` forward 路径在 `filter_candidates` 之后调 `deliberate(candidates)`，让输出已 dedup + ranked
- 默认 `DEDUP_JACCARD_THRESHOLD = 0.7`（无 embedding 时比 0.85 经验值更宽 — 工程化 token 集合相似度比 embedding 精度低，需要松一档）
- `__init__.py` export 7 个新名字

**关键决策**：
- **不用 embedding model 做 dedup**：embedding 引入依赖（sentence-transformers / OpenAI embedding API），L4 阶段为"engineering-first" 不上 LLM。Jaccard on token sets 工程化覆盖大多数候选对比场景 — embedding 留 L5+ 闭环再上
- **`threshold=0.7` 比 ADR §合议 写的 0.85 宽**：embedding cosine 0.85 是高相似（语义匹配）；token Jaccard 0.7 已经是"几乎同一 candidate"。换 metric 必须换 threshold，不能照搬数字
- **合并时保留 acceptance 严 + conservative**：保守目标 + 保守 mode 在风险敏感场景更优。"保留 aggressive" 等于让 Gate 接到风险更高候选 — 不是这层该做的判断
- **`rank_candidates` 4 维 tuple key**：单维（如只 mode）信息不够；4 维让排序在"同 mode 内"也有稳定顺序。注意 acceptance 取负让"严格目标" 排前
- **`deliberate` 不调 cluster**：cluster 是展示用，不影响实际候选列表。让 Gate 接到的是已 dedup+ranked 平铺 list，不是 nested cluster
- **lazy import in service.py**：`from kun.agents.strategist.deliberation import deliberate` 在 `propose_candidates` 内部 — 避免 service.py top-level import deliberation.py 引起的"deliberation 间接 import StrategyExperiment 循环依赖"（实际通过 TYPE_CHECKING 已解决，但 lazy import 是更稳妥的做法）

**23 个新单测**覆盖：Jaccard 4 case（identical / disjoint / partial / both-empty / one-empty）/ signature 2 case / deduplicate 5 case（empty / single / distinct / merge / threshold / mode-preference） / cluster 2 case / rank 4 case（backward first / conservative before aggressive / auto before human / lower sampling）/ deliberate 2 case / 集成 case 验证 StrategistService.propose 输出已 ranked。1065/1065 unit tests pass，ruff clean。

**为下一步**：L4.5 Resource quota — token budget / 时间窗 / dedup_key cooldown 工程化限流，防 RSI 闭环资源爆炸。

---

## L4.5 · Resource quota (token / experiment / cooldown)

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/governance/resource_quota.py`：`ResourceQuota` + `QuotaState` + `QuotaCheckResult` frozen dataclass
- 双预算 per-tenant + 滑动窗口（默认 1 小时）：
  - **token budget** 默认 1M tokens/hour, 防 LLM 调用爆炸
  - **experiment budget** 默认 30 experiments/hour, 防 Pool 多实例 + Explorer 候选无限增长
- `check_and_record_experiment(tenant_id, experiment_id, estimated_tokens=0)` 单步 check + 预扣：
  - experiment_budget 命中 → 拒绝 + 详细 reason
  - token_budget 命中 → 拒绝 + 详细 reason
  - 通过 → 记录新 experiment + 预扣 estimated_tokens
- `record_token_usage(tenant_id, tokens)` LLM call 完成后显式累记真实 token 数
- `snapshot(tenant_id)` 给 monitoring 用，返回 token_usage / experiment_count / budget
- 滑动窗口在 check 时自动 purge 旧记录（`deque + popleft`）
- `asyncio.Lock` 保护并发；per-tenant state isolation
- StrategistService.__init__ 增 `resource_quota: ResourceQuota | None` 注入；`_emit_and_adjust` 在 emit 前调 quota check，被拒的 candidate 不写出 emitter
- governance `__init__.py` export `ResourceQuota` + `QuotaCheckResult`

**关键决策**：
- **双预算 (token + experiment) 而非单一指标**：token 限制 LLM cost，experiment 限制 Strategist 探索频率。两者解耦：高 token 任务 (e.g. 深推理) 可能只 1 experiment 但消耗大；高 experiment 任务 (e.g. 多模式 Pool fan-out) 可能 token 少但实验密。**单一指标会让一种场景误伤**
- **per-tenant 独立 state**：复用同 service instance 但 state 字典 keyed by tenant_id — 高 tenant 不影响低 tenant
- **预扣 estimated_tokens 而非仅事后记**：不预扣会让 burst 短时间内多个 experiment 通过 check 然后集体烧 token；预扣即 reservation pattern
- **`record_token_usage` 单独 API**：LLM call 完成后才知道真实 token 数，与 reservation 分开记。caller 可不调（理论上预扣足够）但调了让窗口数据更准
- **`asyncio.Lock` 全局而非 per-tenant**：state dict 写入需 lock；per-tenant lock 更复杂收益不明显。瓶颈在 quota check 几微秒，全局锁不卡
- **`tenant_id="default"` 在 Strategist 调用**：当前 candidate 不带 tenant 字段，用默认占位。L5+ caller 传真 tenant 时需透传到 `_emit_and_adjust`
- **Quota check 在 emit 前 / dedup 后**：candidate 已经被 deliberate 排好后再 quota；保留高优先级（backward / conservative）candidate 更可能通过。如果在 dedup 前 quota，noise candidate 占用 budget
- **被拒 candidate 不写出**：用户/调用方收到的 list 已是 quota-passed 子集；filter 在 service 内部完成，不让 caller 区分"被生成但拒"和"未生成"
- **`window_seconds=0` 抛 ValueError**：0 窗口让"立即拒绝所有"，没意义；用 `experiment_budget_per_window=0` 表达"拒绝所有"语义更清楚

**13 个新单测**覆盖：defaults / 非法参数 / 首次允许 / experiment exceeded / token exceeded / record post call / tenant isolation / negative token ignored / window purge / result fields / 集成 0-budget / 集成 partial-budget / 集成无 quota unchanged。1078/1078 unit tests pass，ruff clean。

**为下一步**：L4.6 探索惩罚 — 失败候选 3 次内不重复 (failure history) + similar 策略合并 (复用 L4.4 Jaccard)。

---

## L4.6 · Exploration Penalty (探索惩罚)

**完成**：2026-05-27 / commit pending

**做了什么**：
- 新建 `kun/governance/exploration_penalty.py`：`ExplorationPenalty` + `PenaltyCheckResult` + `PenaltyState` + `candidate_signature_key` helper
- 默认 `max_retries_per_window=3` + `window_seconds=86400` (24h) — 同 ADR-024 §探索惩罚
- per-tenant + per-signature deque 累计失败时间戳；purge 自动清窗外旧记录
- `candidate_signature_key(c)` 返回 stable string (target + level + kind + mode + rollout + top-5 change_spec keys) —— 与 deliberation 的 Jaccard set 互补
- 4 个核心 API：
  - `check(tenant, candidate)` → PenaltyCheckResult
  - `record_failure(tenant, candidate, reason)` 累记
  - `record_success(tenant, candidate)` 清失败计数 (signature 整条删)
  - `filter_candidates(tenant, candidates)` 批量过滤掉 blocked 的
- StrategistService `_emit_and_adjust` 在 quota check 之前调 `exploration_penalty.filter_candidates` —— 失败 signature 不进 quota 不烧 budget
- governance `__init__.py` export

**关键决策**：
- **signature 用 stable string 而非 Jaccard set**：deliberation 的 set 是给"模糊匹配 + 计算相似度"；这里需要精确 lookup (是不是同一个曾失败过的 candidate)。两者互补不冲突
- **`record_success` 清整个 signature 失败历史而非递减**：一次成功 = 该 signature 又可探索；递减让"3 次失败再 1 次成功"还有 2 次 buffer，不符合"成功 reset" 直觉
- **top-5 change_spec keys**：避免 signature 过长 (e.g. RAG candidate 的 change_spec 可能有 10+ 字段)；前 5 个 sorted 已足够区分主要差异
- **不算 change_spec 的 dict/list 字段**：嵌套结构序列化不稳定 (顺序 / 缩进)，让 signature 不 stable。**只取 scalar 字段保证 cross-process 一致**
- **filter 在 quota 之前**：失败 signature 先剔，剩下的才占 quota budget。反过来会让"3 次失败 candidate"还占 budget 直到被 penalty 拒
- **`record_failure` 不调 `check`**：caller 显式记，让 Tester / Gate 在拿到 rollback 触发后调一次。让 ExplorationPenalty 不耦合具体业务流
- **window 24h 默认**：足够覆盖一个 promotion cycle (replay → shadow → canary)；24h 后允许"也许之前 bug 已修" retry
- **per-tenant isolation**：与 ResourceQuota 同思路 — 高 tenant 失败不影响其他

**17 个新单测**覆盖：signature key 4 case (stable / kind / target / no dict dump) / defaults + invalid args / check fresh / blocks after max / record_success clears / different signatures isolated / tenant isolated / window purge resets / filter drops blocked / snapshot lists tracked / Strategist 集成 3 case (filter / no penalty unchanged / success re-allows)。1095/1095 unit tests pass，ruff clean。

**为下一步**：L4.7 L4 验收 + retrospective + ≥3 新 methodology seeds —— 关闭 L4 阶段, 蒸馏多实例治理经验.
