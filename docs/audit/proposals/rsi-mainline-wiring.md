# RSI 主链生产化 · 合并方案（F021/F022/F025/F039/F040/F041/F042，关联 F050）

> 状态：needs-design（系统集成 + 跨进程事件流 + DB 读写闭合，不能在 loop 内逐点盲改）
> 北极星：ADR-024「每跑一次都让自己略变更聪明」。当前是**零件齐全、传动轴未装**——
> 5 环引擎都有代码+单测，但生产事件流进不来、合成证据喂门禁、脊柱表零流动。

## 0. 一句话根因

ADR-024 的闭环 = **检测 → 策略 → 安全实验 → 门禁落地 → 影响下次决策**。每一环的"引擎"
都实现了，但**环与环之间在生产 runtime 没接线**：异常事件流不进检测器、策略候选不落库、
实验环整体缺失、门禁吃的是写死的 `pass_rate=1.0`、能力表零读写。所以"闭环"按项目自己的
定义（"闭环=数据流动"）并未成立。这 7 条 finding 是**同一个根因的 7 个断点**。

## 1. 逐环断点（文件:符号，已核实）

| 环 | Finding | 断点 |
|----|---------|------|
| 1 检测 | **F039/F022** | `kun/agents/supervisor/service.py` `SupervisorService.observe()` 全仓唯一实例化在 `scripts/e2e_rsi_demo.py:133`；无 NATS/outbox 消费者调用 `observe()`，`emitter`(写 `strategy_search_requests` 的 DB writer)生产从不注入。LLMRouter **确实**在生产 emit `llm.fallback.triggered` 到 events 表(`router.py`)——但没人把它喂给 `observe()`。 |
| 1.5 防漂移 | **F021** | `ExecutorLoop` 收到模型心跳自评 JSON 只回 `"Status received. Continue working..."`，**从不解析**；`PlanReviewService.observe_step_and_maybe_render_prompt` 的 `executor_self_report` 恒为 `None` → `evaluate_executor_self_report({})` 永远 `aligned`；`submit_self_report` 0 生产调用方。`long_task_orchestrator.py:546` 注释自认 "We do not yet parse the Executor's self_report JSON"。 |
| 2 策略 | **F022** | `StrategistService` 生产调用方只有 `kun/integration/prompt_ab.py`，且默认无 `emitter` → 候选不落库。没有消费者读 `strategy_search_requests` 表来驱动 Strategist。 |
| 3 安全实验 | **F040** | 整环不存在。`RuntimeExperimentRow` 生产代码无 insert/select；`StrategistService.ExperimentEmitter` 生产从不注入 writer；`Executor`(`kun/agents/executor/base.py:35`)只是 `class Executor(Protocol): async def run(...) -> Any: ...` 桩——"读 runtime_experiments 应用候选 override" 只是 docstring。 |
| 3 实验 schema | **F091** | `StrategyExperiment.to_row_payload`(`strategist/service.py:59`)丢 `requires_human_review`/`explorer_mode`/`rationale`——因为 `runtime_experiments` 表(0011)/`RuntimeExperimentRow`(orm.py:522)**根本没有这三列**。其中 `requires_human_review` 是 Gate 自指审查输入(`gate/service.py:174`)，一旦实验环接通(本表开始被生产读写)，落库再读回就会丢这个安全标记。**当前无 live 风险**：to_row_payload 无生产消费者，且 Gate 有 L3.5 独立 `target_module` 自指检查兜底。修法：接线本环时给 `runtime_experiments` 补 `requires_human_review bool / explorer_mode / rationale` 三列(迁移+ORM)并补全 payload，加 round-trip 测试。 |
| 4 门禁落地 | **F041** | `GateService.admit` 的 R1-R6 真实，但唯一生产调用链(`api/main.py` → idle_batch_worker → MethodologyDistillStep → `methodology_to_gate_bridge`)喂的 `test_report` 是写死 `pass_rate=1.0`(`bridge.py:107`)、`debrief evidence_quality=0.75`——验证的是合成数据，必放行；且该路径 `GateService()` 不带 `capability_writer`(`bridge.py:151-153`)，approve 也**不写** `runtime_capabilities`。 |
| 5 影响下次 | **F041/F042** | `runtime_capabilities.enabled` 读侧唯一消费者是 `prompt_ab.pick_active_variant`(且 PromptAB 自身生产无调用方)。6 张脊柱表(0011)中 `runtime_capabilities/runtime_experiments/strategy_search_requests` 仅 demo 读写；`diagnostic_records` 0 写入(`rcdh.run_diagnostic` 只返回对象不落库)；`goal_anchors` 0 写入(`director/intent.py:165` 注释自认未做，anchor 只在内存 pin)；`evidence_ledger` 0 流动。 |
| 5 enable 门禁 | **F089** | `GateService.enable_capability`(`gate/service.py:444`)的自指强门禁有两个绕过：①整段 self-referential 检查被 `if metadata_lookup is not None:` 包住——调用方不传 `metadata_lookup` 就**静默跳过**(fail-open)；②`human_approval_token` 校验只看"非空"——任意非空串即放行。**当前无 live 风险**：`enable_capability` 全仓**无生产调用方**(仅注释"人审完成后 caller 可调")，且无任何 approval-token 签发/验证机制。修法(随本环接通时)：metadata 必须来自权威源、缺失时 fail-closed(拒绝而非跳过)；token 改为对照真实签发方案(promotion_queue 一次性 nonce / 签名)，而非"非空"。**不要**在孤儿函数上加共享密钥长度检查充数(security theater)。 |
| 输入路由 | **F025** | `kun/api/ws.py` 的 `task_state` 从无赋值真值的代码(只 置 None + 读取)，`if task_state["goal_anchor"] is None` 恒真 → 长任务期追加消息永远走"task already running"拒绝分支，`handle_long_task_input` 的 6-bucket 路由(off_topic/scope_expansion/pivot/cancel)在 WS 生产路径是死代码。 |
| 晋级超时 | **F050** | `PromotionTimeoutSweeper.sweep()` 实现真实但生产无调度器调用(见 ADR-024 修正注记)。 |

## 2. 最小可行接线顺序（每步独立可上线、可验证）

闭环要从**源头**往下接，否则下游接了也没数据流。建议顺序：

1. **接事件源 → 检测器（F039/F022 第 1 环）**。已有 `llm.fallback.triggered` 等事件进 events 表 + outbox→NATS。新增一个 **NATS subscriber（或 outbox 消费者）把 `kun.task.*` / `llm.*` 事件喂给 `SupervisorService.observe()`**，并在生产装配处注入 DB-backed `SearchRequestEmitter`(写 `strategy_search_requests`)。
   - 验证：集成测试——发一个 `task.failure`/`fallback` 事件 → 断言 `strategy_search_requests` 落了一行(经 dedup)。
   - 风险：事件风暴 → 依赖 observe 已有的 dedup_ttl；NATS 链路本身见 F029。
2. **接策略消费者（F022 第 2 环）**。一个 worker 读 `strategy_search_requests`(status=pending) → 调 `StrategistService` 生成 `StrategyExperiment` → 注入 `ExperimentEmitter` 写 `runtime_experiments`。
   - 验证：插一条 pending request → 跑 worker → 断言 `runtime_experiments` 落行 + request 置 consumed。
3. **实装实验执行（F040 第 3 环）**。给 `Executor` 一个真实现：读 `runtime_experiments`(pending) → 应用候选 override 跑一次 → 产出真 `test_report`(真实 pass_rate，不是 1.0)。
   - 验证：端到端——一条实验 → 真跑 → test_report 反映真实结果。
   - 风险：最大的一环，需定义"安全实验"的隔离/回滚；建议先只跑只读/影子实验。
4. **门禁吃真证据 + 写能力表（F041 第 4 环）**。`methodology_to_gate_bridge` 改为传入第 3 环产出的**真 test_report**；生产装配 `GateService(capability_writer=<DB writer>)`，approve 时写 `runtime_capabilities`。
   - 验证：approve 路径断言 `runtime_capabilities` 落行；合成 pass_rate=1.0 的旁路被移除(加不变量测试：无真实 evidence 不得 pass，呼应 F008/F016/F017 方案)。
5. **闭合读侧（F041/F042 第 5 环）**。让 LLMRouter / 相关决策点**读 `runtime_capabilities.enabled`** 影响下次路由(目前只有 PromptAB 读，且 PromptAB 没接线)。`promotion advance()` 状态机接真实信号。
   - 验证："写一条 enabled capability → 下次同类任务路由可见其影响"的端到端断言。
6. **补防漂移消费端（F021）**。`ExecutorLoop` 解析心跳自评 JSON → 调 `submit_self_report` → `PlanReviewService` 真消费(drift 时触发 External Supervisor，见外部监督方案)。
   - 验证：喂一个 drift 自评 → 断言 plan_reviews 落行 + 触发漂移处置。
7. **填充 WS task_state（F025）**。`_run_task_stream` 拿到 task_state 引用并在 orchestrator 事件出现时写入 goal_anchor/task_id → 长任务追加消息走 `handle_long_task_input` 6-bucket 路由。
   - 验证：WS 集成测试——长任务进行中发追加消息 → 走 scope/pivot 路由而非"already running"。
8. **调度 sweeper（F050）**。把 `PromotionTimeoutSweeper.sweep()` 接进 daemon 治理 pass / idle-batch，注入 DB-backed reader/writer/emitter。
9. **落库脊柱表其余项（F042）**：`diagnostic_records`(rcdh 落库)、`goal_anchors`(director 写库)。

## 2b. 就绪未接线的旁路组件（F063/F064/F065/F099/F101/F103/F104/F114）

与主链同根的"引擎/组件写好了但生产零接线"实例——接通时按对应环一并落地，落地前不应宣称其能力可用：

| ID | 现状（已核实） | 接线条件 |
|----|------|------|
| **F063** | `kun/context/storage.py:82` `RedisAssetStore` 真实，但全仓无运行时写入方/接线 —— Context 资产层实际只有进程内内存实现，重启即丢、跨进程不一致 | 把资产读写接到 `RedisAssetStore`(生产装配注入)，加跨进程持久化测试。 |
| **F064** | `kun/governance/capability_lifecycle.py` CANARY→PRODUCTION 审批校验器写好但从未接线，生产晋级路径仍是 honor-system | 第 4 环门禁落地时，把 lifecycle 审批校验接进 promotion 路径(与 F041 capability_writer 同处)。 |
| **F065** | `kun/governance/evidence_ledger.py` 是空 stub：`append` 只打日志、`get_trace` 返回 [] —— ADR-024 审计链不存在(已有契约测试 F155 固化 stub 行为) | 第 5 环落 `evidence_ledger` 表(0011)真读写；契约测试(F155)同步升级断言。 |
| **F099** | `kun/evaluation/industry_suite.py` L6 行业评测套件孤儿且度量浅 | 与 F114 一并接入(或明确归档)。 |
| **F101** | `kun/governance/{resource_quota.py:86,exploration_penalty.py:102}` `ResourceQuota`/`ExplorationPenalty` 从未注入生产 Strategist，且单进程内存态 | 第 2 环策略消费者装配 Strategist 时注入这二者(DB backed)；F146 已让 Strategist 透传真实 tenant，接线即生效。 |
| **F103** | `kun/engineering/proactive_tools.py:300` Layer 1a 强制工具分支只 `seen.add` 从不真正 dispatch，反而抑制同名技能的关键词触发 | 让 Layer 1a 真 dispatch(或不抢占 seen)，加"强制工具确被调用"测试。 |
| **F104** | `kun/integration/prompt_ab.py:195,305` `PromptABService` 调 Strategist 私有 `_emit_and_adjust`，且模块生产无调用方 | 改调公开 API + 接生产调用方(第 2/5 环 PromptAB 读 enabled capability)；F091/F146 已为 `_emit_and_adjust` 加 tenant 形参。 |
| **F114** | `kun/evaluation/__init__.py` L6 评测框架(446 行)生产调用方为零，仅测试引用 | 接 L6 评测入生产度量，或明确标注为"离线评测工具、非生产路径"(同 F099)。 |
| **F087** | `kun/agents/supervisor/pool.py:36-49` SupervisorPool fan-out：同一事件被多维度消费(`_DEFAULT_AUDIT_DIMENSIONS`)，但维度实例**不按维度过滤检查项**(与自述"维度间不互扰"矛盾)，同一异常被双发；且 Pool 本身**0 生产调用方**。 | 接线本环监督池时：每个维度实例只跑该维度的 check、emit 前按 (dedup_key) 去重；先证明 Pool 有真实生产入口再启用。 |
| **F090** | `kun/agents/tester/multi_judge.py:140-156` MultiJudge"多判官"实为**同一模型、同 `temperature=0.1`** 调 N 次——票高度相关，多数票的独立性假设不成立。**模型约束(claude-api 权威)**：temperature/top_p 在 Opus 4.7/4.8/Fable 5 已移除(传入 400)，"变温度去相关"在现模型**不可行**。 | 去相关只能靠**不同模型**或**不同提示视角(角色/评分维度各异的 panel)**；否则不要宣称"独立多数票"，应如实表述为"单模型多次采样的自一致性检查"。作为质量信号接入 gate/RSI 时一并改造。 |

## 3. 总体风险 / 排期建议

- **不要一次接完**：按上面 1→9 单步上线，每步加集成测试断言"数据真流动了"。第 3 环(安全实验)是最大不确定项，建议先影子运行。
- **与其它方案的耦合**：第 4 环的"真证据"依赖 F008/F016/F017(去硬编码门禁分数)；防漂移第 6 步依赖外部监督方案(F030/F031/F032)；事件源依赖 watchtower NATS(F029)。建议把这些作为「RSI 生产化」一个 epic 统一排期。
- **诚信优先**：在任一环真正接通前，PROGRESS/decisions 里相关"已闭环/已达成"措辞应保持 F047/F049/F050 那样的如实标注，避免 L5"RSI 真闭合 ✅"的过度宣称。

## 4. 本方案覆盖的 findings
F021, F022, F025, F039, F040, F041, F042, F063, F064, F065, F087, F089, F090, F091, F099, F100, F101, F103, F104, F114, F117, F127（标 needs-design 指向本文件）；F050 已在 ADR-024 注记并在第 8 步接线。F063/F064/F065/F087/F090/F099/F101/F103/F104/F114 见 §2b。

> F089 落地说明：在第 4/5 环接通 gate→`runtime_capabilities` enable 路径时，`enable_capability` 的自指门禁改为：metadata 由权威源保证、缺失即 fail-closed；`human_approval_token` 对照真实签发/验证方案校验（不再"非空即过"）。当前路径无生产调用方，无 live 风险。

> **RCDH 接线（F100/F117/F127）**：`kun/governance/rcdh.py` 的 `run_diagnostic` 只返回对象、不落 `diagnostic_records`(第 5 环已点)；`rsi_trigger` 仅落库字符串、无 heavy-drift 后续动作；`supervisor/service.py` 的 RCDH 诊断链路(含 `narrow_scope` 护栏)生产无人调用。三条同属「RCDH 引擎就绪、生产零接线」——在第 1 环(检测器接 observe)+第 9 步(diagnostic_records 落库)接通时一并落地：诊断结果落 `diagnostic_records`、`rsi_trigger` 触发真实策略搜索、narrow_scope 护栏在生产诊断路径生效。

> F091 落地说明：在第 2/3 环接通 `runtime_experiments` 读写时，同步给该表补 `requires_human_review`/`explorer_mode`/`rationale` 三列并补全 `to_row_payload`，加 payload↔row round-trip 测试（断言安全标记 `requires_human_review` 不丢）。
