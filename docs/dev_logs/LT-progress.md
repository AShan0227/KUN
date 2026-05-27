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

## LT.F · 递归 planning — RecursivePlanner + PlanTree

**完成**：2026-05-27 / commit (latest)

**做了什么**：
- 新建 `kun/agents/director/recursive_planner.py`
- PlanNode frozen dataclass (节点 id + depth + parent + children_ids + is_atomic + success_criterion + verification_hint + metadata)
- PlanTree frozen — by node_id 索引, walk/leaves/children_of/depth/node_count/total_estimated_cost/total_estimated_duration
- RecursivePlanner.expand(root_description, root_steps) 递归构造 tree
- 默认 atomic_decider 启发: success_criterion 已指定 / depth≥2 / desc≤12字符 / skill_hint=tool.x 任一命中
- 默认 sub_planner 把 node 拆"准备/执行/验证"3 步
- max_depth=3 + max_breadth_per_node=8 防爆炸
- sub_planner raise → 视为 atomic 兜底
- 17 单测全过, 1446 总测试 (+17)

**关键决策**：
- **不改 TaskPlanner, 新 module 顶层叠加**: TaskPlanner 现在 orchestrator + 多处用, 改它撞回归. RecursivePlanner 接 flat steps 入参, 与现有 planner 解耦.
- **PlanNode frozen + 用 dict 索引而非引用**: 树修改靠"替换整个 node 副本" — frozen 保证不变性; dict[node_id] = updated_node 是唯一改法.
- **atomic_decider 5 个启发式分支**: success_criterion (用户明确)/depth limit/short desc/tool prefix — 覆盖典型场景, 默认不需要 LLM. 真生产替换 LLM-based decider 给更智能判断.
- **sub_planner 默认"准备/执行/验证"3 步**: 通用兜底模板, 无 LLM 也能用. 真生产用 LLM 给具体场景拆解.
- **sub_planner raise 兜底 atomic**: 不破坏 tree, 整棵树仍可遍历. ADR-024 状态累积失败不阻塞主路径.
- **node_id 复用 task_checkpoint prefix**: 临时复用; 真生产建议加单独 entity kind `plan_node` (prefix `pn-`).
- **estimated_cost 自底向上 sum leaves**: tree.total_estimated_cost() = sum(leaves), 因为 non-atomic 的 cost 是 children cost 的总和概念.

**未完工作 (LT.G 整合)**:
- LLM-based sub_planner (现规则版只是兜底)
- 接到 TaskPlanner 之后: `tree = recursive.expand(task.spec.goal_detail, [PlanStepInput(...) for s in plan.steps])`
- 加 `plan_node` entity kind (现复用 task_checkpoint)
- 落 plan_nodes DB 表 (e.g. alembic 0013)
- ExecutorLoop 按 tree leaves 顺序跑 (而不是 flat steps)

---

## LT.G · e2e integration + retrospective + 3 methodology seeds (收官)

**完成**：2026-05-27 / commit 40596f5 + (latest)

**做了什么**：
- `tests/integration/test_long_task_e2e.py` 把 LT.A-F 6 个 service 串成完整长任务 runtime, 8 个 e2e 测试通过
- `docs/dev_logs/LT-retrospective.md` (9 段 + Methodology Card Candidates + 验收清单 + 未完工作)
- 3 张新 seeds/methodologies:
  - `service_module_not_wired_to_runtime_audit.yaml` — 类完整 + 单测过 ≠ runtime 真用
  - `maybe_x_pattern_threshold_zero_cost.yaml` — 阈值下返 None 零成本 no-op
  - `status_enum_over_bool_observability.yaml` — Literal[多个 status] 替代 bool
- 1454 全套测试通过 (+107 vs LT 开始), ruff 全绿, 27 seeds 全部 yaml.safe_load 通过

**关键决策**：
- **e2e integration test 全 fake DI**: 8 测试零 LLM 实调 / 零 PG / 零 redis. _InMemoryCheckpointStore + _ScriptedLLM + _ScriptedTools 模式. 测的是 "联动契约" 而非 "外部依赖运行".
- **e2e 覆盖 5 场景**: full happy path / resume after crash / anchor mismatch / pivot pause / off_topic / drift detect / compaction trigger / atomic leaves. 验证 6 个 service 都真被调到 + 契约正确.
- **3 张方法论选择**: 选 "实战即时可用" 的 (audit / maybe_X / status enum), 留更抽象的 (frozen_tree, take_strict_verdict, integrating_service_failure) 给未来类似任务一次抽几张.
- **未完工作显式列**: "kun.engineering.orchestrator 集成 + LLMRouter adapter + ToolRegistry adapter + DB writer + 真任务 run" 这 6 项是 LT 范围外的整合, 写在 retrospective 末尾防被遗忘.

**LT 全部完成 (7/7)**:
| sub-task | tests | notes |
|---|---|---|
| LT.A Layer 3 wiring | 13 | LongTaskInputRouter, 6 routing buckets |
| LT.B Layer 4 wiring | 17 | PlanReviewService + render_plan_review_prompt |
| LT.C Checkpoint/resume | 16 | TaskCheckpoint ORM + alembic 0012 + service |
| LT.D Long context compaction | 18 | ConversationCompactor, threshold-based |
| LT.E Multi-step exec loop | 18 | ExecutorLoop 整合 B/C/D |
| LT.F Recursive planning | 17 | PlanTree + PlanNode (frozen) |
| LT.G e2e integration | 8 | LT.A-F 联动验证 |
| **Total** | **107** | 1347 → 1454 全套 (+107) |

---

## LT.INT · 整合 — 真接到主 runtime path (并行 5 Agent + 我整合)

**完成**：2026-05-27 (LT 收官后续追加)

**目标**：把 LT.A-G 6 个 service 真接到 `Orchestrator.stream()`. 之前 service 完整 + 单测 100% 过, 但 orchestrator 从未调 — `service_module_not_wired_to_runtime_audit` 方法论说的就是这个状态.

**做法**：5 个并行 Agent + 我手动整合, **零现有 test 回归** (1454 → 1550, +96 new tests).

**子任务清单**:

| sub-task | Agent | tests | 文件 |
|---|---|---|---|
| LT.INT-A LLMInvoker + ToolExecutor adapter | A 并行 | 28 | kun/integration/llm_invoker.py + tool_executor.py |
| LT.INT-B Checkpoint DB writer/reader/marker | B 并行 | 15 | kun/integration/checkpoint_db.py |
| LT.INT-C PlanReview DB writer | C 并行 | 19 | kun/integration/plan_review_db.py |
| LT.INT-D ExternalSupervisor adapter | D 并行 | 10 | kun/integration/external_supervisor.py |
| LT.INT-E LongTaskOrchestrator skeleton | E 并行 | 14 | kun/engineering/long_task_orchestrator.py |
| LT.INT-F Orchestrator 分流 + branch test | 我 | 10 | kun/engineering/orchestrator.py + branch test |
| **Total** | | **96** | **15 文件** |

**关键决策**:

- **分流而非替换**: `Orchestrator.stream()` 在 step 6 (RuntimeState 设 running) 和 step 7 (skill 选择) 之间插入分流, **短任务路径零改动**, 1538 现有 tests 全过.
- **eligibility = is_long_task + has_anchor**: 缺 anchor 时即使 complex 也走短任务 (anchor 是 drift 检测的契约面, 没有就不该走长任务).
- **本地 OrchestratorEvent 镜像 + 一行转换**: 避免 LongTaskOrchestrator 直接 import kun.engineering.orchestrator (拉起 SQLAlchemy/watchtower/skills 重图). Wire 时 `OrchestratorEvent(kind=lt.kind, data=lt.data)` 转换零成本.
- **lazy imports in _run_long_task_branch**: 5 个 integration adapter 在分流方法内 import, 短任务路径不付加载开销.
- **Parity for validation + writeback**: 长任务 final 仍跑 `ValidationPipeline` + emit "insight" event + `record_outcome` capability writeback — 与短任务步骤 7.4 / 7.5 完全对齐, 现有测试期望保持.
- **Status mapping defense**: branch test 有 `test_status_map_keys_cover_loop_status_literals` 守门, LoopStatus 加新值会让测试失败提示更新.
- **5 agent 并行 + 我整合**: 各 agent 独立目录 (kun/integration/X.py 各自一文件), 互不冲突. 我集中处理 orchestrator.py + branch test. ~30 分钟完成 86 + 10 tests.

**未完 (后续 ticket, LT.INT 范围外)**:
- 真长任务跑 1+ 小时 e2e (产品验证, 用户提供场景)
- LongTaskOrchestrator 接 PlanReview DB writer + ExternalSupervisor verify (现 service 缺这两 callback 接入, 但 e2e fallback 不影响主路径)
- LLM-based summarizer 替 ConversationCompactor default
- 长任务期间用户消息走 LongTaskInputRouter (LT.A) 接 WS 入口

**LT + LT.INT 总成果**: 1347 → 1550 tests (+203), 21 个 commit, 长任务能力从"零件"到"真接到主路径". /loop 后用户给真任务即可跑.

---

## LT.OAUTH · AnthropicProvider 支持 OAuth 订阅 token

**完成**：2026-05-27 / commit add1b14

**背景**：dogfood (#26) 被卡到没法跑 — 三条 LLM 路径全部不可用:

| 路径 | 问题 |
|---|---|
| codex MCP (gpt-5.5) | 沙箱 read-only, approval=never, 不让写文件 |
| Claude CLI | sandbox keychain isolation, KUN 子进程读不到 user macOS keychain |
| Anthropic API key (sk-ant-api03-*) | 没 console 账号, 用户没付费过 |

用户提议方案 B: 用 `claude setup-token` 出的 OAuth token (sk-ant-oat01-*) 走自己 Claude Pro/Max **订阅 endpoint**, 不付 per-token 钱。

**做了什么**：
- `kun/interface/llm/anthropic_provider.py`: `_build_client` 检测 `ANTHROPIC_API_KEY.startswith("sk-ant-oat")` 时:
  1. 用 `AsyncAnthropic(auth_token=token)` 触发 SDK 的 `_bearer_auth` 路径 → `Authorization: Bearer <token>`
  2. `default_headers={"anthropic-beta": "oauth-2025-04-20"}` (订阅端点强制 opt-in)
  3. **显式 `client.api_key = None`** — 否则 SDK init 时 `ANTHROPIC_API_KEY` env var 会被自动填进 `self.api_key`, 同一请求同时发 X-Api-Key + Authorization 双头, 订阅端点拒
- 5 个新 unit test 覆盖 ofox proxy / 直 api_key / OAuth token / SDK auth_headers contract / no credentials
- 顺手清掉 scripts/ 里 5 个 ruff 老 nit (I001 import 排序 / F541 空 f-string / F841 unused var)

**关键决策**：
- **token 前缀检测而非配置标志**: `sk-ant-oat` 是 Anthropic 颁的格式不变, 无需新 env var. 用户换回 sk-ant-api03 时无需改任何配置.
- **保留 SDK 不换 raw httpx**: SDK 0.96 原生支持 `auth_token=` 参数, 改最小. 后续 retry/streaming 不需要重写.
- **显式 nil api_key**: SDK 自动 env var pickup 是个隐式陷阱, 测试里直接断言 `client.api_key is None` + `'X-Api-Key' not in auth_headers` 把这个陷阱钉死.
- **header-build 测试**: 第 4 个 test `test_oauth_token_sdk_emits_bearer_only` 不光看构造参数, 直接读 `client.auth_headers` property — 防未来有人在 OAuth 分支偷加 api_key fallback.

**curl 端到端验证 (commit 前)**：
```
curl -H "Authorization: Bearer sk-ant-oat01-..." \
     -H "anthropic-beta: oauth-2025-04-20" \
     https://api.anthropic.com/v1/messages
→ {"type":"error","error":{"type":"rate_limit_error",...}}
```
**不是 401** — auth 通了, 只是用户订阅额度被打满。这恰好证明 header 配置正确。

**未完工作 (DOGFOOD #26)**：
- 等订阅额度恢复 (Claude Pro/Max 是 5h rolling window) → 重跑 `scripts/dogfood_distill.py` → 产出 3 份 markdown + ≥5 yaml seeds + ≥3 runtime_capabilities rows
- 长期 ticket: Claude CLI sandbox keychain isolation 解决方案 (现下只能 OAuth token, 没法跑完整 CLI 体验)

**总测试**: 1670 → 1675 (+5), ruff 全绿.

---

## LT.CODEX-PURE-LLM · codex MCP 转纯 LLM 模式, gpt-5.5 走 XML 协议

**完成**：2026-05-27 / commit e6cbd2d

**背景 (dogfood v4 死因复盘)**：

dogfood v4 用 gpt-5.5 跑长任务, 任务要求"创建文件 X / 写新 seeds Y"。gpt-5.5 字面回:

> "当前环境是 read-only sandbox，approval policy 为 never，所以我不能新增/修改文件..."

根因不是单一: 是 **codex 宿主和 KUN 给 gpt-5.5 的双重身份冲突**:

```
codex MCP 启动:  sandbox=read-only, approval=never (codex 自家配置)
                  base-instructions: "Do not run tools or take side effects"
KUN 注入:        skill_directive (XML 工具协议描述)
                  system prompt: "用这些 skill 创建文件"

gpt-5.5 看到:
  - codex (宿主) 说: 别用工具, 沙箱锁了
  - KUN (任务) 说:  用这套 XML 协议干活
  → 信宿主, 出 sandbox 拒绝
```

**解 (Path B, 用户选)**：让 codex 闭嘴, 让 KUN 的协议成为 gpt-5.5 唯一信号源。

**做了什么**：
- 重写 `codex_mcp_provider.py` 的 `base-instructions` (从 3 行扩到 15 行):
  - 显式: "你是 pure LLM oracle, 没 file write/shell/codex tools"
  - 显式: "宿主的 XML 协议是 ONLY 行动方式, emit `<skill name='X'><param>val</param></skill>`"
  - 显式: "Never claim sandbox restrictions — 那不是你的事"
- `supports_tools = True` (之前 False; 现在 XML 协议确实是 tool 通道)
- `_build_prompt` 接 `request.tools` (即使现 caller 没传), 自描述契约
- 导出 `PURE_LLM_BASE_INSTRUCTIONS` 常量给测试做 contract check
- 5 个新 unit test: supports_tools / base-instructions 关键短语 / payload `_send` intercept / build_prompt 加 tool spec / 无 tools 保留旧 shape
- 新 smoke script `scripts/codex_pure_llm_smoke.py`: 离线验证 + 真实 codex MCP 调用 + 检测 sandbox 拒绝 pattern + 检测 `<skill>` XML 命中

**关键决策**：
- **改 prompt-engineering 而非沙箱**: codex MCP 的 sandbox=read-only 是 ChatGPT 客户端给的, 我们改不了。但只要 gpt-5.5 不去 codex 的 file tool, sandbox 是不是 read-only 就不重要 — 它根本不用。所以 fix 在 prompt 层就够, 不动 sandbox 参数。
- **`supports_tools` 真改 True**: 这个 flag 现在反映"模型能不能走 tool 流", XML 协议虽不是结构化 tool_use, 但 LT.TOOLS-GAP 已经把 XML→ToolCall 解回正了, ExecutorLoop 看到 tool_calls 走 dispatch。flag = True 不会引入新 bug, 反让 router/Loop 路由决策更准。
- **payload _send intercept 测试**: 不 mock 整个 subprocess (太脆), 只拦 `_send` callback, 断 JSON-RPC payload 里 `base-instructions` 字段。轻量但能锁住 contract。
- **smoke script 独立 stage**: 不进 pytest (pytest 不该启外部 subprocess), 但放 `scripts/` 给开发者跑 + 给 CI 后续做 e2e 验证用。

**真实测试 (commit 前)**：
```
$ .venv/bin/python scripts/codex_pure_llm_smoke.py
🚀 Calling gpt-5.x via codex MCP (model=gpt-5.5)...
📥 Response (81 chars, latency=17497ms):
<skill name="write_file"><path>notes/hello.md</path><content>hi</content></skill>
🧪 Verdict:
   sandbox refusal patterns: none ✓
   <skill name='write_file'> present: ✓
✅ PASS
```

gpt-5.5 出**精确** XML, 无前缀 prose, 无 sandbox 抱怨。

**dogfood v5 应该能跑了**：
- 现在 gpt-5.5 收到 KUN 的长任务 → 看到 skill_directive → 该写文件时 emit `<skill name="write_file">...</skill>`
- KUN 的 `make_llm_invoker` 路径已经有 `parse_skill_calls` 解 XML 回 ToolCall (commit 3b81372 / LT.TOOLS-GAP)
- ExecutorLoop 收 tool_calls, dispatch 给 ToolExecutor, 真落盘
- 闭环

**未完工作 (DOGFOOD #26)**：
- 用户决策启动 dogfood v5 (gpt-5.5 跑全程 Phase A-E)
- v5 跑完后写 dogfood-retrospective 抽 ≥3 张 methodology seeds

**总测试**: 1675 → 1680 (+5), ruff 全绿. 累计长任务相关代码改动: 1347 → 1680 (+333 tests), 23+ commits.

---

## LT.TOOLS-GAP-2 · codex base-instructions 区分 "no codex tools" vs "no tools at all"

**完成**：2026-05-27 / commit c740717

**dogfood v5 死因 (新, 比 v4 更细)**：

跑了 1 step 就 final answer, gpt-5.5 字面输出:
> "我需要读取仓库文件并新增文档/seed，但当前可用技能列表里没有通用'读写文件/执行命令'的主机技能，且任务不匹配现有专用技能（如 presentations、spreadsheets、documents、github 等）。请提供一个可用的文件/命令执行 skill"

奇怪点: gpt-5.5 列的 4 个名字 ("presentations / spreadsheets / documents / github") **都不是 KUN 实际有的 skill**。KUN 实际有 11 个 (5 starter + 6 builtin), 包含 file-io / shell-exec / writing-markdown / web-search 等 —— 完全够这个任务用。

**根因**：上轮 LT.CODEX-PURE-LLM 写的 base-instructions 太"杀绝"。原文:
```
You do NOT have file write access, NO shell, NO local tools.
```

模糊了"没 codex 直接工具" 跟 "啥工具都没" 的区别。gpt-5.5 见 base-instructions 跟 KUN 的"可用工具: file-io, shell-exec, ..."两套矛盾信号, 信前者 (codex 系统层级更高), 然后凭空列出 Claude Code 的真实 plugin 名当"理想工具"。这是个**自己挖坑自己跳**的 prompt-engineering bug。

**做了什么**：
1. **重写 base-instructions** (核心):
   - 显式区分 "no codex DIRECT tools" vs "KUN provides host tools"
   - "可用工具" section 是真工具列表, **TRUST THE LIST. Do NOT invent tool names**
   - 给精确 XML 格式: `<skill name="X">{"key": "value"}</skill>` JSON-in-body
     (之前 base-instructions 误写 `<param>val</param>` 嵌套元素格式, 跟
     KUN 的 `parse_skill_calls` 期望的 JSON-in-body 不符 — 顺便修)
   - "若需要的工具不在列表, 明说 '需要 X 但没有'", 不要静默 refuse
2. **测试 contract 升级**: `test_base_instructions_*` 检查
   - "host tool" / "no codex direct" / JSON 提示 / "trust the list" / 反幻觉
3. **smoke 重写** (`scripts/codex_pure_llm_smoke.py`):
   - 用真 KUN skill IDs (file-io / shell-exec / writing-markdown)
   - 用 `build_skill_directive` 生成 system prompt (跟 LongTaskOrchestrator 一致)
   - 用 `parse_skill_calls` 真验证 XML 解得出来 (之前 smoke 只 string-match)
   - 加 `autoload_builtins()` (parse_skill_calls 过 dispatcher.is_registered)
   - 加 hallucination 检测 (presentations / spreadsheets / documents / github 现在算 fail signal)

**真实 smoke 结果 (commit 前)**：
```
prompt: skill_directive [file-io, shell-exec, writing-markdown]
        + "请把 'hi' 写到 notes/hello.md"
gpt-5.5 返 (83 字符, 11.8s):
  <skill name="file-io">{"op":"write","path":"notes/hello.md","content":"hi"}</skill>
✓ 真实 skill 名
✓ JSON-in-body 格式
✓ schema 字段对 (op/path/content)
✓ parse_skill_calls 解出 1 个 invocation
✓ 无 sandbox 拒绝
✓ 无幻觉名
```

**关键决策**：
- **不动 selector**: 假设 selector 已经合理选 — 修 base-instructions 即可。后面 dogfood v6 跑出来再看是否需要 selector 兜底逻辑 (e.g. "永远把 file-io / shell-exec 钉死在前 N")。
- **不动 build_skill_directive**: 它产 Chinese-mixed-with-XML 已经能 work, 改成纯英文反而可能影响中文任务的语境理解。
- **smoke 增加幻觉检测黑名单**: 不是泛化"任何 unknown name 就 fail", 而是定向钉 v5 出现过的 4 个名字。误伤面小, 回归侦测精度高。
- **parse_skill_calls 自己有 unknown_skill 过滤是好事**: 现 dispatcher 没注册的 XML 会被默默 drop, 防止脏数据进 ExecutorLoop。LT.TOOLS-GAP-2 smoke 自己用 autoload 让 dispatcher 跟 uvicorn 状态一致。

**未完工作 (DOGFOOD #26)**：
- dogfood v6 启动 (gpt-5.5 跑全程 Phase A-E, 修后的 base-instructions)
- v6 跑完后写 dogfood-retrospective 抽 ≥3 张 methodology seeds

**总测试**: 1680 (持平, 改的是 test contract 没新增数量), ruff 全绿.

