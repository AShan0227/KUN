# L3 · 进度微日志（追加式）

> 完整回顾在 `L3-retrospective.md`（L3 全 7 项完成时写）。
> 本文件每完成一个子任务追加 3-5 句。

---

## L3.1 · 第 2 条 RSI 实例：context 压缩策略

**完成**：2026-05-27 / commit pending

**做了什么**：
- `SupervisorService` 增 5 号异常类型 `context_oversized_spike`：监听 `llm.invoke.completed` 事件，窗口内 `input_tokens >= context_oversized_input_tokens (默认 80k)` 的事件数累计 `>= context_oversized_threshold (默认 3)` 触发
- `SupervisorAnomalyState` 加两个 per-tenant 阈值字段，允许单独调整（高 token 任务的 tenant 可放宽）
- `_check_context_oversized` 沿用 `_build_request` 模板，dedup_key `tenant:llm.context:context_oversized_spike` — 1h 不重复
- `StrategistService` 增 `_candidates_for_context_oversized_spike` 生成器，挂入 `_CANDIDATE_GENERATORS` 字典，Explorer Pool 3 模式：
  - **Conservative**: `context_summary_compression`（older_than_turns=10, summary=500 tokens, canary 30%，acceptance = threshold * 0.6）
  - **Aggressive**: `context_hard_truncation`（max_turns=20, preserve_top_pins=True 防丢 anchor pinning, canary 20%）
  - **Performance**: `context_rag_retrieval`（top_k=8, fallback_to_truncation=True, shadow 100%）
- acceptance_threshold 从 evidence 的 `threshold_tokens` 字段动态算（threshold × 0.6/0.4/0.3 按 mode），evidence 缺时 fallback 80k 默认

**关键决策**：
- **context 改动属 L2 (module) 而不是 L1 (activation)**：context 压缩动 prompt assembler 模块的核心实现，比 LLM 路由 (L1) 风险更高。target_level=2 让 RCDH 诊断 + Gate 准入门禁知道这条
- **Aggressive 必带 `preserve_top_pins=True`**：硬截断不能丢掉 anchor pinning（ADR-022 Layer 2）。这是 anchor_pinning_at_prompt_top methodology 的硬约束 — 写进 change_spec 让 Executor 实装时不忘
- **Performance shadow 100%**：RAG 检索改动巨大，先 shadow 全量观察检索相关度，再决定上不上 canary。避免 prod 体验骤变
- **dedup_key 用 `llm.context` 而非 `llm.router`**：context 压缩属 context 子系统，与 LLM 路由分离，让两条 RSI 实例独立触发独立晋级
- **threshold-based acceptance 而非固定常数**：evidence 报 50k 阈值时 acceptance 应该是 30k (50k*0.6) 而不是 48k (80k*0.6 默认)。让 acceptance 跟着真实阈值缩放

**10 个新单测**覆盖：低于阈值不触发 / 单次超阈不触发 / 窗口内 3 次触发 / dedup TTL / 自定义阈值 / 非 llm.invoke 不计入 / 3 mode 候选齐全 + Aggressive 保 pinning / acceptance 动态 / 默认 fallback / 每候选必带 rollback_on。938/938 unit tests pass，ruff clean。

**为下一步**：L3.2 第 3 条 RSI 实例 —— skill 选择启发式（task_type → skill_id 路由也走 RSI 闭环）。

---

## L3.2 · 第 3 条 RSI 实例：skill 选择启发式

**完成**：2026-05-27 / commit pending

**做了什么**：
- `SupervisorService` 增 6 号异常类型 `skill_mismatch_spike`：监听 `skill.invocation.completed` 事件，同 `(task_type, skill_id)` 对在窗口内失败率 `>= skill_mismatch_rate (默认 0.5)` 且样本数 `>= skill_mismatch_min_samples (默认 4)` 触发
- `_check_skill_mismatch` 用 `conservative_sample_threshold` 思路 — 样本不足静默（cold-start damping 同源原则）
- 失败率 ≥ 0.8 时 priority 自动升 `high`（强信号）
- target_module 落 `skill.{skill_id}` 让 dedup 精确到具体 skill
- `StrategistService` 增 `_candidates_for_skill_mismatch_spike` 3 Explorer 候选：
  - **Conservative** `skill_id_swap`: target_level=1（路由层），换 next_best capability_card 推荐，canary 30%，acceptance = `1 - failure_rate * 0.5`（目标失败率减半）
  - **Aggressive** `task_type_split`: target_level=0（设计层！task_type 拆分是产品决策），shadow，requires_director_assistance=True，让 Director 协助拆分定义
  - **Performance** `capability_damping_tweak`: target_module 跳到 `capability_router`，把 cold-start damping 从 `n/30` 调到 `n/15`，让历史失败信号更快显现到路由

**关键决策**：
- **`min_samples=4` 默认起点**：与 `conservative_sample_threshold` 的 weak_signal (5-9 不调整) 不同 — 4 已经足够走 RSI 入口（毕竟 RSI 是探索，不是直接 enable_capability）。Gate 仍然会要求 ≥ 10 才真 promote
- **Aggressive `task_type_split` target_level=0 而不是 1**：拆分 task_type 是产品定义层改动，需要 Director 重新建模这条 task。换 skill 是配置改动（level 1），拆类型是设计改动（level 0）
- **Performance 跨模块改 capability_router 而不是 skill 本身**：根因可能在 router 的 cold-start damping 太保守，让坏信号要积 30+ 样本才反映。target_module 跨模块写得清楚，避免 Gate 误以为是 skill 内部 bug
- **acceptance 动态绑 failure_rate**：失败率 80% 时 acceptance=0.6（成功率 60% 即可），失败率 50% 时 acceptance=0.75。让 acceptance 反映"显著改善"而非"绝对达标"
- **`failure_rate >= 0.8` 自动 priority=high**：严重 mismatch 不能等 default medium 排队，Strategist 应优先处理这类

**9 个新单测**覆盖：min_samples 边界 / 低失败率不触发 / 高失败率触发 / priority 升 high / 不同 pair 独立累积 / 缺字段不触发 / Strategist 3 mode + level / acceptance 动态 / rollback 必备。947/947 unit tests pass，ruff clean。

**为下一步**：L3.3 Forward / Backward 双修复策略 —— Strategist 在 anomaly 发生时 auto-select 是该 forward fix（往前进一步改）还是 backward rollback（回退到上一个 known-good capability）。
