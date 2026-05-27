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
