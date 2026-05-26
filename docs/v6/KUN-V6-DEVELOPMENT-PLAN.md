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
- 必须区分 KUN 自身迭代和 KUN 执行用户任务：任务执行可以产出学习证据，但能力变化只能走 `self_improvement`、Qi/Nuo governance、capability promotion 和 rollback plan；不得在用户任务路径直接改默认能力、runner 行为或生产配置。
- 运行时必须硬校验这条边界：production `CapabilityProfile(runtime_enabled=true)` 只能由已注册的 `self_improvement` mission 和 learning-stage promotion gate 启用；普通用户任务只能产出 artifact、delivery、acceptance、learning signal 或 Qi/Nuo follow-up。
- 基础设施测试必须按真实语义命名：daemon refresh 只验证共享 work queue 可见性，scoped mission refresh 只验证已有 mission 追加 work item 可执行，stop request clear 只验证 daemon 生命周期控制，game write boundary 只验证任务执行隔离；不得写成 KUN self-iteration，除非测试对象确实是 Qi/Nuo/capability governance。

## 2. 子系统分工

| 子系统 | 长期职责 | 不做什么 |
| --- | --- | --- |
| Control Plane | 任务主线、队列、权限、状态机、账本、进程监督、门禁、回滚、进度报告 | 不承担具体 AB 评分，不直接生成能力候选 |
| Mission Director | 任务级交付监督、北极星对齐、信息完整性、拆解和 worker 分配审查、最终验收阻断 | 不替代启/傩，不直接写业务产物，不把自评分或门禁通过当最终完成 |
| 启 Qi | AB 执行、Frontier50 round、互评、报告、gap、同题复测、replay、holdout、shadow、canary、外部项目经验吸收 | 不绕过 Control Plane 自己推进生产变更 |
| 傩 Nuo | stub/fallback/误路由/timeout/EOF/wrapper/report/review 缺失检测，健康诊断，污染结论，风险治理 | 不当执行器，不把污染误算成 agent 能力失败 |
| KUN Runtime | 使用通过验证的能力完成真实任务，产出交付物、证据、测试、评审 | 不在用户任务执行路径自改默认能力、runner 行为或生产配置 |
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
- macOS launchd 服务必须使用可诊断的稳定入口，默认日志写入 `~/Library/Logs/KUN/`，不能因为 Documents/TCC 路径、重复心跳或空闲状态导致 EX_CONFIG、退出循环或无日志失败。
- 多任务 worker pool：daemon 必须记录 worker 槽位；多 daemon / 多机器必须通过持久 resource lock 与 work item lease 避免重复领取和资源冲突。
- resource lock 必须不只在单个调度波次内有效；默认提供 file、SQLite 和 Redis 持久锁适配层，并能写入等待原因、持有者、过期时间和冲突对象。单机多进程 worker pool 推荐 SQLite；跨机器部署使用 Redis/数据库锁。
- 多进程 worker pool 必须能生成多个 daemon 服务计划：共享 Control Plane store 和 resource lock，每个 daemon 独立 state/heartbeat/log，防止单状态文件互相覆盖。
- 对可能写入工作区的 work item，如果没有明确 workspace/worktree/project/repo 锁，daemon 必须自动加 mission-workspace 锁，避免未知写边界下并发污染。
- `kun` owner 的普通 execution、research、review、test、merge work item 必须有默认 KUN runtime runner，不能只依赖产品化 runner、AB runner 或任务专用 runner。
- heartbeat、timeout、retry、cancel、resume。
- 进程崩溃恢复、断电/重启恢复、跨天续跑。
- 卡死检测和失败分类。
- timeout、EOF、wrapper/auth/tool missing 进入系统阻断路径。
- 自动生成 repair、rollback、retest、plan_change work item。
- 定时生成用户可读进度汇报。
- 每次运行写 `RunRecord` 和 `LedgerEvent`。
- 每个 work item 执行前必须运行受限预执行层：绑定 production capability、skill、workspace、checkpoint、rollback、外部信息信号和启/傩反馈通道。
- 预执行 skill 失败必须生成可审计 artifact；如果启/傩 runner 可用，必须自动创建 Qi/Nuo follow-up work item，不得静默忽略，也不得直接算作 KUN 能力失败。
- daemon 默认必须注册 Qi runtime governance runner 和 Nuo runtime repair runner，确保预执行、运行时门禁和傩诊断生成的 Qi/Nuo follow-up work item 可以被后台服务自动执行，而不是只停留在待办列表。
- daemon 默认必须注册 Mission Director runner。Mission Director 是可单独配置模型/provider/档位的任务级监督角色，必须审查任务目标、信息缺口、拆解、worker 分配、证据、验收和“测试通过是否被误判为产品完成”；发现问题时必须生成 review artifact 和 GateEvaluation，并触发 needs_info、needs_human 或 needs_plan_change。它的 work item 优先级必须高于普通业务执行、最终交付和自动返工，避免监督被后续开发或 delivery work 抢跑。
- planning/info_gap 任务如果存在 `TaskPlan.info_gaps`，daemon 必须自动生成协同票据并转入 waiting_human，不能先执行任务方案，也不能只靠 API 抛错。
- 本地测试类预执行必须绑定明确 workspace，禁止在没有工作区边界时误跑当前仓库。
- shell、Python 等执行型 skill 必须默认限制在配置的执行根目录内，拒绝越界 cwd，并把 sandbox root、实际 cwd、sandbox_enforced 写入运行元数据；它是默认工作区隔离，不替代容器级隔离。
- daemon 必须支持显式沙箱模式：`workspace_snapshot`、`container_required`、`external_container`；容器 runner 未就绪时必须显示隔离等级和阻断原因，不能把工作区快照冒充容器隔离。
- 有 workspace 的 work item 必须生成文件级沙箱快照，而不是只保存 hash manifest；快照必须排除 `.git`、依赖缓存和构建产物，并记录 capture limits。
- rollback work item 必须能由 Control Plane 内置恢复 runner 执行，真实还原快照文件、清理快照后新增的无关文件，并写入恢复 artifact。
- merge work item 必须做并发合并治理：检查缺失依赖、重叠 artifact 输出、重复写入声明、互斥修改和需要人工判断的冲突；冲突不能被 artifact 拼接掩盖。
- V6 runtime 事件必须桥接到 Watchtower rule engine；work item 完成/失败和 GateEvaluation 至少要形成可规则化事件。
- daemon 每次 tick 必须生成 `RuntimeObservationReport` artifact，主动标注 runner 缺口、预执行失败、协同票据、Watchtower 触发、能力重复、production 能力未转成执行指令、交付清单缺失等观察重点。
- observation report 必须明确路由给 KUN、启、傩、人类、Control Plane 或外部监督者；中高风险项必须支持外部监督持续检查，并能反向进入启/傩治理闭环。
- 生产能力去重、折叠和回滚后必须自动生成启治理 work item；daemon 可以执行机械安全动作，但启必须记录保留、合并、降级或淘汰的可审计治理结论。
- 功能激活审计必须成为默认回归工具：每个核心功能都要有一条定制化触发任务，运行后明确触发条件、依赖协同关系、证据 artifact、生成的 work item、是否激活、是否需要补触发机制。
- 功能激活审计必须按证据层级输出：代码存在、触发器存在、runner 可执行、真实消费、闭环通过、真实 mission 端到端。fixture、synthetic、static probe 只能算触发/runner 证据，不能算真实长任务协同通过。
- 每个真实 runner 必须对 `CapabilityExecutionPolicy` 给出消费结论：已消费并产出 directive receipt，或显式声明本 runner 不需要消费。没有消费结论的 runner 不能被算作能力激活完成。
- required external info 不能只登记到 `external_source_refs`。任务合同或 evidence plan 声明必须查外部信息时，daemon 必须执行 web/file/repo/doc 预检；网络、权限、auth 或工具不可用时进入阻断或人机协同，而不是静默降级。
- Qi/Nuo follow-up 创建前必须检查 runner 可用；无 runner 时必须开 operator collaboration ticket 并阻断任务，不能只跳过。
- Nuo 诊断和修复必须分阶段：classification artifact 只代表“已诊断”，必须有 clean retest / rerun / replay / rollback 证据后，才允许标为“已修复”或关闭恢复链路。
- Nuo clean retest 必须参与状态协调：权限、写入、workspace、wrapper 或环境阻断被复测为已清除时，Control Plane 自动关闭对应人工阻断票据并恢复任务队列；复测失败才保持 waiting_human / waiting_external / repairing。
- Qi 策略优化必须从单一路径修复升级为多策略搜索：对高价值或长期任务，自动生成候选路径、小规模验证、比较结果质量/风险/成本，再选最优路径推进；失败候选进入治理或淘汰。
- Qi 遇到产品缺口、用户否定、任务理解不足、质量门禁失败或反复返工时，必须先创建 Qi-owned 的 strategy replay / shadow rerun / process audit 工作项，复跑同一任务切片并比较旧策略与新策略。复盘输出只能进入 `self_improvement` 和 replay 候选证据；KUN 执行用户任务的路径只能消费“计划变更/验收标准/测试要求”，不得直接启用新 runtime 能力。
- 新增工程化自我进化闭环：daemon 在发现能力消费缺证、Nuo recovery 未闭环、生产能力晋级边界异常、重复失败、机械返工或交付验收误判时，必须创建 Nuo self-improvement audit work item；Nuo 产出 gap report 后，Qi 必须生成多策略候选，胜出方案只能进入 replay-stage capability candidate 和 KUN-owned sandbox implementation work item。
- Nuo self-improvement audit 不得直接修改生产能力；Qi strategy search 不得直接启用 runtime profile；KUN implementation 必须经过测试、历史 replay、dogfood、clean retest、rollback plan 和 promotion gate，才能进入 production 默认能力。
- 自我进化闭环必须显式覆盖五层 RSI-inspired 协议：Self-Evaluation、Strategy Search、Safe Experimentation、Learning Signal、Capability Governance。Nuo gap 必须带自评问题和客观学习信号；Qi candidate 必须带解法空间、安全实验计划、学习信号要求和治理要求；缺任一层不得算功能激活完成。
- 最终产品类任务必须有真实用户体感门禁。KUN 自评分、机制门禁、残差审计、自动外部门禁或 checklist 通过不能直接触发最终交付；必须验证交互、视觉、体验、长时间试玩、失败反馈、目标用户体感和交付包完整性。
- 任务模板必须强隔离：任务专用模板只能由执行合同显式选择，不能作为 runner 默认值。新任务缺少 `production_mode`、模板 profile 或等价声明时，runner 必须阻断并要求方案补齐，防止历史任务特征污染新任务。可复用经验必须沉淀为 KUN-native 能力、协议、skill、runner 或门禁，不得保留原任务角色、文案、UI、行业假设和工作路径。

验收：

- 卡住任务能被发现。
- 超时先判环境/工具阻断，不直接算 KUN 能力失败。
- 恢复后能继续同一任务方案或触发计划变更。
- daemon 停止、重启、跨天恢复后能继续正确下一步。
- launchd 常驻服务能跨至少两个 heartbeat 周期保持 running，状态文件持续更新，日志可读；重复启动不会崩溃，空闲时不会因一次性运行配置退出。
- 多任务并行时，独立任务能公平推进，共享 workspace、mission 或 merge 资源的工作项会等待锁释放，等待原因在 tick report 和驾驶舱可见。
- worker pool 大于 1 时，两个互不依赖且资源锁不冲突的 work item 必须能在同一 tick 中真实并发执行；测试必须证明 runner 同时处于 running，而不是只证明一轮 tick 串行跑了多个 work item。
- 两个 daemon 使用同一 resource lock store 时，第二个 daemon 不能抢占未过期锁；锁过期或释放后能继续。
- 默认 merge runner 遇到重叠写入或缺失依赖会阻断并给出 GateEvaluation，而不是生成假合并。
- 用户无需手动盯终端或手动重跑同一任务。
- 普通真实任务 work item 能通过默认 KUN runtime runner 执行，产出 artifact，并在完成后形成 delivery manifest。
- Qi/Nuo follow-up 在默认 daemon 路由下能自动执行，并生成治理/修复 artifact。
- Mission Director 在默认 daemon 路由下能自动执行；产品类任务缺少真实玩家体感证据、人工/目标用户验收或完整任务拆解时，不能进入最终完成状态，必须被推回计划变更、继续迭代或人机协同。即使同一 tick 同时生成自动返工项，`max_work_items=1` 时也必须先运行 Mission Director。
- 信息缺口能自动变成用户可理解的协同票据。
- workspace 快照能在测试中真实恢复文件内容并移除快照后新增文件。
- Watchtower 规则能在 V6 gate 事件上触发，触发结果进入 daemon tick report。
- 每个写进度的 daemon tick 都能同时产出 observation artifact；缺 runner、预执行失败、协同票据、能力重复、交付清单缺失等问题不会被埋在日志里，而会被标注为可治理观察项。
- 重复 production capability 被自动折叠后，启 follow-up 会被创建并执行，产出治理 artifact；重复候选只保留为证据，不进入默认 runtime。
- `kun control-plane feature-activation-audit` 能一次性运行核心功能激活任务；报告必须覆盖信息缺口、人机协同、运行时激活、预执行、worker/resource lock、真实并发 worker pool、沙箱、启/傩、能力去重、合并冲突、快照回滚、Watchtower、外部样本学习、自主 App 开发、研究先行开发、游戏生产、AB 回归和产品化 dogfood。验收不能只看测试文件存在，必须看真实 Control Plane 任务是否拉起对应能力并留下证据。
- feature activation audit 的总览必须显示真实 mission e2e 数量；如果真实 mission evidence 为 0，报告必须明确标成“触发器/fixture 通过，真实协同未验证”，不得显示成全部产品化完成。
- 至少一条真实长任务必须覆盖：外部信息获取、Qi/Nuo follow-up、能力策略消费、工作区隔离、资源锁、回滚、人工验收、策略重排和驾驶舱展示。没有覆盖到的功能必须进入“真实任务重点观察清单”。
- 游戏、App、内容产品等最终交付必须有 final player/user experience gate；该 gate 缺失或失败时 final delivery work item 必须 blocked，而不是 awaiting_acceptance。
- 缺少显式模板/生产模式的产品开发任务必须在测试中被阻断，并且不得写入任何历史模板产物。

### 阶段 3：启 Qi AB Runner 接入

目标：Frontier50 成为 Control Plane 下的真实长任务。

开发内容：

- 封装 Frontier50 round 为 Qi WorkItem。
- Frontier50 live runner 必须能通过 daemon 显式挂载到 Qi test work item；AB 后续轮次暂停时不主动跑 round，但 runner 不能只是孤立模块。
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
- 模型路由必须消费 capability card 评分；当多个候选模型存在时，用真实任务/benchmark 写回的能力分选择候选，冷启动或无数据时保持原路由策略。
- 傩 benchmark 结果必须写回 capability card，进入启和路由层可消费的数据面，而不是只停留在面板分数。
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
- capability consumption 不只看 policy binding artifact，还必须看 runner 产出的 directive receipt 或 no-consumption declaration。缺少 receipt 时，能力只能算“已绑定”，不能算“已使用”。
- 启必须维护能力合并/淘汰队列。重复、低价值、负迁移或长期无人真实消费的能力，必须被合并、降级为证据或删除；不能让 runtime profile 越来越吵。
- 策略优化结果必须反写能力治理：成功路径形成候选能力，失败路径形成 known limit 或 discard 记录，所有记录都不得默认启用。
- Qi strategy replay 必须产生三类证据：`strategy_replay_report`、`process_audit`、`capability_candidate/replay_profile`。缺任一证据时只能算“启已诊断”，不能算“启已沉淀”。通过测试也只能进入 replay/holdout，不能跳过 shadow/canary/production。
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
- 展示 worker 槽位、resource lock 冲突、等待原因、沙箱隔离等级和并发合并风险。
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
- Genesis、OpenClaw、Hermes 等外部样本对比必须输出机器可读的 Qi 治理动作，包含 keep、merge、candidate、discard、测试、风险控制、回滚计划和默认运行时禁用边界；Markdown 报告不能替代治理数据。
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

1. 用户提交复杂任务 -> 意图识别 -> 信息缺口补齐或确认 -> 生成 TaskPlan -> 执行合同 -> work item 入队。
2. worker 正常完成 -> ArtifactManifest -> GateEvaluation -> 交付。
3. worker timeout -> 傩判系统阻断 -> repair -> 同题重跑。
4. Frontier50 round -> comparator healthy -> KUN 未过门禁 -> Qi 生成 repair -> 同题复测。
5. Frontier50 round -> comparator unhealthy -> round invalid -> 修 wrapper/网络/路由 -> 重跑。
6. 人类审批缺失 -> CollaborationTicket -> 等待 -> 回复后恢复。
7. 能力候选 -> replay/holdout -> canary -> production -> rollback。
8. 任务跨重启恢复。
9. daemon 自动醒来 -> 获取 ready work item -> 崩溃后恢复 -> 定时汇报。
10. 任务驾驶舱 -> 普通用户看懂进度、风险、下一步、交付物和验收状态。
10a. 多任务并行 -> worker slot 分配 -> resource lock 冲突等待 -> 锁释放后继续 -> 驾驶舱显示等待原因。
10b. 多 worker 代码/素材合并 -> merge governance 检查重叠写入 -> 冲突阻断或安全合并 -> downstream gate。
11. OpenClaw/Hermes 能力样本 -> 源码/行为对照 -> KUN-native 能力 -> 真实长任务验证 -> production runtime。
12. Nuo 污染样本 -> 自动分类 -> 修复建议 -> 同题复测 -> 不计 KUN 失败。
13. 真实长任务 dogfood -> 计划 -> 执行 -> 恢复 -> 合并 -> 交付 -> 验收 -> 学习写回。
14. production capability -> 能力治理去重 -> CapabilityExecutionPolicy -> daemon/runner/supervisor 绑定 -> progress artifact 可审计。
15. workspace 执行 -> 文件级快照 -> 文件被破坏 -> rollback work item -> 内置恢复 runner 还原 -> 恢复 artifact 可审计。
16. V6 work item/gate 事件 -> Watchtower rule engine -> 规则触发 -> daemon tick report 可见。
17. required external info -> 预检检索/读取 -> 证据 artifact -> runner 引用证据；工具不可用 -> Nuo 分类 + 人机协同或环境修复。
18. runtime observation -> Qi/Nuo/KUN/human 路由 -> 可执行 follow-up 或 operator ticket -> clean retest / replay 后关闭。
19. 策略失败 -> 生成多条候选路径 -> 小规模试跑 -> 选择最优路径 -> 淘汰低效路径 -> 记录能力治理。
20. 功能激活审计 -> fixture 通过但真实 e2e 缺失 -> 写入真实任务重点观察清单 -> 下一个 dogfood 强制覆盖。

## 5.1 真实任务重点观察清单

以下能力在静态检查或 fixture 中可以证明触发器存在，但必须等真实复杂任务继续观察，不能提前宣称完全产品化：

| 观察项 | 当前设计状态 | 真实任务中必须观察什么 | 触发后处理 |
| --- | --- | --- | --- |
| 功能激活真实 e2e | 有 feature activation audit，但可能仍是 fixture/synthetic | 真实 mission 是否实际触发、执行、复测并关闭 | 未覆盖则生成观察项和补测 work item |
| capability 真实消费 | 已有 `CapabilityExecutionPolicy` 和部分 runner receipt | 每个 runner 是否真的改变计划、执行、监督或诊断行为 | 无 receipt 则阻断或降级为“已绑定未使用” |
| 外部信息获取 | 有 external info signal 和 preflight | 需要外部资料时是否真的查、读、筛、引用 | 工具/网络失败交 Nuo，人类输入走协同票据 |
| Qi/Nuo 闭环 | 有诊断、治理和 follow-up runner | 诊断后是否有 clean retest / replay / rerun | 只有分类报告不得关闭恢复 |
| 动态最佳策略 | 有 plan_change 和治理候选 | 是否生成多策略、比较结果、选择最优 | 失败路径进入 known limit / discard |
| follow-up 可执行性 | 预检 follow-up 会查 runner | runtime observation follow-up 是否也不静默跳过 | 无 runner 开 operator ticket 并阻断 |
| 并发与锁 | 有 worker pool、SQLite/Redis 锁和驾驶舱字段 | 多任务是否真并行，锁冲突是否等待而非失败 | 冲突进入等待/重排/合并治理 |
| 沙箱隔离 | 有 workspace snapshot / container_required 协议 | 当前隔离等级是否真实显示，是否误报容器化 | 无容器 runner 时阻断或标明降级 |
| AB 执行 | adapter 与 live runner 都存在 | 驾驶舱是否区分 adapter summary 和 live executor | 未挂 live runner 不得显示为真实 AB 已跑 |
| 历史模板污染 | 有 production_mode 阻断 | 新任务是否带入火火兔、WordForge、AB 等历史特征 | 未显式选择模板则阻断并要求补方案 |
| 驾驶舱可读性 | 有进度、锁、沙箱、风险展示 | 普通用户是否看得出哪些功能没真正用上 | 增加“已绑定/已使用/真实验证”视图 |

## 6. 完成定义

整个开发完成必须同时满足：

- Control Plane 是长任务默认入口。
- Qi AB 能在 Control Plane 下自动跑轮次。
- Nuo 能系统化识别污染并触发修复。
- supervisor 能监控、恢复、重试、回滚。
- daemon 能常驻后台自动醒来、拿任务、崩溃恢复、跨天续跑和定时汇报。
- worker pool、持久 resource lock、work item lease、沙箱隔离等级和并发合并治理进入默认执行链路，并有测试覆盖。
- SQLite 多进程 resource lock、Redis 分布式 resource lock、daemon worker-pool service plan、安全默认 mission-workspace 锁和驾驶舱 lock backend 展示进入默认执行链路，并有测试覆盖。
- 沙箱快照和 rollback 已进入默认执行链路，不是未使用字段。
- Watchtower 能消费 V6 runtime 事件并参与异常治理。
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
