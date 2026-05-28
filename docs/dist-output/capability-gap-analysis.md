# Claude Code × KUN 能力差距分析

## Phase B 结论

对比 Claude Code 工程能力、现有 27 张 `seeds/methodologies/*.yaml`、LT/L4 dev logs 后，KUN 已具备不少工程化硬能力，但仍有一批能力缺失或只半具备。

## 缺失能力（≥5）

### 1. RuntimeTodoTracker 动态任务状态机

- Claude Code：运行时 todo 可增删改重排，一次只允许一个 in_progress。
- KUN 现状：有 TaskPlanner / RecursivePlanner，但主要是静态计划结构。
- 缺口：缺 runtime 层 todo 状态机与事件流。

### 2. 通用无状态 WorkerAgentSpawner

- Claude Code：一个 message 内并行启动多个无状态 subagent，主线整合结果。
- KUN 现状：有七角色、Supervisor Pool、External Supervisor Pool。
- 缺口：缺任意任务级 worker spawn、background completion notification、prompt 隔离契约。

### 3. PromptUserDecision 交互决策服务

- Claude Code：产品方向/不可逆改动前暂停问用户。
- KUN 现状：Gate 是 approve/reject，LongTaskInputRouter 能识别 pivot/cancel。
- 缺口：缺 agent 主动发起多选问题并等待恢复的机制。

### 4. SilenceDetector / Monitor-style 失败信号穷举

- Claude Code：monitor 同时 watch success、Traceback、FAILED、OOM、silence。
- KUN 现状：SupervisorService 处理已发生 anomaly，heartbeat 做 step review。
- 缺口：缺长任务静默/卡死检测。

### 5. Cache-aware LLMRouter / Wakeup Scheduler

- Claude Code：调度考虑 prompt cache TTL。
- KUN 现状：idle_batch 固定节奏，LLMRouter 未显式优化 cache window。
- 缺口：缺同 task prompt prefix 复用与 wakeup interval 策略。

### 6. 多模态 ContextPacker

- Claude Code：图片/PDF 原生读入模型。
- KUN 现状：LLMRequest/ContextPacker 主要文本。
- 缺口：缺 image/pdf asset、message content list、视觉 provider 路径。

## 半具备能力（≥5）

### 1. Grep verify before assume

- 已有：`service_module_not_wired_to_runtime_audit.yaml`。
- 半缺：它是审计方法，不是 Executor 每次改动前的默认 pre-action。

### 2. 测试驱动 fail-fast loop

- 已有：ValidationPipeline、Tester 角色、dev log 中持续 pytest/ruff。
- 半缺：Executor 主路径未强制“改一处、测一处、红了立即修”。

### 3. Tool / Skill 渐进披露

- 已有：skills loader、selector、dispatcher。
- 半缺：缺原子 tool registry 与 ToolSearch 式 schema lazy load。

### 4. Context compaction

- 已有：ConversationCompactor、ContextPacker、ImportanceScorer。
- 半缺：文件读取/编辑工具层仍缺行号读与 edit-only 的强制纪律。

### 5. Dev log → seed 持续学习

- 已有：ADR-025、methodology_distill、seeds/methodologies。
- 半缺：distill 主要产 candidates，未完全自动 Gate/promote/enable。

### 6. 小步 promotion 纪律

- 已有：promotion lifecycle、resource quota、exploration penalty。
- 半缺：缺同 target_module 同时只允许一个 active experiment 的互斥约束。

### 7. External Supervisor 取严合并

- 已有：External Supervisor Pool、PlanReviewService 取严合并思路。
- 半缺：新 methodology seed 进入实验时，还需要固定 Selector + External Supervisor + Gate 模板。

## 优先级建议

1. Tier 1：RuntimeTodoTracker、PromptUserDecision、SilenceDetector、Executor grep-verify pre-action。
2. Tier 2：WorkerAgentSpawner、ToolSearch/lazy tool registry、cache-aware LLMRouter。
3. Tier 3：多模态 ContextPacker、KUN as MCP server、distill→Gate→promotion 全自动闭环。
