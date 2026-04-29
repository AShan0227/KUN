# KUN V3 交付开发方案

> 目的：把 V3 产品方案落成可测试、可复盘、可继续开发的真实产品。
>
> 本文只做开发顺序和验收标准，不替代 `KUN-V3.md`。

## 0. 先回答：未完成项是否都在 V3 里

大部分在 V3 里，少数需要补成 V3 交付项。

| 未完成块 | V3 是否覆盖 | 对应位置 | 备注 |
|---|---:|---|---|
| 外部世界打通 | 是 | §11 World Gateway / §14 V3-5 | V3 已写方向，当前代码只到审计网关，需要补真实 handler。 |
| 长周期任务能力 | 部分覆盖 | §4 Mission/Task/Action / §9 State Ledger | V3 有抽象，但缺“恢复、续跑、定时、里程碑”的交付细节。 |
| 前端主体验 | 是 | §3 用户产品形态 / §14 V3-7 | 当前只是第一版，需要做成任务控制台。 |
| NUO 管家体验 | 是 | §13 NUO 和 Qi / §14 V3-7/8 | 当前入口偏多，后续要做减法。 |
| 记忆和策略复用 | 是 | §12 三层记忆写回 / §5-8 Strategy Pack + 评分 | 当前第一版写回已做，复用闭环还缺。 |
| 生产级部署 | 不充分 | ops 文档 / PROMISES | V3 需要补成正式交付阶段。 |
| 诚实边界 | 是 | §0 / §15 / V3-8 | 已补 V3-9 能力边界账本，后续要自动化。 |

结论：这些都属于 V3 要交付的范围，但不是一个 PR 能完成。正确做法是按依赖顺序拆 6 个阶段。

## 1. V3 开发总原则

每个任务必须同时满足 5 条才算完成：

1. 主流程调用了它。
2. 输出被下游真实消费。
3. 用户或 LLM 能看见它产生的影响。
4. 有自动化测试覆盖关键路径。
5. 能力边界写清楚：做了什么、没做什么。

下面几种不算完成：

- 只建表。
- 只 emit 事件。
- 只加字段。
- 只写 UI 卡片。
- 只写文档。
- 只说“后续启会学”。

## 2. 开发顺序总览

顺序不能乱。KUN 的目标是先能真实解决问题，再逐步变聪明。

```text
P0 诚实和可测试底座
  ↓
P1 外部世界最小真实动作
  ↓
P2 长周期任务骨架
  ↓
P3 主体验和 NUO 做减法
  ↓
P4 记忆复用 + 系统级 MoE
  ↓
P5 生产级 dogfood 和部署
```

## P0：诚实和可测试底座

目标：先保证系统不会装成“都完成了”。

当前状态：

- `KUN_LLM_PRIMARY=codex` 已可走 Codex MCP。
- `gpt-5.5` 已实测能跑。
- `get_router()` 默认也已改为 Codex/gpt-5.5 优先；即使 `.env` 缺省，也不会再优先探 Claude。
- `/nuo/health/delivery-status` 已能显示能力边界。

还要做：

1. 能力边界账本自动校验：
   - 从测试结果、runtime telemetry、PROMISES 抽取证据。
   - 如果某能力标 `ready` 但没有测试或主流程调用，CI 失败。
2. 每个核心模块补“真实消费者”检查：
   - WorldGateway
   - StateLedger
   - Hermes
   - MemoryWriteback
   - UnifiedScoring
   - WatchtowerDecisionPlane
3. 本机 dogfood smoke：
   - 发起一个任务。
   - 看到 StateLedger。
   - 看到 scorecard。
   - 看到记忆写回。
   - 看到 NUO 能力边界。

验收：

- `make serve` + 前端能跑。
- Codex `gpt-5.5` 真调用成功。
- NUO 能显示“可测 / 半闭环 / 仅审计 / 未就绪”。
- 任意未真实接通能力不能标完成。

## P1：外部世界最小真实动作

目标：让 WorldGateway 从“只审计”变成“安全执行一小类动作”。

第一批只做低风险动作，不碰支付和公开发布。

优先级：

1. `local_file.write`
   - 只允许写入工作区指定 output 目录。
   - 禁止 path traversal。
   - 写前生成 diff / preview。
   - 用户批准后执行。
2. `webhook.post_dry_run`
   - 先只 dry-run，不真实发。
   - 渲染请求体、headers、目标域名、风险。
3. `email.draft`
   - 只生成草稿，不发送。
   - 用户确认后才进入未来的 `email.send`。
4. `browser.plan`
   - 只生成浏览器操作计划，不真实点网页。

需要补的模块：

- WorldGateway handler registry。
- handler 权限声明。
- handler dry-run / execute 双模式。
- Secret store 接口。
- 审批后执行结果写入 StateLedger。
- 失败补偿记录。

验收：

- pending action approve 后，不再只写 `external_dispatched=false`。
- 对支持的 handler，能真实执行或 dry-run。
- 对不支持的 handler，继续诚实标 `requires_handler=true`。
- NUO 能看见执行结果和审计。

## P2：长周期任务骨架

目标：让 KUN 不只是“一问一答”，而能持续做 Mission。

要做：

1. Mission / Task / Action 真实数据模型收口。
2. durable task resume worker：
   - 任务 crash 后可恢复。
   - paused / queued / running 状态一致。
   - 当前已有 worker shell；没有真实 runner 时会明确 `skipped`，不会假装已恢复执行。
3. checkpoint：
   - 每个长任务有阶段目标。
   - 每阶段有成功标准。
   - 每阶段有预算和风险。
4. scheduler：
   - 支持定时复盘。
   - 支持下一阶段自动启动。
5. failure reaper：
   - 扫描卡死任务。
   - 标记、恢复或要求人处理。
6. 长周期 StateLedger 持久化：
   - 热视图继续快。
   - 事件日志可回放。
   - 快照可恢复。
   - 当前黑板已能从 `runtime_states + tasks` 恢复可读快照；完整事件溯源还没做。

验收：

- 一个 Mission 可以拆多个 Task。
- Task 可以暂停、恢复、失败续跑。
- 中途重启 API 后，任务状态不丢。
- 用户能看到 Mission 进展和下一步。

## P3：主体验和 NUO 做减法

目标：让用户一眼知道 KUN 在干什么，而不是看技术面板。

主入口只保留：

- 对话框。
- 当前任务。
- 当前步骤。
- 当前风险。
- 当前成本。
- 待确认动作。
- 下一步。

当前状态：

- 首页已显示 Mission / 活跃任务 / 成本 / 风险 / 待确认。
- 首页可直接批准或拒绝 pending action，走同一条 NUO 审批接口。
- NUO 已把高级诊断、能力画像、能力边界折叠，不再作为第一层噪声。

NUO 初期只保留 4 个主入口：

- 健康。
- 成本。
- 权限。
- 风险。

高级内容默认折叠：

- 能力画像。
- 诊断发现。
- benchmark。
- 事件日志。
- 节点图。

要做：

1. 任务详情页。
2. StateLedger 驱动的任务卡。
3. pending action 统一审批体验。
4. 能力边界展示继续保留，但不要喧宾夺主。
5. 节点图放二级入口，不做首页。

验收：

- 用户打开首页 5 秒内知道：KUN 在做什么、卡在哪、要不要我确认。
- 不需要懂 Watchtower / Hermes / Qi / Protocol。
- 高风险动作必须能解释为什么需要确认。

## P4：记忆复用 + 系统级 MoE

目标：让 KUN 真正越用越会做选择。

核心是 Strategy Pack，不是单纯堆记忆。

要做：

1. Strategy Pack 数据模型：
   - 场景。
   - 触发条件。
   - context 标签。
   - skill hints。
   - 默认模型档位。
   - 默认评估指标。
   - 风险规则。
   - 写回规则。
2. Watchtower Decision Plane 真实消费 Strategy Pack。
3. MoE 稀疏激活：
   - 不同任务只激活相关 context / skill / metric。
   - 例如教育任务只唤醒教育方法论和理解度评估。
4. 三层记忆复用：
   - 结果记忆影响成功率先验。
   - 过程记忆影响路径选择。
   - 元决策记忆影响模型 / skill / 评估强度。
5. idle-batch 蒸馏：
   - 把多次任务经验变成方法论。
   - 把无效经验降权。
6. 遗忘 / 衰减：
   - 不是全部永久存。
   - 重要经验强化。

验收：

- 同类任务第二次执行时，能看到“参考了哪些历史经验”。
- 守望决策单里出现命中的 Strategy Pack。
- 不同任务激活不同评估指标。
- scorecard 真实写回 capability_card / memory。

## P5：生产级 dogfood 和部署

目标：让 KUN 能被你真实拿来做事，而不是只在本地 demo。

要做：

1. 账号和租户：
   - 显式用户。
   - 显式租户。
   - 去掉生产默认租户 fallback。
2. 密钥管理：
   - LLM key。
   - 外部 API key。
   - webhook secret。
3. CI / release：
   - ruff。
   - mypy。
   - unit。
   - integration。
   - frontend lint/typecheck。
4. 监控：
   - 任务成功率。
   - 成本。
   - 延迟。
   - fallback。
   - 外部动作失败率。
5. 备份恢复：
   - Postgres。
   - object storage。
   - context assets。
6. dogfood 场景：
   - 运营一个真实产品。
   - 做获客列表。
   - 写邮件草稿。
   - 生成落地页修改建议。
   - 复盘转化结果。

验收：

- 用 KUN 跑一个真实 Mission。
- 至少跨 3 天。
- 至少包含 1 个外部动作 dry-run / draft。
- 至少产生 1 条可复用策略经验。
- NUO 能解释成本、风险、能力边界。

## 3. 推荐开发批次

### Batch V3-A：WorldGateway 最小真实执行

- handler registry。
- `local_file.write`。
- `email.draft`。
- `webhook.post_dry_run`。
- NUO 显示 handler 支持状态。

### Batch V3-B：Mission 长周期任务

- Mission/Task/Action 收口。
- resume worker。
- checkpoint。
- scheduler。
- failure reaper。

### Batch V3-C：主体验

- 首页任务详情卡。
- 待确认统一入口。
- NUO 四入口。
- 高级面板折叠。

### Batch V3-D：Strategy Pack + MoE

- Strategy Pack registry。
- Watchtower 消费。
- task_type → metric activation。
- memory/meta-decision 参与策略选择。

### Batch V3-E：学习闭环

- idle-batch 蒸馏。
- 遗忘/衰减。
- capability_card 强接路由。
- 路由策略 shadow/canary。

### Batch V3-F：生产 dogfood

- auth/tenant。
- secrets。
- CI/release。
- backup/restore。
- 真实运营 Mission 验收。

## 4. 当前最优下一步

下一步不要先做复杂自进化，也不要先做支付。

最优路线：

1. 做 `WorldGateway handler registry`。
2. 接 `local_file.write` 和 `email.draft`。
3. 让 pending action approve 后真的执行这两类低风险动作。
4. 把执行结果写回 StateLedger / Event / NUO。

原因：

- KUN 的核心是解决真实问题。
- 外部动作不通，长周期运营只能停留在计划。
- 先从低风险动作开始，安全、可测、能快速 dogfood。
