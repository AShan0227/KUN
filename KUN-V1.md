# KUN-V1 · 鲲产品开发方案

> 基于《汇总版V2.docx》十几轮讨论整理，全新项目（Genesis 为上一代产品，不做迁移）。
> 产品名：**鲲 / KUN**。管家子模块保留为 **傩 / NUO**（内聚于 KUN 前端，未来可独立）。
> 商业化、定价、生态合作等放在第二阶段，不在本方案范围。

---

## 目录

一、产品定位与第一性原则
二、整体架构：三元要素 + 两个大脑 + 黑板
三、Context 子系统
四、接入层
五、工程化子系统（按执行时序）
六、守望子系统（系统隐藏大脑）
七、任务执行大脑
八、评估与进化体系
九、协同机制
十、产品门面：傩（NUO）与交互设计
十一、透明化与用户协作
十二、安全、权限、合规
十三、核心数据模型（能力卡 / TASK.md / 交接协议 / RuntimeState / Starter Pack）
十四、技术栈与工程选型
十五、开发优先级与里程碑
十六、简洁化原则与合并清单（ADR-018）
附录 A：核心术语表
附录 B：十几轮讨论累积的落地点汇总
附录 C：能力卡校准任务集（6 个）
附录 D：启动前准备清单

**权威决策文档**：`decisions.md`（ADR-001 ~ ADR-018）——冲突时以该文件为准。

---

## 一、产品定位与第一性原则

### 1.1 产品定位（三层递进）

| 阶段 | 定位 | 核心能力 |
|------|------|---------|
| 现在 | 个人超级助手 | 替用户完成数字任务 |
| 中期 | Agent 管家 / Agent OS | 替用户管理一批 agent、工具、资产 |
| 终态 | 协作调度平台 / 自运营 agent 无人公司 | 替用户调动整个生态（其他 agent、人、企业）协同完成项目 |

设计时直接按终态架构搭，避免后续推翻重做。

**冷启动承诺**：交付给用户的 KUN **上来就是完整版**，不是从零学起——内置基础能力池、默认 skill 库、预训练路由规则、默认角色模板。用户越用越强，但不会"新装没法用"。

### 1.2 五条第一性原则

1. **结果 > 功能**：不卖能力清单，卖用户要的最终结果。
2. **离钱近**：优先做能直接影响用户账户数字的场景，远离"纯效率工具"定位。
3. **帮用户赚钱**：不仅省用户的钱（成本降），更要让用户赚到钱（收入升）。
4. **透明 + 可解释**：每笔消耗、每个决策、每次优化都讲得出"为什么"——消费可归因、未来可预期。
5. **用户是决策合伙人**：不是被动消费者，是系统的一部分；KUN 可反向派活，用户可接入全域生态。

### 1.3 辅助原则

- **效果第一，成本第二，速度第三**：达标方案里再选最便宜，便宜方案里再选最快。不为省钱牺牲效果。
- **工程化是"做好的必要条件"，不是"压缩成本的借口"**：该用大模型毫不犹豫上。
- **学习和成长放在系统的每一面**：不限于反馈学习——路由规则、评估阈值、辩论结论、压缩策略、能力卡、权限策略……任何"数据足够即可优化"的地方都有自我进化回路。
- **尽量接近常识**：元要素越少、越接近常识，系统越稳、越易扩展。
- **UI 三铁律**：无必要不新增 / 大部分用户停在第 1 层 / 安全按影响面分档。

---

## 二、整体架构

### 2.1 三元要素（最终版）

```
KUN
├─ Context 系统     → 脑子里装的东西（知识/记忆/方法论/skill/角色模板/TASK.md/能力卡）
├─ 接入层           → 和外部世界的所有通道
└─ 工程化系统       → 所有绕开大模型跑的代码（含守望、调度、评估、学习）
```

- 角色（agent）不是独立元素，是运行时临时组装：context + 模型 + skill 许可 + 工程化参数。
- Skill、角色模板、能力卡都归属 Context 系统里的资产类目。
- 不存在"管理 agent"——管好 Context 和工程化，agent 是它们的投射。

### 2.2 两个大脑（并列）

| 类别 | 任务执行大脑 | 系统隐藏大脑（守望子系统） |
|------|-------------|---------------------------|
| 用户可见 | 是（对话入口） | 否（用户不直接交互） |
| 负责 | 任务怎么做 | 整个系统怎么运行 |
| 内部结构 | 意图理解（贵模型）+ 拆解（中档）+ 路由（便宜 + 规则） | 事件订阅 + 规则判断 + 分级自治 |
| 调度谁 | 派角色做具体任务 | 派所有子系统（含任务大脑）怎么动 |

系统隐藏大脑是产品差异化的根基，比任务大脑更核心。

### 2.3 黑板（Blackboard）= 交互式控制台

不是独立存储，而是 Context 系统 + 工程化运行时状态的"实时交互式视图层"。

```
【底层存储】
├── Context 系统（长期持久数据）
└── 工程化运行时（瞬时状态）
      ↓ 同一份数据
【黑板视图层】（聚合 + 权限过滤）
├── 任务看板（在跑任务 + 进度 + 资源占用）
├── 事件流（最近发生了什么）
├── 全局状态区（预算档位、安全等级、系统压力）
├── 共享工作区（角色间协作产物）
└── 资产池活跃切片（当前任务涉及的知识/记忆）
      ↓ 两种渲染出口
├── 对人：UI 看板（三层交互，见第十章）
└── 对 agent：结构化文本（JSON / TOON / XML）
```

三个好处：单一事实来源 / 权限统一管理 / 底层演进时上层不动。

---

## 三、Context 子系统

### 3.1 总览

```
Context 管理子系统
├── 重要度打分器（中央）← 所有其他模块都用它的输出
│   ├── 语义相关度（嵌入向量）
│   ├── 访问频率（带饱和的计数器）
│   └── 近期性（时间戳）
├── 压缩器（LLMLingua + 前缀缓存）
├── 分类/合并器（按时间 + 类型 + 所属）
├── 遗忘器（FadeMem 双层半衰期 + 永久档）
└── 统一存取范式（三级渐进披露）
```

**分配原则**：90% 纯工程 + 9% 便宜模型运行时 + 1% 最强模型离线批处理。但识别到工程效果不够的边缘场景，大模型毫不犹豫上。

### 3.2 中央重要度打分器

综合三因素：语义相关度、访问频率（带饱和）、近期性。

三大作用：检索权重 / 衰减速度 / 层级归属。

**初始打分策略**（启发式 + 自愈）：
- 启发式规则：从任务元数据推导（重要度、角色级别、是否涉及金钱/不可逆）
- 让"访问即强化"机制自愈：60% 即时准确率 + 长期自愈 ≈ 85-90% 长期准确率
- 仅在"持续检索但低分 / 长期不访问但占位大"这类异常时触发大模型复审

### 3.3 压缩器

**三级动态压缩管道（Agent 间通讯默认启用）**：

```
角色 A 产出 → 工程层拦截
  ↓
【第一级】结构化重组（纯工程，毫秒级）
  - 去冗余、合并重复、按 TOON/二进制编码
  ↓
【第二级】语义压缩（LLMLingua，~10ms）
  - 小模型判断 token 贡献度，默认压 5 倍
  ↓
角色 B 接收
  ↓
【第三级】按需解压（接收方触发）
  - B 发现信息不够 → 回问 A → A 返回未压缩版
```

**前缀缓存策略**（大模型端实际效果等价于"符号替代"）：

| 段位 | 命中率 | 策略 |
|------|-------|------|
| 永久段 | ~100% | 标记可缓存，基本白嫖（Anthropic 端 10% 计费） |
| 稳定段 | ~80% | 标记可缓存，TTL 长 |
| 半稳定段 | ~50% | 可缓存但 TTL 短 |
| 变化段 | ~0% | 不缓存，正常付费 |

**子组合缓存**：应用层维护高频子任务组合的压缩模板库，匹配到直接用模板（架在大模型缓存之上）。

**跳过压缩**：输入少于 500 字 → 不压缩（开销 > 收益）。

### 3.4 分类与合并

**分类维度**（四层所属）：
- 按时间：短期 / 中期 / 长期
- 按类型：事实（静态）、事件（动态）、方法论（元知识）、用户偏好
- 按所属：全局 / 组织 / 项目 / 任务

**合并策略**：
- 同质合并（相似度）：融合成一条带"出现 N 次"标注
- 顺序合并（时间段）：会话全部交互压成摘要
- **铁律**：合并不能丢时序，必须保留时间范围戳

### 3.5 遗忘机制（FadeMem + 永久档）

基于艾宾浩斯遗忘曲线（arxiv 2601.18642）。

| 层级 | 半衰期 | 用途 |
|------|-------|------|
| tier 0 永久档 | ∞（衰减率 0） | 系统红线 / 用户核心身份 / 组织级事实 / 成熟方法论 |
| 长期层 | ~11.25 天 | 多次验证过的经验 / 重要记忆 |
| 短期层 | ~5 天 | 某次执行细节 |

**永久档规矩**：衰减率 = 0；修改必须人审批；有版本号和审计；数量 ≤ 100 条。

**晋级机制**：重要记忆被访问后从短期→长期→永久档。

**软/硬遗忘**：先软遗忘（降权不删），确认真没用再硬删。

### 3.6 三级渐进披露（通用存取范式）

Skill / 记忆 / 知识库 / 通讯 / TASK.md 全部按这个三层结构：

| 层级 | 大小 | 触发 |
|------|------|------|
| 第 1 层：元数据 | 极小（几十字） | 始终在 context 里 |
| 第 2 层：摘要/接口 | 中（200-500 字） | 按相关度决定是否加载 |
| 第 3 层：完整内容 | 大（可能 1000+ 字） | 只有真正要用才加载 |

**直接采用 Anthropic Agent Skills 的 SKILL.md 格式（2025-10 规范）**，兼容 OpenAI Codex CLI、ChatGPT，能直接用官方 140+ 开源 skill。

### 3.7 反查能力 + 热更新

**反查**：完整链路记录——哪些记忆被调用 → 被压缩成什么 → 塞进哪次推理。调试和审计的基础。

**热更新（黑板广播机制）**：

```
Context 变化事件层
├── 黑板层（共享状态，变化立即可见）
├── 订阅层（按关注度过滤）
├── 紧迫度层
│   ├── 紧急变化（用户说"停"/安全红线） → 打断当前动作
│   └── 普通变化（用户补信息） → 下一轮 OODA 前合并
└── 冲突解决层（用户 > 角色 A > 角色 B 的明确裁决规则）
```

---

## 四、接入层

### 4.1 结构

```
接入层（管所有对外通道）
├── 对人：消息、邮件、UI、报表
├── 对外部 agent：A2A 协议、MCP
├── 对外部企业 API：REST/GraphQL、数据库连接
├── 对自建工具：内部 tool 调用
└── 对未来硬件：物理设备控制协议（前瞻）
```

### 4.2 翻译适配器层（出口网关）

```
KUN 内部（结构化、压缩、TOON）
  ↓
翻译适配器层
  ├── 对人类 → 自然语言 + 格式化（邮件/消息/报表）
  ├── 对外部 agent → A2A / MCP
  ├── 对外部企业 → 他们的 REST/GraphQL
  ├── 对外部文档系统 → Markdown/PDF
  └── 对未来硬件 → 对应控制协议
  ↓
外部世界
```

### 4.3 统一协作接口（内外一致）

所有协作实体（内部角色 / 外部 agent / 合作企业 / 人）走同一套接口：身份与能力卡、分层通讯格式、协作反馈回路、退出机制。详细数据模型见第十三章。

### 4.4 Agent 间通讯优先级规则

```
优先级 1：直接调 API（最便宜、最快、最可靠）
优先级 2：结构化 JSON 或 TOON
优先级 3：带结构化信封的自然语言（外 JSON 契约，内 payload 字段）
优先级 4：纯自然语言（兜底，尽量避免）
```

内部全部结构化，只在最终对用户展示时用自然语言。

---

## 五、工程化子系统（按执行时序）

### 5.1 事前：启动与规划

```
事前模块
├── 任务接入（用户 / A2A / 内部）
├── 任务说明书（TASK.md）生成
├── 任务指纹登记（幂等键）
├── 意图理解（贵模型）
├── 任务分类与复杂度打分
├── 三维风险预估
│   ├── 财务风险：金额估算 + 预算估算 → 触发档 3 权限门
│   ├── 不可逆风险：动作类型 + 影响面 → 强制辩论 + 档 3
│   └── 复杂度风险：预计步数 + skill 数 + 是否新任务 → 升档 + 严评估
├── 预冲突扫描（左移）
│   ├── 对比任务资源 vs 当前在跑队列
│   ├── 三种处置：延后启动 / 串行化 / 合并
│   └── 无冲突 → 放行
├── Context 预热（主动加载可能用到的记忆 / skill / 方法论）
├── 资源预估（token / 时间 / 角色数）
├── 注意力分配
├── 角色实例化（按模板 + 参数临时组装）
└── 执行计划拟定
```

### 5.2 事中：运行时动态控制（差异化核心）

```
事中动态控制
├── 任务入口处
│   ├── 幂等键检查 → 重复任务直接返回缓存
│   └── 任务指纹登记
├── 资源访问前
│   ├── 分布式锁 acquire（10s 超时防死锁）
│   └── 版本号校验（乐观并发）
├── 实时监控
│   ├── 每步结束后打分
│   ├── 预算追踪（四档收敛）
│   └── 每步 checkpoint 评估
├── 路径纠偏（不是"修复"是"纠偏"）
│   ├── 预期 vs 实际偏差检测
│   ├── 探索分支（备用路径沙箱试跑）
│   ├── 灵光一闪（发现更优路 → 切换）
│   └── 中途 replanning（拆解不对 → 重拆）
├── 早期错误感知（左移核心）
│   ├── 死循环检测
│   ├── 偏离主题检测（范围漂移）
│   ├── 成本飙升检测
│   ├── 一致性掉分检测
│   └── 趋势监测（下降趋势即预警）
├── 自我修复
│   ├── 工具切换 / 角色切换 / 重试 / 重拆任务
├── Context 热更新（订阅机制 + 黑板广播）
├── 动作执行前冲突检测
│   ├── 有副作用的动作先进待执行队列
│   ├── 基于语义标签的冲突检测
│   ├── 冲突仲裁（守望子系统按来源/优先级）
│   └── 无法裁决 → 升级给人（第 4 级）
├── 辩论触发（见 8.2）
└── 升级 / 降级
```

**预算追踪四档（借鉴字节广告投放）**：

| 档位 | 剩余预算 | 行为 |
|------|---------|------|
| HIGH | > 50% | 正常探索 |
| MEDIUM | 20-50% | 保守（优先稳定方案） |
| LOW | 5-20% | 收敛（仅已验证路径） |
| CRITICAL | < 5% | 自动用摘要替换历史 / 询问用户是否追加预算 |

**硬熔断策略（基础版，测试时调整）**：
- 单任务预算耗尽 × 1.2 → 强制停止 + 保存当前进度 + 问人
- 全局日预算耗尽 × 1.1 → 所有新任务排队 + 告警
- 单用户月预算耗尽 → 降级到"只读模式"（继续看结果、不跑新任务）
- 任何时候：已产出的结果永不丢弃，优先保存再熔断

### 5.3 事后：复盘与学习

```
事后模块
├── 结果评估（按"意外度"分档深度）
│   ├── 意外度高 → 深度分析（成本超预估 50%+ / 走意外路径）
│   ├── 意外度中 → 中度分析
│   └── 意外度低 → 只入流水账
├── 反馈传播（事件广播给相关子系统）
├── 经验蒸馏（情节记忆 → 语义记忆）
│   ├── 每次任务后入短期层
│   └── 每晚批量蒸馏，通用规则写入长期/永久层
├── 进化建议生成
├── 审计归档
└── 释放锁 / 更新版本号 / 去重表记录
```

### 5.4 跨阶段常驻

```
跨阶段常驻
├── 异常检测（独立守望进程）
│   - 成本 / 质量 / 行为 / 安全 四类异常
├── 权限与沙箱（三档：Firecracker / gVisor / 硬化容器）
├── 进化管理（影子 / 金丝雀 / 多臂赌博机）
└── 守望调度（事件订阅 + 决策下发）
```

### 5.5 冲突处理

| 类型 | 场景 | 解法 |
|------|------|------|
| 任务重复派发 | 用户连发 / A2A 重发 | 幂等键（哈希 + 用户 ID + 时间窗口） |
| 资源竞争 | 多角色改同字段 | 分布式锁（10s）+ 乐观版本号 + 敏感资源队列化 |
| 执行路径冲突 | A 要发券、B 要停服 | 动作前置审查 + 语义标签冲突检测 + 守望仲裁 + 升级给人 |

**左移**：事前预冲突扫描优于事中仲裁，避免"已投入计算才发现冲突"。

---

## 六、守望子系统（系统隐藏大脑）

### 6.1 职责

**全系统中央调度者**，不只是评估模块。

```
守望子系统
├── 事件接入：订阅所有任务、角色、资产的事件
├── 监控：多维度指标实时计算
├── 判断引擎：基于规则判断信号意味着什么
├── 干预决策（五档）：
│   ├── 不管（90% 事件）
│   ├── 记录（留痕不干预）
│   ├── 轻度（调参 / 换 skill）
│   ├── 中度（暂停 / 触发辩论）
│   └── 重度（回滚 / 隔离 / 升级给人）
├── 执行：决策下发给相关子系统
└── 学习：记录干预效果，调整未来判断规则
```

**基础架构工程化，但调用大模型不受省钱束缚——效果第一**。

#### 6.1.1 规则引擎选型（ADR-004）

守望的规则引擎采用 **YAML 声明 + Python handler** 架构（Prometheus alerting rules 风格），同一引擎也承载评估触发、CI 护栏、异常检测（见 §16 `GuardRule` 统一）。

```yaml
# 示例：rules/cost_runaway.yaml
id: cost_runaway
trigger:
  event_type: task.step.completed
  when: "event.accumulated_cost_usd > task.estimated_cost_usd * 1.2"
severity: medium
actions:
  - handler: pause_task
  - handler: notify_user
    params: { template: cost_exceeded }
```

- 条件表达式：`simpleeval` / AST 白名单求值（禁危险操作）
- handler：Python 函数通过 `@rule_handler("pause_task")` 注册
- 规则文件版本化（git），每条规则有 `version` 字段，改动走 §8.3 的 AB 流程
- 优势：好读好审计 / 无代码回滚 / 不引入重型规则库

### 6.2 分级自治（4 级）

```
第 1 级：角色自己（内部 OODA，纯工程）
  - 工具失败 → 换工具 / 参数调整 / 重试，无需上报

第 2 级：任务内编排（工程 + 中档模型）
  - 拆解不对 → 重新拆 / 换角色，上报给守望留痕

第 3 级：守望子系统（工程为主 + 场景化上大模型）
  - 暂停 / 隔离 / 触发辩论 / 升级到人

第 4 级：人（终极仲裁）
  - 前 3 级处理不了或拒绝处理的 / 不可逆重大决策 / 策略级调整
```

**三条设计原则**：
- 每级有明确决策额度（金额 / 影响面 / 不可逆度）
- 上报有成本 → 鼓励本级解决
- 常规信道 vs 紧急信道（不同路径）

### 6.3 守望调用大模型的正确场景

- 新失败模式归因分析
- 综合异常判断
- 复杂纠偏策略生成
- 给人的告警措辞优化
- 任何"工程做不好"的判断

### 6.4 idle-batch（用户闲置时批处理）

按用户闲置时段智能调度，所有"离线学习/评估/进化"工作归入此系统（术语见 ADR-017）。

```
idle-batch 调度器
├── 任务回放（新旧版本在历史任务上对比）
├── 多样本一致性测试（温度 / 改写 / 模型三重扰动）
├── 方法论蒸馏（情节 → 语义）
├── 知识冲突解决（资产池矛盾记忆仲裁）
├── AB 决策汇总（实验状态更新 / 胜出者推到影子）
├── 健康报告生成（周报 / 月报 / 趋势）
└── 路由规律涌现发现（聚类 + 关联规则挖掘）
```

**用户可关**：每项独立可配置，提供"全开 / 推荐 / 必要 / 自定义"四种模式。

---

## 七、任务执行大脑

### 7.1 三层结构

```
任务大脑
├── 意图理解层（贵模型）  ← 理解错全白费
├── 任务拆解层（中档）     ← 拆成子任务
└── 路由层（便宜 + 规则）   ← 派给哪个角色模板
```

三层都不执行任务，只决定"谁执行"。

### 7.2 模型路由引擎（工程化头等公民）

**模型能力地图**：把典型任务跑过所有主流模型，记录每模型在每类任务上的效果/成本/速度三维曲线，数据驱动决策，每月更新。

**路由自我进化**：

```
基础规则（初始手写）
  ↓
每次路由结果 → 记录（任务特征 × 模型 × 效果 × 成本）
  ↓
定期离线分析（每周）：
  ├── 聚类分析（K-means / DBSCAN）找任务簇最佳路径
  ├── 关联规则挖掘找"特征 + 结果"强关联
  └── 异常检测找"效果 vs 预期差很远"的路由
  ↓
新规律 → 影子 → AB → 合并进基础规则
```

### 7.3 开发阶段的具体模型路由（供调试）

开发期以你现有资源为主，架构抽象允许未来替换：

| 优先级 | 用途 | 当前模型 | 调用方式 |
|-------|------|---------|---------|
| **主力（default）** | 意图理解 / 拆解 / 大部分任务的执行 | **Opus 4.7** | 个人订阅 |
| **次力（secondary）** | 编程专项 / 代码密集任务 | **Codex 5.3** | 个人订阅 |
| **便宜档（cheap default）** | 路由决策 / 分类 / 简单判官 / 压缩小模型调用 | **Claude 系列**（Haiku 4.5 默认；Sonnet 4.6 作为中档备选）| 订阅 |
| **兜底（fallback only）** | 主力超限 / 订阅中断 / 故障转移 | **MiniMax M2.7** | 直接 API |
| 嵌入 / 小模型压缩 | LLMLingua 之类本地跑 | GPT2-small / 开源 embedding | 自部署 |

**路由调用顺序（硬规则）**：
1. 默认走主力 Opus 4.7
2. 识别为"代码密集" → 走 Codex 5.3
3. 识别为"轻量决策 / 分类 / 简单判官" → 走便宜档
4. 任何上述路径不可用（额度耗尽 / API 故障 / 订阅异常）→ 自动降级到 MiniMax M2.7 fallback，并推送通知给你
5. MiniMax 也不可用 → 熔断、排队、问用户

**架构层面**：抽象为 `LLMProvider` 接口，路由层只看模型能力标签（tier / strength / cost），不绑死具体厂商。产品上线后可无缝切换到任一 API 厂商。目前订阅路径统一走 `ofox proxy`（你 CLAUDE.md 里的 `api.ofox.ai`），MiniMax 走其官方 API。

### 7.4 注意力分配（工程化公式）

| 维度 | 打分依据 |
|------|---------|
| 任务重要度 | 规则识别（金额 / 合规 / 客户等级 / 用户显式标记） |
| 任务复杂度 | 历史失败率 + 描述长度 + 需调 skill 数 |
| 任务紧迫度 | deadline / 用户等待行为 |
| 意外度 | 事后打标 |
| 风险度 | 三维风险预估的综合分 |

总分决定用哪档模型、多少预算、多严评估、要不要复盘。全部公式化。

---

## 八、评估与进化体系

### 8.1 评估触发矩阵（纯工程规则）

|              | 风险低                   | 风险高                              |
|--------------|--------------------------|-------------------------------------|
| **复杂度低** | 档 0：无评估（规则通过）    | 档 2：多判官投票（3-5 便宜大模型）   |
| **复杂度高** | 档 1：单判官 + 评分表       | 档 3：完整评估（多判官 + 人抽 + 基准）|

**多判官投票**：同时跑 3-5 个 LLM 裁判，随机打乱顺序，多数票定。

**与人类评审对齐**：目标 Spearman 相关系数 0.80+。

**基准测试**：每周一次离线跑公共基准（SWE-bench Verified / GAIA / WebArena）+ 自建测试集。体检分下降 → 系统注意力提高。

### 8.2 辩论机制（带学习曲线）

**iMAD 风格触发分类器**（arxiv 2511.11306）特征：
- 初始自信度（辅助，不唯一）
- 任务复杂度
- 任务类型（哪类易出错）
- 是否涉及不可逆 / 金额 / 合规

**分层触发**：

| 场景 | 辩论强度 |
|------|---------|
| 常规 80% | 不触发 |
| 矛盾信号 | 3 便宜模型多数票 |
| 高复杂度 | 5 中档模型多数票 |
| 高风险/不可逆 | 完整辩论（主模型 + 反对方 + 主持人） |

**学习曲线（新增）**：同类任务辩论 3 次（可配置 N）都给出同一结论 → 该结论固化成规则，后续同类任务直接走规则不再辩论。把辩论开销收敛为一次性投入。

**触发时机**：事前（决策前 iMAD 分类器）/ 事中（矛盾信号）/ 事后（结果不确定）。

**避免过度消耗**：大部分任务不触发；次数纳入预算追踪；上限（每天/用户/任务 N 次）。

### 8.3 AB 测试与渐进部署

```
阶段 1：【批处理离线】（绝大部分优化在这里就能筛出结论）
  - 真实历史任务回放 + 对比 + 一致性测试 + 显著性检验
  - 对生产零影响

阶段 2：【影子模式】（真实流量但不影响用户）
  - 新旧同时跑，只记录新版结果

阶段 3：【金丝雀】1% 真实用户
  - 严格护栏：任一关键指标破线 → 立即回滚

阶段 4：【放量】5% → 20% → 50% → 100%
```

**多臂赌博机**：效果好的自动加流量，效果差的减，不等实验结束。

**自动回滚**：上线后 N 小时内核心指标超限 → 自动回滚，不等人。

### 8.4 能力卡 + 一致性分数（替代"大模型自信分"）

详细数据模型见第十三章。

**多样本一致性分数**：同任务多次答，一致 = 确信，发散 = 不确信。三重扰动（温度 / 改写 / 模型）。

**诚实通讯**：结构化通讯包必带字段：
```
├── 上游本类任务历史成功率
├── 上游本次一致性分数
└── 建议（如有替代方案优先替代方案）
```

### 8.5 左移原则（全系统应用）

| 机制 | 用在哪 |
|------|-------|
| 事前预估 | 任务接入（不合理直接拒） |
| 每步 checkpoint | 事中每步打分 |
| 趋势监测 | 连续下降就预警 |
| 同比基线 | 异常早识别 |
| 预冲突扫描 | 事前避免资源冲突 |
| 风险预估 | 金额/不可逆/复杂度三维扫描 |
| 幂等键 | 入口去重 |

数学本质：发现问题成本 = 距问题发生的时间 × 影响面。

### 8.6 惊喜反馈 vs 错误上报

**惊喜事件 = 实际好于预期 + 非预期积极副产出**（由 `surprise_score` 判定，见 §8.7）：
- 完成比预估少花 30%+
- 发现用户没说但显然想要的副产出
- 发现新的更优路径
- 发现新的 skill 组合

**通道区分**：错误 → 告警；惊喜 → 推送（措辞是"分享"不是"报告"）。实现上两者走同一 `NotificationLayer`（§16），按类型分流。

**事后回看**：任务完成后系统自问"如果重做一次，有没有更短的路？"。离线不阻塞业务。

### 8.7 surprise_score 公式（ADR-015）

事后复盘按意外度分档深度；意外度有具体公式，不凭感觉：

```
surprise_score = 0.35 · cost_dev + 0.20 · step_dev + 0.25 · path_novelty + 0.20 · quality_dev

  cost_dev      = max(0, actual_cost / estimated_cost - 1)       # 只看超支
  step_dev      = max(0, actual_steps / estimated_steps - 1)
  path_novelty  = 1 - jaccard(actual_skill_set, typical_skill_set)
  quality_dev   = |actual_quality - expected_quality| / max_quality
```

**分档**：
| 区间 | 档位 | 处置 |
|------|------|------|
| `< 0.30` | low | 只入流水账 |
| `0.30 – 0.60` | medium | 中度分析 + 入短期层 |
| `≥ 0.60` | high | 深度分析 + 入方法论 + 推送 insight/surprise |

**权重演化**：初始手定；idle-batch 基于历史标注监督式微调。

---

## 九、协同机制

### 9.1 采用的模式（精简后保留 4 个）

| 模式 | 应用 |
|------|------|
| 统一 context 黑板 + 骨架-细节模式 | 协同基础：信息共享 + 串行变并行 |
| 事件溯源（审计 + 回滚基础） | 两条链路并行：当前状态（普通 DB）+ 事件日志（append-only） |
| 外层 OODA 循环 | 每步 checkpoint + 可纠偏 |
| 任务指挥（自适应）| 指令详细度 = f(执行者能力, 任务复杂度) |

### 9.2 人作为协作实体

```
KUN 可以：
- 给人派活（"请看一下这个方案"）
- 等人决策再继续（档 3 权限门）
- 让人补充缺失信息

人可以：
- 给 KUN 派活 / 中断 / 纠正 / 审核 / 批准 / 纠偏
- 通过 KUN 调动其他 agent / 人 / 企业
```

- 反向派活有 SLA
- 结构化问题（A/B/C 选一 / 填字段），减少认知负担
- 人的能力卡 + 人的预算（时间）

---

## 十、产品门面：傩（NUO）与交互设计

### 10.1 傩的定位与架构

**产品层面**：傩 = KUN 里的**电脑管家式运维门面**。直接对标传统电脑管家（查杀 / 清理 / 加速 / 漏洞修复 / 软件管理 / 网络防护……），但管的是 agent 系统而不是 Windows。对用户一个入口，对内是三元要素的各个子能力集成。

**战略路径**：现在内聚于 KUN 前端，API 层面独立命名空间 `/nuo/*`，未来可整体抽出为独立产品。

```
KUN 前端
├── 主工作区（对话 / 任务 / 结果）
├── 傩（NUO）管家视图 ← 单独入口 / tab
│   ├── 系统健康面板
│   ├── 成本和预算
│   ├── 运行中任务
│   ├── 安全告警
│   ├── 资产管理
│   ├── 接入层管理
│   ├── 批处理控制
│   ├── 历史报告
│   └── 设置
└── 其他功能
```

### 10.2 三层交互

**第 1 层：看板调参（95% 用户停在这）**——极简
```
核心 4 件事：
├── 看状态    一眼扫完所有任务在干啥
├── 开/关     暂停 / 继续 / 取消
├── 要钱      调预算滑块 or 选模式（快/省/平衡）
└── 看账      烧了多少、花哪了

主入口：对话框（核心交互入口）
```

**第 2 层：节点图编辑（<5%）**
```
├── 节点图（核心）
├── 拖拽重排
├── 改节点参数
└── 存为模板
```

**第 3 层：深度编辑（<0.5%）**
```
├── 原始配置手写（TASK.md / 角色模板 / 路由规则）
├── 版本历史查询
└── 所有高级操作
```

### 10.3 UI 三铁律

1. **无必要不新增**
2. **大部分用户的大部分时间停在第 1 层**
3. **安全机制按影响面分档，不一刀切**

### 10.4 影响面分档的安全机制（毫秒级判定）

| 档位 | 判断规则 | 处置 |
|------|---------|------|
| 小 | 单节点纯配置 / 不涉及外部动作 / 无依赖任务 | 直接生效 |
| 中 | 多节点 / 有依赖任务 | 锁定 + 版本 + 无预览 |
| 大 | 整体结构改 / 涉及外部动作 / 跨任务影响 | 锁定 + 版本 + 影子预览 + 确认 |

### 10.5 Agent 管家功能全景

| 类目 | 功能 |
|------|------|
| 安全防护 | 对抗输入检测 / 红队测试 / 威胁响应 |
| 清理 | 过期记忆 / 失效 skill / 碎片压实 |
| 加速 | 慢路径识别 / 性能优化建议 |
| 软件管理 | Skill 安装/卸载/升级/残留清理 |
| 漏洞修复 | 安全补丁 / 依赖升级 |
| 网络防护 | 恶意调用拦截 / 授权管理 |
| 弹窗拦截 | 干扰消息过滤 |
| 隐私保护 | 数据足迹清理 / 访问记录 |
| 硬件检测（对应）| 资源使用 / 延迟 / 供应商健康 |
| 工具箱 | 小工具合集 |
| **故障转移** | 多供应商自动切换（硬门槛）|
| **灾备** | 资产池多副本、跨地域备份 |
| **订阅管理** | 外部服务授权、OAuth 面板 |
| **社区分享** | Skill / 角色模板交换（后期）|
| **管理员策略** | 企业级集中配置（后期）|

---

## 十一、透明化与用户协作

### 11.1 三层透明化报告

```
【实时消费条目】每次任务完成时
  "这次任务花了 $0.12：Sonnet 调用 3 次 / 工具调用 12 次 / 嵌入检索 50 次，
   因为你要求写营销文案，需要好的创造力……"

【批处理报告】用户回来时推送
  "离线作业跑了 1h25min，花了 $0.35：测试了 12 个新路由规则，发现 3 个能省 15% 成本……
   预计本月省 $3.20"

【周报 / 月报】主动推送或用户点开
  "本月总消费 $28.50，比上月省 17%：
   - 积累了 X 条方法论，这月跑类似任务直接套用
   - 缓存命中率从 40% 提升到 65%……"
```

**原则**：每笔钱讲得出"为什么"，未来效益可量化。

**开发期 vs 上线期的计费口径（ADR-008）**：
- 开发期（你自用）：订阅模型（Opus / Codex / Sonnet / Haiku）按"等效 API 价格"折算展示，MiniMax 用真 token 价。字段 `cost_usd_equivalent`。
- 上线期（给用户）：全部走 API 调用，展示真实成本。字段 `cost_usd_actual`。
- 两字段在 RuntimeState 和 event payload 里并存；展示层按部署模式选择。

### 11.2 用户作为生态调度中心（终态）

从"你的助手"到"你的指挥部"：
- 多方任务协调 / 跨企业谈判 / 团队式项目管理 / 生态撮合

**技术底座**：接入层支持大量外部协作实体 + Context 系统的外部关系图谱 + 工程化的协作编排器。

---

## 十二、安全、权限、合规

### 12.1 权限（RBAC + 最小权限）

- 角色权限 = 模板基础权限 ∩ 任务授权 ∩ 用户授权
- 通过 MCP 等协议层强制，不靠 agent 自觉
- Context 调用范围由任务 + 角色 + 租户三者联合决定

### 12.2 多租户隔离（ADR-007，schema-ready + runtime 单租户默认）

- **Day 1 所有业务表都带 `tenant_id` 列**（非空 + 索引），Postgres Row Level Security 立即启用
- `TenantContext` 在应用层 ambient 存在，默认 `"u-sylvan"`；未来从 auth token 解析，业务代码零改动
- Context 拼装、Skill / tool 调用、审计日志都强制带租户 ID（RLS 自动过滤，不依赖业务代码自觉）
- 理由：以后不迁移；开发期单租户无摩擦；多租户开关在 auth 层而非业务层

### 12.3 沙箱策略（ADR-006）

**本地开发（macOS 宿主）**：后端全部跑在 Docker Desktop 的 Linux VM 里的容器里。沙箱在容器层实现（seccomp profile + capability drop + NetworkPolicy）。macOS 宿主只跑前端 dev server 和 Claude Code。

**生产期（Linux 集群）**：

| 档 | 技术 | 适用 |
|----|------|------|
| 强 | Firecracker | 高风险任务（未来启用）|
| 中 | gVisor | 常规任务（默认）|
| 弱 | 硬化容器（Docker + seccomp + NetworkPolicy）| 可信代码 / 本地开发 |

本地不模拟 gVisor（Docker Desktop 不原生支持，代价高收益低）。

### 12.4 审计日志 + 回滚

- 有副作用的操作先进待确认队列
- 不可逆操作（删文件 / 发邮件 / 转账）强制档 3 权限门
- 已执行发现错 → 补偿动作（删错发邮件、退款）
- 完全不可撤销 → 强前置校验（档 3 + 辩论）
- 事件日志（append-only）完整审计

### 12.5 红队测试

每月 + 每重要变更前：越狱 / 长文本轰炸 / 假冒 A2A / 数据投毒。

### 12.6 版本管理

- Skill / 提示词 / 评分表 / 路由规则都有版本号
- 支持 A/B / 回滚 / 审计
- 指针指向"稳定版"，不指最新版
- 回滚 = 改指针，热回滚几秒

### 12.7 异常检测（独立守望进程）

- 成本异常 / 质量下降 / 行为异常 / 安全异常四类

---

## 十三、核心数据模型

这一章定义 KUN 最核心的数据模型——TASK.md、能力卡、交接协议、RuntimeState、Starter Pack。所有子系统围绕这几者协作。

> **§16 `ScoreDescriptor` 基类**：Context 重要度打分（§3.2）、能力卡 stats（§13.2）、一致性分数（§8.4）、rubric 评分（§8.1）统一继承 `ScoreDescriptor`。见 §16.1。

### 13.1 TASK.md（任务说明书）

复用 Anthropic SKILL.md 规范，扩展任务专属字段。三级渐进披露。

**Layer 1（元数据，始终在 context，≤ 80 tokens）**

```yaml
---
# Layer 1：始终加载
task_id: "tk-01HXXXX"           # ULID
fingerprint: "sha256:..."        # 任务指纹（幂等键用）
task_type: "coding.python.fastapi"  # 任务类型分类
risk_level: "medium"             # low / medium / high / critical
complexity_score: 0.65           # 0-1
owner:
  tenant_id: "t-001"
  user_id: "u-007"
  project_id: "p-042"            # 可选
estimated_cost_usd: 0.12
estimated_duration_sec: 45
deadline_iso: "2026-04-23T18:00:00Z"  # 可选
success_criteria_short: "生成能通过测试的 fastapi endpoint"
version: 1
---
```

**Layer 2（执行蓝图，按需加载，≤ 2000 tokens）**

```markdown
## 目标详述
（可验证的、具体的描述）

## 成功指标
- [ ] 测试 X 通过
- [ ] Lint 无错误
- [ ] 响应 < 200ms

## 涉及资源
- Skills: skill-fastapi, skill-pytest
- Tools: bash, file_edit
- External: postgres (tenant-scoped)

## 约束
- 不得调用外部付费 API
- 只改 src/ 下文件
- 预算上限 $0.20

## 预见风险 + 应对
- 数据库迁移失败 → 回滚到上一版
- 测试超时 → 拆分为子任务

## 失败回退方案
（三级降级：A → B → 人审）

## 依赖的其他任务
- parent_task_id: null
- blocking_task_ids: []
```

**Layer 3（完整上下文，真执行时才加载，无大小限制）**

```markdown
## 关联历史
（检索到的相关方法论 / 过往类似任务）

## 用户此类偏好
（从能力卡和偏好库拉取）

## 相关方法论
（从 context 系统语义层拉取的 5-10 条）

## 完整输入数据
（原始用户输入、附件、引用的其他文档）
```

**字段规则**：
- `task_id` 用 ULID（时间序）而不是 UUID，便于排序和归档
- `fingerprint` 是 `task_description + owner + time_window` 的哈希，作去重键
- `task_type` 是层级分类（`coding/python/web-api/fastapi`），支持向上聚合统计
- `risk_level` 四档驱动评估矩阵
- 所有时间戳必须是 ISO 8601 UTC
- Layer 1 必须全填；Layer 2 按任务需要；Layer 3 最后加载

**版本演化**：
- `version` 是 TASK.md 结构版本号（不是任务本身的运行次数）
- 结构改动走兼容或迁移器（旧版 TASK.md 可自动 upgrade 到新版）
- 运行时状态（进度、已执行步骤）**不在 TASK.md 里**，单独放 RuntimeState（见 13.3）

### 13.2 能力卡（Capability Card）

**定位**：Context 系统的一个资产类目，存在所有者（角色模板 / 人 / 外部 agent / 企业 / 模型）的元数据里。不只是"调用工具的能力"，而是**任何可以衡量的能力表现**——包含工具使用、任务类型擅长度、交付质量、可靠度等综合画像。

**两类能力卡严格分开（不合并）**：

| 类别 | entity_type | 供谁查 | 用途 |
|------|------------|-------|------|
| **模型能力卡** | `model` | 路由层（7.2 模型路由引擎）| 决定"这个任务用哪档模型"，粒度是模型×任务类型 |
| **角色模板能力卡** | `role_template` | 任务分配层（7.1 任务大脑）| 决定"这个任务派给哪个角色模板"，粒度是角色模板×任务类型 |
| 其他实体能力卡 | `human` / `external_agent` / `company` | 协作调度器 | 决定"这个任务派给哪个外部协作实体" |

分开的理由：**模型**是"能力原料"（Opus 4.7 在 `coding.python.fastapi` 上的先天水准），**角色模板**是"能力配方"（`rt-coder` 这个角色在同类任务上的综合表现，含其 prompt 模板、skill 许可、参数组合）。同一模型在不同角色模板下表现不同；同一角色模板换底层模型表现也不同。两张卡独立演化，互相正交，合并会丢信息。

**数据模型（简化 YAML）**：

```yaml
capability_card:
  card_id: "cc-01HYYY"
  entity_ref:
    entity_type: "role_template"  # role_template / human / external_agent / company / model
    entity_id: "rt-coder-01"
  version: 12
  created_at: "2026-01-01T..."
  last_updated: "2026-04-23T..."

  # 多个能力按任务类型层级组织
  capabilities:
    - task_type: "coding.python.fastapi"
      short_description: "FastAPI 接口开发"
      
      # 历史表现（带置信区间）
      stats:
        total_invocations: 142
        success_count: 128
        partial_success_count: 10
        failure_count: 4
        success_rate: 0.90
        success_rate_ci95: [0.84, 0.94]    # 置信区间
        avg_cost_usd: 0.08
        avg_duration_sec: 38
        duration_p50: 32
        duration_p95: 62
        duration_p99: 120

      # 质量维度
      quality:
        avg_rubric_score: 4.2          # 0-5
        consistency_score: 0.88         # 多样本一致性
        surprise_rate: 0.12             # 意外度占比
        last_benchmark_score: 0.85      # 最近一次基准测试

      # 失败模式分布
      failure_modes:
        - name: "test_timeout"
          frequency: 2
          last_occurred: "2026-04-10T..."
          typical_root_cause: "外部数据库连接慢"
        - name: "type_hint_missing"
          frequency: 1
          last_occurred: "2026-03-28T..."
          typical_root_cause: "复杂泛型处理"

      # 衰减模型（按任务类型不同）
      decay:
        half_life_days: 30       # 数据半衰期
        last_decay_at: "2026-04-22T..."
        effective_sample_size: 98  # 衰减后的等效样本数

      # 适用边界（写给调度方看）
      boundaries:
        recommended_max_complexity: 0.75
        not_recommended_for:
          - "coding.python.async-heavy"   # 历史上这类任务成功率低
        require_escalation_for:
          - "涉及外部支付接口"

    - task_type: "coding.python.data-pipeline"
      # ...另一条能力...

  # 汇总元信息
  meta:
    primary_strength: "coding.python.fastapi"       # 最擅长的
    primary_weakness: "coding.python.async-heavy"   # 最不擅长的
    overall_reliability: 0.87
    maturity: "mature"   # cold_start / warming_up / mature
```

**更新触发**：
- **即时更新**（每次任务完成）：invocation count、success/failure、最近耗时/成本入滚动窗口
- **批处理重算**（每晚）：success_rate、置信区间、衰减权重、CI95、质量指标
- **手动覆盖**：用户反馈直接修正（例如"这次明明失败了，但它说成功"→ 覆盖）
- **衰减**：按半衰期（coding 类 30 天、创意类 60 天、方法论类 180 天）对老样本降权

**跨实体聚合**：
- 任务类型 taxonomy 层级化，子类型统计自动向上滚到父类型
- 跨实体对比："在 `coding.python.fastapi` 这类任务上，rt-coder-01 vs rt-coder-02 vs 外部 agent X 各是多少"，用于路由决策

**使用场景**：
1. **路由时查询**：任务进来 → 路由层查"这类任务哪个实体最强" → 分配
2. **事前预估**：该实体在这类任务上的历史成功率，决定要不要升档
3. **事后回写**：任务完成 → 更新统计
4. **诚实通讯**：上游结构化通讯包带自己此类任务的成功率，下游据此判断是否信任
5. **用户可见的报表**：傩前端展示"哪些角色在退化 / 在进步 / 从没被正确调用过"

**冷启动**：
- 新实体首次接入 → 从 `prior_stats`（按类型的先验分布）启动
- 标记为 `maturity: cold_start`，跑够 N 次再进 `warming_up`
- 跑够 M 次（默认 50）且 CI95 收敛 → `mature`
- 冷启动期不进入"高风险任务"候选池，只跑低风险样本攒数据

### 13.3 交接协议（Handoff Protocol）

跨角色（或 KUN ↔ 外部 agent / 人）交接时的四层包结构。默认只带 L1+L2；L3/L4 按需拉取。

**L1：任务核心（必带，≤ 500 tokens）**

```yaml
handoff_l1:
  packet_id: "hp-01HZZZ"
  from_entity:
    entity_type: "role"
    entity_id: "rt-coder-01:inst-042"    # 角色模板:实例
  to_entity:
    entity_type: "role"
    entity_id: "rt-reviewer-01"
  task_ref: "tk-01HXXXX"     # 指向 TASK.md
  timestamp: "2026-04-23T..."
  
  intent_one_sentence: "需要 review 我生成的 fastapi 代码"
  deliverable_required: "通过 / 退回 + 具体意见"
  deadline_iso: "2026-04-23T18:00:00Z"
  
  budget_remaining:
    usd: 0.04
    time_seconds: 120
    llm_calls: 8
  
  authorization_scope:
    - "read:src/**"
    - "write:review_comments"
  
  runtime_state_ref: "rs-01HAAA"   # 指向 RuntimeState（13.4）
```

**L2：上游假设与风险（关键场景必带，≤ 2000 tokens）**

```yaml
handoff_l2:
  upstream_assumptions:
    - "postgres 14 已部署"
    - "测试数据库可写"
  known_risks:
    - description: "依赖包版本可能有冲突"
      severity: "medium"
      mitigation_hint: "先跑 pip list 看实际版本"
  upstream_confidence: 0.85                  # 自报自信
  consistency_score: 0.91                    # 多样本一致性（客观）
  capability_card_snapshot:
    task_type: "coding.python.fastapi"
    historical_success_rate: 0.90
    sample_size_effective: 98
  recommendation: "如发现类型提示缺失可直接补，不用回问"
```

**L3：推理链（按需加载，无大小限制）**
```yaml
handoff_l3:
  reasoning_trace:
    - step: 1
      action: "解析用户需求"
      observation: "需要 RESTful + 类型提示"
      decision: "用 FastAPI + Pydantic v2"
    # ...
  considered_alternatives:
    - alternative: "Flask"
      rejected_reason: "用户明确要类型提示"
```

**L4：完整产物（按需加载，无大小限制）**
```yaml
handoff_l4:
  artifacts:
    - type: "code"
      ref: "s3://kun-artifacts/t-001/tk-01HXXXX/output.py"
    - type: "test_result"
      ref: "s3://kun-artifacts/t-001/tk-01HXXXX/test-report.json"
```

**存储与 TTL**：

| 层 | 存储 | TTL |
|----|------|----|
| L1 | 内存 + 租户事件存储（Postgres append-only）| 任务生命周期 + 30 天（审计）|
| L2 | 同 L1 | 同 L1 |
| L3 | 对象存储（MinIO / S3），引用形式 | 90 天 |
| L4 | 对象存储，引用形式 | 依赖用户策略（可永久保留，带同意）|

**序列化**：默认 JSON，可选 TOON（省 token）或 msgpack（二进制更紧凑）。接入层负责选择最合适的格式。

### 13.4 RuntimeState（运行时状态）

和 TASK.md 严格分离。TASK.md 是"身份证，不可变"，RuntimeState 是"进度，可变"。

```yaml
runtime_state:
  state_id: "rs-01HAAA"
  task_ref: "tk-01HXXXX"
  current_step: 3
  total_planned_steps: 7
  status: "running"             # queued / running / paused / done / failed / cancelled
  
  completed_steps:
    - step_id: 1
      skill_used: "skill-project-analyze"
      output_ref: "...​"
      cost_usd: 0.01
      duration_sec: 5
    # ...
  
  next_step_plan:
    skill: "skill-pytest"
    input_preview: "..."
  
  locks_held:
    - resource: "src/api/user.py"
      acquired_at: "..."
      ttl_sec: 10
  
  accumulated_cost_usd: 0.05
  accumulated_tokens: 12400
  
  checkpoints:
    - checkpoint_id: 1
      at_step: 2
      quality_score: 0.82
      notes: "tests all passing"
  
  failures_this_run: 0
```

**存储**：Redis（热数据）+ PostgreSQL（持久化快照，每 N 步或状态变化时），TTL 随任务生命周期。

### 13.5 冷启动包（Starter Pack）

KUN 交付给用户时**预装**的资产。让用户开箱即完整版，不是从零。

```
Starter Pack
├── 【基础 Skill 库】（从 Anthropic Agent Skills 官方市场 140+ 开源 skill 精选，覆盖日常高频）
│   ├── 来源：https://github.com/anthropics/skills 及官方目录
│   ├── 挑选原则：
│   │   ├── 1. 已被社区高频使用 / star 数排前
│   │   ├── 2. 覆盖 coding / writing / research / data / os 五大类
│   │   ├── 3. 不依赖特定外部服务（能独立跑的优先）
│   │   └── 4. 许可证可商用（Apache / MIT）
│   ├── 大致 50-80 个入门版，上线后随使用增删
│   ├── 分类（对齐 Anthropic 官方分类）：
│   │   ├── coding.* （pdf / docx / xlsx / pptx 等官方 skill + Python/TS/Rust/Go 各基础）
│   │   ├── writing.*（文案/摘要/翻译/校对）
│   │   ├── research.*（网页抓取/信息聚合/去重）
│   │   ├── data.*（CSV/JSON/数据清洗/spreadsheet）
│   │   └── os.*（文件管理/shell/基本系统操作）
│   └── 兼容性：全部符合 Anthropic SKILL.md 规范，OpenAI Codex CLI / ChatGPT 亦可用
│
├── 【基础知识库】
│   ├── 任务 taxonomy（层级分类 300+ 项）
│   ├── 通用方法论（20-30 条起步）
│   ├── 安全红线（10-20 条）
│   └── 用户偏好模板（让用户快速填）
│
├── 【默认角色模板】（10-20 个）
│   ├── rt-coder / rt-writer / rt-researcher / rt-reviewer / rt-data-analyst ...
│   ├── 每个带初始能力卡（基于基准测试的先验值）
│
├── 【默认路由规则】（200-500 条）
│   ├── 基于公共基准数据初始化
│   └── 标记 source: "benchmark_prior"，后续被用户数据覆盖
│
├── 【默认评分表】
│   ├── 通用 rubric（4 维度通用）
│   └── 按任务类型的特化 rubric
│
└── 【接入层默认配置】
    ├── A2A / MCP 客户端
    ├── 常用外部 API 适配器框架（REST / GraphQL 模板）
    └── OAuth 授权入口
```

**校准阶段**（用户首次使用）：
1. 运行 5-10 个多样化校准任务（用户同意后）
2. 基于校准结果微调能力卡先验值
3. 前 50 个真实任务内，系统标记为"校准中"，评估档位自动升档（更多监控）
4. 跑够 200+ 任务后，路由规则开始基于个人数据自适应

---

## 十四、技术栈与工程选型

### 14.1 编程语言

**主语言：Python 3.12+**
- 生态丰富（LLM SDK、向量库、数据工具都最成熟）
- 单人开发阶段开发速度第一
- 不够快的模块后续用 Rust 重写，通过 PyO3 或 subprocess 集成

**热点选择性用 Rust**（后期）：
- 结构化压缩/序列化（TOON 编码、msgpack 路径）
- 事件总线的核心路由（如果 NATS 满足不了）
- 向量检索的热路径（如果 Qdrant 满足不了）

**前端：TypeScript + React + React Flow**
- React Flow 专做节点图编辑（第 2 层交互），业内标准
- TypeScript 严格模式，类型系统帮保证 API 契约

### 14.2 存储选型

| 类别 | 选型 | 理由 |
|------|------|------|
| 元数据 / 关系数据 | PostgreSQL 16 | 行级权限（多租户硬门槛）+ JSONB + 时序扩展 + 事件 append-only 表 |
| 向量检索 | Qdrant | Rust 实现快 + 好 Python SDK + 支持租户过滤 + 可嵌入/集群 |
| 缓存 / 热态 | Redis 7 | RuntimeState、分布式锁、去重表、prompt cache 层 |
| 对象存储 | MinIO（本地/自部署）/ S3（未来云）| L3/L4 交接、artifact、审计归档 |
| 事件存储 | Postgres append-only 表 + 按租户分区 | 和元数据库一起，保证事务一致性，避免跨系统复杂度 |

### 14.3 消息/事件（ADR-005，Outbox pattern）

**事件存储单一真理源 = Postgres `events` 表**（append-only，按租户分区）；业务写入和事件写入在**同一事务**完成；后台 poller 读新事件 publish 到 NATS；消费者收到 NATS 通知后按 `event_id` 回 Postgres 拉完整事件。

```sql
CREATE TABLE events (
  event_id BIGSERIAL PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  subject TEXT NOT NULL,
  payload JSONB NOT NULL,
  occurred_at TIMESTAMPTZ DEFAULT now(),
  published_at TIMESTAMPTZ
);
CREATE INDEX idx_events_unpublished ON events(event_id) WHERE published_at IS NULL;
```

**NATS subject 命名**：`kun.{tenant}.{domain}.{event}`（如 `kun.u-sylvan.task.started`）

**交付语义**：at-least-once；消费者必须幂等（用 `event_id` 去重）

| 场景 | 选型 |
|------|------|
| 事件真理源 | Postgres `events` 表（事务性 + append-only）|
| 事件分发 | NATS JetStream（只做 fan-out 通知）|
| 任务队列（长任务） | 复用 NATS 或 Temporal（复杂 workflow 时）|
| 实时推送（前端看板）| WebSocket（详见 §14.5.1）|

**为什么 Outbox 而不是纯 NATS**：纯 NATS 做存储时事务性弱；纯 Postgres 做分发时消费者 poll 太重。Outbox 是业内共识，Uber / Shopify / AWS 都用。

### 14.4 LLM 接入

**抽象层：自建 `LLMProvider` 接口 + 选用 `litellm` 做多 provider 适配**

- Anthropic SDK（Opus 4.7 / Sonnet 4.6）
- OpenAI SDK（Codex 5.3 及未来其他）
- MiniMax 自定义 adapter（M2.7 API）
- 本地开源小模型（GPT2-small 压缩器、本地 embedding）

**路由层不绑死厂商**：只看"模型能力标签"（tier / strength / cost / latency），厂商切换无需改业务代码。

### 14.5 Web 框架

**后端：FastAPI（Python）**
- async 原生，适合大量 LLM I/O
- Pydantic v2 做数据模型（TASK.md、能力卡、交接协议都用 Pydantic）
- OpenAPI 自动生成

**前端：Next.js + React + React Flow + Tailwind**
- Next.js 适合 SSR + 仪表盘场景
- React Flow 节点图
- Tailwind 快速 UI

**API 层设计**：
- `/api/*` KUN 主业务
- `/nuo/*` 傩独立命名空间（未来可抽出，**数据层也独立 schema `nuo.*`**，见 ADR-012）
- `/internal/*` 内部服务间调用（带服务间鉴权）
- `/ws` WebSocket 主对话入口（详见 §14.5.1）

#### 14.5.1 对话协议（ADR-010，KUN 风格）

主入口是 WebSocket 双向流，消息分块格式借鉴 Anthropic Messages API 但做 **KUN 扩展**。

**KUN 特色**：
1. **双通道**：`main`（对话主流）+ `side`（费用 / idle-batch 结果 / 惊喜 / 告警推送）
2. **纠偏即说**：用户文本里出现"不是这样做""停""换个思路"等引导词自动标记为 `correction`，不需要点按钮
3. **费用透明**：每次 LLM 调用后 emit `cost_tick` 块
4. **惊喜分享**：系统发现更优路径 / 意外收获时推 `insight` / `surprise` 块到 side channel
5. **多模态一等公民**：text / file / image / code 都是 content block

**消息块类型**：

```
main 通道：
  user_message | thinking | action_plan | tool_call | tool_result |
  assistant_message | answer | ask_user | correction_ack | error

side 通道：
  cost_tick | evolution_note | insight | surprise | alert |
  idle_batch_report | guard_intervention
```

**流式**：server→client 走 `delta` 增量；client→server 支持 `interrupt` 消息打断当前动作。

**交互铁律**：无必要不打扰；side channel 默认折叠，用户可选"静默 / 摘要 / 全部"三档。

### 14.6 沙箱与权限

- **默认沙箱**：gVisor（开发期用 Docker + seccomp + 网络限制替代，生产期切到 gVisor）
- **高风险任务**：Firecracker（未来加，复杂度高先不上）
- **权限强制**：MCP 协议层 + FastAPI 依赖注入（所有请求强制带 tenant_id）

### 14.7 可观测性

**开发期就上全套，避免后期补不上**：

| 维度 | 选型 |
|------|------|
| Trace | OpenTelemetry + Tempo / Jaeger |
| Metric | OpenTelemetry → Prometheus |
| Log | OpenTelemetry logs + Loki |
| 看板 | Grafana（统一查 trace / metric / log）|
| 采样 | 正常 10% / 错误 100% / 高风险任务 100% |
| 租户维度 | 所有 metric 带 `tenant_id` label，Grafana 按租户切视图 |

**命名空间**：`kun.<subsystem>.<metric>`，例如 `kun.context.cache_hit_rate`、`kun.router.latency_ms`、`kun.watchtower.intervention_rate`。

**必做的几个指标（ADR-016）**：

| 指标 | 告警阈值 | 动作 |
|------|---------|------|
| `kun.cache.hit_rate{tier="permanent"}` | < 80% | warn（可能缓存 key 构造错）|
| `kun.cache.hit_rate{tier="stable"}` | < 40% | warn |
| `kun.cache.cost_savings_usd`（累积）| — | 展示在 NUO 周报 |
| `kun.llm.cost_runaway` | 任务成本 > 预估 1.2x | 守望触发 pause_task |
| `kun.llm.fallback_rate` | MiniMax fallback 触发率 > 10% | warn（主力/次力路径有问题）|
| `kun.quality.rubric_score_p50` | 下降 > 15% 环比 | warn（可能进化搞坏了）|
| `kun.tenant.cross_access_attempt` | 任何一次 | 严重告警（安全红线）|

**动态 TTL**：缓存命中率持续低时自动切换到 Anthropic extended 1-hour cache beta（守望规则 `rules/cache_ttl_escalation.yaml`）。

### 14.8 CI/CD

**CI：GitHub Actions**
- pre-commit hooks（格式化、lint、类型检查、SPDX 许可扫描）
- 单元测试（pytest）+ 集成测试（docker-compose 拉起 postgres/redis/qdrant/nats）
- 评估冒烟（每 PR 跑 20 个核心任务，护栏指标不能破线）

**CI/CD 护栏分级（ADR-013）**：

影响面自动识别（按 git diff 文件路径），三档分流：

| 档 | 触发条件 | 流程 |
|----|---------|------|
| **小改** | `tests/**` / `docs/**` / 单 skill / UI 组件 | CI 通过 → 自动 merge |
| **中改** | 业务服务单模块 / 单子系统内部 | CI 通过 + 你二次确认（一键 approve）|
| **大改** | `core/**` 抽象 / DB schema 变更 / 安全策略 | CI 全套 + 影子 3 天 + 你审阅 idle-batch 报告 + 明确批准 |

配置文件 `.kun/ci-tiers.yaml` 定义路径 → 档位映射。

**紧急通道**：生产告警需立即回滚时，走"一键 revert" + 金丝雀回滚路径，跳过护栏但事后必须补审。

**CD：GitHub Actions + Docker Compose（初期）**
- 初期单机 Docker Compose 部署，极简
- 后期多租户上 Kubernetes（Helm chart）

**Feature Flag / 实验平台（ADR-009，自建）**

分两层：
- **静态开关（on/off）**：YAML 配置文件（生产必用的特性旗标）
- **带状态的实验**（新 skill / 新路由规则 / 新 prompt）：**Postgres `experiments` 表 + Python SDK**

**状态机**：`draft → shadow → canary → rollout → stable`（可 `rolled_back`）

**流量分配**：`hash(tenant_id + experiment_id) % 100 < rollout_percent`（consistent hash，用户体验稳定）

```sql
CREATE TABLE experiments (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                  -- skill / route_rule / prompt / ...
  status TEXT NOT NULL,
  rollout_percent INT DEFAULT 0,
  control_variant JSONB,
  treatment_variant JSONB,
  guardrails JSONB,                    -- 指标护栏
  metrics JSONB,
  created_at TIMESTAMPTZ DEFAULT now(),
  promoted_at TIMESTAMPTZ
);
```

**Python SDK**：
```python
with experiment("new_router_rule_v2", tenant_id) as variant:
    if variant == "treatment":
        result = new_route(task)
    else:
        result = old_route(task)
    # 自动记录 metrics 和成本
```

所有"进化"（新 skill / 新路由规则 / 新提示词模板）统一走此流程：影子 → 金丝雀 → 放量 → 稳定。

### 14.9 测试策略

| 层 | 工具 | 频率 |
|----|------|------|
| 单元 | pytest | 每次提交 |
| 集成 | pytest + testcontainers | 每次 PR |
| E2E | Playwright（前端）+ pytest（后端）| 每次 PR |
| 评估冒烟 | 自研评估 harness（20 核心任务）| 每次 PR |
| 评估回归 | 自研 harness（200 全量任务）| 每晚 |
| 鲁棒性 | 多样本一致性（温度/改写/模型扰动）| 每周离线 |
| 红队 | 越狱 / 投毒 / 假冒脚本集 | 每月 + 重要变更前 |
| 基准 | SWE-bench Verified / GAIA / WebArena + 自建 | 每周离线 |

### 14.10 国际化 / 双语

- 所有**用户可见文本** i18n（中英双语）
- 默认语言：中文（给你自己用）
- **内部字段 / 日志 / API 字段名：全英文**（便于未来国际化和社区协作）
- **错误消息**：双语，错误码永远是 `KUN_<SUBSYSTEM>_<CODE>` 英文结构
- LLM prompt 模板：按对话语言决定（用户中文对话用中文 prompt，英文对话用英文）

### 14.11 部署形态

| 阶段 | 形态 |
|------|------|
| 现在（你自己用）| 单机 Docker Compose，所有组件本机跑 |
| Beta（几个内部用户）| 单租户独立部署，每人一套 |
| 公测 | 多租户云部署，K8s 起集群 |
| 规模化 | 多区域 + 自动扩缩容 + 跨区域灾备 |

---

## 十五、开发优先级与里程碑

### 15.1 按投资回报 + 依赖关系排序

| 优先级 | 模块 | 交付价值 |
|-------|------|---------|
| P0-1 | 基础设施骨架（Postgres / Qdrant / NATS / Redis / MinIO / OpenTelemetry）| 所有东西依赖 |
| P0-2 | LLMProvider 抽象 + MiniMax/Anthropic/OpenAI 适配器 | 能调模型 |
| P0-3 | Context 子系统骨架（资产池 + 中央打分器 + 三级渐进披露）| 记忆和资产有主心骨 |
| P0-4 | TASK.md + RuntimeState + 交接协议（L1+L2）数据模型 | 任务有规范 |
| P0-5 | 能力卡数据模型 + 初始化 | 路由和评估的基础 |
| P0-6 | 守望子系统事件总线骨架（订阅 + 规则引擎）| 动态决策基础 |
| P0-7 | 冷启动包（Starter Pack）初版 | 交付时即是完整版 |
| P0-8 | 提示词缓存集成（Anthropic 90% 折扣）| 立竿见影 |
| P0-9 | 结构化 agent 通讯（TOON + 交接协议）| 省 token + 利于 context |
| P1-1 | 模型路由引擎 + 模型能力地图 | 效果 + 成本可控 |
| P1-2 | 评分表 + 多 LLM 判官投票 | 客观评估 |
| P1-3 | 多维度升级触发 | 防死循环 / 越权 |
| P1-4 | 预冲突扫描 + 三维风险预估 | 事前左移 |
| P1-5 | 分级自治四级实现 | 守望落地 |
| P1-6 | 硬熔断策略（预算保护）| 安全网 |
| P2-1 | idle-batch（回放+蒸馏+AB+一致性）| 自我进化引擎 |
| P2-2 | 租户隔离 + 审计日志（事件溯源）| 订阅化硬门槛 |
| P2-3 | 红队测试机制 | 订阅化硬门槛 |
| P2-4 | 自我进化验收三层门 | 进化安全 |
| P2-5 | 辩论机制 + 学习曲线（3 次同结论 → 规则）| 效率 |
| P3-1 | 傩（NUO）前端三层交互 + 对话框主入口 | 产品门面 |
| P3-2 | 透明化三层报告 | 信任基础 |
| P3-3 | 黑板 UI（可交互控制台 + 节点图）| 用户控制感 |
| P3-4 | 协作编排器（多方 / 反向派活 / 人能力卡）| 终态定位基础 |
| P3-5 | 翻译适配器层（内部高效 + 对外标准）| 外部协作 |
| P3-6 | 故障转移（多供应商自动切换）| 订阅化硬门槛 |
| P4 | 路由引擎自我进化（聚类 + 关联规则挖掘）| 长期 ROI |
| P4 | 跨学科协同模式完整上线（OODA / 任务指挥自适应）| 协同质量 |
| P4 | i18n 前端双语切换 | 走向公测 |

### 15.2 里程碑（按交付标志推进，不排时间）

开发连续推进（ADR-001），每个里程碑以**交付标志**达成为完成条件，不预估人天。

- **M1 | 能跑通一次任务**：P0 全部落地
  - 骨架 + LLM 接入 + TASK.md/能力卡/交接协议数据模型 + Starter Pack + 提示词缓存 + 结构化通讯
  - **交付标志**：从用户对话 → 拆解 → 分配角色 → 执行 → 返回结果的完整链路打通，6 个校准任务能跑过 4 个以上
- **M2 | 能自己评估**：P1 全部落地
  - 路由引擎 + 评估体系 + 预冲突扫描 + 分级自治 + 硬熔断
  - **交付标志**：系统能自动评估任务成败并按规则升级处理，能力卡进入 warming_up
- **M3 | 能自己进化**：P2 全部落地
  - idle-batch + 租户隔离 + 红队 + 进化验收 + 辩论
  - **交付标志**：idle-batch 跑完一轮后能自动优化路由和 skill，并有可读的进化报告
- **M4 | 有个好看的壳**：P3 全部落地
  - 傩前端 + 透明化 + 黑板 UI + 协作编排 + 适配器 + 故障转移
  - **交付标志**：从前端能完整操作 KUN，非技术用户可用；傩 schema 独立
- **M5 | 持续进化**：P4 + 双语 + 准备公测
  - **交付标志**：路由引擎进入自我进化回路；双语 UI 切换无缝；红队测试稳定通过

### 15.3 开发中的持续铁律

- 每个 PR 过护栏指标（评估冒烟 + 成本不涨 + 延迟不涨）
- 每周扫一次批处理报告，看系统是否在变好
- 每月红队测试一次
- 每季度架构审视（技术债 + 下季度扩容预估）

---

## 十六、简洁化原则与合并清单（ADR-018）

> **原则**：工程越简洁越好维护。重复的抽象是技术债的温床。每个子系统引入前问一句"能不能复用已有的"。
>
> **沿用用户指示（审查反馈第 13 条）**：把表面不同、本质相同的东西合并到公共基类；同一流程用不同名字说三遍的，归到一条管道。

以下 8 项合并贯穿全系统，**M1 就按合并后的抽象写代码**，不留"先各写一份以后再合并"的技术债。

### 16.1 `ScoreDescriptor` — 统一所有打分系统

**合并前**：Context 重要度打分器（§3.2）、能力卡 stats（§13.2）、一致性分数（§8.4）、rubric 评分（§8.1）、surprise_score（§8.7）——五套打分逻辑。

**合并后**：

```python
class ScoreDescriptor(BaseModel):
    score_id: str                    # ULID
    kind: Literal["importance", "capability", "consistency", "rubric", "surprise"]
    value: float                     # 0-1 标准化
    confidence: Optional[float]      # 0-1，None 表示单次无置信度
    ci95: Optional[tuple[float, float]]  # 置信区间
    components: dict[str, float]     # 各维度分解（可解释）
    weights: dict[str, float]        # 权重
    sample_size: int                 # 有效样本数
    last_updated: datetime
    decay_half_life_days: Optional[int]
```

**收益**：打分展示、衰减、版本化、调试 UI 全部统一一套实现；新类型打分只是 `kind` 新枚举。

### 16.2 `ValidationPipeline` — 统一所有验证流程

**合并前**：评估触发矩阵（§8.1）、辩论（§8.2）、AB 测试（§8.3）、红队（§12.5）、自我进化验收（§8.3 渐进部署）——五种"判断对不对"的流程各跑各的。

**合并后**：所有验证器实现 `Validator` 接口，由 `ValidationPipeline` 按配置编排：

```python
class Validator(Protocol):
    kind: Literal["single_judge", "multi_judge", "debate", "ab_test", "redteam", "benchmark"]
    async def validate(self, artifact, context) -> ValidationResult

class ValidationPipeline:
    validators: list[Validator]
    trigger_rule: GuardRule          # 见 16.8
    aggregate_policy: Literal["all_pass", "majority", "first_fail"]
```

**收益**：新验证方式 = 新 `Validator` 实现 + 注册；触发条件用同一规则引擎；结果统一进 events 和能力卡。

### 16.3 `NotificationLayer` — 统一所有对外推送

**合并前**：三层透明化报告（§11.1）、惊喜反馈（§8.6）、错误告警（§12.7）、idle-batch 报告（§6.4）、守望干预通知（§6.1）——五种"把东西告诉用户或外部系统"的逻辑。

**合并后**：

```python
class Notification(BaseModel):
    kind: Literal["cost_tick", "insight", "surprise", "alert", "idle_batch_report",
                  "guard_intervention", "weekly_digest", "correction_ack"]
    severity: Literal["info", "insight", "warn", "error"]
    channel: Literal["main", "side", "email", "webhook"]
    payload: dict
    render_hint: dict                # UI 展示提示（折叠 / 置顶 / 通知中心）
```

统一路由到 WebSocket `side` 通道（§14.5.1）、邮件、webhook 等。

**收益**：通知样式统一；"静默 / 摘要 / 全部"三档切换一处生效；用户偏好一次设置到处生效。

### 16.4 `KnowledgePrecipitation` — 统一运行结果转知识的管道

**合并前**：能力卡更新（§13.2）、评分回写（§8.4）、方法论蒸馏（§5.3）、路由规律涌现发现（§7.2）、idle-batch 的各种学习子任务——都是"把一次运行的结果沉淀为长期资产"。

**合并后**：

```python
class PrecipitationStep(Protocol):
    source_event_type: str
    async def precipitate(self, event, context) -> list[AssetUpdate]

class KnowledgePrecipitation:
    steps: list[PrecipitationStep]
    schedule: "immediate" | "idle_batch"
```

**收益**：新学习方式 = 新 step 注册；所有知识写入走同一 asset update 接口（带审计和回滚）。

### 16.5 `ConcurrencySafety` — 统一所有并发安全机制

**合并前**：分布式锁（§5.2）、幂等键（§5.5）、版本号校验（§5.2）、冲突检测（§5.2）、预冲突扫描（§5.1）——五种并发安全机制散在各处。

**合并后**：

```python
class ResourceGuard:
    resource_id: str
    async def acquire(self, actor, intent) -> Lease
    async def release(self, lease)

class IdempotencyKey:
    key: str
    ttl: timedelta
    async def check_or_record(self) -> "first" | "duplicate"
```

统一在事前模块和事中模块的入口处调用，业务逻辑不直接碰锁或版本号。

**收益**：并发 bug 只有一个修的地方；所有资源访问都有 lease 审计链路。

### 16.6 `GuardPolicy` — 统一所有系统保护动作

**合并前**：硬熔断（§5.2）、自动回滚（§8.3）、升级给人（§6.2 第 4 级）、异常检测响应（§12.7）——"系统在危险时的保护动作"散在各处。

**合并后**：

```python
class GuardAction(Enum):
    PAUSE = "pause"
    ROLLBACK = "rollback"
    ISOLATE = "isolate"
    ESCALATE_HUMAN = "escalate_human"
    CIRCUIT_BREAK = "circuit_break"

class GuardPolicy:
    triggered_by: GuardRule          # 见 16.8
    actions: list[GuardAction]
    cooldown_sec: int                # 防抖
```

**收益**：所有保护动作可观测（metric 有统一前缀 `kun.guard.*`）；防抖机制统一；测试时一处 mock 全覆盖。

### 16.7 `LayeredAsset` — 统一三级渐进披露的存取接口

**合并前**：Skill / 记忆 / 知识库 / 通讯 / TASK.md 各自实现三级存取（§3.6）。

**合并后**：

```python
class LayeredAsset(BaseModel):
    asset_id: str
    asset_kind: Literal["skill", "memory", "knowledge", "task", "handoff", "role_template"]
    l1_metadata: dict                # 始终在 context
    l2_ref: str                      # 对象存储引用
    l3_ref: str                      # 对象存储引用

    async def load_l1(self) -> dict
    async def load_l2(self) -> str
    async def load_l3(self) -> bytes
```

检索、加载、缓存、权限过滤、TTL 管理全部一套实现。

**收益**：前缀缓存策略（§3.3）一处实现对所有资产生效；新资产类型零额外成本接入。

### 16.8 `GuardRule` — 统一规则引擎承载四类触发判断

**合并前**：守望干预规则（§6.1.1）、评估触发矩阵（§8.1）、CI 护栏分级（§14.8）、异常检测规则（§12.7）——都是"基于事件和阈值的触发判断"。

**合并后**：ADR-004 的 YAML + Python handler 引擎**统一承载所有四类**。规则文件目录：

```
rules/
├── guard/            # 守望干预（pause / rollback / escalate）
├── validation/       # 评估触发（哪些任务触发哪档评估）
├── ci/               # CI 护栏（哪些改动触发哪档流程）
└── anomaly/          # 异常检测（成本 / 质量 / 行为 / 安全）
```

同一条件语法、同一 Python handler 注册机制、同一版本化流程。

**收益**：规则可互相引用（如"异常检测触发守望干预"）；一处调优全局受益；新规则类型只是新目录。

### 16.9 合并不做的反面清单

有些表面相似但**故意不合并**的，避免过度抽象：

| 不合并 | 理由 |
|--------|------|
| 模型能力卡 vs 角色模板能力卡 | 演化速度 / 调优维度不同（见 §13.2 详述）|
| TASK.md vs RuntimeState | 不可变 vs 可变，混合会破坏 append-only 审计 |
| 对话主通道 vs side 通道 | 用户认知模型不同，合并会导致打扰 |
| 能力卡 vs 评估结果 | 能力卡是画像，评估结果是单次事件，抽象层级不同 |

---

## 附录 A：核心术语表

| 术语 | 定义 |
|------|------|
| 鲲 / KUN | 本产品名 |
| 傩 / NUO | 管家子模块（内聚于 KUN，未来可独立）|
| 三元要素 | Context 系统 / 接入层 / 工程化系统 |
| 守望子系统 | 系统隐藏大脑，全系统动态调度中枢 |
| 任务大脑 | 意图理解 + 拆解 + 路由三层 |
| 黑板 | 实时交互式控制台（数据视图层 + 权限过滤 + 双重渲染）|
| TASK.md | 任务说明书，三级渐进披露 |
| RuntimeState | 运行时状态（和 TASK.md 严格分离）|
| 能力卡 | 任意协作实体的历史表现画像 |
| 交接协议 | 跨角色交接的 L1-L4 分层包结构 |
| Starter Pack | 冷启动包（交付即完整版的基础资产）|
| 分级自治 | 角色 → 任务编排 → 守望 → 人 的四级决策上报 |
| FadeMem | 艾宾浩斯曲线遗忘（双层半衰期 + 永久档）|
| 渐进披露 | 三级存取范式 |
| 预算四档 | HIGH/MEDIUM/LOW/CRITICAL 动态收敛 |
| 左移 | 越早发现问题成本越低（评估/风险/冲突等）|
| 意外驱动注意力 | 90% 例行不分析，预算花在 10% 值得学的 |
| iMAD | 辩论触发分类器（arxiv 2511.11306）|
| 辩论学习曲线 | N 次同结论 → 固化为规则 |
| 多臂赌博机 | 效果好的自动加流量的 AB 动态调度 |

---

## 附录 B：十几轮讨论累积的落地点汇总

### B.1 立即动手（P0）
1. 基础设施骨架（Postgres / Qdrant / NATS / Redis / MinIO / OTel）
2. LLMProvider 抽象（MiniMax / Anthropic / OpenAI / 本地）
3. Context 子系统骨架 + 中央打分器
4. TASK.md + RuntimeState + 交接协议数据模型
5. 能力卡数据模型
6. 守望事件总线 + 规则引擎骨架
7. Starter Pack 初版
8. 提示词缓存（Anthropic 90% 折扣）
9. 结构化 agent 通讯（TOON + 交接协议）

### B.2 建立能力（P1）
10. 模型路由引擎 + 模型能力地图
11. 评分表 + 多 LLM 判官投票
12. 多维度升级触发
13. 预冲突扫描 + 三维风险预估
14. 分级自治四级实现
15. 硬熔断策略

### B.3 系统完整性（P2）
16. idle-batch（回放 + 蒸馏 + 一致性 + AB + 健康报告）
17. 租户隔离 + 审计日志（事件溯源双链路）
18. 红队测试机制
19. 自我进化验收三层门
20. 辩论机制 + 学习曲线
21. 渐进部署节奏（影子 → 金丝雀 → 放量）
22. 冲突处理三类（幂等键 / 分布式锁 / 动作仲裁）

### B.4 产品门面（P3）
23. 傩前端三层交互 + 对话框主入口
24. UI 三铁律
25. 影响面分档的安全机制
26. 透明化三层报告
27. 批处理可开关
28. 翻译适配器层
29. 协作编排器 + 人作为协作实体
30. 故障转移（多供应商自动切换）

### B.5 持续进化（P4）
31. 路由引擎自我进化（聚类 + 关联规则 + 异常检测）
32. 左移应用到全系统（不只评估）
33. 多层协同模式（黑板 + 骨架 + 事件溯源 + OODA + 任务指挥自适应）
34. 定期系统效率复盘（周报工程化 + 月度人工复盘 + 季度架构审视）
35. 启发式 + 自愈打分（初始打分用规则，让使用模式自然校正）
36. 意外驱动的注意力分配
37. 能力卡先验 + 冷启动校准
38. 字节双塔召回（规模到达后上，小规模用简单向量）

### B.6 学习和成长放在每一面（贯穿所有优先级）
- Context 系统：使用即强化 + 夜间蒸馏
- 路由层：数据驱动的规则涌现
- 辩论层：N 次同结论 → 规则
- 能力卡：每次任务后回写
- 评估阈值：基于基准和历史自动调整
- 压缩策略：ACON 失败驱动优化（带反事实检测防误判）
- 预算策略：按历史调整档位阈值
- 权限策略：基于异常检测和红队结果收敛

---

## 附录 C：能力卡校准任务集（Calibration Task Set，ADR-011）

新实体（角色模板 / 模型 / 外部 agent）接入时跑这 6 个任务初始化能力卡。6 个任务覆盖 KUN 的主要能力维度，每个都有预期输出、评分 rubric、成本/时长预估。

### C.1 `calibration.coding` — Python 基础函数

- **任务**：用 Python 写一个 `fibonacci(n: int) -> int` 函数，带类型提示，处理 n<0 抛 ValueError
- **预期**：通过内置 10 个单测（含 n=0/1/边界/负数/大数）
- **Rubric**（0-5 分）：正确性 2 分 / 类型提示 1 分 / 错误处理 1 分 / 代码风格 1 分
- **预估**：成本 $0.01，时长 10s

### C.2 `calibration.writing` — 营销文案

- **任务**：给定产品（"KUN 是一个 agent 管家"）写一条 150-200 字的朋友圈文案，目标是吸引技术人群
- **预期**：结构清晰、有钩子、字数合规、无 emoji 堆砌
- **Rubric**：钩子强度 / 信息密度 / 语气匹配 / 字数控制 / 创意
- **预估**：成本 $0.02，时长 15s

### C.3 `calibration.research` — 信息聚合

- **任务**：搜索 2026 年 AI Agent 领域三个重要趋势，每个给出一个权威来源 URL + 一句话总结
- **预期**：来源可验证（实际存在且指向声明内容）、3 个不重复、时效性 2026 年
- **Rubric**：信息准确性（最重）/ 来源权威度 / 覆盖面 / 总结清晰度
- **预估**：成本 $0.05（含 web 抓取），时长 30s

### C.4 `calibration.data` — 数据聚合

- **任务**：给定 200 行销售 CSV（预置），按地区聚合计算总额 + 中位数 + top 5 产品
- **预期**：数值精确到 2 位小数；用内置 skill-data（pandas 或 sqlite）
- **Rubric**：数值正确性（最重）/ 输出结构化程度 / 是否用对工具
- **预估**：成本 $0.02，时长 20s

### C.5 `calibration.reasoning` — 逻辑推理

- **任务**：解一个 4x4 数独（预置题目，难度中等）
- **预期**：解唯一且正确；展示推理步骤；不靠瞎猜
- **Rubric**：解的正确性（最重）/ 推理链可验证 / 是否有回溯标记
- **预估**：成本 $0.03，时长 30s

### C.6 `calibration.orchestration` — 多步编排

- **任务**：完成 3 步链式任务：搜索（话题 X 最近 3 个月动态）→ 总结（提炼 3 点）→ 产出（写一封给朋友的介绍邮件，含这 3 点）
- **预期**：每步有中间 artifact；最终邮件自然；执行链路可追溯
- **Rubric**：步骤完整度 / 中间产物质量 / 最终产出质量 / 编排效率（不绕圈子）
- **预估**：成本 $0.08，时长 60s

### C.7 校准流程

```
新实体接入
  ↓
跑 6 个校准任务（并行可）
  ↓
对每个任务：
  ├── 运行结果 vs 预期 → success/partial/fail
  ├── Rubric 打分 → ScoreDescriptor
  └── 实际成本/时长 vs 预估 → variance
  ↓
聚合 → 初始能力卡（maturity: cold_start）
  ↓
前 50 个真实任务：评估档位自动升档（更多监控）
跑够 50 个且 CI95 收敛 → maturity: warming_up
跑够 200 个 → maturity: mature，路由规则开始基于个人数据自适应
```

校准任务集本身是 Starter Pack 的一部分，代码仓位 `skills/calibration/*`。

---

## 附录 D：启动前准备清单

进入 P0 之前的 `setup`（不依赖任何 P0 模块）：

- [ ] 创建 GitHub repo，初始化 `/kun`, `/nuo`, `/skills`, `/rules`, `/frontend`, `/docs`, `/tests`
- [ ] `.kun/ci-tiers.yaml` 影响面映射表（ADR-013）
- [ ] `docker-compose.dev.yml` 拉起 postgres/redis/qdrant/nats/minio/otel
- [ ] `pyproject.toml` 定依赖（Python 3.12）
- [ ] pre-commit hooks（ruff / black / mypy / reuse lint）
- [ ] 写 6 个校准任务的预期输出 + rubric（作为 fixture）
- [ ] `decisions.md` 已就位（已完成 ✅）
- [ ] 从 https://github.com/anthropics/skills clone 一份，筛 5 个最小可用 skill 先接入（fastapi / pytest / markdown / csv / shell）
- [ ] 写 hello-world 端到端脚本：对话框发 → 意图 → 单步角色 → 返回结果 + cost_tick

---

*KUN-V1 · 鲲产品开发方案 · §16 合并清单 / ADR-001~018 / 附录 A-D · 2026-04-23*
