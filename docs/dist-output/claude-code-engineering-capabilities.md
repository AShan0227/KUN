# Claude Code 工程能力蒸馏

来源：`docs/claude-code-engineering-manual.md`、`docs/dev_logs/LT-retrospective.md`、`docs/dev_logs/L4-retrospective.md`、dogfood log 与现有 seeds 抽样。

## Phase A 结论

Claude Code 的工程能力不是单点模型能力，而是 **LLM + 确定性工具 + 工程纪律** 的组合。核心可蒸馏为 12 类能力。

## 1. 动态 Todo 状态机

- 能力：把复杂任务拆成可验证 todo，运行时维护 `pending / in_progress / completed`，一次只有一个焦点。
- 工程价值：可观测、可恢复、可重排，避免长任务漂移。
- KUN 对应：已有 `TaskPlanner` 与 `RecursivePlanner`，但偏静态 plan tree，缺 runtime todo tracker。

## 2. 并行 SubAgent / Worker 扇出

- 能力：把互不依赖的工作分给无状态子 agent，并行执行后由主线整合。
- 工程价值：隔离上下文、隔离失败、扩大吞吐。
- KUN 对应：已有七角色与 Supervisor Pool，但缺通用 worker spawner、background completion event。

## 3. Grep verify before assume

- 能力：改动前先 grep caller chain / runtime path，不凭记忆假设。
- 证据：LT retrospective 中发现 Layer 3/4 service 类与单测存在，但 runtime path 0 命中。
- KUN 对应：已有 `service_module_not_wired_to_runtime_audit` seed，但还不是 executor 默认动作。

## 4. 测试驱动与 fail-fast 自纠

- 能力：每个小改动后跑 pytest/ruff；trace 看根因；错误立即修不藏。
- 证据：LT 阶段 sequence counter、regex、ruff 问题均由测试即时暴露。
- KUN 对应：有 ValidationPipeline / Tester 角色，但缺“改一处测一处”的执行节奏。

## 5. 决策点暂停问用户

- 能力：产品方向、不可逆选择、大范围改动前显式暂停，给 2-4 个选项与推荐。
- KUN 对应：有 Gate 与 LongTaskInputRouter，但偏自动判定；缺 `PromptUserDecision` 式交互决策服务。

## 6. 上下文经济：行号读、局部 edit、compaction

- 能力：读文件用 offset/limit；改文件优先 diff；长对话压缩保 anchor + recent。
- KUN 对应：已有 ContextPacker / ConversationCompactor，但文件工具纪律与 edit-only 约束不完整。

## 7. 工具/Skill 渐进披露

- 能力：核心工具常驻，长尾工具按需 ToolSearch；Skill 从 L1 描述到 L3 body 渐进加载。
- KUN 对应：已有 skills loader / selector；缺原子工具 registry 与工具 schema lazy load。

## 8. Monitor 与 silence 检测

- 能力：后台任务必须监控成功和失败信号；静默不等于成功。
- KUN 对应：有 SupervisorService 与 heartbeat；缺 “N 分钟无事件 = suspected_stuck”。

## 9. Cache-aware 任务节奏

- 能力：调度唤醒考虑 prompt cache TTL，避免最差间隔。
- KUN 对应：idle_batch / LLMRouter 尚未把 cache window 当调度输入。

## 10. Commit / promotion 小步纪律

- 能力：小 commit、配套测试、不绕 hook、不做 destructive git。
- KUN 对应：runtime capability promotion 可类比为“小步 enable”，但尚缺同 target 同时只允许一个 active experiment 的纪律。

## 11. ADR-025 dev log 与 seed 沉淀

- 能力：每子任务写 progress，每里程碑写 retrospective，并抽 methodology seeds。
- KUN 对应：已有 methodology_distill 与 seeds，但 distill→Gate→auto-promote 尚未闭环。

## 12. 多模态输入能力

- 能力：图片/PDF 可直接进入模型输入。
- KUN 对应：LLMMessage 仍偏文本，ContextPacker 未建 image/pdf asset 类型。

## 跨切面原则

1. 工具与纪律互锁：没有工具，纪律不可执行；没有纪律，工具会被乱用。
2. Token 经济贯穿：lazy load、行号读、diff edit、cache-aware wakeup、compaction。
3. 失败不阻断主路径：辅助 service 失败 log + skip，状态机推进失败才升级。
4. 决策点显式化：todo、chapter、AskUserQuestion、Gate 都应可观测。
5. 持续学习闭环：dev log → candidate → seed → selector → experiment → promotion。
