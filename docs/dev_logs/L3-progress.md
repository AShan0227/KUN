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
