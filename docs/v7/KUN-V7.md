# KUN V7.1 — 产品迭代方案

> 文档身份：V7 是 V6 之后的迭代版本，**自洽 / 融合 / 严谨**。
> V7.1 是 2026-05-29 X.P phase 把 X.A → X.O 实装的 P0 操作层细节 merge 进 V7.0 后的版本.
>
> V7.1 是后续开发的**唯一对齐锚点**。开发任何功能前必须先回看 V7.1 对应章节，发现 V7.1 不完整或自相矛盾，先改 V7.1 再写代码。
>
> V6 文档归档作历史，不再作为开发对齐参考。
> V7.0 与 V7.1 的 delta 见 `docs/v7/KUN-V7.1-amendments.md` (P0 6 条已 merge, P1+P2 11 条待按需 merge).

---

## 0. 文档说明

### 0.1 V7 与 V6 的关系

V6 是 KUN 第一份完整产品方案。V7 在 V6 基础上**保留 + 增补 + 明确化**，不是另起一套。

| 项 | V6 状态 | V7 处理 |
|---|---|---|
| 北极星指标 | 已定义 (交付结果好 → 速度快 → 成本低) | 保留 |
| 6 个一级子系统 | 已定义 | 保留, 升级为 7 个 (加 Mission Director) |
| 双账本 | 已定义 | 保留 + RSI 入口明确化 |
| 能力 lifecycle | 已定义 | 保留 |
| 6 层激活证据 | 已定义 | 保留 |
| OpenClaw / Hermes 对标 | 已定义 | 升级: 加 Claude Code 第三方对标 |
| 启 (Qi) / 傩 (Nuo) 命名 | 已使用 | 保留 + 独立可发布预留 |
| Mission Director | 散落在 6.2 line 227 | 升级为一级子系统 §9.7 |
| Plan Alignment | CONVERSATION-LEARNING 2.2 提了, 主体未强 enforce | 升级为 §10 硬规则 |
| 双线监督 (代码 + 方案) | 部分散落 | 升级为 §10 集中协议化 |
| multi-LLM ensemble | line 276 提了"多策略候选", 无具体协议 | 升级为 §11 完整协议 |
| RSI 用户任务可触发 | line 158 严格禁止直接修改 | 明确: 严格验收后可 promote (§12) |
| dogfood 任务规范 | 阶段 9 提了 | 升级为 §19 (含 dogfood v8 反例 case study) |
| 命名约定 | implicit | 升级为 §5 explicit |
| 7 个第一性回路 | CONVERSATION-LEARNING 9 提了 | 升级为 §14 |

### 0.2 V7 的写作约束

1. **自洽** — 任何两章互引必须方向一致，不允许内部矛盾。
2. **融合** — 不堆模块。同一能力一个 owner，重叠功能必须 merge。
3. **严谨** — 不写"听起来对但实际跑不动"的部分。每个新 component 必须明确：属于哪个子系统 / 服务什么回路 / 谁消费输出 / 失败时谁知道。
4. **可执行** — 后续开发以 V7 章节为对齐锚点。开发 task 必须能映射到具体章节。

### 0.3 V7 出现原因（写给未来读者）

V6 写完后，在第一次真长任务 dogfood (v8) 执行过程中暴露出 4 类问题：

1. **方案没强 enforce**：执行体直接开跑，没先生成 TaskPlanDraft 并对齐。
2. **监督单线**：监督主要在工程层 (代码 / 测试 / commit)，**方案层是否偏离 / 方案本身是否错漏** 没系统监督。
3. **多 LLM 残废**：V6 设计了 multi-LLM ensemble，实际只 wire 了 1 个 LLM 跑，所有"多策略并行"的架子都在等第二个 LLM。
4. **dogfood 产物直接合并**：5 张 yaml seeds 跳过 capability lifecycle 直接进 `seeds/methodologies/`，违反双账本约束。

这些问题不是 V6 设计错了，是 V6 没把约束写到能 enforce 的程度。V7 的核心动作就是**把已有设计明文化 + 强 enforce + 补缺漏**。

---

## 1. 产品定位

KUN（鲲）是一个面向真实复杂任务的智能执行系统。用户给出目标后，KUN 负责理解目标、补齐信息、形成可执行方案、调度工具和协作者、长期执行、处理阻断、交付结果、接受验收，并把有效经验沉淀为可验证、可回滚、可生产化的能力。

KUN **不是**：
- 聊天机器人
- 多 agent 展示平台
- benchmark 答题器
- 只会生成方案的助手
- Claude Code 的复制（KUN 是 OpenClaw + Hermes 形态，吸收 Claude Code 的工程纪律）

KUN 的最终形态是**用户通过自然语言提出目标，系统自动完成任务理解、计划、分发、执行、恢复、合并、交付、验收和学习**。用户不需要盯终端，不需要手工串联脚本，也不需要记住系统状态。

### 1.1 KUN 的独特价值（与 LLM、与 OpenClaw/Hermes、与 Claude Code 的区别）

| 对比对象 | LLM 自身做不到的 | KUN 补上的 |
|---|---|---|
| **vs 单 LLM (Claude / GPT / Codex)** | 跨 session 持久化、预算硬约束、多租户、外部系统集成、企业治理 | Control Plane + 双账本 + capability lifecycle |
| **vs OpenClaw / Hermes** | 工程纪律不足 (commit / 测试 / dev log / decision pause 不强) | 蒸馏 Claude Code 工程纪律到 multi-LLM ensemble |
| **vs Claude Code** | 单 LLM 视角 (一个模型一种 bias / 一种判断) | 多 LLM ensemble + cross-provider critique |
| **vs 所有当前 agent 系统** | 一次任务跑完就完, 不让自己变更好 | **持续进化 RSI 闭环** — 每跑一次都找更优策略, 验收后让 KUN 略变更聪明 |

KUN 的**核心独特价值（V7 升级版）**：

```
multi-LLM ensemble 架构
  + Claude Code 级工程鲁棒性
  + 持续进化找更优策略 (RSI 核心)
  = KUN
```

**最核心的产品魂**：**KUN 不是一次任务一次活的工具, 是跑每次任务都让自己略变更聪明的系统**。RSI 持续进化找更优策略, 是 KUN 区别于 Claude Code / OpenClaw / Hermes 的最根本独特价值（详见 §12）。

不是"做一个更强的 Claude Code"，是"做 OpenClaw/Hermes 形态的多 LLM 系统，每个 LLM 都按 Claude Code 工程纪律行事，整个 ensemble 自身会越跑越聪明"。

---

## 2. 北极星指标

KUN 的指标顺序固定为：

```text
交付结果好
  → 在交付结果好的基础上速度快
  → 在交付结果好且速度可接受的基础上成本低
```

结果质量**永远优先于**速度和成本。速度和成本只能在结果质量达标后优化，**不能抵消质量失败**。

"交付结果好"包含：

- 准确理解真实业务目标，而不是按关键词套模板。
- 在信息不足时主动询问、检索、读取资料或请求授权。
- 形成能指导执行的任务方案和验收标准。
- 正确拆解任务、分发工作、管理依赖、合并产物。
- 获取、筛选、引用外部信息，并识别信息时效性。
- 跨小时、跨天、跨轮保持状态连续。
- 在失败、阻断、污染、误路由、超时、权限不足时恢复。
- 正确调度用户、操作者、评审者、专家、外部 worker、工具和外部 agent。
- 交付可验收产物，并提供证据、测试、风险和剩余问题。
- 根据验收和复盘结果把经验沉淀成受控能力。

KUN **不把"减少人类介入"当作核心指标**。必要的人类参与、专家输入、审批和外部执行是产品能力的一部分。KUN 的目标是让人类**在正确时机、以正确信息、做正确决策**，而不是让系统假装不需要人。

---

## 3. 产品边界

### 3.1 KUN 必须具备的能力

- 接收自然语言目标并识别任务类型、风险等级、必要信息和验收方式。
- 在信息不足时先补齐信息，再生成任务方案。
- **任务方案先行**（§10.1）：复杂任务必须先出 TaskPlanDraft → 对齐 → 才执行。
- 把任务方案作为运行时对象，驱动拆解、执行、评估、合并和交付。
- 通过 Control Plane 管理队列、权限、状态、进程、产物、账本和门禁。
- 对真实外部动作执行权限、审批、幂等、审计、回滚或补偿。
- 对每个任务保存状态、产物、证据、日志、成本和决策记录。
- **双线监督**（§10.2）：执行中工程线 + 方案线同时跑，独立报警。
- **多 LLM ensemble**（§11）：至少 2 个不同 provider 的 LLM 在线，跨 provider critique。
- 对失败进行分类、修复、回滚、改计划、复测或转人工。
- 对交付物进行验收闭环，支持 accepted、rework、partial、rejected。
- 从真实任务、AB 回归、评审、用户反馈、外部项目和 peer gap 中学习。
- 把 OpenClaw、Hermes、Claude Code、企业项目、开源项目和专家经验作为能力样本，提炼为 KUN-native 能力。
- **RSI 闭环**（§12）：用户任务执行中发现自身不足 → 启 (Qi) 找更优策略 → 严格验收（5 阶段 lifecycle）→ 可 promote 进 production。

### 3.2 KUN 不应做的事

- 用 agent 数量冒充能力。
- 用 prompt 约束冒充工程约束。
- 用结构化答案冒充真实交付。
- 用外部脚本长期代替内置 Control Plane。
- 在没有产物、证据、门禁和验收的情况下宣布完成。
- 将外部人或操作者的执行能力记为 KUN 自动能力。
- 将 replay、holdout、shadow 候选误当成生产默认能力。
- 把 AB 或 benchmark 当成真实长任务产品化的替代品。
- 把历史任务的角色、行业、模板、文案、美术、交互习惯或工作方法默认带入新任务。
- 为了推进速度降低已确认的产品标准。
- **跳过任务方案直接执行**（V7 新增硬约束，§10.1）。
- **单 LLM 跑长任务**（V7 新增硬约束，§11.1）。
- **dogfood 产物直接合并到主路径 seeds/methodologies/**（V7 新增硬约束，§19）。

---

## 4. 三方对标策略

KUN 的架构形态、工程纪律和独特价值来自三方对标。**不是任意一方的复制**，是吸收三方长处后形成 KUN-native 设计。

### 4.1 OpenClaw — 吸收方向

| OpenClaw 长处 | KUN 吸收为 |
|---|---|
| 工具优先（tool-first）：任何动作走显式工具调用，不靠模型自行决定副作用 | Skill registry + skill_directive 协议 + Tool dispatch |
| 当前 run state：每步状态机化，可暂停 / 恢复 / 审计 | Control Plane state ledger + ExecutorLoop checkpoint |
| Pipeline 执行：步骤可独立测试 / 重放 / 替换 | RunRecord + ArtifactRecord + 工具链可重放 |
| 进程 / 日志 / 产物意识：每个执行体明确进程边界 + 日志 + 产物 | Daemon + StateLedgerEvent + ArtifactManifest |

### 4.2 Hermes — 吸收方向

| Hermes 长处 | KUN 吸收为 |
|---|---|
| 深任务理解：goal compiler 把模糊目标编成可执行结构 | 任务理解与方案系统 (§9.1) + TaskPlanDraft |
| 上下文 / session 边界：长任务跨 session 不丢上下文 | Compaction + Checkpoint + ContextPacker |
| 多子任务合成：多 worker 产物合并为统一交付 | Merge governance + DeliveryManifest |
| 证据叙事：每个结论附 evidence + source | Evidence ledger + 证据引用协议 |

### 4.3 Claude Code — 吸收方向（工程纪律 10 维 + 扩展）

**核心定位**：Claude Code 是单 LLM 系统，但工程纪律严谨。KUN 吸收的是**纪律**，不是单 LLM 形态。

| Claude Code 工程纪律 | KUN 吸收为 |
|---|---|
| 任务拆解（TodoWrite / PlanTree）：复杂任务必拆 ≥ 2 层 | RecursivePlanner + PlanTree (§9.1) |
| 并行 sub-agent：可并行的子任务一次性派发 | ExecutorLoop 并行 tool dispatch + multi-LLM ensemble (§11) |
| grep verify before assume：改任何代码前先 grep 确认假设 | grep-verify skill (§9.1) + service_module_not_wired_to_runtime_audit 方法论 |
| 测试驱动 fail-fast：失败测试先于实现 | ValidationPipeline + Tester (§9.5) |
| commit 纪律 ≤ 1000 行：超出强制拆 commit | Git commit gate + 工程线 Watchtower 规则 (§10.2) |
| 错误立即修不藏：mid-stream bug 即报即修 | 工程线动态优化 (§10.3) + bug_root_cause_cases 库 |
| dev_log 沉淀 (ADR-025)：commit 后必更新 dev_log | dev_log 协议 + Control Plane 强 enforce |
| 决策点停下问：模糊需求暂停 + CollaborationTicket | 决策点 classifier + 协同票据 (§9.4) |
| Read with offset+limit：大文件不全 cat | self-reflect skill (§9.3) + file-io / grep-verify 内置 limit |
| Bash 克制：有专用工具就用专用 | Skill 优先于 shell-exec; shell-exec 走 allowlist |

**扩展项**（10 维之外，V7 加进 acceptance criteria）：

| 扩展纪律 | KUN 吸收为 |
|---|---|
| Co-author trail：commit 标注 LLM 作者 | Commit message 必带 `Co-Authored-By: <model>` |
| 单次 commit ≤ 5 文件改动 | 工程线 Watchtower 规则 |
| ruff + pytest 全过才 commit | Tester pipeline pre-commit gate |
| `git status` / `git diff` 优先 | Engineering 工具优先级表 |

**Runtime enforcement (V7.1, A1 / X.I-0a)**: `kun.governance.engineering_discipline.
EngineeringDisciplineEnforcer` 在每个 long-task 完成时跑 (env
`KUN_V7_DISCIPLINE_ENFORCER_ENABLED=true`, 经 `LongTaskRuntimeBundle`).
失败 disciplines emit `long_task.discipline_report` event, 落 PG
`engineering_discipline_reports` 表 (X.S), cockpit `/cockpit/discipline/recent`
可见.

### 4.4 三方对照 + KUN 目标

| 能力维度 | OpenClaw | Hermes | Claude Code | KUN V7 目标 |
|---|---|---|---|---|
| 任务理解 | 中（工具优先，理解偏浅） | **强（深任务理解）** | 强（单 LLM 但精） | Hermes 形态 + Claude Code 精度，跑在 multi-LLM ensemble 上 |
| 工程纪律 | 中 | 中 | **强（10 维 + 扩展）** | Claude Code 级，应用到每个 ensemble LLM |
| Multi-LLM 协作 | **强** | **强** | 无（单 LLM） | OpenClaw + Hermes 形态，3 模式 Explorer Pool |
| 长任务持久化 | **强** | **强** | 中（session-scope） | 三方融合 + Control Plane 持久化 |
| 自我进化 (RSI) | 部分 | 部分 | 弱 | **KUN 核心独有：启 (Qi) 严格验收 RSI 闭环 (§12) — 每跑一次都找更优策略, 持续进化** |
| 跨 LLM critique | 部分 | 部分 | 无 | **KUN 独有：External Supervisor 强制 cross-provider + 持续并行 watchdog (§9.x + §10.2.3 + §11.2)** |
| 双线监督 | 工程线为主 | 方案线为主 | 工程线 | **KUN 独有：双线并行 + Mission Director 统一指挥 (§10)** |
| 成本可控本地化 | 无 | 无 | 无 (单一 cloud LLM) | **KUN 独有：watchdog / 启 Explorer 可用本地模型 (Ollama / vLLM) 降本 (§11.3)** |
| 持续进化找更优策略 | 弱 | 弱 | 弱 | **KUN 核心独有：每跑一次任务都让自己略变更聪明 (§1.1 / §12)** |

**KUN V7 独特价值** = **multi-LLM ensemble × Claude Code 级工程纪律 × 严谨验收的 RSI 闭环 × 双线监督 × 持续进化找更优策略**。

最后一条是**产品魂**: 其他维度都是手段, "持续进化找更优策略"是 KUN 想做成的最终形态。

### 4.5 不复制实现，只吸收设计

V6 line 504-509 写过禁止事项，V7 保留并加强：

- 禁止直接复制 OpenClaw / Hermes / Claude Code 实现代码。
- 禁止把外部系统的复杂度原样搬进 KUN。
- 禁止把重复能力拆成新子系统。
- 禁止未经验证（5 阶段 lifecycle 全过）直接进入生产默认运行时。
- 禁止用三方品牌名作为 KUN 子模块名（如 `kun/openclaw/`、`kun/hermes/` 禁止）。

---

## 5. 命名约定

### 5.1 启 (Qi) 命名规则

**身份**：6.6 能力进化系统负责人。负责把经验转化为受控能力，主理 capability lifecycle、strategy replay、process audit、capability candidate。

**未来定位**：可能独立作为 agent 产品发布。命名规则按"对外可见 + 内部可重命名"双层设计。

| 层级 | 规则 | 例子 |
|---|---|---|
| 文档 / 任务驾驶舱 UI | 中文优先 "启 (Qi)" 双名 | "启 (Qi) 检测到 anomaly_kind=skill_mismatch_spike" |
| 公共 API (class / function) | 英文 `Qi` 顶层 + 内部实现可保留 `StrategistService` | `class Qi: ...`; `class StrategistService(Qi): ...` |
| Module path | `kun/agents/qi/` (V7 迁移目标) | 当前 `kun/agents/strategist/` → V7 重命名 |
| Event type | `qi.*` 前缀 | `qi.strategy_replay_started`, `qi.capability_promoted` |
| Log message | 中文前缀 `[启]` | `[启] capability_candidate_emitted candidate_id=...` |
| Commit message subject (重大改动) | `qi:` 前缀 | `qi: 实装 3 模式 Explorer Pool 第二个 LLM provider` |
| Branch 名 (重大改动) | `qi/...` 前缀 | `qi/multi-provider-pool` |
| PR title | 同 commit subject | `qi: 实装 ...` |
| Docstring / module 顶部注释 | 必标注 "启 (Qi)" 身份 | `"""启 (Qi) — 能力进化系统..."""` |

### 5.2 傩 (Nuo) 命名规则

**身份**：6.5 质量治理与恢复系统负责人。负责识别污染、阻断、风险、质量失败，主理 stub / fallback / EOF / timeout / wrapper / auth / 误路由检测和修复。

**未来定位**：同启，可能独立发布。

| 层级 | 规则 | 例子 |
|---|---|---|
| 文档 / UI | "傩 (Nuo)" 双名 | "傩 (Nuo) 判定 round invalid" |
| 公共 API | `class Nuo`; 内部可保留 `SupervisorService` | — |
| Module path | `kun/agents/nuo/` (V7 迁移目标) | 当前 `kun/agents/supervisor/` → V7 重命名（supervisor 是 V6 历史包袱，跟"质量治理与恢复"语义不完全契合，V7 改 nuo） |
| Event type | `nuo.*` | `nuo.contamination_detected`, `nuo.clean_retest_passed` |
| Log | `[傩]` | `[傩] stub_echo_detected task_id=...` |
| Commit subject | `nuo:` | `nuo: 加污染样本库 EOF 分类` |
| Branch | `nuo/...` | `nuo/wrapper-version-detection` |
| Docstring | 标 "傩 (Nuo)" | `"""傩 (Nuo) — 质量治理与恢复..."""` |

### 5.3 外部监督者 (External Supervisor)

**身份**：跨 LLM critique 角色，独立于 6 个子系统。**强制 cross-provider**（同 provider 拒启动）。

**未来定位**：可能独立发布（作为 SaaS critique 服务接外部 agent 系统）。

| 层级 | 规则 | 例子 |
|---|---|---|
| 文档 / UI | "外部监督者 (External Supervisor)" 双名 | — |
| 公共 API | `class ExternalSupervisor` | — |
| Module path | `kun/agents/external_supervisor/` (V7 保留) | 已有 |
| Event type | `external_supervisor.*` 或缩写 `xs.*` | `xs.verdict_alarming` |
| Log | `[外部监督]` | `[外部监督] verdict=concerning rationale=...` |
| Commit subject | `xs:` | `xs: 强制 cross-provider config 校验` |
| Branch | `xs/...` | `xs/cross-provider-enforcement` |
| Docstring | 标 "外部监督者" 身份 | — |

**不取中文别名** — "外部监督者" 在中文文档里直接用，没有像启/傩那样的单字别名。

### 5.4 三者独立可发布预留

启 / 傩 / 外部监督者三者将来可能各自独立成 agent 产品。命名约定保证：

1. **代码边界清晰**：`kun/agents/qi/`、`kun/agents/nuo/`、`kun/agents/external_supervisor/` 三个独立 package，无 cross-import 到对方内部实现（只能 import 对方 public API）。
2. **公共 API 稳定**：`class Qi`、`class Nuo`、`class ExternalSupervisor` 是对外契约，重构内部实现不破坏 API。
3. **事件命名空间隔离**：`qi.*`、`nuo.*`、`external_supervisor.*` 各自独立，不冲突。
4. **文档独立**：未来三者各自有独立 README / API doc / examples，可独立发布。
5. **测试独立**：`tests/unit/test_qi_*.py`、`tests/unit/test_nuo_*.py`、`tests/unit/test_external_supervisor_*.py` 各自独立。

### 5.5 其他角色命名

| 角色 | 中文 | 英文 | Module path | Event prefix |
|---|---|---|---|---|
| Director (调度) | 督师 | Director | `kun/agents/director/` | `director.*` |
| Executor (执行) | 执行 | Executor | `kun/agents/executor/` | `executor.*` |
| Tester (验师) | 验师 | Tester | `kun/agents/tester/` | `tester.*` |
| Gate (门禁) | 门禁 | Gate | `kun/agents/gate/` | `gate.*` |
| Mission Director (交付总监) | 交付总监 | Mission Director | `kun/agents/mission_director/` (V7 新建) | `mission_director.*` |

### 5.6 V6 → V7 命名迁移策略

代码层迁移按**渐进**原则，不一次大改：

| 阶段 | 动作 | 风险 |
|---|---|---|
| V7-Phase-A | 加新 `kun/agents/qi/__init__.py`，re-export `kun/agents/strategist/`；加 `kun/agents/nuo/__init__.py`，re-export `kun/agents/supervisor/`；加新 module 不删旧 | 零 |
| V7-Phase-B | 新代码统一 import `qi/` `nuo/`，旧代码逐步迁移；CI 加规则禁止直接 import 旧 path | 低 |
| V7-Phase-C | 旧 `strategist/` `supervisor/` deprecate；保留 alias 但加 DeprecationWarning | 中 |
| V7-Phase-D | 删除旧 path，统一 qi/nuo | 高（需所有 caller 迁移完） |

**V7 文档全面采用启/傩中文名 + qi/nuo 英文 path**，代码迁移在 V7 实施阶段按 Phase A→D 渐进。

### 5.7 任务驾驶舱显示规则

UI 必须用中文角色名 + 英文括号，让用户看见 KUN 内部分工：

```
任务 tk-01ABCXYZ 状态：
  ├─ 督师 (Director)         intent_classification: complete
  ├─ 执行 (Executor)         step 5/12 running
  ├─ 验师 (Tester)           pytest passed
  ├─ 门禁 (Gate)             pending approval (high-risk action)
  ├─ 守望 (傩 Nuo)            no anomaly
  ├─ 进化 (启 Qi)             strategy_replay scheduled
  ├─ 交付总监 (Mission Director) alignment: ok
  └─ 外部监督者              verdict: ok
```

---

## 6. 核心用户体验

### 6.1 用户输入目标

用户只需要说明目标、材料、边界和偏好。KUN 负责判断信息是否足够。

**当信息足够时**：KUN 生成 TaskPlanDraft (§10.1)，对齐后写入 ExecutionContract 和验收标准。

**当信息不足时**：KUN 先进入信息补齐流程：

- 向用户提出必要问题。
- 读取用户提供的文件、仓库、网页、系统状态或历史任务。
- 联网检索并筛选可信来源。
- 请求权限、凭据、账号、预算或审批。
- 识别必须由人类或专家提供的判断。

**硬规则**：复杂任务（complexity_score ≥ 0.5）在关键 info_gap 未补齐时，**禁止直接进入长任务执行**。

### 6.1.1 complexity_score 评分协议（V7 新增）

complexity_score 是 0-1 浮点数，由督师 (Director) 在 Intent Triage 阶段计算，**走 engineering-first + LLM 兜底** 路径 (符合 KUN 自己的 `engineering_first_with_llm_fallback` 方法论)。

**简化算法 (3 维规则)**：

| 维度 | 规则 | 加分 |
|---|---|---|
| 任务长度 | < 500 字符 | +0 |
| | 500-1500 字符 | +0.3 |
| | > 1500 字符 | +0.5 |
| 复杂/风险关键词 | 含 "重构 / 迁移 / 蒸馏 / 长任务 / 多阶段 / N 阶段 / 跨天 / 不可逆 / 删 / drop / push --force / 上线 / 发布" 任一关键词 | +0.3 |
| 用户标"长任务" | true | +0.3 |

总分 cap 1.0。

**决策**：

| score | 判定 | 动作 |
|---|---|---|
| < 0.4 | 简单 | **跳过 TaskPlanDraft**，走短任务路径 |
| 0.4 ≤ score < 0.6 | **边缘** | **督师 1 次 LLM call 二次判定** ("此任务简单/复杂? 给 0-1 分") |
| ≥ 0.6 | 复杂 | **强制 TaskPlanDraft + Plan Alignment** |

（**调整**: V7 §6.1 之前写的 "complexity_score ≥ 0.5" 改为 "≥ 0.6"，给边缘档留 0.4-0.6 让 LLM 兜底判，避免规则一刀切误判。所有引用 `≥ 0.5` 的地方都对应改为 `≥ 0.6`）

**反例兜底**:
- LLM 兜底 unavailable (API 挂 / 预算 0) → **默认 0.6** (保守，走 TaskPlan 不会错)
- 用户主动指定 `task_priority='long_task'` → 直接 score=1.0，绕过算法

**可观测**：complexity_score 写入 `TaskPlan.metadata.complexity_score` 字段，驾驶舱可见，审计可追溯。

**简化原则** (回应"不要太复杂"约束): 只 3 维，不引入额外的任务类型分类 / risk_level 单独评分等。这 3 维已覆盖 95% 任务的复杂度判定，剩余 5% 边缘走 LLM。

### 6.2 用户查看进度

用户通过任务驾驶舱查看进度（§20），而不是阅读工程日志。驾驶舱必须展示：

- 当前目标和任务方案版本（含 TaskPlanVersion id）。
- 总进度、当前阶段、下一步。
- 已完成产物和交付物位置。
- 正在执行的工作项、负责人、预计完成时间。
- worker 槽位、资源锁冲突、等待原因、沙箱隔离等级和并发健康度。
- 风险、阻断、失败分类和恢复动作。
- 质量门禁、验收状态、测试状态和证据覆盖。
- 需要用户确认的事项、截止时间和默认处理方式。
- 成本、耗时、资源使用和审计记录。
- **双线监督状态**（§10.2）：工程线 / 方案线分别显示健康度 + 偏离信号。
- **multi-LLM ensemble 状态**（§11）：每个 LLM 当前 verdict + divergence 程度。

### 6.3 用户参与协同

KUN 在必要时通过协同票据向人类请求输入。每张票据必须说明：

- 为什么需要人参与。
- 需要谁处理。
- 需要做什么决定或提供什么材料。
- 不处理会影响什么。
- 超时后系统如何处理。
- 用户拒绝或无法提供时如何收尾。

用户回复后，KUN 必须把回复写回任务状态，恢复执行或更新任务方案。

### 6.4 用户验收交付

KUN 的交付**不是一句总结**，而是交付包（DeliveryManifest）。交付包必须包含：

- 最终产物（ArtifactRecord 引用）。
- 产物清单（每个产物的 path / 类型 / 生成者 / 关联 work item）。
- 证据和来源。
- 测试与评审结果。
- 风险和限制。
- 未完成项和原因。
- 验收方式和后续动作（AcceptanceReview）。

---

## 7. 标准任务生命周期

KUN 的任务生命周期固定为 18 阶段，**任务方案先行硬规则**：阶段 5 (TaskPlan) **必须**在阶段 10 (Execute) 之前，且必须 alignment OK 才能推进。

```text
1.  Intake：接收用户目标
2.  Intent Triage：识别意图、任务类型、风险等级和必要信息
3.  Info Gap：判断信息完整性
4.  Info Acquisition：询问、检索、读取资料、请求授权
5.  TaskPlanDraft：形成任务方案草案（强 enforce, §10.1）
6.  Plan Alignment：用户/Mission Director 对齐 TaskPlan
7.  Execution Contract：生成执行合同
8.  Decomposition：拆解为工作项
9.  Queue：进入持久队列
10. Execute：调度工具、worker、外部资源或人类
       ├─ 工程线监督（§10.2 傩/Watchtower/Tester/Gate）
       ├─ 方案线监督（§10.2 Mission Director + 启 strategy replay）
       └─ 跨线监督（External Supervisor 跨 LLM critique）
11. Observe：记录状态、证据、日志、成本和风险
12. Evaluate：按门禁评估产物
13. Repair：修复、回滚、改计划、复测或转人工
       ├─ 工程线动态优化：改代码
       └─ 方案线动态优化：改方案（PlanChangeProposal）
14. Merge：合并多方产物、证据、测试和评审
15. Deliver：生成可验收交付包（DeliveryManifest）
16. Accept：完成验收、返工或收尾（AcceptanceReview）
17. Learn：归因、沉淀、验证能力（启 capability candidate）
18. Govern：治理权限、成本、健康度和能力库（启 capability lifecycle）
```

任务方案贯穿整个生命周期。任何工作项、门禁、交付包和返工都必须能追溯到当前 TaskPlanVersion。

执行中出现新信息、风险、成本变化、验收变化、路径失败或范围偏移时，KUN 必须触发 PlanChangeProposal 协议（§10.3），更新任务方案并记录原因。高影响变更必须请求用户或授权人确认。

长任务恢复后，KUN 必须先重建任务方案、上下文、等待项、风险和下一步，再继续执行。

---

## 8. 双账本边界

KUN 共享同一个 Control Plane，但必须把**执行用户任务**和**改进 KUN 自身**分成两条账本、两套门禁和两类产物。

### 8.1 用户任务账本

包括 mission、task plan、work item、artifact、delivery manifest、acceptance review 和用户交付包。

用户任务**可以产生学习信号**，但这些信号只能作为**候选证据**进入治理链路，**不能在任务执行路径里直接修改 KUN 默认能力、runner 行为、生产配置或 runtime profile**。

### 8.2 KUN 自身迭代账本

包括启/傩、自我修复、能力晋级、runtime profile、daemon/Control Plane 代码与配置变化。

它只能进入 `self_improvement`、governance、capability candidate、promotion gate 和 rollback plan 路径。**一次用户任务成功不能自动启用新能力，也不能把候选能力显示成生产默认能力**。

### 8.3 双账本对接 RSI（V7 关键升级）

V6 的双账本表述容易让人误解为"用户任务不能触发 RSI"。V7 明确：

- 用户任务**可以触发 RSI**（启发现 KUN 在执行中的不足）。
- 用户任务的产物（如 dogfood v8 输出的 5 张 yaml seeds）**不能直接进 production 路径**。
- 必须走 §12 严格验收：candidate → replay → holdout → shadow → canary → production，5 阶段全过 + production-mode flip 必须 explicit user approval。
- 失败可 rollback / retire。

详见 §12 RSI 闭环。

### 8.4 运行时约束

任何会让 `CapabilityProfile(runtime_enabled=true)` 进入 production 默认运行时的 promotion，都必须绑定已注册的 `self_improvement` mission 和 learning-stage gate。普通 `product_development`、`ops_tooling` 或外部用户任务的学习结果只能写入 learning signal、artifact 或启/傩 follow-up，**不能直接修改 runner 默认行为、runtime profile 或生产配置**。

### 8.5 基础设施 vs 自我迭代语义区分

V6 line 164-171 已经定义，V7 保留：

- daemon refresh 是 Control Plane 基础设施能力，用来发现共享 store 里的任务队列变化；**不代表 KUN 自身能力自动进化**。
- scoped mission refresh 只验证已存在 mission 后续追加 work item 可被执行；**不能写成自我迭代恢复**。
- stop request clear 是 daemon 进程生命周期控制；**不是 mission cancel，也不是任务级暂停/恢复**。
- game production write boundary 是任务执行隔离门禁；**不是能力晋级**。

只有真正测试启/傩/capability governance 时，才允许使用 KUN self-improvement 或 self-iteration 命名。

---

## 9. 一级子系统

KUN 由 **7 个一级子系统** 构成。通信、上下文、权限、评估、预算、压缩、审计、恢复是全系统协议，不再拆成重复子系统。

| # | 子系统 | 负责人 | V6 状态 | V7 状态 |
|---|---|---|---|---|
| 9.1 | 任务理解与方案系统 | 督师 (Director) | V6 6.1 | 保留 + 方案动态优化协议升级 |
| 9.2 | Control Plane 执行控制系统 | 执行 (Executor) + Daemon | V6 6.2 | 保留 |
| 9.3 | 知识与证据系统 | — | V6 6.3 | 保留 |
| 9.4 | 协同与资源调度系统 | — | V6 6.4 | 保留 |
| 9.5 | 质量治理与恢复系统 | 傩 (Nuo) | V6 6.5 | 保留 + 双线监督工程线升级 |
| 9.6 | 能力进化系统 | 启 (Qi) | V6 6.6 | 保留 + 多 LLM Explorer Pool 强 enforce |
| 9.7 | **任务交付监督系统** | 交付总监 (Mission Director) | V6 line 227 散落 | **V7 新升级为一级子系统** |

外部监督者（External Supervisor）独立于 7 个子系统，跨 LLM critique 角色（§9.7 之外，作为系统级 critic）。

### 9.1 任务理解与方案系统（督师 Director）

任务理解与方案系统负责把用户目标变成可执行、可验证、**可更新**的任务方案。

**核心能力**：

- 意图识别和任务类型判断。
- 信息完整性判断。
- 主动提问、资料读取、联网检索和授权请求。
- 真实业务目标、用户约束、验收标准和风险识别。
- 任务拆解、依赖识别、里程碑、资源计划和交付标准。
- 执行中动态改计划（PlanChangeProposal 协议，§10.3）。
- 方案版本管理和偏移检测。
- **任务方案先行（V7 强 enforce）**：复杂任务必须先出 TaskPlanDraft → 对齐 → 才进入执行。
- **方案动态优化（V7 新增）**：执行中接收来自方案线监督（§10.2）的偏离信号或错漏信号，触发 PlanChangeProposal。

**任务方案必须包含**：

- 目标、范围、非目标。
- 成功标准和验收方式。
- 已知信息和缺口。
- 任务拆解和依赖关系。
- 执行资源、工具、worker 和权限。
- 风险、阻断和恢复策略。
- 证据计划和交付物计划。
- 人机协同点。
- 质量门禁。
- 预算和时间约束。
- **方案版本 metadata（V7 新增）**：TaskPlanVersion id, 上一版 id, change rationale, alignment approver。

### 9.2 Control Plane 执行控制系统（执行 Executor + Daemon）

Control Plane 是 KUN 的运行中枢，负责把方案变成可持续执行的系统。

**核心能力**：

- 持久任务队列。
- 状态机。
- 后台 daemon。
- 自动醒来和自动拿任务。
- 进程 supervisor。
- runner 注册、lease、heartbeat、timeout、retry、cancel、resume。
- 多任务 worker pool：同一个 daemon 内必须有 worker 槽位模型，多 daemon / 多机器必须能通过持久 resource lock 和 work item lease 协调，避免重复领取、重复写入或同时修改同一工作区。
- 本机多进程 worker pool：Control Plane 必须能生成多个 daemon 服务实例，每个实例有独立 heartbeat/state，但共享同一任务队列和 SQLite/file resource lock；这是单机 7x24 并发的默认生产路径。跨机器 worker pool 必须走 Redis/数据库级 resource lock 适配层。
- 断电、重启、崩溃、跨天续跑。
- 权限、预算、外部动作审批和审计。
- 产物、证据、日志、账本和门禁统一管理。
- AB runner、真实长任务 runner、工具 runner 和外部 worker runner 的统一接入。
- KUN 通用真实任务 runner：`kun` owner 的 execution、research、review、test、merge 工作项必须有默认执行路径。
- 定时进度汇报和任务驾驶舱 API。
- 运行时功能激活层（6 层证据，§16）。
- 受限预执行层：在主 runner 执行前，按 work item 的 workspace、skill 和外部信息信号运行安全预检查。
- **启/傩默认激活层**：daemon 默认注册启 runtime governance runner 和傩 runtime repair runner。
- **Mission Director 默认激活层（V7 升级）**：daemon 默认注册交付总监 runner（§9.7），任务级监督。
- 信息缺口主动协同层：处于 planning/info_gap 且存在 TaskPlan.info_gaps 的任务，daemon 必须自动生成协同票据。
- 执行型 skill 默认沙箱边界。
- 容器级隔离协议。
- 文件级沙箱快照和真实回滚。
- V7 Watchtower 桥接：work item 完成、失败、门禁评估等运行事件必须能进入守望规则引擎。
- 运行时观察清单：daemon 每次 tick 必须为任务生成机器可读的 observation report，标注当前最该观察的未激活能力、runner 缺口、预执行失败、协同票据、Watchtower 触发、能力重复、交付清单缺失等问题，并明确路由给 KUN、启、傩、人类、Control Plane、Mission Director 或外部监督者。
- 任务模板隔离。

**Control Plane 必须保证**：

- 所有任务状态可恢复。
- 所有状态变更可审计。
- 所有外部动作可追踪。
- 所有失败有分类和下一步。
- 所有交付有产物、证据和验收记录。
- 多任务并行先经过依赖、resource lock、work item lease 和 worker slot 分配。
- 执行/test/merge/repair/retest/rollback 等可能写入工作区的 work item 如果没有显式 workspace 锁，自动退回 mission-workspace 锁。
- 合并多 worker 产物时必须做冲突治理。
- 沙箱、快照、回滚是执行前自动生成、执行中可引用、失败时可运行的恢复路径。
- Watchtower 规则是治理和异常检测入口，不是旁路仪表盘。
- 已开发能力不得长期停留在"存在但未触发"状态。
- 外部样本对比必须输出机器可读的启治理动作。
- 已经进入代码的功能必须有默认触发路径或明确启用开关。
- 鲲在执行真实任务时主动标注"需要重点观察什么"，外部监督者、启和傩、Mission Director 消费同一份 observation report。
- 生产能力去重不能只由 daemon 静默处理。
- 功能激活审计：每个已开发功能转成 Control Plane 触发任务，实际运行后输出触发条件、依赖协同关系、证据、未激活缺口和后续修复任务。

**功能激活按 6 层证据判断**（§16 详述）。

### 9.3 知识与证据系统

知识与证据系统负责让 KUN 能获取、理解、筛选和引用外部信息。

**核心能力**：

- 读取文件、仓库、网页、文档、数据库和历史任务。
- 联网检索和多源交叉验证。
- 信息时效性判断。
- 来源可信度评估。
- 证据引用、摘要、冲突标记和证据链。
- 知识卡片、任务上下文、压缩摘要和恢复摘要。
- 将外部资料转化为任务可用约束、事实和验收依据。

**证据必须结构化保存**，至少包含：来源、时间、内容摘要、适用范围、可信度、过期风险、关联任务和被哪个产物使用。

**V7 新增**：dogfood / 自检任务必须用 `self-reflect` skill（白名单 read + 单写出目录 + offset+limit）和 `grep-verify` skill（结构化 grep + verdict 字段），不允许走 file-io / shell-exec 自由读写。

### 9.4 协同与资源调度系统

协同与资源调度系统负责把人、工具、外部 agent、专家和 worker 纳入统一任务执行链路。

**核心能力**：

- 人机协同票据（CollaborationTicket）。
- 专家输入和外部 worker 调度。
- 多 worker 分发与合并。
- worker pool、持久 resource lock、work item lease、并发等待原因和资源冲突治理。
- 多进程 daemon fleet。
- 工具边界、权限边界和责任边界管理。
- 冲突检测、依赖管理和产物合并。
- merge conflict governance。
- 超时、拒绝、无响应、返工和替代路径处理。
- 协作者输出的信用分配和审计。
- **决策点 classifier（DIST-C）**：6 类硬规则（destructive_action / auth_posture_change / external_api_unlock / product_direction / out_of_anchor / no_decision）作为 safety net，方案线监督的输入之一。

**KUN 可以调用外部资源，但必须区分**：

- KUN 自动完成的能力。
- 人类或外部 worker 提供的输入。
- 工具执行产生的结果。
- 外部 agent 作为参考或被调度对象的结果。

这些来源必须在交付包和学习系统中保持清晰边界。

### 9.5 质量治理与恢复系统（傩 Nuo）

质量治理与恢复系统由 **傩 (Nuo)** 承担，负责识别污染、阻断、风险和质量失败。

**核心能力**：

- 识别 stub、fallback、空输出、模板输出、无效交付。
- 识别误路由、错误任务族、wrapper 变更、接口不兼容。
- 识别 timeout、EOF、网络阻断、auth failure、permission denied。
- 识别报告缺失、互评缺失、产物缺失、测试缺失、证据缺失。
- 区分系统污染、环境阻断、工具失败、执行失败和能力失败。
- 自动生成修复、回滚、复测、改计划或转人工动作。
- 对污染任务判定 invalid，禁止把污染结果算作 KUN 能力失败。
- **双线监督工程线主体**（§10.2）：傩 + Watchtower + Tester + Gate 共同构成工程线，识别执行级异常。
- **bug_root_cause_cases 库**（DIST-E）：积累的 bug 根因案例，下次类似问题 lookup 命中提速诊断。

**质量治理必须覆盖**：

- 输入质量。
- 执行过程质量。
- 产物质量。
- 证据质量。
- 测试质量。
- 安全和权限质量。
- 用户验收质量。
- 学习晋级质量。

**RCDH 4 层诊断**（V6 既有，V7 保留）：runtime / contract / data / hardware 4 层根因排查，由傩主理。

### 9.6 能力进化系统（启 Qi）

能力进化系统由 **启 (Qi)** 承担，负责把经验转化为受控能力。

**能力来源包括**：

- 真实任务复盘。
- 用户验收和返工原因。
- AB 回归和 peer gap。
- OpenClaw、Hermes、Claude Code 等能力样本。
- 企业项目和开源项目经验。
- 外部专家经验。
- 工具失败和系统恢复记录。
- **启自身的 strategy replay**（V7 升级）：跑完任务后启自己反思"如果换 X 方案更好" → process_audit + capability_candidate。

**能力生命周期固定为**：

```text
Observation → Candidate → Replay → Holdout → Shadow → Canary → Production → Monitor → Rollback / Retire
```

详细 lifecycle 规则见 §15。

**3 模式 Explorer Pool（V7 强 enforce）**：

启在 anomaly 触发时必须并行产 3 个候选实验：

| 模式 | 默认 LLM provider | 风险特征 | sampling_rate | rollout_mode |
|---|---|---|---|---|
| Conservative | 小模型快（Anthropic Haiku 类 / **本地小模型如 Ollama**） | 最小风险，改路由/参数不改架构 | 高（30-50%） | shadow / replay |
| Aggressive | 大模型深（Anthropic Opus / gpt-5 类） | 大胆改动（换 primary / 拆 task_type） | 低（5-15%） | canary |
| Performance | 中等（Anthropic Sonnet 类 / **本地中等模型**） | Shadow 不影响生产，优化指标 | 中（10-30%） | shadow |

**强制 cross-provider**：3 模式必须 wire 至少 2 个不同 provider 的 LLM（同 provider 拒启动）。

**本地模型降本路径（V7 新增）**：

Conservative 和 Performance 模式可以**优先选本地部署模型**（Ollama / vLLM）降本，因为这两个模式样本率高 / 频繁跑，cloud LLM 累积成本快。Aggressive 模式因为样本率低 + 决策影响大，**仍建议 cloud 大模型**。

⚠️ **诚实成本声明** (回应 V7 攻击审视): 本地 LLM 不是零成本。需 GPU 硬件 (A100 / RTX 4090, 月折旧 $300-600/卡 + 电费)。**仅在用户已有 GPU 资源场景下边际成本低**。新采购 GPU 跑 Explorer Pool 经济不划算。具体月成本数字 V7 不写, 待真实部署一周后用 metrics 校准 (附录 A Phase 0)。

**本地模型在 cross-family 校验里算独立 family**: 本地 Ollama 跑的 Qwen / Llama vs cloud Anthropic 的 Claude, 满足"不同 family"约束 (详见附录 B 术语表 cross-family 定义)。

**自指限制（V6 既有，V7 保留）**：target_module ∈ {strategist.*, supervisor.*, gate.*, director.*, mission_director.*} → 强制 requires_human_review=True，Gate 拒绝自动启用。

**启的 strategy replay 三类产物**（V6 line 377 既有，V7 升级为运行时 enforce）：

1. **strategy_replay_report** — 证明重新跑过，旧策略 vs 新策略指标对比。
2. **process_audit** — 说明原链路哪里浅、哪里错、哪里该问人。
3. **capability_candidate / replay_profile** — 只记录可复用改进，不进入默认运行时。

**缺任一证据**：只能算"启已诊断"，不能算"启已沉淀"。通过测试也只能进入 replay/holdout，不能跳过 shadow/canary/production。

### 9.7 任务交付监督系统（交付总监 Mission Director）— V7 新一级子系统

V6 line 227 把 Mission Director 写在 Control Plane 子层，V7 升级为**独立一级子系统**。

**身份**：任务级监督角色，**可单独配置 model、provider 和档位**，不替代启、傩或具体执行 runner，只对 mission 交付闭环拥有**监督和阻断权**。

**核心职责**：

1. **任务方案对齐监督（方案线主线）**：持续审查任务执行是否仍在 TaskPlanVersion 范围内，发现偏离生成 PlanAlignmentEvent。
2. **信息缺口监督**：审查 info_gap 是否真补齐，避免半补齐就开跑。
3. **任务拆解审查**：审查 work item 拆解是否覆盖任务方案的所有 deliverable。
4. **worker 分配审查**：审查 worker 配置是否匹配 work item 风险等级和 skill 要求。
5. **证据审查**：审查 evidence ledger 是否真覆盖 TaskPlan 声明的证据计划。
6. **验收状态审查**：审查 AcceptanceReview 是否真满足用户验收方式，**禁止把测试/自评分/门禁通过误判为最终交付完成**。
7. **方案错漏发现**：发现 TaskPlanVersion 本身设计错漏（验收标准不可达 / 资源不足 / 范围矛盾），触发 PlanChangeProposal。

**调度优先级**：必须**高于普通业务执行、最终交付和自动返工**，确保监督先于继续开发或关闭任务。

**输出产物**：

- `MissionAlignmentReview`（每 tick / 每 milestone 生成）
- `PlanChangeProposal`（发现方案错漏时）
- `GateEvaluation`（阻断 deliver / accept 时）

**监督结论必须参与任务状态机**：若发现任务方案不完整、worker 分配缺失、产品体验证据不足、人工/目标用户验收缺失，或 runner 把测试/自评分/门禁通过误判为最终交付完成，必须生成 review artifact 和 GateEvaluation，并把任务推进到 needs_info、needs_human 或 needs_plan_change，**而不是允许直接关闭任务**。

**与启 / 傩 / 外部监督者 / Watchtower 分工**：

| 角色 | 关注层 | 触发时机 | 输出 |
|---|---|---|---|
| 交付总监 | **任务方案 + 交付闭环** | 持续 (每 tick / 每 milestone) | MissionAlignmentReview / PlanChangeProposal / GateEvaluation |
| 启 (Qi) | **方案级反思** (后置) | 任务跑完后 / 异常累积时 | strategy_replay_report / process_audit / capability_candidate |
| 傩 (Nuo) | **执行级污染** | 实时事件流 | 污染分类 + 修复票据 |
| 外部监督者 | **跨 LLM critique** | 周期触发 / 重大决策点 | verdict (ok / concerning / alarming) + drift_signals |
| Watchtower | **规则级异常** | 事件流匹配规则 | rule fired / 告警 / 暂停 |

四者职责不重叠，串行 / 并行运行不冲突（详见 §10.2）。

---

## 10. 任务方案先行 + 双线监督协议（V7 核心新章节）

V6 把方案先行 + 双线监督相关协议散落在 5.1、6.1、6.2 各处，V7 把它们集中到一个章节，强制 enforce。

### 10.1 任务方案先行（硬规则）

**复杂任务**（complexity_score ≥ 0.5 或 risk_level ∈ {medium, high}）：

1. **第 1 动作必须是生成 TaskPlanDraft**，不允许直接执行。
2. TaskPlanDraft 由督师 (Director) 生成，督师必须在 ≤ 2 次 LLM 调用内出方案（避免无限思考）。
3. TaskPlanDraft 必须经历 **Plan Alignment** 阶段：
   - 信息完整 → 直接 alignment_status='aligned' 推进
   - 信息有缺口 → 进 `Info Acquisition` 补齐
   - 用户/Mission Director 显式拒 → alignment_status='rejected' → 改方案
4. **未 alignment 前禁止进入 Execute 阶段**。Control Plane 状态机强 enforce。
5. TaskPlanVersion 是 first-class object，可 query，可 diff，可 rollback。

**简单任务**（complexity_score < 0.5 且 risk_level=low）：

可以跳过 TaskPlanDraft 直接走短任务路径，但 Control Plane 仍记录隐式 plan（intent / 范围 / 预期产物）。

**KUN 自身任务也走方案先行**（V7 重申）：

- KUN 给 sub-agent 派任务时，必须先生成 sub-task plan。
- KUN 自我迭代任务（capability candidate / replay）也必须先有 self_improvement plan。
- Dogfood 任务必须先有 dogfood plan（§19）。

### 10.2 双线监督

执行中**两条监督线并行跑**，独立报警，独立优化。

#### 10.2.1 工程线

| 监督主体 | 监督内容 | 触发频率 | 输出 |
|---|---|---|---|
| 傩 (Nuo) | stub / fallback / EOF / timeout / wrapper / auth / 误路由 / 报告缺失 | 实时事件流 | NuoHealthFinding + 污染 GateEvaluation |
| Watchtower 规则引擎 | 规则匹配（llm_fallback_spike / budget_burnrate / tool_failure_clustering 等） | 事件流匹配 | rule fired alert + 自动动作 |
| 验师 (Tester) | pytest / ruff / mypy / 测试覆盖 | 每 commit / 每 work item | ValidationReport |
| 门禁 (Gate) | destructive action approval / 风险阻断 | 风险动作前 | GateEvaluation (approve / reject) |

**工程线发现的问题** → **改代码**（动态优化路径之一，§10.3.1）。

#### 10.2.2 方案线

| 监督主体 | 监督内容 | 触发频率 | 输出 |
|---|---|---|---|
| 交付总监 (Mission Director) | TaskPlan 对齐 / info_gap 真补齐 / 拆解覆盖度 / worker 分配 / 证据覆盖 / 验收闭环 | 持续 (每 tick / 每 milestone) | MissionAlignmentReview / PlanChangeProposal / GateEvaluation |
| 启 (Qi) strategy replay | 任务跑完后/累积异常时反思"换 X 方案更好" | 后置触发 | strategy_replay_report + process_audit + capability_candidate |

**方案线发现的问题** → **改方案**（动态优化路径之二，§10.3.2）。

#### 10.2.3 跨线（系统级 / 外部视角持续监督，V7 升级）

**核心升级**：V7 把 External Supervisor 从"周期触发"升级为"**风险等级驱动的持续并行 watchdog**"。

用户实证支撑（本 V7 设计依据）：用户在使用 KUN 时一直用 gpt-5.5 当外部监督者，动态发现了多个问题并修复了代码（包括本 V7 起草过程中我自己跑偏砍架构的提议）。这证明外部 LLM 持续监督**在长任务/高风险场景真有效**。但**不是机械式 always-on** — 短任务无脑挂会烧钱且告警疲劳。

##### 监督主体 — 外部监督者 (External Supervisor)

| 维度 | V6 / V7-draft (episodic) | V7 最终版 (持续并行 watchdog) |
|---|---|---|
| 触发 | 每 K 步 / 重大决策点 | **daemon-registered 长驻 runner**, 按风险等级自动调 tick interval |
| Provider | 跨 LLM | **跨 family LLM**（详见 §11.2 cross-provider 定义和附录 B 术语表） |
| 用户控制 | 隐式 | **显式开关**（global default + per-task override + 驾驶舱实时切换） |
| 风险驱动 | 无 | **自动 on/off** + tick interval 按风险调 |
| 成本控制 | 单一 cloud LLM | **本地 LLM 优先降本**（详见 §11.3） |
| **监督范围 (V7 修复)** | 只看主 Executor | **看主 Executor + Mission Director + 启 + 傩 的关键输出** (跨 LLM critique 所有监督角色, 详见 §10.2.5) |

##### 风险等级驱动 on/off + tick interval 矩阵

⚠️ **具体数字 (tick interval / 月成本) 待真实数据校准**。V7 暂定起步值，跑一周真实数据后再调。

| 任务类型 | 默认 mode | 初版 tick interval (起步值, 待调) | 推荐 provider |
|---|---|---|---|
| 短任务 (<5min, low risk) | **off** | — | — |
| 中等任务 | **episodic milestone** | per milestone | 任意 cross-family |
| 长任务 (>30min, multi-step) | **continuous** | 起步 120s, 实际跑一周看是否够灵敏再调 | **本地模型优先** |
| 高风险 (destructive / 不可逆) | **continuous + 较高频** | 起步 60s, 真实数据再校准 | **必须 cross-family cloud** |
| RSI / 自我修改 KUN | **continuous + cross-family 强制** | 起步 120s | 必须不同 family |
| Dogfood 任务 | **continuous + cross-family 强制** | 起步 120s | 必须不同 family |
| AB / Frontier50 round | **episodic per round** | per round | 任意 cross-family |

**月成本估算说明 (V7 不写具体数字)**：cost 高度依赖 (a) 选哪个 model (b) 真实 tick 数 (c) 单次 critique 的 prompt 长度 (d) 本地 vs cloud。这些数字必须**真实运行一周后**用 metrics 收集校准，不允许在 V7 写具体数字诱导决策。详见附录 A Phase 0 (运行一周收集真实指标后再 commit V7.1 校准版)。

##### 用户开关协议

**全局默认**:
```
KUN_EXTERNAL_SUPERVISOR_DEFAULT = continuous | episodic | off
KUN_EXTERNAL_SUPERVISOR_TICK_SEC = 30 | 60 | 120 | ...
KUN_EXTERNAL_SUPERVISOR_PROVIDER = anthropic_haiku | local_ollama | ...
```

**per-task override** (在 TaskPlan 里):
```yaml
supervisor_mode: continuous | episodic | off
supervisor_tick_sec: 60
supervisor_provider: local_ollama
```

**驾驶舱实时切换**: 用户可以在任务进行中通过驾驶舱 UI 切换 mode (e.g. 发现告警太多 → 降到 episodic; 发现 LLM 行为可疑 → 升到 continuous + 高频)。切换记入 TaskPlanVersion change log。

##### 监督主体输出 (V7 升级)

每个 tick (或 episodic 触发时), External Supervisor 输出:

```python
@dataclass(frozen=True)
class ExternalSupervisorObservation:
    observation_id: str
    task_id: str
    target_anchor_id: str | None
    verdict: Literal["ok", "concerning", "alarming"]
    drift_signals: list[str]
    self_aggrandizement_detected: bool
    rationale: str
    recent_steps_reviewed: int
    provider_used: str  # e.g. "local_ollama:qwen2.5-72b" or "anthropic_haiku"
    cost_usd: float
    tick_at: datetime
```

**verdict 升级路径** (§10.4 三级信号映射):

- `ok` → 弱信号 (log only)
- `concerning` → 中信号 (推 NUO panel, 启动 review)
- `alarming` → 强信号 (任务 pause + CollaborationTicket 等人)

##### 本地模型降本路径（与 §11.3 协同）

watchdog 用本地模型 (Ollama / vLLM 部署 Qwen / Llama / DeepSeek 等) 跑, **不打主 LLM cloud 账单**。这条路径对**长任务持续监督**特别重要 —— 长任务 watchdog 高频 tick, cloud LLM 累积成本快, 本地 LLM **边际成本接近零** (硬件折旧已存在的前提下)。

⚠️ **诚实成本声明**: 本地 LLM 不是"零成本"。需要 GPU 硬件 (A100 / RTX 4090 等), 硬件折旧 + 电费实际月成本可能 $300-600 / 卡。**只在用户已有 GPU 资源** (开发机 / 公司服务器) 场景下才"边际成本零"。**新采购 GPU 跑 watchdog 经济不划算**。这部分在附录 A Phase X 详述部署条件。

**只对"高风险/不可逆"任务**坚持 cloud 大模型当 watchdog (本地小模型能力差, 抓不到微妙偏移)，其他场景**本地模型默认 (前提是用户已有 GPU)**。

#### 10.2.5 监督者递归不变量（V7 关键新增）

**问题**: 既然 Mission Director / 启 / 傩 / External Supervisor 都是 LLM 跑的 agent, 它们自己跑偏了谁拦? 本 V7 起草过程暴露的反例: 我 (扮演 Mission Director 候选角色) 反复跑偏砍架构, **是用户 (外部) 拦的, 不是 KUN 内部任何角色**。

**V7 解 (A + C 组合, 不加新角色)**:

**A. External Supervisor 跨 LLM critique 的范围扩大 — 不光主 Executor，还看所有监督角色的关键输出**

External Supervisor 默认监督对象升级为:
- 主 Executor 的 LLM 行为 (drift / self-aggrandizement / hallucination) — 已有
- **Mission Director 输出的 PlanChangeProposal + MissionAlignmentReview** (V7 新增)
- **启 (Qi) 输出的 capability_candidate + strategy_replay_report + process_audit** (V7 新增)
- **傩 (Nuo) 输出的 NuoHealthFinding + 污染分类** (V7 新增, 抽样检查)

External Supervisor 不增加 tick 次数, 只在每 tick 看的内容里加入"最近一次监督角色输出"摘要 (token 增加可控)。

**C. 严重决策走用户兜底**

V7 §10.3.3 决策权三档已有 (Low/Medium/High)。本不变量补强:

- Mission Director / 启 / 傩 任一发出 **High 等级** 动作 (e.g. PlanChangeProposal high / capability_candidate 进 Production 阶段 / 大范围回滚) → **强制 CollaborationTicket 等人审**, 不允许自动执行
- External Supervisor 自己 verdict='alarming' → 强制 user notification + 可干预

**不变量声明** (V7 顶层):

> **任何监督角色 (含 External Supervisor 自己) 的关键决策必须能被另一个跨 family LLM 看见, 且 high 等级动作必须有用户兜底**。

**为什么不引入新角色** (回应"不要太复杂"):

- 加"长老 / 总监督"角色会无限递归 (谁监督长老?), V7 拒绝
- 加多份 Mission Director 实例 peer-review 太贵, V7 拒绝
- A 解 "自动监督" + C 解 "重大兜底", **两层够了, 链条到 user 截止**

**实施成本**: External Supervisor critique prompt 扩展 (+200 字符量级), 不增加角色 / 不增加进程。

#### 10.2.4 监督主体不冲突原则

四类监督主体（傩 / Watchtower / Tester / Gate / Mission Director / 启 / External Supervisor）**职责分开，不重复**：

- 傩 / Watchtower / Tester / Gate → **执行级**（代码 / 运行时事件）
- Mission Director → **任务级**（方案 / 闭环）
- 启 → **方案级反思**（后置 strategy replay）
- External Supervisor → **LLM 行为级**（跨模型 critique，**含监督所有监督角色**，详见 §10.2.5）

**冲突时优先级原则 (V7 修复)**: 同一信号原则上不允许两个主体同时主张所有权, 实践中可能重叠时按以下原则:
- **更接近问题源头的优先**: 执行级污染由傩第一时间响应, 不让 Mission Director 自作主张
- **更结构化的优先**: Watchtower 规则 vs LLM critique, 规则确定性高优先
- **跨视角的总有最后一票**: External Supervisor 即使不主张所有权, 仍保留"alarming"否决权 (升 user, §10.2.5)

(原 V7 草稿写的 "执行级 > 任务级 > 方案级 > LLM 行为级" 优先级表删除 — 那个顺序跟实际不符: 本 session 用户外部视角拦住我跑偏, 就是 LLM 行为级胜过方案级。所以**不是固定优先级, 是 case by case**。)

### 10.3 双线动态优化

#### 10.3.1 工程线动态优化（改代码）

- 工程线发现问题 → 生成 RepairTicket
- RepairTicket 路径：自动修 (low) / 自动修+通知 (medium) / 等人审 (high)
- 修完跑回归测试，红了再修，循环到绿
- bug 根因写入 `bug_root_cause_cases` 库（DIST-E）

#### 10.3.2 方案线动态优化（改方案）

- 方案线发现问题 → 生成 PlanChangeProposal
- PlanChangeProposal 必须包含：
  - 触发源（哪个 Mission Director / 启 review）
  - 变更类型（scope / criteria / resource / risk）
  - 严重等级（low / medium / high）
  - 影响范围（哪些 work item / 哪些 deliverable）
  - 候选方案（≥ 1 个）
  - 回滚条件

#### 10.3.3 方案变更决策权（按严重等级）

| 等级 | 触发 | 决策权 | 动作 | 例子 |
|---|---|---|---|---|
| **Low** | 子任务顺序换 / 参数微调 / 内部依赖增减 | **KUN 自动改** + log | 直接更新 TaskPlanVersion，capability_candidate 记录 | "拆 phase B 的 read step 为并行 4 个" |
| **Medium** | 范围微调 / 验收标准增减 / 资源补齐 / 工具切换 | **KUN 自动改** + 立即推 NUO panel / 驾驶舱通知 + 用户可一键回滚（72h 内） | 更新 TaskPlanVersion，flag pending_user_review=True | "加一个 holdout 验证步骤" |
| **High** | 任务边界变 / 目标变 / 不可逆动作 / 预算上限突破 | **CollaborationTicket 等人审** | 任务进 needs_human，不执行变更 | "把 mission 从 distillation 改成 production deployment" |

### 10.4 方案错漏发现机制

#### 10.4.1 三类信号源

| 信号源 | 类型 | 触发例子 |
|---|---|---|
| **LLM 判定** | 主观判断 | External Supervisor verdict=concerning/alarming; 启 strategy replay 找到更优方案 |
| **工程指标** | 量化指标 | Watchtower 规则触发（失败率高 / cost 超预算 / latency 异常）; Tester 持续红 |
| **用户反馈** | 用户输入 | 用户在 CollaborationTicket 显式说"方案有问题"; AcceptanceReview verdict=rejected |

#### 10.4.2 三级信号强度

| 强度 | 触发 | 自动动作 | 升级条件 |
|---|---|---|---|
| **弱（log only）** | External Supervisor concerning 1 次 / 工程指标偏离 1 次 / 启 replay 找到边际改进 | log + alignment_status='drifting' | **连续 3 个 tick 内同源弱信号 ≥ 3 次 → 升中** |
| **中（自动改）** | External Supervisor alarming 1 次 / 工程指标连续 3 次偏离 / 启 replay 找到 medium 收益 | 触发 PlanChangeProposal (Low/Medium) | 改方案后 1 个 milestone 内同源信号再现 → 升强 |
| **强（pause+ticket）** | 不可逆错误 / mission level drift / user verdict=rejected / 严重 self_aggrandizement | 任务 pause → CollaborationTicket 等人审 | — |

⚠️ **修复说明**: V7 草稿原写"5 分钟内同源弱信号 ≥ 3 次"。问题: §10.2.3 默认 watchdog tick interval 120s, 5 分钟内最多 2.5 ticks, 永远凑不齐 3 次 → 升级路径死的。V7 修正为**按连续 tick 计数**, 跟 tick interval 解耦。

**严重升级到人**：用户最初要求"严重就上升到人"，由强信号路径 enforce。

---

## 11. multi-LLM ensemble 协议（V7 新章节）

KUN 的核心独特价值（§1.1）是 multi-LLM ensemble + Claude Code 工程纪律。V7 把 ensemble 协议明文化。

### 11.1 multi-LLM ensemble 硬规则

- **复杂长任务必须至少 2 个不同 provider 的 LLM 在线**。同 provider 跑长任务 → 警告 + 降级为短任务模式。
- **External Supervisor 强制不同 provider**（§11.2）。
- **启 3 模式 Explorer Pool 必须 cross-provider**（§9.6）。
- **LLM provider matrix**（§11.3）至少覆盖 2 家。

### 11.2 External Supervisor cross-family 强制

**"cross-family" 定义 (V7 明文, 解决"cross-provider 没定义"硬伤)**:

跨 family = **不同 vendor 的不同 training pipeline**, 具体: Anthropic Claude 系 / OpenAI GPT 系 / Qwen 系 / Llama 系 / DeepSeek 系 / 本地小模型 等是不同 family。

**注意**:
- 同 vendor 不同 tier (e.g. Anthropic Opus + Anthropic Haiku) **不算 cross-family** — 同一 training pipeline 的 bias 一致
- 同 family 的不同 fine-tune (e.g. Llama-base + Llama-Anthropic-distill) **不算 cross-family** — training data 重叠
- 本地小模型 (e.g. Qwen-32B) vs cloud 大模型 (e.g. Anthropic Sonnet) **算 cross-family** — 不同 vendor 不同 pipeline

| 配置 | 行为 |
|---|---|
| Executor LLM = family A，External Supervisor LLM = family B (A ≠ B) | ✅ 启动正常 |
| Executor LLM = family A，External Supervisor LLM = family A (同家) | ❌ 启动失败：`ExternalSupervisorConfigError: same family not allowed` |
| External Supervisor 配置缺失 | ❌ 启动失败：`ExternalSupervisorConfigError: cross-family required for long task` |

**Config 校验在 daemon 启动时执行**，不允许运行时 fallback 到同 family。

详细 family 分类表见附录 B 术语表。

### 11.3 LLM provider matrix

V7 支持的 provider family 至少 (cross-family 定义见附录 B):

| Provider family | Tier | 合规级别 | 用途 |
|---|---|---|---|
| Anthropic（Opus / Sonnet / Haiku） | top / strong / cheap | 🟢 API key / 🟡 OAuth subscription | 主 Executor / External Supervisor / 启 Aggressive |
| OpenAI / Codex（gpt-5 / gpt-5.5） | coding / top | 🟢 API key | 主 Executor (编码任务) |
| **本地（Ollama / vLLM 部署 Qwen / Llama / DeepSeek 等）** | **local** | 🟢 完全合规 | **watchdog / 启 Conservative & Performance / 离线 / 敏感数据** |
| MiniMax | fallback | 🟢 API key | 网络受限 fallback |

**合规级别说明**:
- 🟢 **完全合规**: API key 路径或本地部署, ToS 无争议, 商用部署默认推荐
- 🟡 **灰色合规区**: OAuth subscription token, 个人开发自用可, **商用部署建议改 API key 路径** (与 §22.4 一致)
- 🔴 **不推荐**: 多账号轮换 / 凭据共享等 — 不在 V7 支持范围

**最小生产配置**：至少 2 个 provider family 同时在线（一个主一个 critique），**不允许单 family 运行长任务** (跨 family 定义见附录 B)。

**本地 LLM 跟 cloud 跨 family**: 本地 Ollama 跑的 Qwen / Llama / DeepSeek 跟 cloud Anthropic / OpenAI 是不同 family, 满足 "cross-family" 约束。这让本地降本路径合规地满足 External Supervisor cross-family 强制要求 (§11.2)。

⚠️ **具体月成本数字 V7 不写** (回应 V7 攻击审视). cost 高度依赖: (a) 选哪个 model (b) 真实 tick 频率 (c) 单次 prompt 长度 (d) 本地 vs cloud (e) 是否有现成 GPU. 任何在 V7 写的具体数字都会误导决策。**附录 A Phase 0 规定: 部署一周收集真实 metrics 后才能在 V7.1 写校准数字**。

**本地 vs cloud watchdog 能力定性对比** (定量数据待校准):

| 场景 | 本地小模型 (Qwen-32B 类) | Cloud 小模型 (Haiku 类) | 推荐 |
|---|---|---|---|
| 工程 drift (函数走错 / 改错文件) | 大概率抓得到 | ✅ | 本地 (前提: 用户已有 GPU) |
| 数据校验错误 (类型 / 边界) | ✅ | ✅ | 本地 |
| 微妙 self-aggrandizement | 可能漏 | ✅ | 高风险任务 cloud, 一般任务本地 |
| 跨 task pattern | 单 tick 看不到 | 单 tick 看不到 | 都需要 Watchtower (规则引擎) |
| 安全/合规判定 | 可能漏 | ✅ | cloud + Gate 硬规则 |

### 11.4 ensemble_invoke API contract

`LLMRouter.ensemble_invoke(request, providers)` 是 multi-LLM 并行调用的 primitive：

**输入**：
- `request: LLMRequest`
- `providers: list[LLMProvider]`（至少 2 个，至少 2 个不同 family）
- 可选 `consensus_strategy`：`majority_vote` / `weighted` / `pick_best_by_metric`
- 可选 `divergence_threshold`：偏离阈值，超过触发 Mission Director 告警

**输出 `EnsembleResponse`**：
- `responses: list[LLMResponse]`（每个 provider 一个）
- `consensus: LLMResponse | None`（共识结果，按 consensus_strategy 计算）
- `divergence_score: float`（0-1，0=全一致，1=完全分歧）
- `divergence_signals: list[str]`（具体哪些字段分歧）
- `usage: AggregateUsage`（多 provider 总 token / cost）

**用法场景**：

- 启 3 模式 Explorer Pool：3 个 provider 并行产候选 → consensus 不 enforce，每个候选独立进 lifecycle。
- Mission Director：高风险决策点用 ensemble，divergence_score > 0.5 → 升 user。
- 任务方案生成：复杂任务 TaskPlanDraft 由 ensemble 产 3 个方案，督师选最优。

**Cost multiplier — 真 LLM 实测 (V7.1, A2 / X.D-2 dogfood v12)**:

| 配置 | §12.4.4 估值 | 真 LLM 实测 (Anthropic Haiku) |
|---|---|---|
| 全 trifecta (3 lines) | 5-6x | **5.16x** ✅ |
| Single-LLM baseline | 1.0x | $0.00008 / call |

来源: `scripts/dogfood_v12_real_trifecta_checkpoint_collab.py`. §12.4.4 估值
第一次被真 LLM 实证.

### 11.5 Claude Code 工程纪律到 multi-LLM 蒸馏

**核心目标**：让 ensemble 内每个 LLM 都按 Claude Code 工程纪律行事，整个 ensemble 输出工程可信。

| Claude Code 纪律 | multi-LLM ensemble 落地 |
|---|---|
| 任务拆解 | Mission Director 把任务拆给 ensemble 不同 LLM；同时启 RecursivePlanner 给 ensemble 出 plan tree |
| 并行 sub-agent | ensemble 本身就是并行 sub-agent；ExecutorLoop 并行 dispatch list[ToolCall] |
| grep verify before assume | 每个 LLM 改代码前必须用 `grep-verify` skill 验证假设，不允许只用 shell-exec |
| 测试驱动 | 每个 LLM 改完代码 → Tester pipeline 跑 pytest+ruff → 红了立修 |
| commit 纪律 ≤ 1000 行 | Git commit gate 工程线规则：每个 LLM 生成的 commit 超 1000 行强制拆 |
| 错误立修不藏 | 工程线 1 次错就触发 RepairTicket；不积压 |
| dev_log 沉淀 (ADR-025) | 每个 LLM 改动都要 `dev_log_append` skill 调用记录 |
| 决策点停下问 | ensemble divergence_score > threshold OR 决策点 classifier 命中 → CollaborationTicket |
| Read with offset+limit | self-reflect / file-io / grep-verify 内置 limit cap，强制大文件分块 |
| Bash 克制 | shell-exec 走 allowlist，专用 skill 优先 |

(原 V7 草稿把"持续进化找更优策略"放在这张表里 — **删除**, 因为它不是从 Claude Code 蒸馏的。Claude Code 是单 LLM 系统, 没有持续进化能力。持续进化是 **KUN 原创独有概念**, 详见 §12 RSI 闭环 + §1.1 产品魂)

**蒸馏验证标准**：dogfood v9 + 后续 真实长任务 必须 multi-LLM 模式跑通，且 P3 多维测试 battery 在 multi-LLM 下仍 ≥ 19/20。

**"持续进化"额外验证标准** (V7 新增, KUN 原创非蒸馏): 
- 累计跑 ≥ 10 个真实长任务后, 启 capability candidate 库必须 ≥ 5 张 ("跑任务有进化")
- 累计跑 ≥ 30 个任务后, production lifecycle 必须有 ≥ 1 张候选过 5 阶段 promoted ("进化真有沉淀")
- 本地 LLM watchdog / 启 Explorer 占比 ≥ 60% (前提: 用户有 GPU 资源) — "进化经济上可持续"

---

## 12. RSI 闭环（V7 新章节, **产品魂所在**）

V6 把 RSI 概念散落，V7 把它集中并升级为**产品魂**:

> **"KUN 不是一次任务一次活的工具, 是跑每次任务都让自己略变更聪明的系统"**

这不是技术细节, 是 KUN 区别于所有当前 agent 系统的根本独特价值。Claude Code / OpenClaw / Hermes 都是任务工具, KUN 是**进化中的任务工具**。

### 12.0 RSI 的核心承诺

**KUN 原创独有概念** (Claude Code / OpenClaw / Hermes 都没有, 详见 §11.5 蒸馏表注释).

| 项 | 承诺 |
|---|---|
| 每跑一次真实任务 | 启 (Qi) 自动反思"有没有更优策略" |
| 找到更优策略 | 写 capability_candidate, **不直接改 production** |
| 候选积累 | 走 **9 阶段 lifecycle**, 其中 **Replay → Holdout → Shadow → Canary → Production 是严格验收 5 阶段** (前置: Observation/Candidate; 后置: Monitor/Rollback) — 详见 §15 |
| user-approve | production flip 必须 explicit user approval |
| 经济可持续 | 启 Conservative + Performance 模式**优先用本地 LLM 跑 (前提: 用户已有 GPU 资源)**, 不烧 cloud 账单 (§9.6 + §11.3). **新采购 GPU 经济不划算, V7 不推荐** |
| 进化方向用户可干预 | 用户可在驾驶舱看 capability lifecycle 各阶段, 可手动拒/通过候选 |
| 进化结果可回滚 | 任何 promoted capability 都可 rollback / retire |

⚠️ **"lifecycle 5 vs 9 阶段不一致"修复**: V7 草稿在不同章节写 "5 阶段" 或 "9 阶段" 不统一。V7 修正版统一: **9 阶段总流程**, 其中**严格验收是中间 5 阶段** (Replay → Holdout → Shadow → Canary → Production)。前置 Observation/Candidate + 后置 Monitor/Rollback 不算"验收"。所有引用统一改用这个口径。

### 12.1 RSI 触发条件

| 触发源 | 例子 | 是否允许用户任务触发 |
|---|---|---|
| 启发现 KUN 不足 | 任务跑完启 strategy replay 找到更优策略 | ✅ 允许 |
| 傩发现 KUN 不足 | 同类污染反复出现 → 启介入做能力候选 | ✅ 允许 |
| External Supervisor 发现 | 跨 LLM critique 反复发现同模式 drift | ✅ 允许 |
| AB benchmark / Frontier50 | 同题反复失败 → 启生成候选 | ✅ 允许 |
| 用户验收 / 返工 | 用户反复 reject 同类交付 | ✅ 允许 |
| OpenClaw / Hermes / Claude Code 样本 | 源码/行为对照 → 找到 KUN 缺失 | ✅ 允许（§17） |

**用户任务可以触发 RSI**（V6 双账本不矛盾） — 但**触发的是 capability candidate**，**不是 production runtime 直接改**。

### 12.2 RSI 9 阶段 lifecycle (严格验收 5 阶段)

任何 RSI candidate 进入 production runtime **必须**走完 9 阶段 (同 §15 能力 lifecycle), 其中 **Replay→Holdout→Shadow→Canary→Production 是严格验收 5 阶段**:

```text
1. Observation     — 发现机会 (前置)
2. Candidate       — 启写 capability_candidate (前置, 含 strategy_replay_report + process_audit)
─────────────────────────────────────────────
   严格验收 5 阶段 ↓
3. Replay          — 用历史任务数据重放, 验证不劣于 baseline
4. Holdout         — 隔离一组 held-out 任务测候选, 不影响 production
5. Shadow          — 与 production 并行跑, 输出对比, 不真切换
6. Canary          — 小比例切到候选 (起步 5-15%, 待真实数据校准), 监控指标
7. Production      — 全量切换, runtime_enabled=true
─────────────────────────────────────────────
8. Monitor         — 持续观察指标, 回归则 rollback (后置)
9. Rollback/Retire — 失败回退到 baseline (后置)
```

**production-mode flip 必须 explicit user approval** (V7 强 enforce):

- 进入 Production 阶段必须经 CollaborationTicket 等用户 approve
- 不允许 KUN 自动 flip
- approval 必须 log 在 capability lifecycle 历史

**降低用户认知负担** (回应 V7 攻击审视"用户每次都 click approve 反成噪音"):

- KUN 在 user approval ticket 里**主动**附: candidate 的 replay_report + shadow vs production 指标对比 + 风险 / 回滚方案
- 用户**默认 OK 一键 approve**, 看到红字才需要细看 — UI 设计原则: "不让用户失败前看不到 / 不让用户疲劳"

**TicketVerifier 协议 (V7.1, X.H.TICKET-VERIFY)**:

V7.0 仅要求 `user_approval_ticket_id` 非空字符串. 实测发现这是 honor-system,
任何字符串都过 — 攻击者或 buggy admin script 可绕过. V7.1 强制完整校验:

CANARY → PRODUCTION 必须 **ALL OF**:

  1. `user_approval_ticket_id` (非空字符串)
  2. **ticket 真在 InMemoryCollaborationQueue (或未来 PG queue) 存在**
  3. `ticket.status ∈ {"answered", "fallback_selected"}`
  4. `response.selected_option == "approve"`

由 `kun.governance.capability_lifecycle.TicketVerifier` Protocol 强制;
生产 wiring 是 `kun.integration.collab_ticket_verifier.InMemoryQueueTicketVerifier`.
无 verifier wired 时退化为 legacy truthy-check (向后兼容). **生产 caller 必须
传 verifier**.

**攻击者矩阵 (V7.1 acceptance, 12 测试)**:

| 输入 | 结果 |
|---|---|
| 伪造 ticket id (queue 里没有) | DENIED |
| status='open' / 'waiting' / 'escalated' | DENIED |
| status='cancelled' / 'closed' | DENIED |
| answered + selected='hold' | DENIED |
| fallback_selected + fallback='hold' | DENIED |
| fallback_selected + fallback='approve' | ALLOWED |
| answered + selected='approve' | ALLOWED |
| verifier raise 异常 | fail-closed (CapabilityLifecycleError) |

测试位置: `tests/integration/test_v7_ticket_verify_attacker_matrix.py`.

### 12.3 启 strategy replay 三类产物

V6 既有，V7 升级为运行时强 enforce：

| 产物 | 用途 | 缺失影响 |
|---|---|---|
| **strategy_replay_report** | 证明旧策略 vs 新策略指标对比 | 缺 → candidate 不能进 Replay 阶段 |
| **process_audit** | 原链路哪里浅 / 哪里错 / 哪里该问人 | 缺 → 不能识别"为什么需要换策略"，下次仍可能犯 |
| **capability_candidate** | 只记录可复用改进，不进入默认 runtime | 缺 → 没东西可走 lifecycle |

**缺任一证据** → 只能算"启已诊断"，**不能算"启已沉淀"**。

**RSI 写侧自身闭环 (V7.1, A4 / X.M)**: §12.3 三证据规则也适用于 KUN
**自己** dev_log 蒸出的 methodology 候选 — 不允许 short-circuit cp 进
`seeds/methodologies/`. 任何 methodology 候选必须先过 ProcessAudit +
StrategyReplayReport. 实例: `docs/dist-output/seeds-new/v12-xh/` 事后
追溯审计了 2 张 X.H 蒸的 seed. 让 RSI 写侧**自审**.

### 12.4 RSI 三线并行 trifecta (V7 核心新增)

用户指出 KUN 自进化的本质: **错误无法穷举, 必须靠三条互补的线同时跑**。V7 把三线整合成 RSI 核心机制。

#### 12.4.1 三条线定义

```
                  KUN 主任务 跑
       ┌──────────┬──────────┬──────────┐
       ↓          ↓          ↓
   过去线        现在线       未来线
  (复盘)       (监督)        (重做)
       │          │          │
       ↓          ↓          ↓
   失败案例库  实时拦截+   候选策略
  (bug_root_  动态优化    并行跑
   case_lib)              (启 Explorer Pool)
       │          │          │
       └────┬─────┴─────┬────┘
            ↓           ↓
        capability    candidate
       记忆库 →       走 9 阶段 lifecycle
       下次规避       → 验收通过 → production
```

| 线 | 时间维度 | 主体 | 触发 | 输出 | KUN 现有载体 |
|---|---|---|---|---|---|
| **过去线** | post-hoc | 启 (Qi) | 任务结束时 | 重大失误清单 + 规避策略 | `bug_root_cause_cases` 库 (DIST-E 既有) + 启 strategy replay |
| **现在线** | real-time | External Supervisor (watchdog hat) | 持续 tick | 实时 verdict + drift signals + 自动拦截 | §10.2.3 watchdog (V7 已设计) |
| **未来线** | parallel try | 启 (Qi) Explorer Pool | 主线跑中并行 | 候选策略 (Conservative + Aggressive + Performance) → 综合评分 → 走 lifecycle | §9.6 启 3 模式 Explorer Pool (V7 已设计) |

#### 12.4.2 三线冲突协调 (用户授权)

**优先级**: **现在线 > 过去线 > 未来线**

- 现在线 (实时监督) 发现问题 → **立即拦截 + 修代码 / 改方案** (§10.3 双线动态优化), 不等候选
- 过去线 (复盘) 跟现在线**不冲突**: 复盘是任务结束后跑, 不影响主线
- 未来线 (候选) 跟现在线**异步收敛**: 候选**不直接覆盖主线**, 收敛后走 9 阶段 lifecycle → 通过 → 下次任务用

**为什么不让候选直接覆盖**: 双账本约束 (§8) + lifecycle 严格验收 (§12.2). "覆盖"= 候选通过验收后自然取代旧策略, 不是当场切换。

#### 12.4.3 "候选赢"判定 (用户授权综合评分)

未来线产 3 个候选 (Conservative / Aggressive / Performance), 验师 (Tester) 评分:

| 维度 | 权重 | 评分依据 |
|---|---|---|
| **质量** | **60%** | 输出准确性 / 完整性 / 验收测试通过率 / 用户体感 |
| **速度** | **25%** | 任务完成 wall clock time / step 数 |
| **成本** | **15%** | total token / total $ / 跨 LLM provider cost |

综合得分 = 0.6 × 质量分 + 0.25 × 速度分 + 0.15 × 成本分。

**得分最高的候选 + 显著高于 baseline** (≥ baseline + 5% 综合得分) → 进 capability_candidate → 9 阶段 lifecycle。**显著低于 baseline 或并列 → known_limit 归档, 不进 lifecycle**。

判定主体: **验师 (Tester) 评分 → Mission Director 决策是否进 lifecycle → 启写 capability_candidate**。三角色分工不冲突。

#### 12.4.4 三线成本控制 (用户授权按风险驱动 + 用户开关)

**默认按风险等级开关三线**:

| 任务类型 | 过去线 | 现在线 | 未来线 | 总成本对比单 LLM 主线 |
|---|---|---|---|---|
| 短任务 (low risk) | off | off | off | 1x |
| 中等任务 | on (轻量复盘) | episodic | off | 1.2x |
| 长任务 (>30min) | on | continuous (本地 LLM) | **2 个候选** (Conservative + Performance) | 3-4x |
| 高风险 / RSI / Dogfood | on (深度复盘) | continuous + 高频 | **3 个候选全开** | 5-6x |

**用户控制开关** (跟 §10.2.3 watchdog 同一套):

```
KUN_RSI_TRIFECTA_DEFAULT = full | watchdog_only | off
KUN_RSI_PAST_LINE = on | off
KUN_RSI_NOW_LINE = continuous | episodic | off
KUN_RSI_FUTURE_LINE_CANDIDATES = 0 | 1 | 2 | 3
```

per-task override 在 TaskPlan 里, 驾驶舱实时切换。

#### 12.4.5 三线产物归账本

- 过去线 → `bug_root_cause_cases` 增量 + `process_audit` 写入 `self_improvement` 账本
- 现在线 → 实时 verdict log + 即时 RepairTicket / PlanChangeProposal
- 未来线 → `capability_candidate` 进 lifecycle, 通过验收后**才**取代旧策略

**双账本边界 (§8) 仍然严守**: 三线产物**不允许直接修改 production runtime**, 必须走 9 阶段 lifecycle + user approval。

### 12.5 RSI 自指限制 (V6 既有 V7 保留)

V6 既有：target_module ∈ {strategist.\*, supervisor.\*, gate.\*, director.\*, mission_director.\*, external_supervisor.\*} → 强制 `requires_human_review=True`。

启不能自动修启自己，傩不能自动修傩自己，Mission Director 不能自动修自己，External Supervisor 不能自动修自己。

### 12.6 RSI 闭环 wiring (V7.1, X.G + X.I-0b)

V7.0 §12 描述了 RSI 协议但未规定**读侧 (蒸馏的方法论怎么影响下次任务)**
和**写侧 (蒸馏候选怎么进 Gate)**. V7.1 两侧都接通了.

**读侧 (X.G — 已 promote 的 methodology 到达下次任务 LLM)**:

```
promoted yaml 在 seeds/methodologies/
    ↓ kun.engineering.methodology_runtime_loader.load_methodologies
    ↓ MethodologyRuntimeSelector.select_for(TaskContext, top_k=3)
    ↓ render_for_system_prompt (engineering-first 关键词重合打分)
    ↓ append 进 LongTaskOrchestrator system prompt
    ↓ LLM真 在对话上下文里看到方法论
    ↓ emit long_task.methodology_injected event 给 cockpit
```

Env: `KUN_V7_METHODOLOGY_INJECT_ENABLED=true` + `KUN_V7_METHODOLOGY_TOP_K=N`.

**写侧 (X.I-0b — 蒸馏候选进 Gate)**:

```
MethodologyDistillStep.run (位于 kun.engineering.idle_batch,
   由 kun/api/main.py:152 launch 的 idle_batch_worker 周期触发)
    ↓ 对每个 novel candidate (来自 methodology_distill.distill())
    ↓ kun.integration.methodology_to_gate_bridge.
       admit_methodology_candidate_via_gate
    ↓ GateService.admit 用合成 payload
    ↓ 如果 approve: chain capability_lifecycle_v7_bridge → lifecycle_transitions 真行
    ↓ chain auditor_report_v7_bridge → auditor_reports 真行
```

X.I-0b 之前, GateService.admit 只被 `kun/integration/prompt_ab.py` (A/B
框架, 非用户路径) 调过. X.I-0b 让整个 V7 §15 lifecycle **写侧**通过既有
daemon 生产激活.

Env: `KUN_V7_METHODOLOGY_TO_GATE_BRIDGE_ENABLED=true` (默认开).

这一节闭合了 V7 §1.1 "每跑一次都让自己略变更聪明" 产品魂 — 不再只是声明,
而是**写侧 + 读侧双向 wired**.

---

## 13. 全系统运行协议

V6 既有，V7 保留 + 新增 13.7 双线监督协议。

### 13.1 状态协议

所有任务必须有可持久化状态。状态必须覆盖：

- Mission
- TaskPlan
- ExecutionContract
- WorkItem
- RunRecord
- ArtifactRecord
- ArtifactManifest
- StateLedgerEvent
- GateEvaluation
- CollaborationTicket
- RecoveryTicket
- AcceptanceReview
- CapabilityProfile
- **TaskPlanVersion**（V7 升级为 first-class）
- **PlanChangeProposal**（V7 新增）
- **MissionAlignmentReview**（V7 新增）
- **EnsembleResponse**（V7 新增）

状态必须支持断点恢复、跨天恢复、审计、回放和复盘。

### 13.2 权限协议

所有真实外部动作必须经过权限协议。包含：

- 动作类型
- 风险等级
- 需要的权限
- 审批人或自动授权规则
- 幂等策略
- 回滚或补偿策略
- 审计记录

高风险动作必须请求确认，低风险动作可根据策略自动执行。

### 13.3 产物协议

任何完成声明都必须绑定产物。产物协议包含：

- 产物类型
- 路径或外部位置
- 生成者
- 关联任务方案版本
- 关联工作项
- 证据引用
- 测试和评审状态
- 验收状态

没有产物清单的任务不能进入交付完成状态。

### 13.4 门禁协议

门禁必须根据任务类型配置，但必须统一记录。

通用门禁包括：

- 目标一致性
- 完整性
- 正确性
- 可用性
- 证据覆盖
- 测试覆盖
- 风险可接受
- 权限合规
- 用户可验收
- **方案对齐**（V7 新增）：TaskPlanVersion 仍 valid
- **双线监督健康**（V7 新增）：工程线 + 方案线均无强信号未处理
- **multi-LLM divergence**（V7 新增）：divergence_score 在阈值内

最终产品类任务必须增加真实用户体感门禁。KUN 自评分、机制门禁、残差审计、自动外部门禁或 checklist 通过，**只能作为证据**，**不能单独作为最终可验收结论**。系统必须证明交互、视觉、内容完整度、失败反馈、长期试玩、目标用户体感和交付包都达到任务定义的最终产品标准。

门禁失败必须产生明确下一步：修复、回滚、改计划、请求输入、降级交付或拒绝交付。

### 13.5 上下文和压缩协议

KUN 必须在长任务中保持上下文连续。覆盖：

- 当前任务方案（TaskPlanVersion）
- 已完成事项
- 未完成事项
- 等待人或外部系统的事项
- 关键决策
- 风险和阻断
- 产物和证据
- **GoalAnchor**（顶部 pinning，V6 anti-drift Layer 2）
- **anti-sycophancy directive**（顶部 pinning，V6 anti-drift Layer 5）
- **ensemble divergence 记录**（V7 新增）

压缩协议：长 context 触发阈值 → ConversationCompactor 压缩中间步骤，保留头尾 + anchor + 关键决策。

### 13.6 通讯协议

跨 agent / 跨子系统通讯：

- `@dataclass(frozen=True)` 输出契约（方法论 `frozen_dataclass_agent_io_contract`）
- `to_row_payload(tenant_id)` 转 ORM dict
- `EmitCallback = Callable[[T], Awaitable[None]]` 注入式 emitter（测试 fake / prod 真 DB）
- **hint 字段而非 constraint**（方法论 `engineering_hints_not_constraints`）：上游建议下游自决，命名带 `_hint` / `_suggested` / `_recommended` 后缀
- Event type 命名空间：`{role}.{action}` 格式（`qi.strategy_replay_started` / `nuo.contamination_detected`）

### 13.7 双线监督协议（V7 新增）

详见 §10.2。本节为协议层 short-form：

- 工程线主体：傩 / Watchtower / Tester / Gate
- 方案线主体：Mission Director / 启（strategy replay 后置）
- 跨线主体：External Supervisor
- 主体不冲突原则：执行级 > 任务级 > 方案级 > LLM 行为级
- 三级信号强度：弱 log / 中自动改 / 强升人

---

## 14. 7 个第一性回路

CONVERSATION-LEARNING 9 提了 7 个第一性回路，V7 升级为正式章节，每个回路明确归属。

### 14.1 目标回路（Goal Loop）

```text
用户目标 → 督师 (Director) 意图识别 → TaskPlanDraft → Mission Director 对齐审查
  → Plan Alignment → ExecutionContract
```

**主负责**：督师 / Mission Director  
**关键产物**：TaskPlanVersion, ExecutionContract  
**漂移信号**：alignment_status='drifting' / 'rejected'

### 14.2 执行回路（Execute Loop）

```text
ExecutionContract → WorkItem 拆解 → 调度 → Executor 执行
  → Tool dispatch → 产物 → StateLedger
```

**主负责**：督师（拆解） / 执行（运行） / Control Plane（调度）  
**关键产物**：WorkItem / RunRecord / ArtifactRecord  
**漂移信号**：max_steps / budget_exceeded / wall_clock_exceeded / consecutive_tool_fails

### 14.3 证据回路（Evidence Loop）

```text
Execute → 产物生成 → Evidence ledger 写入
  → 证据引用、来源、可信度、过期风险 → 关联 Artifact
```

**主负责**：知识与证据系统（§9.3） / Mission Director（审查）  
**关键产物**：EvidenceRecord, ArtifactManifest  
**漂移信号**：证据缺失 / 来源不明 / 过期未续

### 14.4 纠错回路（Repair Loop）

```text
工程线异常 / 方案线偏离 → RepairTicket / PlanChangeProposal
  → 改代码 / 改方案 → 复测 → 通过 / 升级
```

**主负责**：傩（工程修复） / 督师（方案修改） / Mission Director（裁决）  
**关键产物**：RepairTicket, PlanChangeProposal, ValidationReport  
**漂移信号**：repair_attempt > N / plan_change > M

### 14.5 协同回路（Collaboration Loop）

```text
信息缺口 / 决策点 / 高风险动作 → CollaborationTicket
  → 用户/专家/operator 回应 → 恢复执行
```

**主负责**：协同与资源调度系统（§9.4） / 门禁  
**关键产物**：CollaborationTicket, ApprovalRecord  
**漂移信号**：ticket 超时未回 / 用户 reject

### 14.6 验收回路（Acceptance Loop）

```text
交付包（DeliveryManifest） → AcceptanceReview
  → accepted / partial / rework / rejected → 闭环 or 返工
```

**主负责**：Mission Director（强 enforce 不能 KUN 自宣布 done） / 用户  
**关键产物**：AcceptanceReview, DeliveryManifest  
**漂移信号**：自宣布 done 而无用户验收 / 验收维度缺失

### 14.7 学习回路（Learning Loop）— RSI 入口

```text
任务复盘 / 用户反馈 / AB peer gap / External Supervisor 累积信号
  → 启 strategy replay → process_audit + capability_candidate
  → 5 阶段 lifecycle (Replay / Holdout / Shadow / Canary)
  → user-approved Production flip → runtime_enabled=true
```

**主负责**：启（Qi）  
**关键产物**：strategy_replay_report, process_audit, capability_candidate, CapabilityProfile  
**漂移信号**：缺三类证据 / 跳过 lifecycle 阶段 / production flip 无 user approval

---

## 15. 能力 lifecycle

KUN 的能力库严格按 9 阶段 lifecycle 管理：

```text
Observation → Candidate → Replay → Holdout → Shadow → Canary → Production → Monitor → Rollback / Retire
```

### 15.1 阶段语义

| 阶段 | 语义 | 进入条件 | 退出条件 |
|---|---|---|---|
| Observation | 发现机会 | 启 / 傩 / Mission Director / External Supervisor 触发 | 写出 hypothesis |
| Candidate | 写候选 | 含 strategy_replay_report + process_audit + capability_candidate 三件套 | 通过初步 sanity |
| Replay | 用历史数据重放 | 候选 + 重放数据集 | 重放结果不劣于 baseline |
| Holdout | 隔离测 | 重放过 | held-out 任务通过率 ≥ baseline |
| Shadow | 与 production 并行 | holdout 过 | shadow vs production 指标对比 + ≥ N 周观察 |
| Canary | 小比例切换 | shadow 过 + sampling_rate ≤ 15% | 指标稳定 ≥ M 天，无重大异常 |
| Production | 全量切换 | canary 过 + **user explicit approval** | runtime_enabled=true |
| Monitor | 生产观察 | production 后 | 持续观察，无回归 |
| Rollback / Retire | 回退 / 淘汰 | Monitor 发现回归 OR 长期无人使用 | 写决策归档 |

### 15.2 关键规则

- Replay / Holdout / Shadow **只能作为证据**，**不能作为生产默认能力**。
- KUN Runtime **默认只能消费 production 阶段能力**。
- 能力晋级必须有证据、测试、回归、质量门禁、风险评估和回滚方案。
- 能力失败必须能 rollback、禁用、降级或退休。
- 能力库必须去重、合并、标记来源、标记适用范围和淘汰重复候选。
- 傩 benchmark、真实任务结果、用户验收和外部样本对比结果必须写入 capability card，供启、路由层和运行时策略消费。
- 模型路由和 worker 分发必须读取生产能力与 capability card 数据；有真实能力分时用能力分选择候选，无数据时保持冷启动策略。
- 启必须把"重复执行后发现更优策略"拆成三类产物（§9.6 / §12.3）。
- **速度和成本提升不能掩盖结果质量下降**。
- **production flip 必须 explicit user approval**（V7 新增）。

**Gate 5 条 engineering 规则 (V7.0 既有 R1-R4) + R6 production reachability (V7.1, X.I-2)**:

| 规则 | 含义 | 失败处理 |
|---|---|---|
| R1 test_report | 测试通过率 ≥ `_DEFAULT_MIN_PASS_RATE` | reject |
| R2 diagnostic | 诊断证据存在 | reject |
| R3 debrief | debrief evidence_quality ≥ threshold | reject |
| R4 self_referential | 监督角色 / 启 / 傩 / MD 自指 | awaiting_human_review |
| **R6 production_reachability (V7.1)** | 当 `experiment.kind ∈ {runtime, capability}`, `target_module` 必须能从 `docs/PRODUCTION_ENTRIES.md` 列的入口至少一处可达 (由 `kun.governance.production_path_traceability.check_symbol_reachable` 实例化检查). methodology kind 不强制. Legacy caller 不带 kind 字段不影响 | reject if R6 fails on runtime kind |

R6 是 V7.1 加的"防 X.H/X.I 静默孤儿"协议级护栏 — 之前 3 次 release
(X.E/X.G/DIST-D) shipped runtime feature 但生产 entry 不调用, R6 在 Gate
入口拦下这种 nominal-wired 但实际孤儿 candidate.

### 15.3 启 strategy replay 三类产物（重申）

V6 line 377 + §9.6 + §12.3：

1. strategy_replay_report
2. process_audit
3. capability_candidate / replay_profile

缺任一证据：只能算"启已诊断"，不能算"启已沉淀"。

### 15.4 Production-entry diff check (V7.1, A8 / X.I-3-FIX + X.O)

`TaskSpec.production_entry_changes_required: list[str]` 声明任务打算改哪些
生产入口文件. long-task 完成时 `LongTaskOrchestrator` 调
`kun.governance.production_entry_diff_check.ProductionEntryDiffChecker.
check_against_actual(declared, actual)`, emit `long_task.production_entry_diff`
event, verdict ∈ {match, drift, no_declaration, undeclared_changes}.

`actual` paths 由生产 WS 入口 (`orchestrator.py`) 走 executor final_messages
里 tool_calls 的 path arg 提取 (`extract_changed_paths_from_messages`, X.O).
任何 LLM 声明要改 X 但实际没改 → verdict='drift', 给后续 MD review 信号.

---

## 16. 生产闭环协议 (V7 大改, 合并 V6 6 层证据 + Claude Code 5 层闭合)

⚠️ **V7 攻击审视触发的关键章节升级**。原 §16 "6 层激活证据" 跟 Claude Code 给用户的"5 层闭合"复盘是**同一事实两种讲法**, V7 合并为一套 7 层模型 + 6 反模式 + 7 角度审计协议。

### 16.0 产品魂级硬规则

> **凡是不能进入真实生产链路被真实数据消费的功能, 都不算完成**

这条规则是 KUN 跟其他 agent 系统的关键区别之一: 不让 KUN 自己骗自己, "功能开发完成 ≠ 生产链路生效"。Claude Code 给用户的真实复盘原话:
> "**核心责任在我这里：我把'功能开发完成'误当成了'生产链路生效'。这在工程上是很严重的问题。**"

KUN 必须**设计上规避**这个问题, 不只是事后审计补救。

### 16.1 七层激活证据 (V7 合并升级)

V6 6 层 + Claude Code 5 层闭合 (方案 → 模块 → 入口 → 真实数据 → 验收) **合并为 7 层**:

| Layer | 名称 | 含义 | 通过判定 |
|---|---|---|---|
| **0** | **方案能力** (V7 新增, 来自 5 层 layer 1) | TaskPlan / 产品方案明文写过此能力 | 能在 V7 / TaskPlan / capability_card 找到对应描述 |
| 1 | 代码存在 (V6 既有) | 模块 / 类 / API / 字段 / 文档已写 | grep 找得到 |
| 2 | 触发器存在 (V6 既有) | 真实任务状态能触发, **不只能人工 call** | 有 daemon-registered runner / event subscriber / API endpoint |
| 3 | runner 可执行 (V6 既有) | 触发后有明确 runner / 工具 / 人机协同路径承接 | runner 注册, dispatch 不抛 NotImplementedError |
| 4 | 真实消费 with receipt (V6 既有) | runner 实际消费 capability / skill / 外部信息 / 沙箱 / 锁 / 回滚 / 监督指令, **并产出 receipt / artifact** | log 里有 directive receipt 或显式 no-consumption declaration |
| 5 | 闭环通过 (V6 既有) | 修复 / 复测 / 回滚 / 验收 / 能力晋级完成 | 状态机从"已诊断"进入"已恢复/已沉淀/已关闭" |
| **6** | **真实 mission 端到端 + 攻击型测试** (V6 既有 + V7 强化) | (a) ≥ 1 真实长任务证明非 fixture (b) 攻击型测试全过 (坏样例 / 边界 / 故障注入) | dogfood / 用户任务真触发 + ValidationPipeline 攻击 case 全过 |

**6 层证据 (V6) → 7 层证据 (V7) 映射**:
- 新增 Layer 0 (方案能力) — 来自 Claude Code 5 层 layer 1, V6 隐含, V7 显式
- 升级 Layer 6 — 加了攻击型测试硬要求, 不只是真长任务
- 中间 Layer 1-5 完全保留 V6 既有

### 16.2 6 个反模式 (来自 Claude Code 复盘) + KUN 规避机制

| # | 反模式 | KUN 规避机制 |
|---|---|---|
| 1 | **功能存在但不是生产必经** (旧入口 / 调试入口可绕过) | §16.3 唯一生产入口强制 + Layer 4 真实消费 receipt 必须 |
| 2 | **测试只测模块成功, 没攻击型测试** | §16.4 攻击型测试硬规则 + Layer 6 通过条件含攻击测试 |
| 3 | **primitive done 当成 done** (有 schema / 函数 / 字段 ≠ 真用) | 启 (Qi) 9 阶段 lifecycle (§12.2) — Replay → Holdout → Shadow → Canary 才能 Production |
| 4 | **缺唯一中枢** (多入口各自做选材/渲染/审片) | §16.3 唯一生产入口 + Control Plane 强制 |
| 5 | **fallback 被当真实能力** (无 VLM 用规则猜) | Layer 4 receipt 必须严格 + fallback 必须显式 declare + §6.5 信息缺口主动协同 (缺真能力时阻断, 不静默降级假装成功) |
| 6 | **缺强制 trace** (产物看不出来源/决策/门禁) | §16.5 强制 trace 协议 + StateLedger + ArtifactRecord 强 enforce |

### 16.2.5 5 个 hidden-orphan 根因 (V7.1, X.H 自检蒸出)

§16.2 6 个反模式描述**实装**的失败形状. X.H 自检 (2026-05-29) 发现了
5 个**审计本身**的失败根因 — LLM (Claude / gpt / Qwen) 和工程师在审计 6
反模式时会**反复犯**这 5 条而导致漏审. 完整分析: `docs/dev_logs/
X.H-self-audit-rootcause.md`.

| # | 根因 | KUN 防御机制 |
|---|---|---|
| **R1** | **grep-verify 颗粒度错**: `import X` ≠ runtime 真用 X | `docs/templates/hidden-orphan-audit-prompt.md` §4 Step 4 + Step 7 chain-reach; `kun.governance.production_path_traceability.check_symbol_reachable` |
| **R2** | **没生产入口 inventory**: "生产是哪几个文件?" 无答案 | `docs/PRODUCTION_ENTRIES.md` mandate (§16.3) |
| **R3** | **opt-in 默认 OFF + 无消费者强制 = 静默孤儿** | `LongTaskRuntimeBundle` pattern + `EXPECTED_BUNDLE_KEYS` CI audit |
| **R4** | **测试 fixture caller 语法上 == 生产 caller** | AST 测试仅解析生产 entry 文件 (`tests/integration/test_production_entry_runtime_bundle.py`) |
| **R5** | **retrospective 漏 entry-level grep** | 模板 §7 banned phrases + §6 强制元自审 |

每个 retrospective 在 claim "feature done" 前必须验证全部 5 个根因
safe=true. 模板提供结构化 JSON `root_cause_check.{R1..R5}.safe: bool`.

### 16.3 唯一生产入口强制

任务执行**只能有一个入口**: 用户输入 → API / WS / CLI → Control Plane → `Orchestrator` → `ExecutorLoop` / `LongTaskOrchestrator`。

**禁止旁路**:
- 旧入口 / 调试入口 / `scripts/*.py` 演示脚本 **不允许走主能力链路**
- 这些路径只能走 `dry-run` / `fixture` / `replay` 模式
- **产物 strictly 不进 `capability_card` / `seeds/methodologies/` / `production runtime`**

**KUN 现状 (grep 结果, V7 §0.4 写实)**:

| 入口 | 走 Orchestrator? | 状态 |
|---|---|---|
| `kun/api/ws.py` (WS) | ✅ | 主入口, OK |
| `kun/api/chat.py` (REST) | ✅ 间接 | OK |
| `kun/api/runtime.py` | ✅ | OK |
| `kun/api/long_task_intake.py` | ✅ 间接 | OK |
| `kun/cli.py` (CLI) | ✅ | OK |
| `kun/control_plane/kun_runtime_runner.py` | ✅ | OK |
| `kun/skills/calibration.py` | ✅ | OK |
| **`scripts/dogfood_distill.py`** | ✅ 经 WS | **OK** (走 WS 间接 Orchestrator) |
| **`scripts/e2e_rsi_demo.py`** | ❌ **直接调 services 绕过 Orchestrator** | 🔴 **fixture-only, 产物不进 capability_card** |
| `scripts/multi_dim_test.py` | N/A (静态分析工具) | OK |
| `scripts/codex_pure_llm_smoke.py` | ❌ 直接调 provider | OK (smoke test, 不产 capability) |

**附录 A Phase 0.5 必做**: scripts/e2e_rsi_demo.py 类的脚本必须**标注 `# FIXTURE-ONLY`**, 产物加 `metadata.fixture_only=true` flag, capability_card 写入时门禁拒。

**V7.1 PRODUCTION_ENTRIES.md inventory + LongTaskRuntimeBundle pattern (X.H)**:

V7.0 列了入口表但不是单一权威源, 也无自动化强制. V7.1 升级:

1. **生产入口清单** `docs/PRODUCTION_ENTRIES.md` (X.H R2 fix) — 唯一权威源.
   每个 blessed 生产入口必须:
   - 在文件里登记 (file path + purpose + bundle wired?)
   - 由 `tests/integration/test_production_entry_runtime_bundle.py` AST audit
     解析验证
   - 对 LongTaskOrchestrator-using 入口, 必须 spread
     `**runtime_bundle.as_orchestrator_kwargs()` 或显式命名每个 opt-in 特性

2. **`LongTaskRuntimeBundle` pattern** (X.H, V7.1) — opt-in runtime feature
   的唯一中枢:
   - 所有 `LongTaskOrchestrator` opt-in ctor 参数走 bundle
   - `LongTaskRuntimeBundle.as_orchestrator_kwargs()` 投影到 ctor kwargs
   - `from_env_defaults()` 工厂统一读 env 开关
   - `enabled_flags` 报告**实际激活** vs precondition-missing (不只 ctor 设了)
   - CI 测试 `EXPECTED_BUNDLE_KEYS` 集合强制: 任何新 opt-in 特性必须接 bundle

   没有 bundle 之前, 三次 release (X.E trifecta / X.G methodology /
   DIST-D critique) shipped class-level opt-in 但生产 WS entry 静默漏传,
   成为 runtime 孤儿. Bundle 让此模式**不可重复**.

### 16.4 攻击型测试硬规则

验师 (Tester) 的 `ValidationPipeline` 升级:

| 测试类型 | 现状 | V7 要求 |
|---|---|---|
| 单元测试 (模块能跑) | ✅ 1706 个 | 必须全过 |
| 集成测试 (模块联动) | ✅ 部分 | 必须全过 |
| **攻击型测试 (坏样例必须失败)** | ❌ 缺 | **V7 新硬规则, 必须全过** |

**攻击型测试至少 5 类**:

1. **坏输入**: 故意喂半句话 / 截断 / 编码错误 → 系统**必须** raise / 拦, 不允许静默继续
2. **错配数据**: 类型错 / 边界值 (空集 / 单元素 / 极大值) → 必须有明确 error 而非 silent corruption
3. **故障注入**: 模拟 LLM 超时 / API 500 / 网络断 → 必须 fallback 路径明确, 不允许假装成功
4. **权限剥夺**: 模拟无 skill 权限 / 无文件读权限 → 必须阻断 + 协同票据, 不允许伪造产出
5. **依赖缺失**: 模拟 capability_card 缺 / runner 缺 / provider 挂 → 必须降级 + 阻断 + 显式 declare, 不允许把 fallback 当真实能力

**验收**: 攻击型测试**全部通过**才允许进 Layer 5 闭环通过。pytest 单元 + ruff lint **不够**。

### 16.5 强制 trace 协议

每个产物 (ArtifactRecord) 必须包含:

```yaml
trace:
  input_refs: []           # 输入证据 / 上游产物 refs
  decision:
    chosen_llm: ...        # 哪个 LLM 决策
    chosen_capability: ... # 哪条 capability 起效
    chosen_skill: ...      # 哪个 skill 派发
    rule_fired: ...        # 哪条规则触发
  gates_passed: []         # 经过哪些 gate, verdict
  scores: {}               # 各维度评分
  failures: []             # 失败原因 (如有)
  repairs: []              # 修复记录 (如有)
  fallback_used: false     # 是否走了 fallback (true 时必须显式 declare)
```

**写不出 trace 的产物**:
- 不算合规产物
- 不允许进 DeliveryManifest
- 不允许进 capability_card

**runtime_features_used trace shape (V7.1, A11 / X.H.TRACE)**: long-task
的 `TaskCheckpoint.working_state["runtime_features_used"]` 落:

```python
{
    "methodologies": [{"title", "topic", "score", "file_path"}],   # X.G
    "trifecta_ticks": [{"call_count", "past_state", "present_state",
                        "future_state", "n_findings", "total_cost_usd"}],  # X.E
    "discipline_report": {"overall_score", "failed_disciplines"},  # X.I-0a
    "production_entry_diff": {"verdict", "has_drift", "declared_count",
                              "actual_count"},  # X.I-3-FIX
}
```

经 `ExecutorLoop.runtime_features_provider` callback 落 PG. 任何
`task_checkpoints` 行可查"这次任务用了哪些 feature" — 闭 §16 cause #6.

### 16.6 生产闭环攻击审计员 — External Supervisor auditor hat

V7 明确 **External Supervisor 戴两顶帽子**:

| Hat | 频率 | 任务 | 工具 |
|---|---|---|---|
| **watchdog hat** (§10.2.3) | 持续 tick (60-120s) | 跨 LLM critique 主 LLM 行为 + 监督所有监督角色 (§10.2.5) | critique prompt |
| **auditor hat** (§16.6 新增) | 周期 (每周 / dogfood 完成后 / Canary→Production 阶段 gate / release 前) | 7 角度生产闭环审计 | audit prompt (用户提供, 见下) |

**7 角度审计 prompt** (V7 内置, External Supervisor 周期跑):

```text
你是 KUN 的"生产闭环攻击审计员"。目标不是证明功能存在, 是证明系统不能被绕过。

请从攻击者视角检查方案、代码、测试、真实运行路径和产物。重点检查:

1. 文档声称 done 的能力, 真实生产路径是否必经?
2. 是否存在旧入口、脚本入口、调试入口、直接渲染入口绕过核心能力?
3. 是否有 schema / helper / mock / fallback 被包装成真实完成?
4. 测试是否只测模块成功, 还是测试坏样例必须失败?
5. 每个产物是否有 trace: 输入、决策、门禁、评分、失败原因、修复记录?
6. 如果某能力缺失, 系统是降级并阻断, 还是继续假装成功?
7. 真实用户最关心的结果, 是否被端到端验收覆盖?

输出:
- 设计承诺
- 真实代码路径
- 可绕过方式
- 最小复现
- 风险级别 P0 / P1 / P2
- 必须修复项
- 验收测试
- 是否允许上线 / 交付
```

**审计输出存档**: 写入 `audit_reports/<date>.md`, 进 NUO panel 显示, 严重项 (P0 / P1) 触发 CollaborationTicket 等人审。

**Angle 8 — Production-path traceability (V7.1, X.I-1)**:

V7.0 §16.6 列了 7 角度. X.I-1 加 Angle 8:

> 对每个声称完成的 capability, 审计员必须验证它从 `docs/PRODUCTION_ENTRIES.md`
> 列的入口至少一处真实例化或调用. 如果只能从 `tests/` 或 `scripts/dogfood_*`
> 到达, capability 是**nominal-wired 但实际孤儿** — 必须 `risk_level ≥ P1`,
> `allow_release=false`, `must_fix` 加 "wire to at least one production entry".

实装自动计算 reachability 经 `kun.governance.production_path_traceability.
check_symbol_reachable`, 喂给 LLM auditor render (`render_auditor_prompt`
的 `production_path_check` arg). Heuristic auditor 在 target_module 是
symbol-shape 但 unreachable 时也自动 escalate.

**Audit prompt template (V7.1, X.N)**:
`docs/templates/hidden-orphan-audit-prompt.md` 是可复用的 §1-§8 prompt,
任何 LLM (Claude / gpt / Qwen / 本地) 拿到 + 一份代码库都能跑同款审计.
结构化 lint 守卫: `tests/unit/test_hidden_orphan_audit_template.py`.
任意-LLM 驱动脚本: `scripts/run_audit_prompt_on_capability.py`.

**Step 7 chain-reach (V7.1.1, X.O)**: 模板 §4 原本是直接符号 grep, 有
R1 颗粒度 bug — 漏 chain wiring. X.O 加 Step 7: 走符号的 caller, 检查
caller 的 host file 是否生产入口, 2-hop 终止判定 chain-wired (不是孤儿).
强制 §6 元自审承认 Step 7 catch 出的 R1 false positive.

### 16.7 与其他子系统的关系

| 章节 | 关系 |
|---|---|
| §9.5 傩 (Nuo) | 工程线实时检测 (污染 / EOF / timeout), 跟 auditor hat 互补 (实时 vs 周期) |
| §9.7 Mission Director | 任务级监督, 用 7 层证据当门禁: capability 没过 Layer 4 不允许进 deliver |
| §12 启 (Qi) RSI | 9 阶段 lifecycle 直接映射 7 层证据: Replay/Holdout 验证 Layer 4-5; Canary/Production 验证 Layer 6 |
| §13.3 产物协议 | strong trace 字段强 enforce, 跟 §16.5 一致 |
| §13.4 门禁协议 | 加新门禁项: 7 层证据 Layer 4-6 通过 (V7 升级) |

### 16.8 验收硬规则 (产品魂)

任何 capability / feature **必须 7 层全过**才允许:
- 进 production runtime
- 写入 capability_card
- 显示为"已完成"在驾驶舱
- 进 release / dogfood 产物
- 标记 `runtime_enabled=true`

7 层有任一层不过 → **该能力对外是"未完成", 内部走启 / 傩治理**, 不允许在驾驶舱 / 文档 / commit message 里宣称 done。

**V7.1 自动化验收要求 (A13 / X.H + X.N)**:

- `tests/integration/test_production_entry_runtime_bundle.py` 必须 8/8 过 —
  这是防 R1/R5 复发的 CI 守卫 (AST audit 生产入口文件确保 bundle plumbing).
- `tests/unit/test_hidden_orphan_audit_template.py` 必须 8/8 过 — 确保
  audit 模板结构不静默漂移.
- 每个 §16.8 "approved" capability **应**至少跑过一次 audit-prompt 模板
  (人工或 CI), response 存 dev_logs.

---

## 17. 三方对标吸收方法

V6 ch 8 既有，V7 升级为三方（加 Claude Code）。

### 17.1 吸收对象

| 对象 | 长处 | KUN 吸收为 |
|---|---|---|
| OpenClaw | 工具优先 / 当前 run state / pipeline 执行 / 进程/日志/产物意识 | Skill registry / Control Plane / Daemon |
| Hermes | 深任务理解 / 上下文边界 / 多子任务合成 / 证据叙事 | Task understanding / Compaction / Merge / Evidence |
| **Claude Code** | **工程纪律 10 维 + 扩展项**（§4.3） | **multi-LLM ensemble 内每个 LLM 都按 Claude Code 纪律行事** |

### 17.2 吸收方式

- 读取外部系统源码、文档和运行样本。
- 对照 KUN 现有子系统，识别缺口、重复和负迁移风险。
- 提炼 KUN-native 协议、runner、测试、能力 profile 和门禁。
- 通过真实长任务和 AB 回归验证。
- 通过启的能力生命周期进入生产。

### 17.3 禁止事项

- 直接复制外部实现代码。
- 把外部系统的复杂度原样搬进 KUN。
- 把重复能力拆成新子系统。
- 未经验证直接进入生产默认运行时。
- **用三方品牌名作为 KUN 子模块名**（如 `kun/openclaw/`、`kun/hermes/`、`kun/claude_code/` 都禁止）。

### 17.4 三方对标吸收 → 启 capability lifecycle

外部样本对比**必须输出机器可读的启治理动作**，包含：keep / merge / candidate / discard / 测试 / 风险控制 / 回滚计划 / 默认运行时禁用边界。**Markdown 报告不能替代治理数据**。

通过真实长任务 dogfood、holdout、shadow、canary 和 rollback readiness 后，才能进入 production。

---

## 18. AB 与真实任务评估

V6 既有，V7 调整 AB 与真实任务的权重。

### 18.1 主评估路径

**真实长任务 dogfood 是主评估路径**。AB 和 benchmark 是回归门禁，用于发现能力缺口、防止倒退和验证改动，**不是产品价值的替代品**。

### 18.2 AB 执行规则

AB runner 必须作为 Control Plane 内置任务类型，而不是长期依赖外部脚本。

- 所有 agent 做题。
- 对照 agent 只作为被测对象或评审对象，不作为优化对象。
- 互评、报告、gap、健康检查和 repair ticket 都进入产物和账本。
- comparator 不健康、污染、误路由、timeout、EOF、报告缺失、互评缺失时，结果 invalid。
- invalid 结果先进入傩的系统污染治理，不计算 KUN 能力失败。
- KUN 未过门禁时，由启生成能力候选和复测任务。
- 只有同题复测过门禁后，相关能力才可进入后续晋级。

### 18.3 真实长任务评估必须观察

- 是否先补齐信息再形成方案。
- 是否能长期自主推进。
- 是否能分发、合并和验收。
- 是否能从失败中恢复。
- 是否能通过驾驶舱让用户理解状态。
- 是否能产出真实可用交付物。
- 是否能沉淀可复用能力。
- **是否双线监督都健康**（V7 新增）。
- **是否 multi-LLM ensemble 一直在跑（不退化为单 LLM）**（V7 新增）。

---

## 19. dogfood 任务规范（V7 新章节）

dogfood 任务是 KUN 自检 / 自蒸馏的特殊任务类型。V7 把规范明文化，防止本 session "5 张 yaml 直接合并到 seeds/methodologies/" 这类违反双账本的事件再发生。

### 19.1 dogfood 任务定义

dogfood 任务 = KUN 在自己代码库 / 自己 dev_logs / 自己设计文档上跑的任务，输出影响 KUN 自身的产物。

例子：
- 读 dev_logs 蒸馏方法论
- 跑多维能力测试
- 对照 OpenClaw / Hermes / Claude Code 源码做 capability gap analysis
- 自我修复（傩发现 KUN 自身污染）
- 自我改进（启 strategy replay）

### 19.2 dogfood 任务硬规则

1. **必须有 Mission Director 监督**：dogfood 任务自带 Mission Director runner，默认 cross-provider（不允许同 provider Executor + Supervisor）。
2. **产物必须先进 `docs/dist-output/`**，作为 candidate 证据。不允许直接 commit 到 `seeds/methodologies/` / `kun/` / `tests/`。
3. **产物升 production 必须走 capability lifecycle**：candidate → replay → holdout → shadow → canary → production。
4. **production flip 必须 user explicit approval**（CollaborationTicket）。
5. **必须有 dogfood-specific TaskPlanDraft**：含目标、产物清单、验收标准、回滚方案。
6. **写入工作目录必须用 `self-reflect` skill**（白名单 + 单写出目录 + 无 delete），不允许走 file-io 或 shell-exec 自由写。
7. **读 KUN 仓库必须用 `self-reflect` 或 `grep-verify` skill**，不允许走 file-io 默认沙箱（路径不匹配会失败）。

### 19.3 反例 case study：dogfood v8 直接合并 5 yaml seeds

**事件**：本 session dogfood v8 跑通后，5 张 yaml seeds 从 `docs/dist-output/seeds-new/` 直接 cp 到 `seeds/methodologies/`，进了 seeds 总数 27 → 32。

**违反**：
1. 违反双账本约束（§8）：用户任务（dogfood）产物直接改 KUN 默认能力库。
2. 跳过 capability lifecycle（§15）：没走 Replay / Holdout / Shadow / Canary，直接到 Production-equivalent。
3. 缺三类证据（§12.3 / §15.3）：没 strategy_replay_report 对比新旧、没 process_audit 说明为什么换、capability_candidate 直接当 production。
4. 无 user explicit approval：production flip 没经 CollaborationTicket。

**修复路径**（V7 立项时补回）：

1. 把 5 张 yaml 从 `seeds/methodologies/` 移回 `docs/dist-output/seeds-new/candidates/`。
2. 给每张 yaml 配 strategy_replay_report（与既有 27 张方法论对比）+ process_audit（为什么需要这条）。
3. 走 Replay 阶段：用历史 dev_log / 任务样本验证 candidate 真起作用。
4. 走 Holdout / Shadow / Canary。
5. user explicit approval 后 flip 到 production。

**教训写入 V7**：dogfood 产物处理流程是 §19.2 + §19.3，未来 dogfood 任务必须按这个走。

---

## 20. 任务驾驶舱

⚠️ **现状说明 (V7.1 X.F/X.K 更新)**: V7.0 写时驾驶舱 UI 几乎不存在.
X.B/X.F/X.K 后已建: `frontend/src/app/cockpit/page.tsx` 6 面板 8s 自动刷新,
真浏览器渲染验证过 (X.K, 19KB HTML). **仍缺**: 真用户日用 (没人在 cockpit
上做过真决策). 下面 §20.1-20.3 是完整目标视图.

**V7.1 cockpit reader layer (A14 / X.B + X.F + X.O)**:

| Endpoint | 数据源 | reader | wave |
|---|---|---|---|
| /cockpit/capabilities | `lifecycle_transitions` | `list_recent_lifecycle_transitions` | X.B+X.F |
| /cockpit/missions/{id}/alignment | `mission_alignment_reviews` | `list_recent_mission_reviews` | X.B |
| /cockpit/missions/{id}/rsi-trifecta | `task_checkpoints.working_state.runtime_features_used` | checkpoint reader | X.Q |
| /cockpit/supervisor/auditor-reports | `auditor_reports` | `list_recent_auditor_reports` | X.B |
| /cockpit/ensemble/recent | `ensemble_calls` | `list_recent_ensemble_calls` | X.F |
| /cockpit/discipline/recent | `engineering_discipline_reports` PG (X.S) | `list_recent_discipline_reports` | X.O+X.S |
| /cockpit/writes-status | meta | `_writes_wired_status` | X.B.MF-3 |

所有 reader 返 `ReaderResult` 带 `error_kind` 区分 "tenant 真无数据" vs
"DB 不可达" (X.B.MF-6).

### 20.1 用户看到的内容（普通用户视图）

- 当前目标和任务方案版本
- 总进度、当前阶段、下一步
- 已完成产物和交付物位置
- 正在执行的工作项
- 风险、阻断、失败分类和恢复动作
- 质量门禁、验收状态
- 需要用户确认的事项
- 成本、耗时、资源使用

### 20.2 工程视图（开发者可见）

- worker 槽位、resource lock 冲突、等待原因、沙箱隔离等级
- 后台 supervisor / daemon 健康、最近 heartbeat、恢复状态
- **双线监督面板（V7 新增）**：
  - 工程线：傩 / Watchtower / Tester / Gate 状态
  - 方案线：Mission Director / 启 状态
  - 跨线：External Supervisor verdict + divergence_score
- **multi-LLM ensemble 面板（V7 新增）**：
  - 当前 ensemble 配置（每个 provider 在哪个 tier / 模式）
  - 实时 divergence_score
  - 每个 LLM 的最近 verdict
- AB adapter summary vs Frontier50 live executor 分开展示
- capability lifecycle 各阶段计数
- **6 层激活证据视图（V6 既有，V7 强 enforce 显示）**：每个 capability 在哪一层

### 20.3 关键 UI 规则

- 前端或 API 输出对非技术用户友好，**不能只是工程日志**。
- 中文角色名 + 英文括号（§5.7）。
- 监督结论必须可点击跳转到具体 review artifact。
- "已绑定 / 已使用 / 真实验证" 三态分开显示（V6 既有）。

---

## 21. 部署与运行

### 21.1 单机生产路径（默认）

- 本机多进程 worker pool：多个 daemon 服务实例共享同一任务队列和 SQLite resource lock。
- 每个进程独立 heartbeat / state。
- 单机 7x24 并发。

### 21.2 多机扩展路径

- 跨机器 worker 必须走 Redis / 数据库级 resource lock 适配层。
- 多 daemon fleet 共享同一 Control Plane store。
- 每个进程独立状态文件，驾驶舱和审计能区分 holder daemon / worker / 等待原因 / 锁过期时间。

### 21.3 多 LLM provider 部署要求

- 至少 2 个 provider 同时在线（§11.1）。
- provider 失联时降级路径明确：fallback 到 next-tier，不允许悄悄退化为单 LLM。
- provider rate limit / quota 监控接 Watchtower。

---

## 22. 安全、隐私与合规

### 22.1 沙箱

- 执行型 skill（shell-exec / file-io）默认沙箱根目录强制。
- 跨边界访问必须显式声明 + Gate 审批。
- 容器级隔离对高风险任务可声明 `container_required`。

### 22.2 数据隔离

- 多租户：tenant_id 强制传递，所有 DB 查询 RLS 校验。
- 跨租户数据不允许互访。
- 用户数据不进 capability candidate（除非显式 opt-in + 脱敏）。

### 22.3 凭据

- API key / OAuth token / 凭据走 `.env` + 加密存储。
- 不入 commit / 不入 log。
- 凭据轮换可在不停机情况下进行。

### 22.4 LLM 调用合规

合规级别表 (跟 §11.3 LLM provider matrix 协同, 不重复定义):

- 🟢 **完全合规**: API key 路径 (Anthropic / OpenAI API key) + 本地部署 (Ollama / vLLM)
- 🟡 **灰色合规区**: OAuth subscription token (Anthropic / Codex MCP) — 个人开发自用可, **商用部署必须切 API key 路径**
- 🔴 **不允许**: 多账号轮换 / 凭据共享 / token 二次分发

- 调用日志保留（用户 query / model response 摘要 / cost），但用户数据脱敏。
- 三方 LLM 服务 ToS 合规检查在 §22.1-22.3 安全协议里。

(V7 修复: §11.3 + §22.4 现统一表述, 不再矛盾)

---

## 23. 产品验收标准

V7 产品验收必须满足以下硬指标：

### 23.1 功能验收

#### 23.1.0 产品魂级硬规则 (V7 顶级, 一切其他验收的前置)

> **凡是不能进入真实生产链路被真实数据消费的功能, 都不算完成**

这条是 V7 §16.0 + §16.8 的产品魂级表达, **任何 acceptance criteria 之前的硬规则**:

- 7 层激活证据 (§16.1) 任一层不过 → **该能力不算完成**, 不进 production, 不写 capability_card, 不在驾驶舱显示 done, 不在 commit message / dev_log 宣称完成
- 攻击型测试 (§16.4) 不过 → 同上
- trace 写不出 (§16.5) → 产物不合规
- 跑过的算, 没跑过的不算; 真在生产链路用的算, 旁路演示的不算

#### 23.1.1 具体功能验收项

| 项 | 标准 |
|---|---|
| 任务方案先行 | 100% 复杂任务 (complexity_score ≥ 0.6) 有 TaskPlanDraft → Alignment → ExecutionContract 链 |
| 双线监督 | 每个长任务有工程线 + 方案线 + 跨线 三套监督数据流 |
| multi-LLM ensemble | 至少 2 个 cross-family provider 在线，External Supervisor 跨 family |
| External Supervisor 持续并行 watchdog (V7 新增) | 长任务/高风险/RSI/dogfood 默认 continuous mode + tick interval ≤ 120s; 短任务 default off |
| **External Supervisor auditor hat (V7 新增)** | 周期 / pre-release / dogfood 后 / capability Canary→Production gate 前必须跑 7 角度审计 (§16.6), P0/P1 issue 必须 closed 或显式 accept |
| 用户开关 (V7 新增) | global default + per-task override + 驾驶舱实时切换三层都生效 (含 watchdog 三个开关 + trifecta 4 个开关) |
| 本地 LLM 降本 (V7 新增) | 长任务 watchdog 本地 LLM tick 占比 ≥ 60% (前提: 用户已有 GPU); 启 Conservative/Performance 模式本地 LLM 占比 ≥ 50% |
| capability lifecycle | RSI candidate 100% 走 9 阶段 (中间严格验收 5 阶段)，production flip 100% user approval |
| 持续进化 (V7 新增, 产品魂) | 跑 ≥ 10 任务后启候选库 ≥ 5 张; 跑 ≥ 30 任务后 production lifecycle promoted ≥ 1 张 |
| **RSI 三线 trifecta (V7 §12.4 新增)** | 长任务默认 3 线 (过去 + 现在 + 未来) 全开, 按风险等级降级; trifecta 产物 100% 进 capability_card 走 lifecycle, 0 直接进 production |
| **7 层激活证据 (V7 升级, 原 6 层 + Layer 0 + Layer 6 攻击测试)** | 驾驶舱可见每个 capability 的当前层, ≥ Layer 4 才允许 runtime_enabled=true, ≥ Layer 6 + 7 角度审计过才允许 production |
| **攻击型测试硬规则 (V7 §16.4 新增)** | Tester ValidationPipeline 必须含 5 类攻击测试 (坏输入 / 错配 / 故障注入 / 权限剥夺 / 依赖缺失), **全过**才允许 Layer 5 闭环通过 |
| **强制 trace (V7 §16.5 新增)** | 每个产物 100% 含 input_refs / decision / gates_passed / scores / failures / repairs / fallback_used 字段 |
| **唯一生产入口 (V7 §16.3 新增)** | scripts/e2e_rsi_demo.py 等 fixture-only 脚本 100% 标 `# FIXTURE-ONLY`, 产物 0 进 capability_card |
| dogfood 规范 | dogfood 产物 100% 进 dist-output，0 直接合并到主路径 |
| **AST production-entry audit pass (V7.1, A15 / X.H)** | `tests/integration/test_production_entry_runtime_bundle.py` 8/8 green; 生产 WS 入口 spread `**bundle.as_orchestrator_kwargs()` |
| **Audit prompt template lint (V7.1, A15 / X.N)** | `tests/unit/test_hidden_orphan_audit_template.py` 8/8 green |
| **8 机制 env-all-on 真 fire (V7.1, A15 / X.L)** | `scripts/dogfood_v15_all_8_mechanisms_on.py` exit 0; enabled_flags 4/4 True; 5 event 类型 fire |

### 23.2 Claude Code 工程纪律 10 维（acceptance criteria）

P3 多维测试 battery（`scripts/multi_dim_test.py`）必须**在 multi-LLM 模式下** ≥ **19/20**：

1. 任务拆解 ≥ 2/2
2. 并行 sub-agent ≥ 2/2
3. grep verify before assume ≥ 2/2
4. 测试驱动 fail-fast ≥ 2/2
5. commit 纪律 ≤ 1000 行 ≥ 2/2
6. 错误立修不藏 ≥ 2/2
7. dev_log 沉淀 ≥ 2/2
8. 决策点停下问 ≥ 2/2
9. Read with offset+limit ≥ 2/2
10. Bash 克制 ≥ 2/2

### 23.3 真实长任务验收（V6 line 297-303 + V7 补充）

- 任务跨小时、跨天或重启后可恢复
- 人参与时通过 CollaborationTicket 闭环
- 交付物通过 GateEvaluation 和 AcceptanceReview
- 真实长任务复盘能进入启能力治理
- **multi-LLM ensemble 全程在跑**（V7 新增）
- **双线监督全程在跑**（V7 新增）
- **方案动态优化（PlanChangeProposal）在需要时真触发**（V7 新增）

---

## 24. 非目标

V7 明确**不做**的事：

- 用 agent 数量冒充能力
- 用 prompt 约束冒充工程约束
- 用结构化答案冒充真实交付
- 用外部脚本长期代替内置 Control Plane
- 在没有产物、证据、门禁和验收的情况下宣布完成
- 将外部人或操作者的执行能力记为 KUN 自动能力
- 将 replay、holdout、shadow 候选误当成生产默认能力
- 把 AB 或 benchmark 当成真实长任务产品化的替代品
- 把历史任务的角色、行业、模板、文案、美术、交互习惯或工作方法默认带入新任务
- 为了推进速度降低已确认的产品标准
- **跳过任务方案直接执行**（V7 新硬约束）
- **单 LLM 跑长任务**（V7 新硬约束）
- **dogfood 产物直接合并到主路径**（V7 新硬约束）
- **production flip 无 user approval**（V7 新硬约束）
- **External Supervisor 同 provider 运行**（V7 新硬约束）
- **不复制 OpenClaw / Hermes / Claude Code 实现代码**
- **不取三方品牌作为 KUN 子模块名**

---

## 25. V7 vs V6 差异附录

| Section | V6 状态 | V7 改动 | 影响范围 |
|---|---|---|---|
| 北极星 | 已定义 | 保留 | 无 |
| 一级子系统 | 6 个 | 升级 7 个（加 Mission Director） | §9.7 新章节 |
| 命名约定 | implicit | explicit §5 + 启傩独立可发布预留 | §5 新章节 + 代码层 V7-Phase A-D 迁移 |

### V6 子系统当前实装层级 (V7 Phase 0.4 grep 评估, 按 §16.1 7 层证据)

| 子系统 | 代码 module 数 | 当前最高 Layer | 备注 |
|---|---|---|---|
| 6.1 督师 (Director) — 任务理解 | 11 modules (含 planner / recursive_planner / intent / decision_point_classifier / long_task_router) | Layer 4 (真实消费) | dogfood v8 真用过 intent + planner; 但 RecursivePlanner 实际没被 LLM 用 (gpt-5.5 自己拆 phase) |
| 6.2 Control Plane (Executor + Daemon) | 34 modules + ExecutorLoop + LongTaskOrchestrator | Layer 5 (闭环通过) | dogfood v8 跑通 multi-step + checkpoint + budget guard, 接近 Layer 6 |
| 6.3 知识与证据 | self-reflect / grep-verify / file-io / shell-exec / web-search / pdf-read / csv-query / python-exec (8 个 builtin) | Layer 4 (真实消费) | dogfood v8 真用过 self-reflect 23 次 + grep-verify 单测覆盖 |
| 6.4 协同与资源调度 | CollaborationTicket 在 control_plane/v6.py, worker_pool 部分实装 | Layer 3 (runner 可执行) | runner 在, 真实 ticket triggered 数 < 10, **需 dogfood v9 验证** |
| 6.5 傩 (Nuo) — 质量治理 | SupervisorService + Watchtower 规则引擎 + ValidationPipeline + 4 个 Gate modules + RCDH 4 层 | Layer 4 (真实消费) | dogfood v8 真触发 PlanReview + 但 RCDH 4 层未端到端走过 |
| 6.6 启 (Qi) — 能力进化 | StrategistService + 3 模式 Explorer Pool 架子 + CapabilityProfile (13 处引用) | Layer 3 (runner 可执行) | StrategistService 跑得通, 但 **Explorer Pool 多 LLM 没 wire** (残废态, V7 Phase C 修); capability lifecycle 9 阶段架子在, gate 没 enforce (V7 Phase D 修) |
| **6.7 (V7 新) 交付总监 Mission Director** | **0 modules** | **Layer 0 (V7 方案能力)** | **完全没建, V7 Phase B 必须从 0 实装** |
| External Supervisor (独立) | 2 modules + critique impl | Layer 3 (runner 可执行) | critique 实装, **持续 watchdog mode 没接 (V7 §10.2.3 升级要求)**, **cross-family 校验没强 enforce (V7 §11.2 要求)**, **auditor hat 没接 (V7 §16.6)** |

**总体结论**:
- V7 7 层证据角度: V6 大部分子系统在 Layer 3-4, **没一个到 Layer 6 (真实 mission e2e + 攻击型测试)**
- 缺口最大的 3 个: **Mission Director (Layer 0)** / **External Supervisor 升级 (Layer 3 → Layer 4+)** / **Mission Director's auditor hat (新)**
- Phase B (双线监督 protocol) 是工程量最大的 Phase, 因为要从 0 建 Mission Director

### V6 → V7 Phase 工作量评估 (V7 Phase 0.4 输出)

| Phase | V6 起点 | V7 目标 | 估时 | 风险 |
|---|---|---|---|---|
| Phase 0 | 5 yaml 违规已撤回 (Phase 0.1) + 旧入口标 fixture-only (Phase 0.5) + 本评估 (Phase 0.4) | 全部完成 | 已完成 | ✅ 全部已落地 |
| Phase A 命名迁移 | strategist/ supervisor/ 旧 path | qi/ nuo/ 新 path + re-export | 1 周 | 中 (50+ files import 改) |
| Phase B 双线监督 + Mission Director | 部分 (有 watchtower + tester + gate) | 全套 + Mission Director 一级子系统 (从 0 建) | 2-3 周 | 高 (新建 module + daemon 默认注册 + 状态机改) |
| Phase C multi-LLM ensemble | LLMRouter + ab_alternates (单 LLM challenger) | ensemble_invoke API + cross-family 强 enforce + Haiku 二号 wire | 1-2 周 | 中 |
| Phase D RSI 9 阶段 lifecycle | capability lifecycle 架子在 | gate 强 enforce + user approval ticket + 5 yaml 走完整 lifecycle | 2 周 | 中 |
| Phase E UI 框架 + 6 层证据驾驶舱 | 几乎 0 | UI 框架 + 7 层证据可视化 | 3-4 周 | 高 (UI 工程量大) |
| Phase F Claude Code 蒸馏到 multi-LLM | P3 10 维 battery 已 19/20 (单 LLM) | 多 LLM 模式 ≥ 19/20 | 1-2 周 | 中 (依赖 Phase C + D) |
| Phase G External Supervisor 扩范围 | episodic critique | 持续 watchdog hat + auditor hat + 看所有监督角色 | 1 周 | 低 |
| **总计** | — | — | **11-15 周** | — |
| 任务方案先行 | CONVERSATION-LEARNING 提了 | 硬规则化 §10.1 | Control Plane 状态机加 enforce |
| 双线监督 | 散落 | 集中 §10 | Watchtower / Mission Director / 启 接口统一 |
| 方案动态优化 | PlanChangeProposal 提了 | 三档决策权 + 三级信号 §10.3 / §10.4 | 新 enum + 决策 logic |
| multi-LLM ensemble | line 276 提了 | 完整协议 §11 | LLMRouter 加 ensemble_invoke + cross-provider config 校验 |
| RSI 触发权 | line 158 严格禁止 | 明确：可触发但严格验收 §12 | capability lifecycle gate 强 enforce |
| External Supervisor cross-family | 隐含要求, 没明文定义 | **§11.2 cross-family 明文定义 + §10.2.3 持续并行 watchdog 升级 + §10.2.5 监督所有监督角色** | daemon 启动校验 + 风险等级驱动 on/off + 监督者递归不变量 |
| **本地 LLM 一等公民 (V7 升级)** | 仅作 fallback (V6 line 643) | **watchdog / 启 Explorer 优先本地, 前提是用户已有 GPU 资源 §11.3** | 部署 Ollama / vLLM (具体成本待真实部署校准, V7 不写假数字) |
| **持续进化产品魂 (V7 新增, KUN 原创非蒸馏)** | RSI 散落 | **§1.1 + §12.0 升级为产品魂** | "每跑一次都让自己略变更聪明", Claude Code / OpenClaw / Hermes 都没有, KUN 原创独有 |
| **用户开关 (V7 新增)** | 隐式 | **global + per-task + 驾驶舱实时 三层** | 用户可全程控制 watchdog 行为 |
| **complexity_score 算法明文 (V7 新增)** | 无定义 | **§6.1.1 简化 3 维 + LLM 兜底 + 反例兜底** | 引用 ≥ 0.5 改为 ≥ 0.6, 中间档 LLM 二次判 |
| **监督者递归不变量 (V7 新增)** | 无 | **§10.2.5 External Supervisor 监督所有监督角色 + High 级动作走用户兜底** | 防止 Mission Director / 启 / 傩 自己 drift 不可挽回 |
| dogfood 规范 | 阶段 9 | §19 + 反例 case study + **附录 A Phase 0 强制回滚 5 yaml** | dogfood runner 强 enforce, V7 立项前必须先清 dogfood v8 违规 |
| 三方对标 | OpenClaw + Hermes | 加 Claude Code 工程纪律 10 维 §4 + §17 | acceptance criteria §23.2 |
| 7 第一性回路 | CONVERSATION-LEARNING | 升级 §14 | 每回路明确归属 |
| 任务驾驶舱 | 已定义 | 加双线 + ensemble 面板 §20.2 | UI 升级 |
| Mission Director | line 227 加挂 | §9.7 一级子系统 + daemon 默认注册 | Control Plane 升级 |

---

## 附录 A：V7 实施阶段建议

V7 写完后的实施阶段。**Phase 0 是前置, 必须完成才允许进 Phase A** (回应 V7 攻击审视: dogfood v8 5 yaml 违规未修复 + UI 实装零)。

### Phase 0：V7 立项前置动作 (V7 攻击审视后强制新增)

**0.1 撤回 dogfood v8 5 yaml 违规合并**:
- 把 `seeds/methodologies/cache_aware_llm_wakeup_scheduler.yaml` + `prompt_user_decision_at_irreversible_branch.yaml` + `runtime_todo_tracker_state_machine.yaml` + `silence_detector_for_long_task_monitoring.yaml` + `worker_agent_spawner_prompt_isolation.yaml` 移到 `docs/dist-output/seeds-new/candidates/`
- 每张配 strategy_replay_report + process_audit (启 三类产物之二)
- 走 §15 lifecycle Observation → Candidate → Replay 起步, **不允许直接进 production seeds**

**0.2 跑一周真实数据收集 metrics, 校准 V7 数字**:
- watchdog tick interval / 月 cost / 本地 vs cloud 实际差距 / Mission Director tick overhead — 所有 V7 写"待真实数据校准"的数字
- 产物: V7.1 校准版 docs (注意: V7 本体不动, 校准是 V7.1 增量)

**0.3 dev 共识 V7 全文一遍**:
- 团队 (含 user) review V7 修订版 (本次修完的版本)
- 确认无遗漏后才允许进 Phase A

**0.4 进度同步 V7 vs V6 差异附录**:
- 把 V6 当前实装度评估写进 §25 差异附录的"V6 当前状态"列, 让后续 Phase 有起点

**0.5 旧入口收口 (V7 §16.3 强 enforce)**:
- `scripts/e2e_rsi_demo.py` 类绕过 Orchestrator 的脚本: 头部加 `# FIXTURE-ONLY: 不允许走 production 链路, 产物不进 capability_card / seeds /` 注释
- 在 `kun/control_plane/` 加 gate: 检测产物 metadata.fixture_only=true 时, 拒写 capability_card / seeds/methodologies/
- grep 全仓库, 找其他可能绕过的入口 (类似 `from kun.agents.X import Y; await Y(...)` 这种), 列清单
- 验收: 每个非主路径入口要么标 fixture-only, 要么改走 Orchestrator

**完成条件**: 5 项全过 → 允许进 Phase A。**未完成 0.x 任一项, 禁止启动 Phase A-G**。

### Phase A：命名迁移
- 加 `kun/agents/qi/` `kun/agents/nuo/` re-export (不动旧 path, 渐进迁移)
- 文档全面用启/傩中文名
- 估时: 触及 ~50 个文件 import, 约 1 周 (含 test 跑通)

### Phase B：双线监督 protocol 实装
- Mission Director runner 接 daemon 默认注册 + 调度优先级实装 (推荐: 应用层 priority queue)
- 工程线 / 方案线 / 跨线主体分工明文化
- 三档决策权 + 三级信号 enum + logic (注意: 阈值按 tick 数, 不按时间, 见 §10.4)
- 估时: 约 2 周

### Phase C：multi-LLM ensemble 实装
- `LLMRouter.ensemble_invoke` API
- **cross-family config 校验** (按附录 B family 表)
- Anthropic Haiku 第二 family wire (用户已有 OAuth, 走 §22.4 合规级别 🟡 仅用于个人开发自用)
- 商用前必须切 API key 路径
- 估时: 约 1-2 周

### Phase D：RSI 严格验收 lifecycle
- 9 阶段 lifecycle 实装 (其中 Replay→Holdout→Shadow→Canary→Production 是严格验收 5 阶段)
- production flip user approval ticket (UI 简化设计, 默认 OK 一键 approve, §12.2)
- dogfood v8 5 yaml 走 lifecycle 补回 (Phase 0.1 已撤回到 candidate, 这里走 Replay 起步)
- 估时: 约 2 周

### Phase E：UI 框架 + 6 层激活证据驾驶舱 (UI 实装零, 起点为 0)
- **先建 UI 框架** (V7 §20 当前是设计目标, 现有 UI ≈ 0)
- 每个 capability 当前层可视化
- 1-3 层长期停留触发启/傩治理
- 估时: 约 3-4 周 (UI 实装是 V7 最大工程量之一)

### Phase F：Claude Code 工程纪律到 multi-LLM
- 10 维 + 扩展项每条对应到 ensemble 内 LLM 行为约束
- P3 多维测试 battery 升级到 multi-LLM 模式
- 验收: 多 LLM 模式下 P3 仍 ≥ 19/20
- 估时: 约 1-2 周

### Phase G：External Supervisor 扩大监督范围 (§10.2.5 不变量实装)
- External Supervisor critique prompt 扩展, 包括 Mission Director / 启 / 傩 关键输出
- Daemon 启动 cross-family config 校验
- 估时: 约 1 周

**总估时**: Phase 0-G 累计约 11-15 周 (3-4 个月)。**注意每个 Phase 估时是初版起步, 实际跑一两个 Phase 后用真实数据校准 V7.1**。

**并行依赖**:
- Phase A / B 可并行
- Phase C 依赖 Phase A 完成
- Phase D 依赖 Phase C
- Phase E 几乎独立 (UI 工程)
- Phase F 依赖 Phase C + Phase D
- Phase G 依赖 Phase C + Phase B

---

## 附录 B：术语表

### 角色术语

| 术语 | 中文 | 英文 | 定义 |
|---|---|---|---|
| 督师 | 督师 | Director | §9.1 任务理解与方案系统负责人 |
| 执行 | 执行 | Executor | §9.2 Control Plane 执行体 |
| 验师 | 验师 | Tester | §9.5 工程线测试 |
| 门禁 | 门禁 | Gate | §9.5 工程线门禁 |
| 守望 | 傩 (Nuo) | Nuo | §9.5 质量治理与恢复系统负责人 |
| 进化 | 启 (Qi) | Qi | §9.6 能力进化系统负责人 |
| 交付总监 | 交付总监 | Mission Director | §9.7 任务级监督，可单独配 model/provider |
| 外部监督者 | 外部监督者 | External Supervisor | 跨 family LLM critique 角色 (§10.2.3 / §11.2), V7 升级监督所有监督角色 (§10.2.5) |

### 核心概念

| 术语 | 定义 |
|---|---|
| **complexity_score** | 0-1 浮点, 督师在 Intent Triage 计算, 走 engineering-first + LLM 兜底 (§6.1.1). ≥ 0.6 强制 TaskPlanDraft |
| **cross-family** | **不同 vendor 的不同 training pipeline**. Anthropic Claude 系 / OpenAI GPT 系 / Qwen 系 / Llama 系 / DeepSeek 系 / 本地小模型 等是不同 family. **同 vendor 不同 tier (e.g. Opus + Haiku) 不算 cross-family**, 同 family 不同 fine-tune 不算 cross-family. 详见 §11.2 |
| **RCDH 4 层诊断** | Runtime / Contract / Data / Hardware 四层根因排查模型, 傩主理 (§9.5). V6 ADR-021 既有, V7 沿用 |
| **TaskPlan / TaskPlanDraft / TaskPlanVersion** | 任务方案: Draft 是草案, Version 是版本化的 first-class object, 整体叫 TaskPlan |
| **PlanChangeProposal** | 方案变更提案, 方案线发现问题后触发 (§10.3.2). 三档严重等级: Low / Medium / High (§10.3.3) |
| **MissionAlignmentReview** | 任务对齐审查, Mission Director 周期性输出 (§9.7) |
| **PlanAlignmentEvent** | 计划对齐事件, 在长任务执行中周期性触发 (CONVERSATION-LEARNING 2.2 既有) |
| **EnsembleResponse** | `ensemble_invoke` 输出, 含 responses[] / consensus / divergence_score / divergence_signals (§11.4) |
| **ExternalSupervisorObservation** | External Supervisor 每 tick 输出 (§10.2.3): verdict + drift_signals + self_aggrandizement_detected |
| **capability_candidate** | 启 strategy replay 三类产物之一 (§9.6 / §12.3) |
| **strategy_replay_report** | 启 strategy replay 三类产物之一 (旧策略 vs 新策略指标对比) |
| **process_audit** | 启 strategy replay 三类产物之一 (原链路哪里浅 / 哪里错) |
| **GoalAnchor** | 任务目标锚点, anti-drift Layer 2 顶部 pinning (§13.5) |
| **AnomalyKind** | 异常类型枚举: llm_fallback_spike / task_failure_spike / context_oversized_spike / skill_mismatch_spike / task_anomaly_spike / duration_outlier (傩规则引擎用) |
| **TaskType** | 任务类型分类, intent 阶段识别 (e.g. `coding.research.rsi.capability_distillation`) |

### Family 分类表 (V7 cross-family 校验依据)

| Family | 例子 |
|---|---|
| Anthropic Claude 系 | Opus / Sonnet / Haiku (所有 tier 同一 family) |
| OpenAI GPT 系 | gpt-4 / gpt-5 / gpt-5.5 / Codex |
| Qwen 系 | Qwen-7B / Qwen-32B / Qwen-Max (本地或 cloud 部署都属同 family) |
| Llama 系 | Llama-3 / Llama-3.1 / Llama-derivatives |
| DeepSeek 系 | DeepSeek-V3 / DeepSeek-R1 |
| Mistral 系 | Mistral / Mixtral |
| Google Gemini 系 | Gemini Pro / Gemini Flash |
| MiniMax 系 | MiniMax-M2 / M2.7 |
| 本地小模型其他 | Phi / Gemma / OpenHermes 等 |

**判 cross-family 规则**: 不同 Family 行 = cross-family ✅; 同 Family 行不同 tier 或 fine-tune = 同 family ❌

---

**KUN V7 — End of Document**

> 本文档是后续 KUN 开发的唯一对齐锚点。
>
> 发现 V7 不完整、自相矛盾、不可执行时，先改 V7 再写代码。
>
> V7 实施阶段（附录 A）由开发任务排期决定，本文档不涉及具体时间表。

---

## V7.1 Amendments (X.P, 2026-05-29)

X.A → X.O 15 implementation waves shipped operational specifics that
V7.0 didn't cover (verified via grep: 0 hits on 13/14 X.A-O concepts in
this file). The amendments live in:

  **`docs/v7/KUN-V7.1-amendments.md`** — 17 amendments (A1-A17),
  P0/P1/P2 prioritized, each with V7.0 § pointer + exact INSERT text.

From X.P onward, any new wave (X.Q+) MUST:
  1. First merge relevant amendments into V7.0 if applicable
  2. Then implement
  3. Then add the wave's own amendments back to KUN-V7.1-amendments.md

This makes V7 itself part of the RSI loop — the protocol doc evolves
with the implementation instead of falling behind.

**Merge status (X.R, 2026-05-29 — 全 17 条已 merge)**:
  - P0 (6): A3 §12.2 / A6 §12.6 / A7 §15.2 / A9 §16.2.5 / A10 §16.3 /
    A12 §16.6 — ✅ merged in X.P-followup commit ae10c4f
  - P1 (9): A1 §4.3 / A2 §11.4 / A4 §12.3 / A5 §12.4 / A8 §15.4 /
    A11 §16.5 / A13 §16.8 / A14 §20 / A15 §23.2 — ✅ merged in X.R
  - P2 (2): A16 Appendix A / A17 Appendix B — ✅ merged below (X.R)

V7.1 spec 现 100% 同步 X.A → X.Q 实装. KUN-V7.1-amendments.md 留作 merge
审计轨迹.

### 附录 A 补充: X.A → X.Q 实装 phase (A16 / X.R)

| Phase | 内容 | 状态 |
|---|---|---|
| X.A | qi/ + nuo/ rename re-export | ✅ |
| X.B | 4 X.B 表 + bridges + writes_wired_status (MF-1..MF-6) | ✅ |
| X.C | LLM auditor / crash resume / trifecta class / lifecycle walker / collab e2e / capstone v11 | ✅ |
| X.D | ProcessAudit merge + dogfood v12 (5.16x 实测) | ✅ |
| X.E | Trifecta 接 orchestrator + dogfood v13 | ✅ |
| X.F | Cockpit daily UI + ensemble reader | ✅ |
| X.G | Methodology 读侧 selector + dogfood v14 | ✅ |
| X.H | 5 根因 + bundle + ticket verify + trace + META | ✅ |
| X.I-0..4 | 3 孤儿修 + production-path 原语 4 子系统接 | ✅ |
| X.I-3-FIX | 半孤儿 diff checker consumer | ✅ |
| X.J | 真用户真任务 ≥30 min | ⏳ 等用户 |
| X.K | Cockpit 浏览器验证 | ✅ |
| X.L | dogfood v15 8 机制全开 | ✅ |
| X.M | ProcessAudit X.H seeds | ✅ |
| X.N | CI 模板 lint + driver | ✅ |
| X.O | 2 self-audit bug + Step 7 chain-reach | ✅ |
| X.P | V7.1 amendments doc | ✅ |
| X.Q | full-review (rsi-trifecta stub fix + rsi_loop fence) | ✅ |
| X.R | 11 P1/P2 amendments merge (本节) | ✅ |
| X.S | discipline→PG + 删 dead 骨架 | ⏳ |

### 附录 B 补充: V7.1 新术语 (A17 / X.R)

| 术语 | 定义 |
|---|---|
| **hidden orphan** | capability 名义上 wired (class signature / 测试 / 引用) 但生产入口从不实例化或调用. X.H 前 KUN 5 次 release 复发率. |
| **chain-reach** | 经 1+ caller 跳的间接生产 wiring. Step 4 直接 grep 漏, Step 7 显式 walk 抓 (X.O). |
| **production entry** | `docs/PRODUCTION_ENTRIES.md` 列的文件. Audit / Gate / 模板对此集合查 reachability. |
| **LongTaskRuntimeBundle** | opt-in `LongTaskOrchestrator` 特性的唯一中枢. CI 守卫防新 opt-in 绕过 bundle. |
| **TicketVerifier** | 解析 `user_approval_ticket_id` 对 queue 校验 status + selected_option=='approve' 的 Protocol (§12.2). |
| **Angle 8** | V7 §16.6 攻击审计的 production-path-traceability 轴. X.I-1 加. |
| **R1-R5 根因** | 5 个 LLM 复发审计失败 (grep 颗粒度 / 无 inventory / opt-in 无消费者 / fixture-as-prod / module-not-entry). |
| **MethodologyRuntimeSelector** | RSI 读侧 — 加载 `seeds/methodologies/*.yaml`, 关键词重合打分, top-K 注入 system prompt. |
| **production_entry_diff_check** | 比对 TaskSpec.production_entry_changes_required (声明) vs tool_calls 提取的实际改动路径. verdict ∈ {match, drift, no_declaration, undeclared_changes}. |
