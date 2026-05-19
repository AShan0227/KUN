# Conversation Learning Card：V6 产品纠偏

> 日期：2026-05-17
>
> 目的：记录用户对 KUN Control Plane 和长周期任务执行的纠偏，供 KUN 后续自我迭代、任务启动和执行中防跑偏使用。

## 1. 触发背景

Frontier50 ABtext 自动执行过程中，监督者持续按 peer gap 修 KUN，并把 KUN 从 round-01 到 round-09 拉到过门禁。但对话中暴露出一个重要偏差：

- AB 优化容易变成局部补题和格式合同优化。
- 这不能证明 KUN 能 7x24 小时自主解决真实问题。
- 监督者在长周期任务中也会被最新 gap 带跑，忘记回到产品方案和北极星。

## 2. 用户关键纠偏

### 2.1 北极星没有变

错误表述：

```text
把“人类介入是否足够少”写成核心指标。
```

用户纠正：

```text
北极星仍然是交付结果好，速度快，成本低。
复杂长周期任务、人机协同、外部人员调度，都属于交付结果好的能力。
人必要时肯定要参与，KUN 要做好人机协同机制，甚至调度外部的人。
```

V6 写回：

- 人类参与不是失败。
- 错误的人类参与才是失败。
- 目标是该自动的自动，该确认的确认，该调人的调人。

### 2.2 产品方案是长任务对齐锚点

用户观察：

```text
执行长周期任务时很容易跑偏。
解决方案是先有确定的产品方案，执行过程中偶尔回去看产品方案对齐。
KUN 必须具备这个能力。
```

V6 写回：

- 新增 Plan Alignment Engine。
- 长任务启动前读取权威产品方案。
- 执行中定期生成 PlanAlignmentEvent。
- 用户纠偏写 ConversationLearningCard。

### 2.3 Control Plane 不是外部脚本壳

用户要求：

```text
把“执行器”做进鲲：队列、权限、进程 supervisor、AB runner、污染检测、自动回滚、同题复测、进度报告全部内置。
以后用户只对话，鲲自己跑。
```

V6 写回：

- Durable Queue。
- Process Supervisor。
- Artifact Store。
- Gate Engine。
- Repair / Rollback / Retest。
- Progress Ledger。
- 内置 AB Runner。

### 2.4 先对齐已有产品方案和代码

用户要求：

```text
一部分功能鲲在产品设计时已经包含了。
需要对齐哪些有了，哪些需要重新开发，也要审阅代码。
```

审阅结论：

- Mission、StateLedger、NUO、Qi、WorldGateway、多车道 scheduler、completion gate 都已有基础。
- 缺口集中在持久执行队列、真实 supervisor、内置 AB runner、证据引擎、人机协同调度、Plan Alignment、Capability Registry。

### 2.5 OpenClaw / Hermes 的长处要蒸馏进 KUN

用户要求：

```text
既然已经监督它们变形跑了这么久，就要看到不足并一并解决。
```

V6 写回：

- Hermes 长处：深任务理解、上下文/session 边界、多子任务合成、证据叙事。
- OpenClaw 长处：工具优先、当前 run state、pipeline 执行、进程/日志/产物意识。
- KUN 不复制外壳，吸收为 Goal Compiler、Evidence Planner、Tool Adapter Registry、Process Supervisor、Synthesis Worker、Merge Gate。

### 2.6 产品方案要收拢，不要堆模块

用户进一步纠偏：

```text
哪怕代码现在没有完善，产品方案里面的设计一定要包含。
产品方案不应该太复杂，而是把很多相似和重复功能汇总到一起，变成一个个子系统。
要甄别哪些历史方案无用或应淘汰，哪些应该保留甚至强调。
```

V6 写回：

- 把散落功能收拢为 8 个一级子系统。
- 明确历史方案的保留、合并、淘汰。
- 后续开发和测试必须以产品方案为对照，而不是按最新灵感堆功能。

### 2.7 任务方案先行，再长周期执行

用户要求把这次协作方法复现到 KUN 里：

```text
用户给任务后，KUN 需要先给相对完善的任务方案并和用户对齐。
信息不足时要主动向主客户询问、联网检索、补齐资料。
没问题再进入长周期执行。
执行中发现外部信息或优化点，要补到任务方案里。
任务怎么拆、怎么分配、怎么评估、怎么合并，都要有这个方案做参考。
重要部分要和人交互确认。
```

V6 写回：

- 新增 `TaskPlanDraft`、`InfoGap`、`PlanApproval`、`PlanChangeProposal`、`TaskPlanVersion`。
- 复杂任务未完成必要对齐前，不能直接进入长期自动执行。
- 任务方案是运行时对象，不是一次性文档。
- 重要计划变更必须有原因、影响、选项、确认人和 StateLedger 引用。

## 3. 反复出现的错误模式

### 3.1 Benchmark 局部最优

表现：

- 只看当前 round gap。
- 快速修 fast_path / prompt contract。
- 忽略产品主线。

修正：

- AB 修复必须生成 CapabilityGap 和 ProductGap。
- 如果 gap 暴露的是系统能力缺口，优先建设模块，不堆模板。

### 3.2 把方案当交付

表现：

- 输出看起来完整，但没有真实 artifact、命令、证据、测试、回滚。

修正：

- 没有 artifact refs 只能 partial。
- 没有 replay proof 不能声明已验证。

### 3.3 把监督者能力算成 KUN 能力

表现：

- 外部监督者手动跑命令、修 wrapper、读报告，然后 KUN 被算作能自动执行。

修正：

- 监督者介入必须标记 `supervisor_intervention`。
- 只有 KUN 控制面自己发起、监控、修复、复测的动作，才算 KUN 能力。

### 3.4 人机协同指标写偏

表现：

- 把“人类介入少”当成核心。

修正：

- 核心是协同质量：上下文清楚、选项明确、风险说明、决策后自动恢复。

### 3.5 模块堆叠冒充工程化

表现：

- 把历史方案里的每个名词都变成独立模块。
- 文档看起来完整，但产品主线、消费者和决策影响不清楚。

修正：

- 先归并为少数一级子系统。
- 每个对象必须说明谁消费、影响哪个决策、失败谁知道。
- 不被消费的字段、事件、报告要删除或降级为 debug。

## 4. 给 KUN 的任务启动检查

任何复杂长任务启动前，KUN 应问自己：

1. 我是否读取了当前权威产品方案？
2. 我是否先生成 TaskPlanDraft，而不是直接开跑？
3. 哪些信息还缺？是否需要问用户、查资料、联网检索或请求 operator？
4. 用户或 reviewer 是否批准了当前 TaskPlanVersion？
5. 我是否把用户目标写成 Mission Contract？
6. 当前优化是在提升交付结果，还是只是在追 benchmark 分？
7. 需要人类、operator 或外部专家吗？如果需要，handoff 是否清楚？
8. 是否有证据、artifact、测试、回滚和复测路径？
9. 我是否把监督者介入和 KUN 自身能力分开记录？
10. 我是否准备了 PlanAlignmentEvent？

## 5. 推荐写入对象

```json
{
  "memory_type": "ConversationLearningCard",
  "card_id": "conversation-learning-20260517-kun-v6-control-plane",
  "summary": "KUN 长周期任务容易被 benchmark gap 带跑；V6 必须以产品方案为对齐锚点，把 Control Plane 做成真实执行系统。",
  "north_star": ["交付结果好", "速度快", "成本低"],
  "anti_patterns": [
    "benchmark_local_optimum",
    "方案冒充交付",
    "监督者能力冒充 KUN 能力",
    "把人类介入少误写为核心指标",
    "模块堆叠冒充工程化"
  ],
  "required_runtime_checks": [
    "load_product_plan_before_long_task",
    "write_task_plan_draft_before_long_run",
    "resolve_info_gap_before_execution",
    "record_plan_approval",
    "write_mission_contract",
    "emit_plan_alignment_event",
    "emit_plan_change_proposal_when_plan_changes",
    "create_human_handoff_ticket_when_needed",
    "record_supervisor_intervention",
    "require_artifact_refs_for_done"
  ],
  "target_doc": "docs/v6/KUN-V6.md"
}
```

## 6. 运行时使用方式

这张卡不是普通备忘录。KUN 后续执行复杂长任务时必须这样使用：

1. `Mission Intake` 阶段读取本卡和 `docs/v6/KUN-V6.md`。
2. `Mission Contract` 写入 `plan_alignment_refs`，至少包含 V6 和相关 ConversationLearningCard。
3. 每个阶段结束时生成 `PlanAlignmentEvent`，检查当前执行是否仍符合北极星和产品方案。
4. 如果发现正在做 benchmark 局部最优、模板补丁、外部监督者代跑，必须标记 `alignment_status=drifting`。
5. 用户新的纠偏必须追加到本卡或生成新卡，不允许只停留在聊天记录里。
6. NUO 定期扫描最近 Mission 的 PlanAlignmentEvent；若同类 drift 重复出现，生成治理建议。
7. Qi 只能把本卡作为反例和约束，不得用它绕过 shadow / canary / 人审。

## 7. 对 V6 的新增要求

用户进一步要求：

```text
要看所有历史对话和 KUN 历史版本，最后收拢汇总为 V6。
新开发功能散落各处，必须先整合为有顺序、有逻辑、完整的产品方案，再补代码。
产品方案是后期开发和测试的对标依据，哪里遗漏、哪里顺序不对、哪里不完善，都要能回看方案对齐。
```

写回要求：

- V6 必须显式列出来源文档。
- V6 必须把 V1 到 V5.1 的演进主线收拢。
- V6 必须列出已有、半成品、缺口。
- V6 必须按真实任务生命周期排序，而不是按模块随意堆叠。
- V6 必须包含测试对标方式。
- Plan Alignment 必须成为 KUN runtime 能力，而不只是人工提醒。

## 8. 本轮新增写回

本轮用户强调的最终产品方法：

```text
产品方案先收拢，代码再补齐。
任务开始先形成任务方案，信息不足先补齐，对齐后再执行。
执行中任务方案持续版本化更新，重要变更要人确认。
历史方案要有取舍，不是全部堆进新版本。
```

写入 V6 的对象：

- `TaskPlanDraft`：复杂任务启动前的任务方案草案。
- `InfoGap`：缺失信息和补齐路径。
- `PlanApproval`：任务方案批准记录。
- `PlanChangeProposal`：执行中改计划的提案。
- `TaskPlanVersion`：被队列、worker、评估和交付引用的计划版本。

运行时要求：

- KUN 必须能说明当前任务使用哪个任务方案版本。
- 分发、执行、评估、合并、交付都要引用任务方案。
- 新外部信息或更优路线要写回任务方案，不允许只在聊天里口头漂移。
- 重要变化必须通过人机协同机制确认。

## 9. 第一性原理复审写回

用户要求再次审核：

```text
产品功能逻辑闭环么？
功能执行顺序闭环么？
从第一性原理出发再次审视。
```

复审结论：

- 原 V6 主体方向正确，但主生命周期里 `TaskPlan` 没有明确排在 `Mission Contract` 前面。
- `Plan Alignment` 不能作为末尾步骤，它应该贯穿启动、执行、返工、交付、学习。
- 原方案缺少显式 `AcceptanceReview`，容易让 KUN 自己宣布 done，而不是进入用户 / reviewer 验收闭环。

已写回 V6：

- 更新完整生命周期为：产品方案读取 -> 用户目标 -> 信息补齐 -> TaskPlan 对齐 -> Mission Contract -> 决策 -> 队列 -> supervisor -> artifact / ledger -> gate -> repair / plan change -> delivery -> acceptance -> learning -> NUO 治理。
- 新增 7 个第一性回路：目标、执行、证据、纠错、协同、验收、学习。
- 新增统一执行状态机，禁止未对齐计划直接执行、未写产物直接交付、未验收直接关闭。
- 新增 `AcceptanceReview` 对象，支持 accepted、partial_accepted、rework_required、rejected。

后续 KUN 长任务必须记住：

- 交付物不是终点，验收才是交付闭环的判定点。
- 返工不是失败噪音，而是 RepairTicket 或 PlanChangeProposal 的输入。
- 用户验收信号也是学习信号，要进入 Credit Assignment 和 CapabilityRegistry。
