# LT · 长复杂任务能力 补齐 (追加式)

> 用户指令: "完善鲲做长复杂能力" — 一次性补齐开发部分.
> 7 个子任务: LT.A → LT.G.
>
> 完整回顾在 `LT-retrospective.md`(LT 全部完成时写, ADR-025 强制).

---

## LT.A · Layer 3 wiring — LongTaskInputRouter

**完成**：2026-05-27 / commit (latest)

**做了什么**：
- 新建 `kun/agents/director/long_task_router.py` (LongTaskInputRouter)
- 把 `classify_input` 的 6 类分类映射到 6 个 routing bucket: executor / off_topic_reply / scope_expansion_review / pivot_pause / cancel / passthrough
- 4 个可注入 callback (off_topic_replier / scope_expansion_emitter / pivot_handler / cancel_handler) — caller 决定怎么真正消费
- 短任务 / 无 anchor → passthrough (Layer 3 不干预)
- 13 单测全过覆盖 6 类路由 + 异常路径 + 优先级 (interrupt > pivot)

**关键决策**：
- **不直接在 orchestrator.run 里写 if/elif**: 路由是 cross-cutting concern (WS/REST/event 都用), 抽 service 易测.
- **callback 全可选 + raise 不打挂主路径**: ADR-024 frozen_dataclass 模式. 即使 off_topic_replier 挂了, RoutingDecision 仍正确返回, caller 可自己处理.
- **礼貌回复 + 二次确认**: off_topic_noise 自动生成"我先记下…当前任务完成后再处理", explicit_pivot 自动生成"是确认切换吗? (yes/no)" — 模板化, 单测可验证内容.
- **bucket 名 vs category 名分开**: classification.category 是分类器结果, bucket 是路由动作 — 解耦让未来加 LLM 分类时分类器换实现但 bucket 不动.

**未完工作 (caller 集成)**：
- WS / REST entry point 集成 LongTaskInputRouter (~3 行代码, 不在 LT.A 范围)
- off_topic_replier 实装 (走 NUO alert / WS event)
- pivot_handler / cancel_handler 接 task lifecycle

---

## LT.B · Layer 4 wiring — PlanReviewService

**完成**：2026-05-27 / commit (latest)

**做了什么**：
- 新建 `kun/agents/supervisor/plan_review_prompt.py` (render_plan_review_prompt)
- 新建 `kun/agents/supervisor/plan_review_service.py` (PlanReviewService 高层 wiring)
- prompt 拼 `current_step / on_anchor / scope_creep_detected / criteria_done_count / recent_step_summary / drift_risk` 7 字段, JSON 输出契约
- internal + external verdict 取严合并 (`max severity wins`); External Supervisor (`ok/concerning/alarming`) 映射到内部 (`aligned/drifting/off_track`)
- External Supervisor 不可用 / raise / 未知 verdict → 仅用 internal 不阻塞主路径
- 17 单测全过, 1377 总测试 (+17)

**关键决策**：
- **prompt rendering 独立模块**: 比塞进 service 里好测; 未来 LLM 兜底分类时 prompt 风格演化不影响 service.
- **verdict 取严而非"两票同意"**: 安全敏感场景宁可误报 pause 也不能漏报 drift. external alarming 即使 internal aligned 也 force off_track.
- **External Supervisor 调用 raise 被吞**: 走 internal 兜底. 不让 ollama 挂掉拖垮整个主线 (与 LT.A safe_call 模式一致).
- **observe vs submit 分两个方法**: observe 触发 prompt 注入 (Executor 还没自评), submit 处理 Executor 给的 self_report. 解耦让 Executor 实现简单 (先注入 prompt 让 LLM 答, 答完再 submit).
- **prompt extras 不带任务 ID / anchor ID**: 防 prompt injection 攻击面 (LLM 看到 ID 可能瞎编). 只给 total_steps / total_reviews 数字.

**未完工作**：
- 接到真实 Executor loop (要 LT.E 完成 multi-step loop 才能集成)
- ExternalSupervisorVerify 接到 ExternalSupervisorService.analyze_observation (~5 行 adapter)
- PlanReviewOutcome 落 `plan_reviews` DB 表 (alembic 0011 已经有表, 只缺 writer)

---

## LT.C · Checkpoint / resume — TaskCheckpoint 持久化

**完成**：2026-05-27 / commits dd1d712 + (latest)

**做了什么**：
- 新数据脊柱表 `task_checkpoints` (alembic 0012)
  - pk (tenant_id, checkpoint_id), RLS tenant_isolation, FORCE ROW LEVEL SECURITY
  - sequence (单调递增 per-task) + step_idx + conversation_snapshot + working_state + artifact_refs
  - goal_anchor_id + last_self_report (resume 时校验 + 回退)
  - cost_usd_so_far + tokens_used_so_far (per-task budget tracking)
  - status: active / final / failed_resume (CHECK constraint)
  - 2 索引: (task_id, sequence) for resume, (status) for dashboard
- 新 EntityKind `task_checkpoint` (prefix `tcp-`)
- TaskCheckpointRow (kun/core/orm.py)
- TaskCheckpointService (kun/agents/executor/checkpoint.py) + 16 测试
  - save / resume / finalize 三个方法
  - resume: anchor 不匹配自动标 failed_resume 防 stale resume
  - 全 DI callback (writer / reader / status_marker), 单测 FakeStore in-memory

**关键决策**：
- **sequence 单调递增而不是依赖时间戳**: 防时钟跳变 / 跨进程 / 跨副本. resume 取 sequence 最大值的 active row.
- **anchor mismatch 自动 failed_resume**: 任务跑到一半重新拆 anchor (产品方向变了), 旧 checkpoint resume 会拿到错 context — 主动 fail 比静默错误好.
- **writer raise propagates (状态机推进失败必须可见)**: 与 ADR-024 frozen_dataclass_agent_io_contract 一致. status_marker 失败仅 log (状态累积可重试).
- **finalize 要求 status_marker**: 若 caller 没注入直接 RuntimeError, 提前暴露配置问题, 不让 final status 静默丢.
- **conversation_snapshot 是 list[dict] (JSONB)**: 落 LLM messages list 直接 round-trip; mutable copy in to_row_payload 防 alias.

**未完工作 (整合)**:
- 真实 session_scope 接 writer/reader/status_marker (~10 行 adapter)
- Executor loop 每个 step 后调 save (LT.E)
- 启动时调 resume 决定从哪开始 (LT.E)

---

## LT.D · Long context compaction — ConversationCompactor

**完成**：2026-05-27 / commit (latest)

**做了什么**：
- 新建 `kun/agents/executor/compaction.py`
- `estimate_tokens(messages)` 启发式 (chars/4), 多模态 list content 兼容, 非 dict 容错
- `ConversationCompactor.maybe_compact(messages, anchor=None) → CompactionResult | None`
  - 阈值下 None (caller 直接 no-op 不付 LLM)
  - 阈值上: head (protect_first_n) + summary (1 条) + tail (keep_last_k)
  - 中间 < min_compactable 时不压缩 (1 → 1 无意义)
  - summarizer 全 DI: 默认规则版 (anchor recap + msgs preview), 真生产替换 LLM
- summary message 自动标 `_kun_compacted=True` + `_kun_compacted_count=N`, 便审计追溯
- 18 单测覆盖

**关键决策**：
- **chars/4 启发式 tokenizer**: 真 tokenizer 在 LT.E 时换 (provider 提供). 当前能看趋势就够触发判断.
- **summary 用 system role**: LLM 看到 system role 默认更信任. 中文/英文 prompt 风格都吃这套.
- **summary 显式标 `_kun_compacted` 元数据**: 不污染 role/content. 下次 audit / Replay 可识别 + 跳过 / 展开.
- **head + summary + tail 三段结构, 不嵌套**: 直接喂 LLM, 模型不需要理解"什么是 compaction". 多次压缩时 caller 自己决定要不要把上次 summary 也折进新 summary.
- **min_compactable=2 默认**: 防"中间只有 1 条 → 折叠成 1 条 summary, 净增开销". 实测 LLM tokenizer 估算误差时这是个有用 guard.
- **原 messages list 在 result 里完整保留**: audit / replay 不丢; 一旦 compaction 走完, caller 可以 archive 原始的去 DB 但当前 working list 用 compacted_messages.

**未完工作 (整合)**:
- 真 LLM tokenizer 替换 (LT.E 接 provider 时, e.g. tiktoken / anthropic.count_tokens)
- LLM-based summarizer 接 ExternalSupervisor 或主 LLMRouter 跑摘要
- Executor loop 集成 (每次 LLM call 前调 maybe_compact, 用 compacted_messages)

---

## LT.E · Multi-step execution loop — ExecutorLoop

**完成**：2026-05-27 / commit (latest)

**做了什么**：
- 新建 `kun/agents/executor/exec_loop.py`
- ToolCall / ToolResult / LLMStepResponse / LoopResult 全 frozen dataclass
- LoopStatus 7 个: `final / max_steps / budget_exceeded / wall_clock_exceeded / stuck / failed / user_cancelled`
- ExecutorLoop.run() 真 agent loop: 终止检查 → compact → plan_review prompt → LLM → tool_calls dispatch → checkpoint → repeat
- 整合 LT.B (plan_review) + LT.C (checkpoint) + LT.D (compactor), 全 DI 可选
- 18 单测全过, 1429 总测试 (+18)

**关键决策**：
- **永远不 raise, 用 LoopResult.status 表退出原因**: caller 不用 try/except 写循环外层. 7 种退出类型可观测.
- **LLMStepResponse provider-agnostic**: content + tool_calls + finish_reason + usage_tokens + cost_usd. Anthropic/OpenAI/Gemini 都能映射进来.
- **所有整合 service 失败不破坏 loop**: compactor / plan_review / checkpoint 任一 raise → log warning, 主路径继续. ADR-024 状态累积失败不阻塞主路径.
- **tool_executor 抛 → 全 ToolResult is_error=True**: 不让 1 个 tool 异常导致整个 loop 挂. 但连续 N 次 tool failures (默认 3) → status=stuck.
- **`steps_taken` 含 final step**: final answer 那次 LLM call 也算 1 step (调过 LLM, 付了 cost).
- **`final_messages` 是完整对话**: caller 复盘 / replay 用. checkpoint 落库的是 snapshot, 这里是内存视图.

**未完工作 (LT.G 整合)**:
- 接到 kun.engineering.orchestrator 或 control_plane 主路径 — Executor 调用 ExecutorLoop 跑长任务
- LLMRouter → LLMInvoker adapter (~10 行 wrapper, 把 LLMResponse 映射成 LLMStepResponse)
- ToolRegistry → ToolExecutor adapter (skill / tool registry 已存在, 接一层)

---
