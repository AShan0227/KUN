# KUN V6 完整产品方案

> 文档性质：产品规格、开发对标、测试对标、验收依据。

## 1. 产品定义

KUN 是一个真实问题解决系统。用户给出目标后，KUN 负责理解目标、补齐信息、形成任务方案、获得必要确认、长期执行、调用工具和外部协作者、处理阻断、交付结果、接受验收，并把经验沉淀为下一次更好的能力。

KUN 不是聊天机器人，不是多 agent 展示平台，不是 benchmark 答题器，也不是只会生成方案的助手。KUN 的核心价值是把复杂目标变成可执行、可验证、可恢复、可交付、可学习的真实任务闭环。

### 1.1 北极星

KUN 的指标顺序固定为：

```text
交付结果好
  -> 在结果好的基础上速度快
  -> 在结果好且速度可接受的基础上成本低
```

硬规则：

- 结果质量不达标时，速度和成本不能抵消。
- 只有交付物满足验收标准后，才比较速度。
- 只有结果质量和速度可接受后，才优化成本。
- 任何学习晋级必须证明结果更好，或在结果不下降的前提下速度更快、成本更低。

“交付结果好”包含：

- 准确理解真实业务目标。
- 主动补齐缺失信息。
- 拆解、分发、执行、验证、合并。
- 获取、筛选、引用外部信息。
- 跨小时、跨天、跨轮保持状态连续。
- 在阻断、失败、污染、误路由、超时、权限不足时恢复。
- 正确调度用户、operator、reviewer、expert、external worker、工具和外部 agent。
- 明确交付物、证据、风险、剩余问题。
- 接受验收、处理返工，并把经验写回能力系统。

### 1.2 产品边界

KUN 必须做：

- 把用户目标编译成任务方案和执行合同。
- 在信息不足时主动询问、检索、读取资料或请求授权。
- 执行真实任务，而不是停留在建议。
- 对真实外部动作执行权限、审批、审计和补偿。
- 对每个任务保存状态、产物、证据、日志和决策记录。
- 对失败进行分类、修复、回滚、复测。
- 对交付物进行验收闭环。
- 从真实结果、用户反馈、评审和 peer gap 中学习。
- 在长周期执行中持续回看当前任务方案和产品方案，发现目标、范围、证据、风险或验收偏移时主动纠偏。
- 把外部优秀系统作为能力样本，通过源码和行为对照提炼 KUN-native 能力、测试和晋级路径。

KUN 不应做：

- 用 agent 数量冒充能力。
- 用 prompt 约束冒充工程约束。
- 用结构化答案冒充真实交付。
- 用外部脚本长期代替内置控制面。
- 在没有 artifact、gate、验收的情况下宣布完成。
- 将外部人或操作者的执行能力记为 KUN 自动能力。
- 让自我学习绕过 replay、holdout、shadow、canary 和 rollback。
- 把 replay 候选误当成生产默认能力。
- 把 AB 或 benchmark 当成真实长任务产品化的替代品。
- 简化已确认的产品标准来换取短期进度。

## 2. 产品原则

### 2.1 意图识别和信息补齐先行

复杂任务必须先做意图识别、任务类型判断和信息完整性判断。信息不足时，KUN 必须先主动询问、检索、读取材料或请求授权，不能先生成看似完整但基于缺口的执行方案。

当关键信息足够支撑执行边界后，KUN 才生成任务方案。任务方案不是展示文档，而是运行时对象。后续拆解、执行、评估、合并、交付和返工都必须引用当前任务方案版本。

### 2.2 状态可恢复

任何长任务都必须能解释当前状态、上一步发生了什么、下一步是什么、卡在哪里、谁负责、需要什么输入、如何恢复。

### 2.3 证据可追踪

交付结果必须能追溯到产物、证据、日志、测试、评审或外部输入。没有 artifact 引用的“完成”只能是 partial。

### 2.4 人机协同是能力

人类参与不是失败。错误是该问不问、不该问乱问、问了不记录、人回复后不能恢复。

### 2.5 外部动作必须受控

任何真实外部动作必须通过统一策略和权限协议，具备权限、审批、幂等、审计、回滚或补偿。

### 2.6 学习必须可验证

KUN 可以自我学习，但学习候选必须经过 replay、holdout、shadow、canary 和 rollback。benchmark 提升只能生成学习候选，不能直接触发生产晋级。

### 2.7 奥卡姆剃刀

一个功能只有在满足以下条件时才进入主线：

- 有明确输入和输出。
- 有明确消费者。
- 影响真实决策。
- 失败时有人或系统知道。
- 数据能进入复盘和学习。
- 不能被已有子系统或协议简单覆盖。

否则应删除、合并或降级为调试能力。

### 2.8 方案对齐防跑偏

KUN 处理复杂长任务时，必须把任务方案和产品方案当作运行时约束，而不是一次性文档。

执行要求：

- 启动复杂任务前必须先完成意图识别和信息完整性判断。
- 信息不足时必须主动询问、检索、读取材料或请求授权，不得先编造完整任务方案。
- 关键信息补齐后，才形成可执行任务方案。
- 执行中发现外部信息、风险、成本、验收或路径变化时，必须更新任务方案或触发计划变更。
- 每个 WorkItem、GateEvaluation、交付包和返工都必须能追溯到任务方案版本。
- 长任务恢复后必须先重建当前任务方案、WorkingContext、等待项和下一步，不得凭临时记忆继续。
- 系统不能把“方案写完”当成“代码完成”或“任务完成”。

### 2.9 外部能力样本 KUN-native 化

KUN 可以学习 OpenClaw、Hermes、企业项目、开源项目和外部专家经验，但只能把它们作为能力样本。

要求：

- 必须读取和对照外部系统源码、文档、行为轨迹、运行习惯和工程边界。
- 提炼对象是任务理解、执行习惯、状态组织、工具边界、恢复策略、多 worker 协同、上下文管理、日志诊断、后台运行、审批恢复等系统行为。
- 产物必须是 KUN-native 子系统、协议、测试、能力候选和可回滚晋级记录。
- 严禁复制粘贴外部实现代码。
- 如果外部能力与 KUN 现有系统重复、增加复杂度或不符合奥卡姆剃刀，必须合并、降级或舍弃。
- 外部能力进入生产默认运行时前，必须经过 replay、holdout、shadow、canary、production promotion 和 rollback readiness。
- GPT-5.5 或等价监督者必须作为独立评审者，对能力吸收、真实长任务验证和负迁移风险给出监督结论。

## 3. 产品级闭环

KUN 的标准任务闭环如下：

```text
1. Intake：接收用户目标
2. Intent Triage：识别意图、任务类型、风险等级和必要信息
3. Info Gap：信息不足时主动询问、检索、读取材料或请求授权
4. Plan：关键信息足够后生成任务方案
5. Align：对齐任务方案并获得必要确认
6. Contract：生成执行合同
7. Decide：选择策略、风险等级、资源和执行路径
8. Decompose：拆解为可执行工作项
9. Queue：进入持久队列
10. Execute：由 supervisor 调度工具、worker、外部 agent 或人
11. Observe：记录状态、证据、日志、成本、风险
12. Evaluate：按北极星和任务类型 rubric 生成 GateEvaluation
13. Repair：修复、回滚、改计划、复测或转人工
14. Merge：合并多 worker、证据、测试和评审输出
15. Deliver：编译可验收交付物
16. Accept：用户或 reviewer 验收、返工、partial 或拒绝
17. Learn：评估、信用分配、能力写回
18. Govern：系统健康治理、瘦身、权限和成本管理
```

Plan Alignment 贯穿 4 到 18。Intent Triage 和 Info Gap 是任务方案前置门禁；只要目标、验收、风险、成本、证据或执行路线偏离当前任务方案，就必须触发计划变更协议。

### 3.1 七个必须闭合的回路

| 回路 | 第一性问题 | 闭环路径 |
| --- | --- | --- |
| 目标回路 | 用户到底要什么结果？ | 目标 -> 意图识别 -> 信息补齐 -> 任务方案 -> 确认 -> 执行合同 |
| 执行回路 | 任务是否真的被推进？ | 工作项 -> 队列 -> supervisor -> 产物 -> 账本 |
| 证据回路 | 结果凭什么可信？ | 证据计划 -> 来源获取 -> 证据 artifact -> GateEvaluation -> 交付 |
| 纠错回路 | 失败后能否恢复？ | 失败归因 -> 修复/回滚/改计划 -> 队列 -> 复测 |
| 协同回路 | 何时需要人或外部资源？ | 协同票据 -> 决策或输出 -> 写回 -> 恢复执行 |
| 验收回路 | 交付是否真的被接受？ | 交付 -> 验收 -> accepted / rework / partial / rejected -> 后续动作 |
| 学习回路 | 下次能否更好？ | GateEvaluation -> 信用分配 -> 学习候选 -> 验证 -> 能力治理 |

任一回路缺少对象、消费者、门禁或恢复路径，都不能宣称 KUN Control Plane 完整。

## 4. 五个一级子系统

KUN V6 保留五个一级子系统。通信、上下文、评估、预算、权限、审计、恢复等不再拆成独立一级子系统，而作为全系统运行时协议。

### 4.1 任务方案系统

职责：

- 接收用户目标。
- 编译上下文、材料、历史记忆和偏好。
- 识别意图、任务类型、风险等级和信息缺口。
- 在信息不足时主动补问、检索、读取材料或请求授权。
- 在关键信息足够后生成和版本化任务方案。
- 请求必要确认。
- 生成执行合同。
- 管理计划变更。

核心输出：

- `TaskPlan`
- `ExecutionContract`
- `WorkItem(type=plan_change)`
- `LedgerEvent(type=decision)`

门禁：

- 信息缺口未处理时，不得生成可执行方案、执行合同或进入长期执行。
- 未获得必要确认时，只能推进被明确授权且低风险的部分。
- 计划变更必须说明原因、影响、选项、需要确认的人和旧工作项处理方式。

### 4.2 知识与证据系统

职责：

- 读取和整理内部资料。
- 查询外部信息。
- 判断来源质量、时效性和冲突。
- 生成证据 artifact 和 missing reason。
- 生成面向不同消费者的 `WorkingContext`。
- 管理记忆调用、压缩、遗忘和写回候选。

核心输出：

- `WorkingContext`
- `ArtifactRecord(kind=evidence | source | context)`
- `ArtifactManifest`
- `LedgerEvent(type=context_refresh)`

门禁：

- 研究型任务无证据不得 ready。
- 过期、不可访问、冲突来源必须显式标记。
- 高风险结论必须优先使用主源或可验证来源。
- 长任务的当前上下文必须可追踪、可刷新、可判定过期。

### 4.3 执行控制面

职责：

- 管理持久队列和调度策略。
- 启动和监督 worker、工具、命令、browser、外部 agent。
- 管理 lease、heartbeat、timeout、retry、cancel、resume。
- 作为常驻 supervisor / daemon 自动醒来、自动拿任务、跨进程恢复和跨天续跑。
- 识别环境故障、工具故障、污染输出、fallback、权限失败。
- 执行策略、权限、预算和门禁协议。
- 生成修复、回滚、复测、治理类工作项。

核心输出：

- `WorkItem`
- `RunRecord`
- `GateEvaluation`
- `ArtifactManifest`
- `LedgerEvent`

门禁：

- 无 idempotency key 的外部动作不得执行。
- 超时、EOF、wrapper missing、auth failure 先按环境阻断处理，不直接算 KUN 能力失败。
- 任何执行结束必须写入 artifact manifest 和 ledger。
- 后台进程崩溃、断电、重启或跨天恢复后，必须从持久状态继续，而不是要求用户手动重跑。
- 策略、权限和门禁的 runtime owner 是执行控制面；能力治理系统负责审计和改进策略。

### 4.4 协同交付系统

职责：

- 管理用户、operator、reviewer、expert、external worker。
- 生成上下文清楚、选项明确、可恢复的协同票据。
- 管理外部人员 SLA、提醒、升级、替代、取消、降级。
- 展示当前任务状态、风险、成本、等待项和下一步。
- 提供普通用户可读的任务驾驶舱，展示进度、风险、下一步、需不需要确认、交付物位置、质量门禁、阻断原因、恢复动作和验收状态。
- 编译交付包。
- 管理验收和返工。

核心输出：

- `CollaborationTicket`
- `AcceptanceReview`
- `ArtifactManifest(kind=delivery)`
- `LedgerEvent(type=message | approval | acceptance)`

门禁：

- 该问人时必须问。
- 不该问人时不得反复打断。
- 待回复协同必须有 deadline、升级路线和恢复规则。
- 交付物必须引用任务方案版本、产物、证据、测试、风险和剩余问题。
- 返工必须重新进入队列或触发计划变更。

### 4.5 能力治理系统

职责：

- 聚合 `GateEvaluation`、验收结果、失败归因、成本和返工。
- 做信用分配。
- 识别重复失败、能力退化、成本浪费、无消费者事件、孤立 artifact。
- 管理 replay、holdout、shadow、canary、promotion、rollback。
- 维护能力档案。
- 治理能力库去重、合并、来源版本、替代关系和淘汰原因。
- 将 AB gap、真实任务复盘、企业项目、开源项目、外部专家经验和外部 agent 源码/行为样本转化为 KUN-native 能力候选。
- 管理能力进入 KUN Runtime 生产默认路径前的验证、监督、晋级和回滚。
- 将通过生产晋级的能力编译为 KUN Runtime 可消费的执行策略，而不是只保存在档案中。

核心输出：

- `CapabilityProfile`
- `CapabilityExecutionPolicy`
- `CapabilityExecutionDirective`
- `WorkItem(type=governance)`
- `GateEvaluation(stage=learning | governance)`
- `LedgerEvent(type=promotion | rollback | governance)`

门禁：

- 学习候选不得直接进入生产。
- benchmark 提升不能单独触发晋级。
- 只有证明结果更好，或结果不下降且速度更快/成本更低，才允许晋级。
- 重复失败必须生成治理动作，而不是继续重试。
- replay 阶段只代表“可复测候选”，不得被 KUN Runtime 默认消费。
- production 能力必须有 evidence、holdout、regression、shadow/canary 记录、rollback plan 和监督结论。
- 进入默认运行时前必须先经过能力治理去重，重复、过时或复杂化的 profile 必须合并、降级或舍弃。
- 生产能力必须被编译成 planner、worker_distribution、runner、supervisor、diagnostics、approval、context 或 evaluation 指令，并被实际执行模块读取。

## 5. 核心对象

### 5.1 Mission

顶层任务对象。

关键字段：

- `mission_id`
- `owner`
- `objective`
- `non_goals`
- `task_type`
- `priority`
- `risk_level`
- `status`
- `current_plan_version`
- `execution_contract_ref`
- `working_context_ref`
- `ledger_refs`
- `artifact_manifest_refs`
- `acceptance_ref`

### 5.2 TaskPlan

版本化任务方案。

关键字段：

- `plan_id`
- `mission_id`
- `version`
- `objective`
- `known_facts`
- `unknowns`
- `assumptions`
- `info_gaps`
- `acceptance_criteria`
- `constraints`
- `risk_register`
- `evidence_plan`
- `decomposition`
- `worker_plan`
- `merge_plan`
- `test_plan`
- `rollback_plan`
- `human_confirmation_points`
- `change_log`
- `approval_status`

### 5.3 ExecutionContract

从已对齐任务方案生成的执行边界。

关键字段：

- `contract_id`
- `mission_id`
- `task_plan_version`
- `allowed_actions`
- `forbidden_actions`
- `permissions`
- `budget`
- `deadline`
- `evidence_policy`
- `delivery_contract`
- `risk_policy`
- `rollback_policy`
- `external_worker_policy`
- `approval_policy`

### 5.4 WorkItem

队列中的最小可调度工作单元。

类型：

- `execution`
- `research`
- `review`
- `test`
- `collaboration`
- `external_worker`
- `repair`
- `rollback`
- `retest`
- `plan_change`
- `merge`
- `governance`

关键字段：

- `work_item_id`
- `mission_id`
- `task_plan_version`
- `type`
- `owner`
- `dependencies`
- `priority`
- `resource_locks`
- `lease`
- `heartbeat`
- `timeout`
- `retry_budget`
- `idempotency_key`
- `expected_output`
- `artifact_manifest_ref`
- `status`

### 5.5 RunRecord

一次 worker、工具、agent、命令或人类协作运行。

关键字段：

- `run_id`
- `work_item_id`
- `runner_type`
- `runner_identity`
- `started_at`
- `ended_at`
- `exit_status`
- `stdout_ref`
- `stderr_ref`
- `cost`
- `failure_category`
- `artifact_manifest_ref`
- `gate_evaluation_ref`

### 5.6 ArtifactRecord

单个可引用产物。

类型：

- `answer`
- `evidence`
- `source`
- `context`
- `log`
- `diff`
- `test_result`
- `review`
- `report`
- `screenshot`
- `decision`

关键字段：

- `artifact_id`
- `kind`
- `path_or_uri`
- `content_hash`
- `created_by`
- `mission_id`
- `work_item_id`
- `access_status`
- `supports`
- `freshness`
- `source_quality`
- `expires_at`

### 5.7 ArtifactManifest

一次运行、一次合并或一次交付的产物清单。

关键字段：

- `manifest_id`
- `mission_id`
- `work_item_id`
- `kind`
- `artifact_refs`
- `primary_artifact_ref`
- `test_refs`
- `evidence_refs`
- `review_refs`
- `created_by`
- `content_hash`
- `supports_delivery`
- `rollback_refs`

### 5.8 LedgerEvent

不可变系统账本事件。Message、Decision、Approval、PlanChange、StateChange 都是强类型 ledger event。

关键字段：

- `event_id`
- `mission_id`
- `sequence`
- `event_type`
- `actor`
- `time`
- `correlation_id`
- `causation_id`
- `subject_ref`
- `before`
- `after`
- `payload`
- `artifact_refs`
- `idempotency_key`
- `replay_hint`

强类型要求：

- `event_type=message` 必须包含 sender、receiver、intent、requires_response、deadline、resume_rule。
- `event_type=decision` 必须包含 options、selected_option、reason、risk_impact、quality_impact、speed_impact、cost_impact、approver。
- `event_type=approval` 必须包含 requested_action、approval_scope、expires_at。

### 5.9 WorkingContext

面向模型、worker、人类 reviewer 或外部协作者的压缩上下文包。

关键字段：

- `working_context_id`
- `mission_id`
- `task_plan_version`
- `audience`
- `scope`
- `summary`
- `critical_facts`
- `acceptance_criteria`
- `constraints`
- `open_questions`
- `risk_flags`
- `artifact_refs`
- `decision_refs`
- `source_hashes`
- `freshness`
- `invalidated_by`
- `omitted_reason`

WorkingContext 不能降级成普通 artifact。它是长任务执行燃料，必须可版本化、可失效、可恢复。

### 5.10 CollaborationTicket

人类、operator、expert、reviewer、external worker 和外部动作协同的统一票据。

类型：

- `user_decision`
- `operator_action`
- `review`
- `expert_input`
- `external_worker`
- `approval`
- `external_action`

关键字段：

- `ticket_id`
- `mission_id`
- `type`
- `role_needed`
- `why_needed`
- `decision_options`
- `recommended_option`
- `context_ref`
- `risk_if_skipped`
- `deadline`
- `sla_policy`
- `escalation_policy`
- `fallback_policy`
- `resume_after_response`
- `output_contract`
- `status`

### 5.11 GateEvaluation

全系统唯一裁判事件。它同时承担评估和门禁动作。

关键字段：

- `gate_evaluation_id`
- `mission_id`
- `task_plan_version`
- `subject_ref`
- `stage`
- `task_type`
- `rubric_version`
- `metric_pack_version`
- `north_star_verdict`
- `result_quality`
- `speed`
- `cost`
- `risk`
- `evidence_quality`
- `collaboration_quality`
- `score_breakdown`
- `hard_gate_failures`
- `evidence_refs`
- `artifact_refs`
- `test_refs`
- `review_refs`
- `source_freshness`
- `evidence_conflicts`
- `failure_category`
- `root_cause`
- `responsibility_scope`
- `confidence`
- `next_action`
- `next_state`
- `next_ticket_refs`
- `learning_eligibility`
- `governance_signal`
- `created_by`
- `ledger_event_ref`

消费规则：

- result_quality 是硬门禁，没过线时速度和成本不得补偿。
- 不同 task_type 使用不同 rubric，但必须服从同一个北极星顺序。
- GateEvaluation 必须能改变状态或生成后续 WorkItem。
- AcceptanceReview 只能引用 GateEvaluation，不另建评分体系。
- Learning 只能消费带证据、失败归因、rubric 版本和 confidence 的 GateEvaluation。
- Governance 必须聚合同类 GateEvaluation，不基于单次高分做系统判断。
- 环境失败、工具失败、权限失败、外部等待不能误记为 KUN 能力失败。
- 缺证据时最高只能 partial，不能 ready 或 accepted。

### 5.12 AcceptanceReview

用户或 reviewer 对交付结果的验收事实。

结果：

- `accepted`
- `partial_accepted`
- `rework_required`
- `rejected`

关键字段：

- `acceptance_id`
- `mission_id`
- `task_plan_version`
- `delivery_manifest_ref`
- `gate_evaluation_ref`
- `reviewer`
- `decision`
- `satisfaction`
- `reason`
- `requested_changes`
- `new_info_or_constraints`
- `followup_work_item_refs`
- `ledger_event_ref`

### 5.13 CapabilityProfile

能力治理档案。它不属于最小执行闭环，但属于完整 KUN 产品。

关键字段：

- `capability_id`
- `capability_name`
- `governance_key`
- `source_refs`
- `source_versions`
- `supersedes_refs`
- `evidence_refs`
- `known_limits`
- `promotion_stage`
- `holdout_refs`
- `regression_refs`
- `last_verified_at`
- `rollback_plan`
- `runtime_enabled`
- `rolled_back_at`
- `rollback_reason`
- `rollback_refs`

消费规则：

- `review_only`、`replay`、`holdout`、`shadow`、`canary` 都只能作为证据和验证状态，不得作为默认运行时能力。
- `runtime_enabled=true` 只对 `production` 有效。
- production profile 被 rollback 后必须从默认运行时列表移除，并保留 rollback 原因和引用。
- `source_refs` 和 `source_versions` 必须能说明能力来自 AB gap、真实任务、企业项目、开源项目、外部专家或外部 agent 样本中的哪一版。
- `governance_key` 用于识别同类能力，避免 OpenClaw/Hermes 或多轮 dogfood 生成重复默认能力。

### 5.14 CapabilityExecutionPolicy

生产能力的运行时消费策略。它是 `CapabilityProfile` 和真实执行模块之间的 adapter，保证能力不只是“已生产化”，而是真的影响计划、调度、诊断、恢复、审批和评估。

关键字段：

- `policy_id`
- `built_at`
- `capability_profile_refs`
- `source_versions`
- `governance_decisions`
- `directives`

Directive 类型：

- `planner`
- `worker_distribution`
- `runner`
- `supervisor`
- `diagnostics`
- `approval`
- `context`
- `evaluation`

门禁：

- `CapabilityExecutionPolicy` 只能由 governed production profiles 生成。
- 同类 profile 必须先治理去重，再进入 policy。
- daemon、planner、runner、supervisor 或 productization runner 至少一个真实消费者必须读取 policy，否则该能力只能停留在档案层。
- 每次 policy 绑定执行模块时，必须写入可审计 artifact 或 ledger 引用。

## 6. 统一状态机

KUN 任务必须使用统一状态机。

```text
intake
  -> info_gap
  -> planning
  -> info_gap
  -> awaiting_approval
  -> contracted
  -> queued
  -> running
  -> waiting_human
  -> waiting_external
  -> blocked
  -> retrying
  -> repairing
  -> rolling_back
  -> changing_plan
  -> merging
  -> delivering
  -> awaiting_acceptance
  -> learning_writeback
  -> closed

terminal:
  closed
  partial_closed
  failed
  cancelled
```

允许暂停和升级：

- 任意非终态可进入 `paused`。
- `blocked` 可进入 `escalated`、`changing_plan`、`waiting_human`、`failed`。
- `waiting_human` 和 `waiting_external` 超过 SLA 后必须进入 `escalated`、`changing_plan`、`partial_closed` 或 `cancelled`。

禁止跳跃：

- 未经过意图识别和信息完整性判断直接生成 `TaskPlan`。
- 信息缺口未处理直接进入 `planning`、`awaiting_approval` 或 `contracted`。
- 未经过任务方案确认直接进入 `contracted`。
- 未写 artifact manifest 直接进入 `delivering`。
- 未经过 GateEvaluation 直接进入 `awaiting_acceptance`。
- 未完成验收或明确 partial 原因直接进入 `closed`。
- 学习候选未经过验证直接改生产策略。

## 7. 运行时协议

运行时协议是五个子系统共同遵守的强规则。它们不是独立一级子系统。

### 7.1 调度协议

WorkItem 调度必须支持：

- dependency DAG。
- priority 和 priority aging。
- resource lock。
- concurrency limit。
- lease 和 heartbeat。
- retry backoff。
- cancellation。
- preemption。
- blocked propagation。
- idempotency。
- daemon wakeup。
- process supervisor。
- crash recovery。
- cross-day resume。
- scheduled progress report。

调度规则：

- 依赖未完成不得执行。
- 同一资源的高风险动作必须串行。
- blocked 的上游会阻塞下游，除非下游被标记为 safe_parallel。
- retry budget 用尽后必须进入 repairing、changing_plan、waiting_human、partial_closed 或 failed。
- 抢占只能用于更高优先级或风险止损任务，并必须写 LedgerEvent。
- 常驻运行时不得依赖用户手动看终端或手动重启同一任务。

### 7.2 计划变更协议

触发条件：

- 新信息改变目标、验收、风险、成本或证据。
- 当前拆解无法交付结果。
- 重试多次失败。
- 用户或 reviewer 修改范围。
- 外部权限、时间、预算发生变化。

必须记录：

- plan version diff。
- 变更原因。
- 影响范围。
- 被废弃或需要重定向的 WorkItem。
- 是否需要 owner / operator / reviewer 确认。
- 回滚路径。

计划变更通过后：

- 生成新的 TaskPlan version。
- 失效旧 WorkingContext。
- 更新 ExecutionContract。
- 重新评估受影响 WorkItem。

### 7.3 失败分类与恢复矩阵

| failure_category | 示例 | 默认恢复 |
| --- | --- | --- |
| `environment_failure` | 网络 EOF、进程崩溃、wrapper missing | retry -> repair -> blocked |
| `permission_failure` | auth expired、无账号、权限不足 | waiting_human/operator -> retry |
| `tool_failure` | 工具返回错误、格式不兼容 | repair -> retry -> alternate tool |
| `model_quality_failure` | 输出质量差、漏需求、误解 | repair -> re-evaluate -> changing_plan |
| `evidence_failure` | 来源缺失、过期、冲突 | research -> evidence refresh -> GateEvaluation |
| `plan_failure` | 拆解错、路线错、范围错 | changing_plan -> approval -> queued |
| `external_dependency_failure` | 外部系统超时、外部人无响应 | waiting_external -> escalate -> fallback |
| `user_input_missing` | 关键取舍未确认 | waiting_human -> partial/paused |
| `delivery_failure` | 验收不通过、返工 | repair/rework -> retest -> deliver |
| `cost_overrun` | 重试或外部成本超预算 | GateEvaluation -> reduce scope / ask / partial |

每类失败必须绑定下一状态、重试预算、是否问人、是否换策略、是否回滚、是否允许 partial。

### 7.4 合并协议

多 worker 或多人协同输出必须进入合并流程。

合并必须确定：

- primary artifact。
- supporting artifacts。
- 冲突项。
- 冲突裁决人或裁决策略。
- 证据优先级。
- 测试和评审结果。
- 最终责任 owner。
- 需要返工的 WorkItem。

合并完成后必须生成 `ArtifactManifest(kind=merge)` 和 `GateEvaluation(stage=merge)`。

### 7.5 Artifact 和 Ledger 协议

Artifact 规则：

- 单个产物用 ArtifactRecord。
- 一次运行、合并、交付必须生成 ArtifactManifest。
- 交付、验收、复测、回滚都引用 manifest，不引用散点文件。

Ledger 规则：

- append-only。
- 全局 sequence。
- 每个事件有 correlation_id 和 causation_id。
- 幂等写入。
- 支持 replay。
- message、decision、approval、state_change 都是强类型 LedgerEvent。

### 7.6 协同 SLA 协议

所有 CollaborationTicket 必须定义：

- 需要谁。
- 为什么需要。
- 决策选项。
- 推荐选项。
- deadline。
- reminder。
- escalation。
- fallback。
- resume rule。

无人响应后必须进入以下动作之一：

- 升级到替代角色。
- 缩小范围继续。
- 暂停。
- partial 收尾。
- 取消。
- 改计划。

### 7.7 WorkingContext 协议

刷新触发：

- 阶段切换。
- 任务方案版本变化。
- 长时间中断后恢复。
- 关键证据更新。
- 用户或 reviewer 给出新约束。
- GateEvaluation 判定上下文过期。

质量检查：

- 必须包含当前 TaskPlan version。
- 必须包含验收标准。
- 必须包含关键约束和风险。
- 必须包含开放问题。
- 必须引用关键 artifact 和决策。
- 必须记录省略内容及省略原因。

旧 WorkingContext 失效后，不得继续用于新 WorkItem。

### 7.8 GateEvaluation 协议

GateEvaluation 发生在：

- 任务方案确认前。
- 关键决策前。
- WorkItem 完成后。
- 合并后。
- Delivery 前。
- Acceptance 后。
- 学习晋级前。
- 治理诊断时。

统一结论：

- `continue`
- `needs_info`
- `needs_human`
- `needs_external`
- `needs_repair`
- `needs_rollback`
- `needs_plan_change`
- `ready_to_deliver`
- `accepted`
- `partial`
- `rejected`
- `promote_candidate`
- `rollback_capability`

### 7.9 学习晋级协议

学习候选必须经过：

1. replay。
2. holdout。
3. regression。
4. shadow。
5. canary。
6. rollback readiness。

晋级必须满足：

- 结果质量提升，或结果不下降且速度/成本改善。
- 无关键回归。
- 无任务族偏置。
- 无 benchmark 泄漏。
- 有失败回滚方案。
- 有线上监控窗口。
- 非 production profile 必须保持 evidence-only，不能通过 `runtime_enabled` 或 API 表现成已启用能力。
- production profile 进入默认运行时前，必须经过能力库治理和 `CapabilityExecutionPolicy` 编译。

### 7.10 常驻运行协议

KUN 的长任务执行必须具备常驻后台能力。

常驻运行时必须支持：

- 自动醒来并扫描 ready WorkItem。
- runner 注册、lease 获取、heartbeat 写入和 lease 释放。
- 进程崩溃、worker 卡死、超时、网络中断和工具异常检测。
- 断电、重启、跨天恢复后继续同一任务方案和队列状态。
- 对外汇报当前进度、风险、等待项、下一步和交付物位置。
- 对失败进行分类，必要时自动生成 repair、rollback、retest、plan_change 或 collaboration WorkItem。
- 所有动作写入 RunRecord、ArtifactManifest、GateEvaluation 和 LedgerEvent。

常驻运行时不得：

- 把进程或环境失败记为 KUN 能力失败。
- 在没有 idempotency、审计和回滚的情况下执行真实外部动作。
- 静默吞掉长期卡住的任务。

### 7.11 外部能力样本生产化协议

外部能力样本进入 KUN 的路径如下：

```text
source review
  -> behavior distillation
  -> KUN-native candidate
  -> replay
  -> holdout
  -> shadow
  -> canary
  -> production default runtime
  -> monitoring / rollback
```

每个外部能力样本必须记录：

- 来源系统和源码/文档/行为引用。
- 观察到的执行过程和工程化结构。
- 适配到 KUN 的子系统和协议。
- 为什么不复制外部实现。
- 与 KUN 现有能力的重叠和取舍。
- 测试、回归、真实长任务验证和监督结论。
- 生产默认运行时的消费边界和回滚方式。

OpenClaw/Hermes 只能作为被学习对象和对照样本，不得在评测中被修改为 KUN 优化的一部分。

### 7.12 能力运行时消费协议

KUN Runtime 消费能力必须遵守三段式边界：

```text
CapabilityProfile(production)
  -> capability governance
  -> CapabilityExecutionPolicy
  -> planner / worker_distribution / runner / supervisor / diagnostics / approval / context / evaluation
```

规则：

- 默认运行时只读取 production 且未 rollback 的 capability。
- 读取前必须按 `governance_key` 去重，保留证据更强、来源更新、验证更完整的 profile。
- 被合并或舍弃的重复 profile 必须记录 governance decision。
- `CapabilityExecutionPolicy` 必须把能力转成可执行指令，不能只返回 profile 列表。
- 执行模块绑定 policy 后，必须把 capability refs 写入 progress artifact、run artifact 或 ledger。
- 如果生产能力没有实际消费者，能力治理系统必须降级为治理问题，而不是默认为“已完成融合”。
- 外部能力样本带来的指令必须 KUN-native 化，不能把 OpenClaw/Hermes 的实现代码或私有协议直接搬进运行时。

## 8. 北极星指标和任务类型 Rubric

### 8.1 统一指标包

KUN 的统一指标包包括：

- 用户目标达成度。
- 一次验收通过率。
- 返工率。
- 返工严重度。
- 证据质量。
- 证据冲突处理率。
- 恢复成功率。
- 平均恢复时间。
- 用户等待时间。
- 人机协同质量。
- 超预算率。
- 重复失败率。
- 能力复用后的边际成本下降。

### 8.2 任务类型 Rubric

| task_type | result_quality 过线标准 |
| --- | --- |
| `product_development` | 可运行变更、测试或验证、用户需求覆盖、无明显回归 |
| `research_evidence` | 主源或可信来源、时效标记、冲突处理、引用完整 |
| `ops_tooling` | 命令或工具真实执行、日志可追踪、失败可恢复 |
| `collaboration` | 决策上下文清楚、SLA 有效、回复后可恢复 |
| `external_action` | 权限正确、审批正确、幂等、审计、回滚或补偿 |
| `self_improvement` | replay/holdout/shadow/canary 通过，无负迁移 |

Rubric 可以按任务类型扩展，但不得改变北极星顺序。

### 8.3 7x24 产品验收门禁

KUN V6 必须通过：

1. 连续运行至少 72 小时不中断，任务状态、队列、上下文、产物可恢复。
2. 同时处理多类真实任务：开发、研究、运维、协同、外部动作。
3. 重启后能恢复到正确下一步，不丢任务方案、等待项、证据和成本记录。
4. 工具失败、超时、网络失败、权限失败必须正确归因并进入修复或等待。
5. 人类延迟回复后，KUN 能用原上下文恢复执行，不重复问、不跑偏。
6. 所有交付必须经过 GateEvaluation，并引用 artifact、测试、证据或评审。
7. 返工必须进入队列，生成新 GateEvaluation，并能复测。
8. 学习候选必须通过 replay、holdout、shadow、canary、rollback。
9. 长运行期间不得出现无消费者事件、无引用产物、无账本状态跳转。
10. 最终按北极星判定：结果质量达标后，才比较速度和成本。
11. 至少完成一个真实业务长任务 dogfood，覆盖计划、分发、执行、恢复、合并、报告、验收和学习写回。
12. AB / Frontier50 只能作为回归门禁或对照测试，不能替代真实长任务验收。
13. 外部能力样本进入生产默认运行时前，必须通过源码/行为对照、真实任务验证和独立监督。
14. production capability 必须通过能力治理去重后编译成执行策略，并被 daemon / runner / supervisor 等真实执行路径消费。

## 9. 用户体验

KUN 的主体验是对话，但对话背后必须有可见控制面。

### 9.1 用户第一屏

用户应看到：

- 当前目标和 KUN 对意图的理解。
- 当前任务方案版本；若关键信息不足，应显示“尚未生成可执行任务方案”。
- 当前阶段和状态。
- 已知信息、未知信息和关键信息缺口。
- 正在执行的工作项。
- 阻断和需要用户决策的事项。
- 等待外部人的事项和 SLA。
- 已产出的关键 artifact。
- 当前 GateEvaluation 结论。
- 风险、成本、预计下一步。
- 交付物位置和验收状态。
- 当前是否由系统自动恢复、等待人、等待外部系统或需要改计划。

### 9.2 任务方案确认

复杂任务启动时，KUN 应向用户展示：

- 要解决的问题。
- 不做什么。
- 已知事实和缺失信息；信息缺口未处理时，应先展示补问信息，不展示可执行方案。
- 关键假设。
- 拆解方式。
- 需要用户或 operator 确认的点。
- 验收标准。
- 风险和回滚方式。
- 预算和时间边界。

用户可以：

- 批准。
- 修改。
- 缩小范围。
- 要求先补信息。
- 只授权低风险部分先执行。

### 9.3 执行中进度

KUN 不应只说“我在处理”。它必须说明：

- 已完成什么。
- 正在做什么。
- 卡在哪里。
- 谁或什么系统负责下一步。
- 是否需要决策。
- 是否存在超时、超预算或风险升级。
- 预计何时有下一个可用产物。
- 最近一次 GateEvaluation 的结论。
- 如果失败，失败属于系统污染、环境阻断、权限问题、证据问题、计划问题还是模型质量问题。
- 如果正在恢复，当前恢复动作和同题复测状态。

### 9.4 交付和验收

交付包必须包含：

- 最终结果。
- ArtifactManifest。
- 证据和测试引用。
- 关键决策和取舍。
- GateEvaluation。
- 未解决问题。
- 风险和后续建议。
- 验收入口。

### 9.5 任务驾驶舱

任务驾驶舱是协同交付系统的用户表面，必须让普通用户不翻终端也能理解任务状态。

驾驶舱必须展示：

- 当前目标、任务方案版本和执行阶段。
- 已完成、正在执行、等待、阻断、返工和已关闭的工作项。
- 质量门禁、最新 GateEvaluation 和验收状态。
- 当前风险、阻断原因、恢复动作、下一步和预计更新时间。
- 需要用户、operator、reviewer、expert 或 external worker 回复的票据。
- 交付物、证据、测试、报告和关键决策入口。
- 后台 supervisor / daemon 的健康、最近 heartbeat 和恢复状态。

驾驶舱不得只是工程日志包装；它必须是用户和 KUN 协作的任务控制台。

## 10. 奥卡姆剃刀后的取舍

### 10.1 保留

- 意图识别和信息补齐先行；任务方案在关键信息足够后生成。
- 五个一级子系统。
- WorkingContext。
- 持久队列和 supervisor。
- ArtifactManifest。
- LedgerEvent。
- CollaborationTicket。
- GateEvaluation。
- AcceptanceReview。
- CapabilityProfile。
- CapabilityExecutionPolicy。
- replay / holdout / shadow / canary / rollback。

### 10.2 合并

- Goal Compiler、Mission Contract、Plan Alignment 合并为任务方案系统。
- MissionContract 改为 ExecutionContract。
- GateResult 和 EvaluationRecord 合并为 GateEvaluation。
- MessageRecord、DecisionRecord、ApprovalRecord 合并为强类型 LedgerEvent。
- HandoffTicket、ApprovalRequest、ExternalActionRequest、ExternalWorkerOutput、ResumeSignal 合并为 CollaborationTicket。
- RepairTicket、RollbackTicket、ReworkTicket、PlanChange、GovernanceAction 合并为 WorkItem 类型。
- EvidenceCard、SourceRecord、ConflictMatrix、MissingReason 合并为 ArtifactRecord 元数据和 ArtifactManifest。
- AB Runner 降级为能力治理系统中的评测 adapter。
- 同类能力 profile 合并为 governed default runtime profile，重复候选保留为证据或 superseded 记录。

### 10.3 砍掉或降级

- agent 数量作为产品卖点。
- benchmark 专用模板作为主线能力。
- prompt-only guardrail。
- 没有消费者的事件和字段。
- 没有 artifact manifest 的完成状态。
- 没有验收的 done。
- 外部脚本长期代跑控制面。
- 自我学习直接改生产。
- 用户不可见、不可解释、不可恢复的后台动作。
- 各模块自定义评估分数。
- 各 worker 私有上下文摘要。
- 无 correlation_id 的跨系统消息。

## 11. 完成定义

KUN V6 只有同时满足以下条件，才能称为产品闭环完成：

- 用户目标能形成版本化 TaskPlan。
- 信息不足时能在 TaskPlan 前主动补齐、检索、索权或请求确认。
- ExecutionContract 能约束执行、权限、证据、交付和风险。
- WorkItem 能进入持久队列并被 supervisor 执行。
- 调度协议能处理依赖、锁、并发、重试、取消和阻塞传播。
- 执行过程能写入 ArtifactManifest 和 LedgerEvent。
- WorkingContext 能压缩、刷新、失效和恢复。
- GateEvaluation 能贯穿计划、执行、合并、交付、验收、学习和治理。
- 失败能按分类生成修复、回滚、改计划、等待人或 partial。
- 多 worker 输出能通过合并协议生成交付 manifest。
- CollaborationTicket 能管理人类和外部 worker 的 SLA、升级、恢复。
- AcceptanceReview 能接收 accepted、partial、rework、rejected。
- 返工能重新进入队列并复测。
- 学习写回有 replay、holdout、shadow、canary、rollback。
- 用户能随时看到当前状态、风险、成本、等待项、下一步和需要自己做的决策。
- 常驻 supervisor / daemon 能自动醒来、拿任务、恢复、重试、跨天续跑和定时汇报。
- 普通用户可读任务驾驶舱可用，不需要翻终端判断任务是否健康。
- replay 能力不能被生产默认消费；生产能力必须走完 holdout、shadow、canary、rollback 和监督。
- production 能力必须先经过治理去重，再编译为 CapabilityExecutionPolicy，并被真实执行模块消费。
- OpenClaw/Hermes 等外部能力样本已完成 KUN-native 化、真实长任务验证和生产运行时集成，且无复制粘贴外部实现。
- AB round-03 到 round-10 不作为当前主线推进；真实长任务 dogfood 是产品化主验收路径，AB 保留为回归门禁。
- 代码边界清楚，实验状态文件、dogfood 状态文件和正式代码产物不能混杂。

## 12. 最小完整实现范围

最小完整实现不是最小 demo，而是最小闭环产品。必须包含：

- `Mission`
- `TaskPlan`
- `ExecutionContract`
- `WorkItem`
- `RunRecord`
- `ArtifactRecord`
- `ArtifactManifest`
- `LedgerEvent`
- `WorkingContext`
- `CollaborationTicket`
- `GateEvaluation`
- `AcceptanceReview`
- 当前任务控制台
- 一个能跑通的真实任务：计划、执行、阻断、修复、合并、交付、验收、学习写回

`CapabilityProfile` 和 `CapabilityExecutionPolicy` 不属于最小执行闭环，但属于完整 KUN 产品闭环；当 KUN 开始自我学习、能力晋级和外部能力样本生产化时必须启用。

缺少其中任何一项，只能称为局部模块，不能称为 KUN Control Plane。
