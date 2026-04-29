# KUN-V3 产品方案

> 核心目标：鲲不是普通 AI 助手，而是一个能用最佳策略解决真实世界问题的系统。
> 用户给目标，鲲负责拆解、调度、执行、监控、交付、复盘和成长。

## 0. 开发前声明

V3 开始，以下情况一律不算完成：

- 只加字段，没有任何主流程读取。
- 只 emit 事件，没有任何模块消费。
- 只写接口，没有执行路径接入。
- 只写文档，没有测试证明。
- 只说“后续启会学”，但没有反馈写回或候选策略产生。
- 只做 demo，不能在真实 orchestrator / runtime / API 中跑。

真正完成必须满足四件事：

1. 有清楚的产品目的。
2. 有代码接入主流程。
3. 有测试覆盖关键路径。
4. 有诚实边界：哪些做了，哪些没做，不能把占位说成闭环。

## 1. 一句话定位

KUN 是一个“真实问题解决系统”：

> 守望根据任务类型，从策略包里稀疏激活最合适的 context、skill、模型、评估和外部动作，用最小资源解决真实问题，并把执行经验写回系统，让下次更优。

## 2. 产品第一性原则

1. 先解决问题，再谈自进化。
2. 自进化必须让用户感知到成长，但不能抢走交付主线。
3. 效果第一，速度第二，成本第三。
4. 守望负责决策，不亲自下场执行。
5. 所有重要决策必须可追溯：为什么这么选，依据是什么，结果怎么样。
6. 前端交互要简单，内部系统可以复杂。
7. 对真实世界有影响的动作必须可授权、可审计、可回滚或可补偿。

## 3. 用户看到的产品形态

用户不应该先看到一堆技术词。用户看到的是：

- 目标：我想达成什么。
- 进度：现在做到了哪一步。
- 风险：哪里可能出问题。
- 成本：花了多少资源。
- 待确认：哪些动作需要我拍板。
- 结果：交付了什么。
- 成长：这次鲲学到了什么，下次会怎么更好。

内部可以叫 Watchtower、Hermes、Qi、Protocol、NUO。用户侧要降维：

- Watchtower = 守望
- Hermes = 格式转换 / 沟通适配
- Qi = 学习引擎
- Protocol = 做事方法
- NUO = 系统管家

## 4. 三层任务抽象

V3 不把 TASK.md 当产品核心，而把它当工程核心。

### Mission

长期目标。比如：

- 运营一个产品实现商业化。
- 做一个长期内容增长系统。
- 建立外部客户获取链路。

Mission 的特点：

- 周期长。
- 开始时不一定能准确估预算。
- 需要持续反馈、调整和复盘。

### Task

阶段任务。比如：

- 做首批用户访谈。
- 生成冷启动获客清单。
- 搭建官网转化页。

Task 的特点：

- 有相对清楚的目标。
- 可以评估风险和成本。
- 可以记录执行过程。

### Action

具体动作。比如：

- 给 20 个潜在客户发邮件。
- 调一个 API。
- 写一个页面。
- 查一批竞品资料。

Action 的特点：

- 可执行。
- 可审计。
- 有些动作会影响真实世界，需要授权。

## 5. V3 最重要的新核心：系统级 MoE

传统 MoE 是模型内部选择部分专家。鲲要做的是系统级 MoE：

- 任务来了，不唤醒所有能力。
- 守望判断任务类型。
- 命中一个或多个策略包。
- 只激活相关 context、skill、评估指标和风险规则。

例子：教育任务不需要唤醒商业化、代码审查、财务风控。它应该唤醒：

- 教育方法论。
- 学习路径记忆。
- 课程规划 skill。
- 理解度评估。
- 复习价值评估。

如果教育任务突然出现付款、合同、合规，就触发偏离预警，守望介入。

## 6. Strategy Pack：策略包

策略包是 V3 的核心资产。

每个策略包包含：

- 适用场景。
- 触发关键词。
- 默认方法论。
- 默认 context 标签。
- 默认 skill hints。
- 默认模型档位。
- 默认评估指标。
- 默认风险规则。
- 默认写回规则。

第一批内置策略包：

- default：兜底任务。
- education：教育、学习、课程、训练。
- coding：代码、调试、审查、测试。
- commercialization：商业化、获客、定价、增长。
- product_ops：产品运营、用户反馈、留存。
- external_collab：外部协作、邮件、企业、人类协作者。
- data_analysis：数据分析、指标、报表。

策略包不是写死的。启可以实验新策略包，但进入生产必须经过守望的影子测试、金丝雀和回滚。

## 7. Watchtower Decision Plane：守望决策层

守望不做业务执行。守望负责生成决策单。

决策单必须包含：

- 命中的策略包。
- 使用的执行模式。
- context 拉取深度。
- skill hints。
- 激活的评估指标。
- reward_weights。
- 需要盯的风险。
- 偏离预警规则。
- 决策理由。

守望决策必须被执行层真实消费。否则就是伪功能。

## 8. 统一评分系统

评分系统分两层。

### 全局基础指标

所有任务都轻量记录：

- 成功率
- 成本
- 耗时
- 风险
- 可撤回性
- 用户满意度
- 异常度
- 复用价值

### 策略包专属指标

只在相关任务激活。

教育任务：

- 理解度
- 难度递进
- 知识点覆盖
- 复习价值

代码任务：

- 测试通过率
- 可维护性
- 类型安全
- 回归风险

商业化任务：

- 获客可能性
- 成本收益比
- 转化路径
- 外部依赖风险

产品运营任务：

- 用户价值
- 增长潜力
- 留存影响
- 执行成本

## 9. State Ledger：状态账本

runtime state、events、blackboard、Panorama、NUO panel 本质都在回答：

> 鲲现在到底在干什么，为什么这么干，干到哪了。

V3 要统一成状态账本：

- 当前目标。
- 当前任务。
- 当前步骤。
- 当前风险。
- 当前预算。
- 当前模型 / skill / context。
- 当前决策理由。
- 当前待确认事项。

工程实现要分三层：

- 当前状态表：执行时快速读取。
- 事件日志：审计、回放、复盘。
- 快照视图：给用户和 LLM 消费。

不要为了可追溯拖慢执行。热路径读当前状态，冷路径读事件日志。

## 10. Hermes：全链路沟通适配

Hermes 不只是最终回答美化。

Hermes 有两个职责：

### 表达适配

- 对用户：大白话。
- 对 LLM：结构化 context。
- 对 skill：输入输出契约。
- 对 API：严格字段。
- 对外部 agent：协议包。
- 对企业/人类协作者：邮件、表单、报告、审批单。

### 信息密度控制

- 用户只看结论、风险、下一步。
- 执行模型看目标、约束、上下文。
- skill 只看所需字段。
- 外部 agent 只看授权范围内的信息。
- API 只看参数。

## 11. World Gateway：真实世界网关

真实世界交互必须独立出来。

World Gateway 负责所有影响外部世界的动作：

- 发邮件。
- 发消息。
- 调 API。
- 操作浏览器。
- 操作电脑。
- 支付。
- 发布内容。
- 联系人类。
- 联系企业。
- 联系外部 agent。

关系如下：

- 守望决定该不该做。
- Orchestrator 决定什么时候做。
- Hermes 决定怎么表达。
- World Gateway 真正执行外部动作。

World Gateway 必须有：

- 身份。
- 权限。
- 密钥管理。
- 审计。
- 风险等级。
- 用户确认。
- 可撤回判断。
- 补偿动作。

## 12. 三层记忆写回

记忆不能每个模块自己乱写。

V3 统一三层：

### 任务结果记忆

这件事最后成没成，交付质量如何。

### 执行过程记忆

用了什么路径，哪里卡住，哪些 skill 有效。

### 元决策记忆

为什么守望选这个模型、这个策略包、这个 skill、这个验证强度。

第三层最值钱，因为它让鲲越来越会选择。

## 13. NUO 和 Qi 的关系

NUO 是管家，负责看健康、成本、权限、风险、异常、待确认。

Qi 是学习引擎，负责实验、探索、新策略、新协议、新 skill 组合。

用户应该感知 Qi 的成长，但不要被实验过程打扰。

用户看到的应该是：

- 鲲这次学到了什么。
- 哪个策略变好了。
- 下次能省多少。
- 哪类任务成功率提高了。
- 哪些实验没通过，所以没有进生产。

## 14. 第一批 V3 开发优先级

### V3-1：守望决策层 + 策略包

目标：让 Strategy Pack 真实影响执行模式、context 深度、skill hints、指标维度。

验收：

- orchestrator 会调用守望决策层。
- 决策结果会 emit 事件。
- 决策结果会改变 task_ref.meta.execution_mode。
- 决策结果会改变 context_limit。
- 决策结果会补充 required_skills。
- 有单测证明。

### V3-2：状态账本

目标：统一 RuntimeState、events、Panorama、NUO 视图的数据口径。

第一版落地边界：

- State Ledger 是热视图，不替代 RuntimeStateRow 和 EventRow。
- orchestrator 在任务创建、守望决策、计划生成、运行、单步完成、暂停、结束时写入。
- 黑板 state 数据源读取同一个 State Ledger 快照。
- 黑板提供 `/api/blackboard/state-ledger` 和 `/api/blackboard/state-ledger/{task_id}` 查询当前快照。
- `/api/blackboard/full/{task_id}` 给 LLM 的完整 dump 也带同一份 `state_ledger`。
- 这一版先用内存快照，保证执行链路轻，不加 DB 迁移；持久化账本放后续版本。

验收：

- 每次任务有可查询的状态快照。
- 状态快照包含当前目标、步骤、预算、风险、决策理由。
- 用户 UI 和 LLM 消费同一份底层数据。

### V3-3：Hermes 全链路

目标：不仅最终回答走 adapter，执行过程也走对象适配。

第一版落地边界：

- Hermes Adapter 是一层统一翻译契约，不执行任务。
- orchestrator 给 LLM 的 step prompt 必须由 Hermes 包装成结构化任务包。
- agent_loop 调 skill 前后必须经过 Hermes 适配，不能直接裸传。
- API / external agent / human 的格式转换先复用现有 adapter registry。
- 真正“调 API / 发邮件 / 联系外部 agent”的动作归 World Gateway，不在 V3-3 偷偷执行。

验收：

- LLM step prompt 走 LLM adapter。
- skill 输入走 skill adapter。
- skill 结果走 skill adapter 再回喂 LLM。
- 外部 agent 输出走 A2A adapter。
- API 调用走 REST adapter。
- 有单测证明 orchestrator / agent_loop 真实消费 Hermes，而不是只存在一个类。

### V3-4：三层记忆写回

目标：任务结果、过程、元决策分别写回。

第一版落地边界：

- 三层记忆写入现有 Context AssetStore，不另造孤岛。
- 结果记忆记录 status、验证结果、统一评分、成本、最终答案摘要。
- 过程记忆记录 step、skill、model、成本、输出摘要。
- 元决策记忆记录 Strategy Pack、execution_mode、metric_dimensions、skill_hints、reason。
- 后续 ContextPacker 可以检索这些记忆。

验收：

- task.done 写结果记忆。
- step.completed 写过程记忆。
- watchtower.decision_plan 写元决策记忆。
- 有测试证明写入后的记忆会被 ContextPacker 检索出来。

### V3-5：World Gateway

目标：真实世界动作不再散在普通 tool 里。

第一版落地边界：

- pending action 审批后的执行必须经过 World Gateway。
- World Gateway 负责把动作转成 Hermes 外部格式并写入审计。
- 这一版不假装已经真发邮件、真调 API、真付款；没有外部 handler 时只释放审批 gate。
- payload 里必须明确 `external_dispatched=false`。

验收：

- 外部动作统一登记。
- 高风险动作必须走授权。
- 所有动作有审计记录。
- 没有外部 handler 时，用户/日志能看见“未真正外发”。

### V3-6：统一评分系统

目标：把成功率、成本、耗时、风险、可撤回性、用户满意度、复用价值、异常度统一成一个 Scorecard。

第一版落地边界：

- 所有任务结束时生成 scorecard。
- scorecard 消费真实 runtime、validation、surprise、Watchtower decision。
- scorecard 会进入 capability writeback 的 rubric 来源。
- scorecard 会作为事件和 WebSocket side channel 暴露。

验收：

- 有单测证明 scorecard 由真实 runtime signals 算出。
- orchestrator 会 emit `scorecard`。
- capability writeback 不再只依赖 validation_score。

### V3-7：主交互入口

目标：用户第一眼看到“任务有没有做好”，不是先看复杂节点图。

第一版落地边界：

- 首页仍以对话框为主。
- 对话框上方增加任务看板：运行中、成本、风险、待确认。
- 任务看板读取 State Ledger / Blackboard 同一份数据。
- 节点图和高级诊断不作为第一入口。

验收：

- 前端主页面调用 `/api/blackboard/state`。
- 用户能看到当前目标、步骤、风险、策略、模型/skill、成本。

### V3-8：伪功能审计

目标：专门防止“写了但没人用”的功能继续堆积。

第一版落地边界：

- 提供一份 V3 审计清单。
- 每个核心模块必须标明调用方、影响的决策、消费者、测试。
- 暂不做静态代码全自动分析，先做可维护的人工审计入口。

验收：

- 文档列出 V3-1 到 V3-7 的真实接入点。
- 对未完成或边界保守的地方明确标注。

## 15. V3 最重要的反误区

不要把“功能存在”当“功能参与协作”。

鲲的每个核心模块都要回答：

- 谁调用我？
- 我影响了什么决策？
- 我的输出谁消费？
- 失败时谁知道？
- 结果会写回哪里？
- 用户是否能理解我做了什么？

答不上来，就不是核心能力，只是仓库里的装饰。

## 16. V3 完整交付路线

V3 不是只做一批模块，而是把 KUN 做成一个能真实解决问题的产品。开发顺序必须按依赖走：

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

### P0：诚实和可测试底座

目标：KUN 必须知道自己真实能做什么，不能把占位、事件、字段说成完成。

交付内容：

- `gpt-5.5` Codex MCP 主链路可测。
- `/nuo/health/delivery-status` 显示能力边界。
- 核心能力按“可测 / 半闭环 / 仅审计 / 未就绪”标注。
- 未真实接通的能力不能标 ready。

验收：

- 用户能在 NUO 看到哪些功能已可测、哪些还没接通。
- 每个 ready 能力必须有主流程调用和测试证据。

### P1：外部世界最小真实动作

目标：World Gateway 从“只审计”变成“可控执行一小类低风险动作”。

第一批只做低风险 handler：

- `local_file.write`：只写入受控输出目录，禁止路径穿越。
- `email.draft`：只生成草稿，不真实发送。
- `webhook.post_dry_run`：只渲染请求，不真实联网。
- `browser.plan`：只生成操作计划，不真实点击。

明确不做：

- 支付。
- 转账。
- 公开发布。
- 真正发邮件。
- 真实浏览器提交表单。

验收：

- pending action approve 后，支持的 handler 会真实产生受控产物。
- 不支持的 handler 继续明确 `requires_handler=true`。
- 审计里能看见是否 `external_dispatched`。

### P2：长周期任务骨架

目标：KUN 能承接 Mission，而不是只做一次性问答。

交付内容：

- Mission / Task / Action 数据模型收口。已落第一版 `missions`、`mission_tasks`、`mission_milestones`。
- durable task resume worker。第一版已做 resume request 扫描和可注入 runner 的 worker；默认无 runner 时明确 skipped，不假装自动执行。
- checkpoint / milestone。第一版 milestone 已持久化，checkpoint 先进 JSONB。
- scheduler。已有 cron scheduler，Mission 复盘 job 待接。
- failure reaper。
- State Ledger 持久化快照。

验收：

- 一个 Mission 能挂多个 Task。
- Task 能暂停、审批后恢复到 queued；Mission resume request 能发现 queued task；resume worker 能把请求交给真实 runner 或诚实返回 skipped。
- API 重启后状态不丢。
- 用户能看到 Mission 进展和下一步。

### P3：主体验和 NUO 做减法

目标：用户第一眼看到任务有没有做好，而不是看到复杂技术面板。

首页只保留：

- 对话框。
- 当前任务。
- 当前步骤。
- 当前风险。
- 当前成本。
- 待确认动作。
- 下一步。

NUO 初期只保留四个一级入口：

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

验收：

- 用户 5 秒内知道 KUN 在干什么、卡在哪里、是否需要确认。
- 用户不需要理解 Watchtower / Hermes / Qi / Protocol。

### P4：记忆复用 + 系统级 MoE

目标：KUN 真正越用越会做选择。

交付内容：

- Strategy Pack registry。
- Watchtower Decision Plane 消费 Strategy Pack。
- 按任务类型稀疏激活 context / skill / metric / risk rule。
- 结果记忆、过程记忆、元决策记忆参与下次路由。
- idle-batch 蒸馏方法论。
- 遗忘 / 衰减机制。

验收：

- 同类任务第二次执行时能看到参考了哪些历史经验。
- 决策单里出现命中的 Strategy Pack。
- 不同任务激活不同评估指标。

### P5：生产级 dogfood 和部署

目标：KUN 能被真实拿来运营一个产品，而不是只在本地演示。

交付内容：

- 用户账号。
- 租户 onboarding。
- 密钥管理。
- CI / release / tag。
- 线上监控。
- 备份恢复。
- dogfood 真实 Mission。

验收：

- 用 KUN 跑一个至少跨 3 天的真实 Mission。
- 至少包含一个外部动作 draft / dry-run / 低风险执行。
- 至少产生一条可复用策略经验。
- NUO 能解释成本、风险和能力边界。

## 17. 当前开发切入点

下一步先做 P1，不先碰复杂自进化，也不先碰支付。

优先实现：

1. `WorldGateway handler registry`。
2. `local_file.write`。
3. `email.draft`。
4. `webhook.post_dry_run`。
5. pending action approve 后真实调用这些 handler。
6. 执行结果写回 StateLedger / Event / NUO。

原因：

- KUN 的核心是解决真实问题。
- 外部动作不通，长周期运营只能停留在计划。
- 从低风险动作开始，安全、可测、能快速 dogfood。
