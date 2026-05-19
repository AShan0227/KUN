# KUN V6 开发执行方案

> 文档性质：开发路线、代码对齐、验收清单。本文只描述要开发成什么，不记录讨论过程。

## 1. 开发目标

把 KUN V6 产品方案落成真实运行系统。用户给出复杂目标后，KUN 能形成任务方案、排队执行、长期监督、调用启和傩、处理污染和阻断、交付可验收结果，并把经验通过启沉淀为可验证能力。

北极星不变：

```text
交付结果好 -> 在结果好的基础上速度快 -> 在结果好且速度可接受的基础上成本低
```

开发硬规则：

- Control Plane 是唯一主线，所有长任务状态、队列、账本、门禁、进度报告都归它统一管理。
- 启 Qi 负责实验、复测、回放、能力提取和能力验证。
- 傩 Nuo 负责污染检测、健康诊断、风险治理和系统阻断判断。
- KUN Runtime 负责使用已验证能力执行真实任务。
- 高风险外部动作必须有权限、审批、幂等、审计和回滚。
- 没有测试、证据、账本和恢复路径的功能不能算完成。
- 不能简化已确认的产品标准，不能把方案完成当代码完成，不能把 replay 候选当生产默认能力。
- 每个长周期开发回合必须先回看产品方案和开发方案关键约束，防止目标漂移。
- AB round-03 到 round-10 暂停主动推进；AB 只作为必要回归门禁。真实长任务 dogfood 是主评估路径。
- OpenClaw/Hermes/GPT-5.5 是对照或监督对象，不得作为被优化对象修改。

## 2. 子系统分工

| 子系统 | 长期职责 | 不做什么 |
| --- | --- | --- |
| Control Plane | 任务主线、队列、权限、状态机、账本、进程监督、门禁、回滚、进度报告 | 不承担具体 AB 评分，不直接生成能力候选 |
| 启 Qi | AB 执行、Frontier50 round、互评、报告、gap、同题复测、replay、holdout、shadow、canary、外部项目经验吸收 | 不绕过 Control Plane 自己推进生产变更 |
| 傩 Nuo | stub/fallback/误路由/timeout/EOF/wrapper/report/review 缺失检测，健康诊断，污染结论，风险治理 | 不当执行器，不把污染误算成 agent 能力失败 |
| KUN Runtime | 使用通过验证的能力完成真实任务，产出交付物、证据、测试、评审 | 不在生产路径随意自改能力 |
| Human / External Worker | 审批、专家输入、外部执行、人工评审 | 不被计为 KUN 自动能力，必须通过协作票据记录 |

阶段性规则：

- 当前 Frontier50 AB 阶段可以只修 KUN，不修 OpenClaw/Hermes/GPT-5.5。
- 长期不保留“KUN-only 修复”作为一级能力系统；它会演变为启的能力进化系统。
- 启的能力来源包括 AB gap、真实任务复盘、企业项目、开源项目和外部专家经验。
- OpenClaw/Hermes 只能作为能力样本和源码/行为对照，必须提炼为 KUN-native 子系统、协议、测试和可回滚能力晋级，严禁复制粘贴外部实现。
- GPT-5.5 作为监督者，负责评估能力吸收、真实长任务验证和负迁移风险。

## 3. 开发顺序

### 阶段 1：Control Plane 持久化和 API

目标：V6 Control Plane 从内存骨架升级为可恢复主入口。

开发内容：

- 新增持久化接口，覆盖 `Mission`、`TaskPlan`、`ExecutionContract`、`WorkItem`、`RunRecord`、`ArtifactRecord`、`ArtifactManifest`、`LedgerEvent`、`GateEvaluation`、`CollaborationTicket`、`AcceptanceReview`、`CapabilityProfile`。
- 提供内存实现和数据库实现边界，测试先覆盖内存实现，数据库实现接现有 SQLAlchemy/ORM。
- 增加 Control Plane API：创建任务、查看任务、列出 ready work item、记录 runner 结果、读取进度报告。
- 所有状态变更必须经过 V6 状态机。

验收：

- 重启后关键状态可恢复。
- 没有批准的 `TaskPlan` 和 `ExecutionContract` 不能进入执行。
- 没有 `GateEvaluation` 和 `ArtifactManifest` 不能 ready to deliver。
- 单测、类型检查、风格检查通过。

### 阶段 2：Supervisor、心跳、超时和恢复

目标：KUN 能长期运行，不靠人盯终端。

开发内容：

- 常驻 supervisor / daemon 进程。
- 自动醒来扫描 ready work item。
- runner 注册和 lease 协议。
- heartbeat、timeout、retry、cancel、resume。
- 进程崩溃恢复、断电/重启恢复、跨天续跑。
- 卡死检测和失败分类。
- timeout、EOF、wrapper/auth/tool missing 进入系统阻断路径。
- 自动生成 repair、rollback、retest、plan_change work item。
- 定时生成用户可读进度汇报。
- 每次运行写 `RunRecord` 和 `LedgerEvent`。

验收：

- 卡住任务能被发现。
- 超时先判环境/工具阻断，不直接算 KUN 能力失败。
- 恢复后能继续同一任务方案或触发计划变更。
- daemon 停止、重启、跨天恢复后能继续正确下一步。
- 用户无需手动盯终端或手动重跑同一任务。

### 阶段 3：启 Qi AB Runner 接入

目标：Frontier50 成为 Control Plane 下的真实长任务。

开发内容：

- 封装 Frontier50 round 为 Qi WorkItem。
- 自动执行回答、互评、报告、gap 分析、KUN-only 修复、同题复测。
- round 输出统一为 `ArtifactManifest`。
- 20 个回答、45 个互评、report、health、repair tickets 都进入 artifact/ledger。
- KUN 未过门禁时生成 repair work item。
- KUN 过门禁才推进下一轮。

验收：

- round 状态在 Control Plane 可见。
- comparator 不健康时 round invalid，不计算 agent 排名。
- 同题复测通过后才允许下一轮。
- 不修改 OpenClaw/Hermes/GPT-5.5。

### 阶段 4：傩 Nuo 污染检测和健康治理

目标：系统污染和环境阻断不再误判为 KUN 能力失败。

开发内容：

- 检测 stub echo、fallback、family routing 误路由。
- 检测 timeout、network EOF、unexpected EOF、wrapper missing、wrapper 版本或接口变更、auth failure、permission denied、tool schema mismatch。
- 检测 report 缺失、review 缺失、互评数量不足、comparator unhealthy。
- 建立真实污染样本库和 fixture，覆盖 EOF、timeout、auth、wrapper 变更、stub、fallback、误路由、报告缺失、互评缺失、网络阻断。
- 输出 `NuoHealthFinding` 和污染 GateEvaluation。
- 触发 repair/rerun/rollback/pause。

验收：

- 污染 round 自动标记 invalid。
- 修复动作和重跑结果可追溯。
- 污染不进入能力失败统计。
- 每类污染样本都有分类、修复建议、重跑路径和单测。

### 阶段 5：启 Qi 能力进化系统

目标：从“修一题”升级为“吸收经验并验证能力”。

开发内容：

- 定义 `CapabilityCandidate`、`CapabilityEvaluation`、`CapabilityPromotion`。
- 输入来源：AB gap、真实任务复盘、企业项目、开源项目、外部专家经验。
- 通过 replay、holdout、shadow、canary、production 晋级。
- 傩检查来源污染、过拟合和风险。
- Control Plane 记录批准、版本、回滚。
- KUN Runtime 只消费通过验证的能力。
- 能力库必须治理去重、合并、来源版本、superseded profile 和 rollback 边界。
- production 能力必须编译为 `CapabilityExecutionPolicy`，并被 daemon、planner、runner、supervisor、diagnostics、approval、context 或 evaluation 路径实际消费。
- replay 能力档案必须继续推进到 holdout、shadow、canary、production 的代码路径。
- 能力晋级必须绑定真实长任务验证和回归门禁。
- OpenClaw/Hermes 能力样本必须在源码/行为对照后进入 Qi 候选和晋级流程。

验收：

- 没有 replay/holdout 证据不能进生产能力库。
- 能力晋级必须有证据、回归、回滚。
- 失败能力可以自动回滚。
- KUN Runtime 默认只加载 production 阶段能力，不加载 replay 候选。
- `runtime_enabled=true` 只对 production 有效；review_only、replay、holdout、shadow、canary 都不能显示成默认启用能力。
- 默认 runtime profile 必须先治理去重；重复 OpenClaw/Hermes 样本必须合并、降级或作为证据保留。
- production profile 必须生成可审计 `CapabilityExecutionPolicy`，并在执行路径中产生 capability policy binding artifact。
- GPT-5.5 或等价监督者对晋级结果、负迁移和复杂度给出评审记录。

### 阶段 6：人机协同和外部人员调度

目标：该问人时问得准，问完能继续。

开发内容：

- `CollaborationTicket` API。
- 用户审批、专家输入、外部 worker、人工 review 分类型。
- SLA、超时 fallback、推荐选项和风险说明。
- 人回复后恢复对应 work item。
- 高风险动作必须通过审批。

验收：

- 用户看到的是明确问题，不是日志。
- 人一回复，Control Plane 自动恢复执行。
- 高风险动作不会静默执行。

### 阶段 7：进度报告和产品化表面

目标：用户只和 KUN 对话，KUN 自己汇报进度。

开发内容：

- Mission progress API。
- AB round progress API。
- 阻塞点、下一步、风险、质量门禁、成本、耗时。
- 自动生成阶段报告。
- 普通用户可读任务驾驶舱 UI。
- 展示进度、风险、下一步、需不需要人确认、交付物位置、质量门禁、阻断原因、恢复动作、验收状态。
- 展示后台 supervisor / daemon 健康、最近 heartbeat、恢复状态和等待票据。
- 前端或 API 输出对非技术用户友好，不能只是工程日志。

验收：

- 用户能看到当前跑到哪、为什么卡、下一步是什么。
- 不需要手动翻终端。
- 报告能回溯每次决策和产物。
- 任务驾驶舱能通过测试或可视化验证，普通用户能不看终端判断任务是否健康。

### 阶段 8：外部能力样本生产化

目标：把 OpenClaw/Hermes 的优势变成 KUN 生产默认运行时能力，而不是停留在 replay 候选。

开发内容：

- 读取 OpenClaw/Hermes 源码、文档和行为轨迹。
- 对照执行过程和工程化结构，提炼任务理解、执行习惯、状态组织、工具边界、恢复策略、多 worker 协同、上下文管理、日志/诊断、后台运行、审批恢复。
- 将有效能力转成 KUN-native 子系统、协议、测试、CapabilityCandidate 和 CapabilityProfile。
- 重复、复杂化或不符合奥卡姆剃刀的能力必须合并、降级或舍弃。
- 通过真实长任务 dogfood、holdout、shadow、canary 和 rollback readiness 后，才能进入 production。
- production 能力进入默认运行时后，必须通过能力治理和 `CapabilityExecutionPolicy` 影响真实执行模块，而不是只进入档案列表。
- GPT-5.5 监督能力对照、晋级、真实任务验证和复杂度取舍。

验收：

- 不复制粘贴 OpenClaw/Hermes 实现代码。
- 每个保留能力都有源码/行为引用、KUN-native 设计、测试、回归、真实任务验证和回滚。
- KUN Runtime 生产默认路径能消费通过 production 晋级的能力。
- 能力消费路径能覆盖 planner、worker_distribution、runner、supervisor、diagnostics、approval、context 或 evaluation 中的相关模块。
- 重复 profile 有 governance decision，保留 profile 有 source version 和证据优势说明。
- 被舍弃或合并的能力有明确原因。

### 阶段 9：真实长任务 dogfood

目标：证明 KUN 能 7x24 解决真实问题，而不是只在 AB 题上表现好。

开发内容：

- 选择真实业务长任务作为主评估路径。
- 覆盖计划、分发、执行、外部信息获取、多 worker 合并、污染恢复、权限审批、报告、交付、验收和学习写回。
- GPT-5.5 监督任务方案对齐、执行质量、恢复质量和最终交付。
- AB 只作为回归门禁，不主动推进 round-03 到 round-10。

验收：

- 任务跨小时、跨天或重启后可恢复。
- 人参与时通过 CollaborationTicket 闭环。
- 交付物通过 GateEvaluation 和 AcceptanceReview。
- 真实长任务复盘能进入 Qi 能力治理。

### 阶段 10：代码整理、边界审计和提交

目标：把产品化改动整理成可维护、可审计、可提交的工程状态。

开发内容：

- 审阅当前改动边界。
- 分离正式代码、测试、fixture、dogfood 状态文件和实验输出。
- 清理重复代码、临时脚本、无消费者字段和不符合奥卡姆剃刀的抽象。
- 运行相关测试、lint、类型检查和关键回归。
- 准备提交或 PR，除非用户另有指示。

验收：

- git diff 边界清楚。
- 实验状态文件没有混入正式代码路径。
- 测试、lint、自检通过。
- PR 或提交说明能解释功能、验证、风险和回滚。

## 4. 并行开发切分

第一批并行：

- A：Control Plane store/API。
- B：Supervisor/lease/heartbeat/timeout。
- C：Qi AB runner contract。
- D：Nuo contamination detector contract。

第二批并行：

- E：Qi capability evolution。
- F：Collaboration ticket API。
- G：Progress report API/UI。
- H：Frontier50 end-to-end fixture。

第三批并行：

- I：daemon 常驻执行和恢复验证。
- J：任务驾驶舱 UI。
- K：OpenClaw/Hermes 源码/行为对照和能力生产化。
- L：Nuo 污染样本库和修复复测 fixture。
- M：真实长任务 dogfood 和 GPT-5.5 监督。
- N：代码边界审计、整理、提交/PR。

合并规则：

- 每个切片必须有自己的测试。
- 每个切片只能写自己的模块，跨模块变更先通过 Control Plane 接口。
- 合并前必须跑相关测试、ruff、mypy。
- 合并后必须跑 Control Plane 相关测试集。

## 5. 全链路验收场景

必须覆盖：

1. 用户提交复杂任务 -> 生成 TaskPlan -> 执行合同 -> work item 入队。
2. worker 正常完成 -> ArtifactManifest -> GateEvaluation -> 交付。
3. worker timeout -> 傩判系统阻断 -> repair -> 同题重跑。
4. Frontier50 round -> comparator healthy -> KUN 未过门禁 -> Qi 生成 repair -> 同题复测。
5. Frontier50 round -> comparator unhealthy -> round invalid -> 修 wrapper/网络/路由 -> 重跑。
6. 人类审批缺失 -> CollaborationTicket -> 等待 -> 回复后恢复。
7. 能力候选 -> replay/holdout -> canary -> production -> rollback。
8. 任务跨重启恢复。
9. daemon 自动醒来 -> 获取 ready work item -> 崩溃后恢复 -> 定时汇报。
10. 任务驾驶舱 -> 普通用户看懂进度、风险、下一步、交付物和验收状态。
11. OpenClaw/Hermes 能力样本 -> 源码/行为对照 -> KUN-native 能力 -> 真实长任务验证 -> production runtime。
12. Nuo 污染样本 -> 自动分类 -> 修复建议 -> 同题复测 -> 不计 KUN 失败。
13. 真实长任务 dogfood -> 计划 -> 执行 -> 恢复 -> 合并 -> 交付 -> 验收 -> 学习写回。
14. production capability -> 能力治理去重 -> CapabilityExecutionPolicy -> daemon/runner/supervisor 绑定 -> progress artifact 可审计。

## 6. 完成定义

整个开发完成必须同时满足：

- Control Plane 是长任务默认入口。
- Qi AB 能在 Control Plane 下自动跑轮次。
- Nuo 能系统化识别污染并触发修复。
- supervisor 能监控、恢复、重试、回滚。
- daemon 能常驻后台自动醒来、拿任务、崩溃恢复、跨天续跑和定时汇报。
- 人机协同可见、可恢复。
- 任务驾驶舱对普通用户可读、可追溯，不需要翻终端。
- 能力进化由启主导，并有 replay、holdout、shadow、canary、production、治理和回滚。
- KUN Runtime 默认能力库边界清楚：非 production 不启用，production 经治理去重后编译为执行策略并被真实执行路径消费。
- OpenClaw/Hermes 能力样本完成 KUN-native 化、真实长任务验证和生产默认运行时集成。
- Nuo 污染样本库覆盖 EOF、timeout、auth、wrapper、stub、fallback、误路由、报告缺失、互评缺失和网络阻断。
- 真实长任务 dogfood 作为主验收路径通过，AB 后续轮次暂停，只保留回归门禁。
- 代码边界清楚，实验状态文件和正式代码没有混杂，并完成提交或 PR 准备。
- 所有关键路径有测试。
- 不把速度和成本作为质量失败的抵消项。
