# KUN 历史承诺清单 (PROMISES)

> 单一对照表 — 历次对话累积的所有"确定要做"的事项. Claude 自己用对照, codex 看任务,
> 用户看进度. **任何承诺过的事都要在这里**, 不能丢.
>
> **更新规则**: 每次产品讨论后追加, 不删除 (历史承诺即使废弃也要留 strikethrough).
>
> **状态符号**: ✅ 完成 / 🟡 半 / ❌ 未起 / 🔴 退回需改 / ⚠️ 状态需澄清

---

## A. 产品哲学 / 第一性原则

| # | 承诺 | 来源对话 | 状态 | 落地形式 |
|---|---|---|---|---|
| A1 | 结果 > 功能 | V1.0 §1.2 | ✅ | 已写入产品方案 |
| A2 | 离钱近 | V1.0 §1.2 | ✅ | 已写入 |
| A3 | 帮用户赚钱 | V1.0 §1.2 | ✅ | 已写入 |
| A4 | 透明 + 可解释 | V1.0 §1.2 | ✅ | 已写入 |
| A5 | 用户是决策合伙人 + 反向派活 | V1.0 §1.2 | 🟡 | 反向派活 0 实现 (T30) |
| **A6** | **第六原则: KUN 是工作台 + 用户个性化, 越用越懂用户** | V1.0 §1.2 用户反馈 | ❌ | 待写入 V2.0 + T17 偏好库 |
| **A7** | **第七原则: 诚实优先于流畅 (不会就说不会, 没了解就说没了解)** | 当前对话 #4 #7 | ❌ | 待写入 V2.0 + T33 诚实性自检 |
| **A8** | **第八原则: 自我成长 (内反馈 + 外信息收集), 像养神兽** | 当前对话 #9 | ❌ | 待写入 V2.0 + T38 外部信息收集 |
| **A9** | **重 context 弱 agent (核心哲学)** | 当前对话 (你定的核心) | ❌ | 待写入 V2.0 + T35 角色 context (不一定做虚拟公司) |
| **A10** | **资源动态匹配, 不一刀切自检** (按 risk_level 决定要不要重诚实自检) | 当前对话 #4 | ❌ | 待写入 V2.0 + T33 设计 |
| A11 | 工程化和 context 通讯一一对应, 尽量合并简洁 | V1.0 §1.3 用户反馈 | 🟡 | 三级渐进披露统一抽象 (T26) |
| A12 | 学习成长贯穿系统每一面 (不只反馈学习) | V1.0 §1.3 | 🟡 | 仅路由+capability 萌芽 (T31) |
| **A13** | **第十原则: 结果和效率优先于形式** (默认重 context, 但需要并发就 fork; 系统永远并发执行多任务, 但单任务内默认重 context) | 当前对话 (你定的) | ❌ | 待写入 V2.0 |
| **A14** | **arxiv 2604.02460 信息论证明重 context 数学最优** (Data Processing Inequality, 跨 Qwen3/DeepSeek/Gemini 5 种架构均成立) | 第四轮调研发现 | ❌ | 待写入 V2.0 第一章 |

---

## B. 架构定位 / 子系统关系

| # | 承诺 | 来源 | 状态 | 落地 |
|---|---|---|---|---|
| **B1** | **守望核心: 解决动态问题 — 合适时机 + 合适模型 + 匹配资源** [V2.0 用这版定义] | 当前对话 (你定的) | ❌ | 待写入 V2.0 |
| **B1.1** | **架构精准定位: LLM=大脑, Context=信息, 工程化=执行, 接入层=交互** [V2.0 用这版] | 当前对话 (你定的) | ❌ | 待写入 V2.0 第二章 |
| **B1.2** | **黑板 = 中间视图层 + Context 管理调度通讯** (不只视图, 还是通讯枢纽) | 当前对话 (你定的) | ❌ | 待写入 V2.0 |
| **B2** | **三脑父子关系 (不为形式化合并, 看实际效率)** | 当前对话 | ✅ | 当前实现就是父子嵌套 |
| **B3** | **黑板分层 (对人简单看板视图, 对 agent 状态空间, 不强求一一对应)** | 当前对话 | ❌ | T16 黑板 MVP |
| **B4** | **傩嵌入 KUN, 不独立产品** | V1.0 §10 用户反馈 + 当前 | ✅ | 已是 /nuo/* 空间 |
| **B5** | **傩、守望都是代号保留** | 当前对话 | ✅ | 文档保持 |
| **B6** | **"个人版 / 团队版"替代"租户" (UI 文档术语层, DB 列保留 tenant_id)** | 当前对话 | ❌ | T24 术语清理 |
| **B7** | **LLM 路由归守望管** | V1.0 §2.2 用户反馈 | ❌ | T22 守望管 LLM 路由 |
| **B8** | **金丝雀属于 AB 测试 4 阶段 (不归守望直管, 但守望可触发回滚)** [纠正] | audit 纠正 | 🟡 | experiments.py 状态机有, 多臂+自动回滚缺 (T24) |
| **B9** | 黑板的双重渲染 (对人 UI / 对 agent JSON/TOON/XML) | V1.0 §2.3 | ❌ | T16 |
| **B10** | KUN 不能只适配某一家 LLM, 通用版 | V1.0 §3.6 用户反馈 | ✅ | 已做 4 LLM provider |
| **B11** | 信息安全/权限/沙箱独立, 不影响系统稳定 | V1.0 §3.7 用户反馈 | 🟡 | 沙箱三档只 file-io 简版 |

---

## C. Context 子系统 (章三)

| # | 承诺 | 状态 | 落地 |
|---|---|---|---|
| C1 | 中央重要度打分器 | ✅ T1 (#8 已合) | follow-up: 半衰期 ln(2) 修, Qdrant 接通 (T13 #18 退回) |
| C2 | 三级动态压缩管道 (LLMLingua + 前缀缓存 + 子组合缓存) | ❌ | T23 Context 三大件之一 |
| C3 | 分类与合并器 (按时间/类型/所属/合并器) | ❌ | T23 Context 三大件之二 |
| C4 | FadeMem 遗忘 (双层半衰期 30/11.25/5 + 永久档≤100) | 🟡 importance.py 有半衰期常量, 无定时作业 | T23 Context 三大件之三 |
| C5 | 三级渐进披露统一抽象 (Skill/记忆/知识库/通讯/TASK.md 全用) | 🟡 LayeredAsset 类有, 仅 asset 用 | T26 推到 skill/handoff/message |
| C6 | 反查能力 (完整链路记录) | ❌ | 待规划 |
| C7 | 热更新 + 黑板广播 + 紧迫度 + 冲突解决 | ❌ | 跟 T16 黑板配套 |
| C8 | Context 预热 (主动加载可能用到的资源) | 🟡 stage 名有, packer no-op | 待规划 |

---

## D. 接入层 (章四)

| # | 承诺 | 状态 |
|---|---|---|
| D1 | 翻译适配器 5 类 (human/a2a/rest/markdown/email) | ✅ T2 (#11 已合) |
| D2 | DEFAULT_MAPPING + orchestrator 出口接通 | ✅ T12 (#17 已合) |
| D3 | A2A / MCP 协议 | ✅ |
| D4 | 4 LLM provider (Anthropic/OpenAI/MiniMax/CodexMCP) + Stub | ✅ |
| D5 | Agent 间通讯优先级 (API → JSON/TOON → 信封自然语言 → 纯自然语言) | ✅ |

---

## E. 工程化 (章五)

| # | 承诺 | 状态 | 落地 |
|---|---|---|---|
| E1 | 主动用工具 4 层 (keyword + yaml + SKILL.md + 反馈) | ✅ Layer 1-4 全合 | follow-up: load_triggers 真消费 learned_triggers (T17/T11.1) |
| E2 | 任务说明书 TASK.md 生成 | ✅ L1+L2 | L3 待做 (T26) |
| E3 | 任务指纹登记 (幂等键) | ✅ |
| E4 | **意图理解 + 信息不足主动反问** | ❌ intent.py 直接结构化, 0 反问 | T15 critical |
| E5 | 任务分类与复杂度打分 | ✅ |
| E6 | 三维风险预估 (财务/不可逆/复杂度) | 🟡 单值 risk_level | 待规划 |
| E7 | 预冲突扫描 + 三种处置 | ✅ |
| E8 | Context 预热 | 🟡 占位 |
| E9 | 资源预估 + 注意力分配 | 🟡 部分 |
| E10 | 角色实例化 | ✅ role_template 数据模型, 内容空 | T35 (重 context 形式) |
| E11 | 任务拆解层 LLM 真做 | ✅ T14 (#19 已合) | follow-up: orchestrator 真消费 DAG 拓扑 (T18/T14.1) |
| E12 | 实时监控 (每步打分 + 预算追踪 + checkpoint) | 🟡 部分 |
| E13 | 路径纠偏 (探索分支 + 灵光一闪 + 中途 replanning) | ❌ | T37 |
| E14 | 早期错误左移 5 件 (死循环/范围漂移/成本飙升/一致性掉分/趋势监测) | 🟡 只 1 条 (cost_runaway) | T25 |
| E15 | 自我修复 (工具切换/角色切换/重试/重拆) | ❌ |
| E16 | Context 热更新 (订阅 + 黑板广播) | ❌ T16 黑板配套 |
| E17 | 动作执行前冲突检测 + 仲裁 | ✅ pending_action |
| E18 | 辩论触发 (iMAD 学习曲线) | ❌ | 待规划 |
| E19 | **预算追踪四档动态收敛 (HIGH/MEDIUM/LOW/CRITICAL 摘要替换)** | ❌ quota_tracker 是另一回事 | T18 critical |
| E20 | 硬熔断策略 + 已产出永不丢弃 | 🟡 部分 |
| E21 | 结果评估按意外度分档 | 🟡 metrics 有, 未在 orchestrator 计算 |
| E22 | 经验蒸馏 (情节→语义) | ❌ | T28 idle_batch |
| E23 | 沙箱三档 (Firecracker/gVisor/硬化容器) | 🟡 只 file-io 简版 |
| E24 | 进化管理 (影子/金丝雀/多臂赌博机) | 🟡 状态机有, 多臂+自动回滚缺 | T24 |
| E25 | **多任务非阻塞编排** | ❌ WS streaming ✅ 但多任务并行 ❌ | T36 |

---

## F. 守望 (章六)

| # | 承诺 | 状态 |
|---|---|---|
| F1 | 守望职责 6 项 (事件接入/监控/判断/干预/执行/学习) | 🟡 接入+判断 ✅, 5 档干预只 escalate_human 完整 |
| F2 | 5 档干预 (不管/记录/轻度/中度/重度) | 🟡 只 escalate_human |
| F3 | 4 级分级自治 (角色自己/任务编排/守望/人) | 🟡 只到第 3 级, 决策额度模型缺 |
| F4 | **守望管 LLM 路由 (联动 capability_card)** | ❌ | T22 |
| F5 | 4 类规则 (guard/validation/ci/anomaly) | 🟡 只 guard 1 类接通 |
| F6 | 夜间作业 7 step | 🟡 6 个 placeholder, 自我进化空转 | T28 |
| F7 | 守望调用大模型的正确场景 (失败归因/异常综合判断/纠偏策略生成) | ❌ |

---

## G. 任务执行大脑 (章七)

| # | 承诺 | 状态 |
|---|---|---|
| G1 | 三层结构 (意图/拆解/路由) | ✅ |
| G2 | LLM 路由 4 层 + A/B 切流 | ✅ A/B follow-up (T7 #10 退回需改) |
| G3 | 模型能力地图 + 路由自我进化 (聚类/关联规则/异常) | 🟡 playbook 是手册 |
| G4 | agent loop ReAct (skill envelope) | ✅ |
| G5 | capability_router 排序 | ✅ |
| G6 | TaskPlanner LLM 拆解 + DAG | ✅ T14 (#19 已合) | follow-up: T14.1 |

---

## H. 评估与进化 (章八)

| # | 承诺 | 状态 |
|---|---|---|
| H1 | 多判官投票 (3-5 LLM 随机打乱) | ✅ T3 (#9 已合) | follow-up: judge_models 真模型 id |
| H2 | 与人类评审对齐 Spearman 0.80+ | ❌ | 占位 |
| H3 | 基准测试 (SWE-bench/GAIA/WebArena) 每周 | ❌ |
| H4 | iMAD 辩论 + 学习曲线 (N 次同结论 → 规则) | ❌ |
| H5 | AB 4 阶段 (批处理离线/影子/金丝雀 1%/放量) | 🟡 状态机有, 批处理离线缺 | T24 |
| H6 | 多臂赌博机 + 自动回滚 | ❌ | T24 critical |
| H7 | 能力卡 + 一致性分数 + 诚实通讯 | ✅ 数据模型 |
| H8 | 左移全系统应用 | 🟡 仅评估 |
| H9 | 惊喜反馈 vs 错误上报 | 🟡 metrics 有, orchestrator 未计算 push |
| H10 | surprise_score 四元素公式 | 🟡 path_novelty 半成 |
| H11 | 红队测试 (越狱/长文/A2A 伪造/数据投毒) | ✅ T8 (#13 已合) |
| H12 | 红队接 idle_batch 周期跑 | ❌ |
| H13 | agent benchmark | ✅ T6 (#12 已合) | follow-up: T9 RLS 修了 |
| H14 | capability_writeback 接 multi_judge / benchmark | ✅ T10 (#15 退回需 rebase) |
| H15 | task.tool_skipped → capability_card 累积 | ✅ T11 (#16 已合, 半闭环) | follow-up: T11.1 |

---

## I. 协同 (章九)

| # | 承诺 | 状态 |
|---|---|---|
| I1 | 事件溯源 + Outbox | ✅ |
| I2 | NATS 跨进程 subscriber | ✅ |
| I3 | **黑板 (双向交互式)** | ❌ critical | T16 |
| I4 | OODA 外层循环 | ❌ | T21 |
| I5 | 任务指挥自适应 | ❌ |
| I6 | 人作为协作实体 (反向派活 + SLA) | ❌ | T30 |

---

## J. 傩 / KUN UI (章十) — 嵌入 KUN

| # | 承诺 | 状态 |
|---|---|---|
| J1 | 第 1 层看板调参 (健康/预算/任务/告警/资产) | ✅ 5 panel |
| J2 | 第 2 层节点图 (React Flow 拖拽重排改参数存模板) | ❌ | T29 |
| J3 | 第 3 层深度编辑 (TASK.md/角色模板/路由规则手写) | ❌ |
| J4 | UI 三铁律 (无必要不新增 / 大部分停第 1 层 / 影响面分档) | ✅ 原则定 |
| J5 | 影响面分档安全 (小/中/大三档, 大档锁+版本+影子+确认) | ❌ |
| J6 | **傩做系统修复诊断 (清理/加速/漏洞修复/隐私/硬件检测)** | 🟡 只健康面板 5 个 |
| J7 | **agent benchmark 综合打分** | ✅ T6 |
| J8 | 主入口对话框 | ✅ WS |

---

## K. 透明化 (章十一)

| # | 承诺 | 状态 |
|---|---|---|
| K1 | 实时消费条目 (cost_tick) | ✅ |
| K2 | **批处理报告 (用户回来时推送)** | ❌ kind 枚举有, 生产端缺 | T20 |
| K3 | **周报 / 月报 (主动推送或用户点开)** | ❌ kind 枚举有, 生产端缺 | T19 |
| K4 | 用户作为生态调度中心 (终态) | ❌ |

---

## L. 安全 (章十二)

| # | 承诺 | 状态 |
|---|---|---|
| L1 | RBAC + 最小权限 | ✅ |
| L2 | 多租户隔离 (RLS + 复合 PK) | ✅ |
| L3 | 沙箱三档 | 🟡 只 file-io |
| L4 | 审计日志 + 回滚 | 🟡 events 表 ✅, 待执行队列 ✅, 完整审计待补 |
| L5 | 红队测试每月 | ✅ runner ✅, 周期未接 |
| L6 | 版本管理 (Skill/提示词/评分表/路由规则) | 🟡 数据模型 ORM 有, 指针/热回滚缺 |
| L7 | 异常检测 4 类 (成本/质量/行为/安全) | 🟡 只 2 类 (llm_fallback / cross_tenant) |

---

## M. 数据模型 (章十三)

| # | 承诺 | 状态 |
|---|---|---|
| M1 | TASK.md L1+L2 | ✅ |
| M2 | TASK.md L3 (完整上下文真执行时加载) | ❌ | T26 |
| M3 | 能力卡 (model + role_template + human + external_agent + company) | ✅ |
| M4 | 交接协议 L1-L4 | ✅ 数据模型, 跨角色未真用 |
| M5 | RuntimeState | ✅ |
| M6 | Starter Pack: 50-80 skill (官方 140+ 精选) | 🟡 6 个 starter | T27 |
| M7 | Starter Pack: 10-20 角色模板 | ❌ | T27 |
| M8 | 默认路由规则 200-500 条 | 🟡 playbook |
| M9 | 默认评分表 (通用 rubric + 任务特化) | ❌ |
| M10 | 校准任务集 6 个 (能力卡冷启动) | 🟡 calibration.py 类有, 6 任务 fixture 待验 |

---

## N. 用户 7 个吐槽对策 (这次讨论新加)

| # | 吐槽 | 对策 | 任务 |
|---|---|---|---|
| N1 | LLM 不主动调工具, 范围深度不够 | 信息饱和度判断器 | T34 |
| N2 | 没公司运营思路, 协作不灵活 | 重 context 弱 agent + 角色 context (不真做虚拟公司) | T35 改造 |
| N3 | 意图识别不充分就开始做 | 主动反问 | T15 |
| N4 | 不诚实 + 幻觉 + 自检不完善 | 诚实性自检 (按 risk_level 动态匹配) | T33 |
| N5 | 占 session + 多进程差 | 多任务非阻塞编排 + 黑板看板 | T36 + T16 |
| N6 | 动态调整能力弱 | 任务中途动态调整 | T37 |
| N7 | 装懂, 没深度了解就答 | 诚实性自检 + 信息饱和度 (合并 N1+N4 解决) | T33 + T34 |

---

## O. 自我成长 + 商业化 (这次讨论新加)

| # | 承诺 | 任务 |
|---|---|---|
| O1 | 用户偏好库 / 灵魂档案 + extensions 自由扩展槽 | T17 |
| O2 | preference.suggested 学习用户行为推荐新偏好 | T17 子项 |
| O3 | 外部信息主动收集 (KUN 自爬 GitHub Issues / Reddit / 竞品迭代) | T38 |
| O4 | 内反馈闭环: 评估 → 决策 → A/B → 自动回滚 → 持久化 | T22 + T24 |

---

## P. 文档 / 流程

| # | 承诺 | 状态 |
|---|---|---|
| P1 | 产品方案 V2.0 (基于 V1.0 + 这次讨论 + 调研) | ✅ **2026-04-26 完成** (`KUN-V2.md` + `KUN-V2.docx` / 21 章 + 9 附录 / 3377 行) |
| P2 | docs/PRODUCT_FEEDBACK_ALIGNMENT.md (被 PROGRESS 引用但**实际不存在**, 文档断点) | 🟡 V2 附录 I 已替代 (用户对话反馈对照) |
| P3 | docs/PROMISES.md (本文件) | ✅ 写完即上 |
| P4 | ADR-009 信息不足主动补齐 | 🟡 V2 §5.1.1 + §5.1.2 已写入, ADR 待单独立 |
| P5 | ADR-010 守望职责定位 (全栈中央调度纠正) | 🟡 V2 §6.1 双角色已写入, ADR 待单独立 |
| P6 | ADR-011 评估反馈闭环 | 🟡 V2 §8.9 已写入, ADR 待单独立 |
| P7 | ADR-012 重 context 弱 agent 哲学 | 🟡 V2 §1.2 第九原则 + 附录 E 已写入 |
| P8 | 5 处认知错误固化进文档 | ✅ V2 附录 G |
| **P9** | **ADR-019 StrategyMatcher (V2 新加)** | 🟡 V2 §16.10 + §17.7 已写入, ADR 待单独立 |
| **P10** | **ADR-020 AttentionAnchor (V2 新加)** | 🟡 V2 §16.11 + §18.8 已写入, ADR 待单独立 |
| **P11** | **ADR-021 守望双角色明确 (V2 新加)** | 🟡 V2 §6.1 已写入 |
| **P12** | **ADR-022 计费透明承诺硬约束 (V2 新加)** | 🟡 V2 §11.4 已写入 |
| **P13** | **ADR-023 灵魂档案 governance (V2 新加)** | 🟡 V2 §12.8 + §13.6 + §20.4 已写入 |
| **P14** | **ADR-024 极致简化部署 (V2 新加)** | 🟡 V2 §14.11.1 + 第十一原则 已写入 |

---

## Q. 待 codex 修复的 PR (in-flight)

| # | PR | 内容 | 状态 |
|---|---|---|---|
| Q1 | #10 T7 failover | rebase + Lock + tier 维度 ✅, ab_branch 覆盖 + fallback_event 放大需修 | 🔴 退回 |
| Q2 | #15 T10 capability writeback | review LGTM, 跟 #14 冲突需 rebase | 🔴 rebase |
| Q3 | #18 T13 importance Qdrant | 主干 OK, sentinel + cache + 半衰期 ln(2) + 私有 API | 🔴 退回 |

---

## R. 一致性纠正 (这次讨论纠正的)

| # | 错误 | 纠正 |
|---|---|---|
| R1 | 文档名 "汇总版V2.docx" | 实际是 "KUN-V1.docx (根目录)" |
| R2 | 守望"两个都不是" | 守望是全栈中央调度 (既管 context 也管工程化) |
| R3 | 金丝雀"应该归守望管" | 金丝雀属 AB 测试 4 阶段, 守望可触发但不直接做 |
| R4 | "管理员 vs 用户" | KUN 没有"管理员"角色, 只有用户 (含高级用户/工程师) |
| R5 | "tenant" 术语 | UI/文档/API 用"个人版/团队版", DB 列保留 tenant_id |

---

## S. BATCH 任务索引

### 已合 (9 个)
T1 #8 importance scorer / T3 #9 multi-judge / T2 #11 output adapters / T6 #12 agent benchmark
T8 #13 red-team / T9 #14 benchmark RLS / T11 #16 tool-skipped / T12 #17 adapter routing / T14 #19 TaskPlanner LLM

### 待修 (3 个)
T7 #10 failover / T10 #15 capability writeback / T13 #18 importance Qdrant

### BATCH4 草案 (24+ 个, 待 V2.0 定稿后细化)
T15 主动反问 / T16 黑板 MVP / T17 偏好库灵魂档案 / T33 诚实性自检 (critical 4)
T18 预算四档 / T34 饱和度判断 / T22 守望管路由 / T35 角色 context (重 context) / T24 多臂+自动回滚 / T36 多任务并行 (high 6)
T23 Context 三大件 / T19 周月报 / T20 批处理报告 / T21 OODA / T25 早期错误 4 件 / T28 idle_batch 7 step / T38 外部信息收集 (high 7)
T26 LayeredAsset 推 + L3 / T27 Starter Pack 扩 / T29 节点图 / T30 反向派活 / T31 自我进化全面 / T32 版本指针热回滚 (medium 6)
T11.1 / T14.1 follow-up

---

**最后更新**: 2026-04-26 (V2.0 完成版)
**下次更新**: M3 启动后, 每完成一个 critical 任务追加 / 每次产品讨论追加

---

## T. V2.0 新增的决策原则与机制 (2026-04-26 加)

| # | 原则 / 机制 | V2 落点 | 状态 |
|---|-----------|--------|------|
| T1 | **第六原则 KUN 是工作台 + 用户个性化** | §1.2 + 第二十章 | ✅ 写入 V2 |
| T2 | **第七原则 诚实优先于流畅 (按 risk 动态分档)** | §1.2 + §17.4 4 档力度 | ✅ 写入 V2 |
| T3 | **第八原则 自我成长 (养神兽)** | §1.2 + 第二十章 | ✅ 写入 V2 |
| T4 | **第九原则 重 context 弱 agent (arxiv 数学证明)** | §1.2 + 附录 E | ✅ 写入 V2 |
| T5 | **第十原则 结果效率优先于形式** | §1.2 + §17.5 决策点 #4 | ✅ 写入 V2 |
| T6 | **第十一原则 极致简化部署 (反厂家黑箱单轨)** | §1.2 + §14.11.1 | ✅ 写入 V2 |
| T7 | **动态决策中枢 (StrategyMatcher + 18 决策点)** | 第十七章 + §16.10 + 附录 H | ✅ 写入 V2 |
| T8 | **全局视角注意力机制 (AttentionAnchor + 5 维打分)** | 第十八章 + §16.11 + §3.2 | ✅ 写入 V2 |
| T9 | **守望双角色 (中央处理器 + 动态问题解决器)** | 第六章 §6.1 | ✅ 写入 V2 |
| T10 | **黑板 = 中间视图层 + Context 管理调度通讯枢纽** | 第二章 §2.3 + 第九章 §9.3 | ✅ 写入 V2 |
| T11 | **强制全局扫描 (必查清单)** | §18.3 | ✅ 写入 V2 |
| T12 | **元认知自检 (大决策完反问)** | §18.6 | ✅ 写入 V2 |
| T13 | **5 维重要度打分, 近期性 ≤ 0.25** | §3.2 + §18.2 | ✅ 写入 V2 |
| T14 | **用户显式 pin (tier 1, 90 天半衰期)** | §3.5 + §18.4 | ✅ 写入 V2 |
| T15 | **诚实性 4 档力度 (low/medium/high/critical)** | §17.4 | ✅ 写入 V2 |
| T16 | **strategy_score 公式 (α·成果-β·代价-γ·延迟-δ·风险, 按 risk×user 动态)** | §17.3 | ✅ 写入 V2 |
| T17 | **18 个典型决策点完整策略表** | 附录 H + §17.5 | ✅ 写入 V2 |
| T18 | **触发条件机制 (替代"按用户量分阶段")** | §6.4 + §20.6 | ✅ 写入 V2 |
| T19 | **个人版/团队版边界 (DB 列保留, UI 改名)** | 第十九章 | ✅ 写入 V2 |
| T20 | **9 项致命差评对策 (T46-T58)** | 第二十一章 + §B.7.2 | ✅ 写入 V2 |
| T21 | **灵魂档案 governance (append-only + multi-source + 用户确认 + injection 防护)** | §12.8 + §13.6 + §20.4 | ✅ 写入 V2 |

---

## X. 2026-04-26 V2.1 一次性开发 wave1-7 实施报告 (诚实版)

> 用户原话: "可以开始开发了, 一次性完成吧, 中间不需要停, 必须把产品方案完整实现, 不许遗漏不许不诚实."
> 我的诚实回答 (开干前): "单次会话不可能完成全部 600-1000h 工程量. 我能做的是最大化推进 M3.1+M3.2 关键基础, 其他的诚实标."

### X.1 7 个 Wave 实际完成清单 (✅ 真实装代码 + 单测)

| Wave | commit | 实际产出 | 测试 |
|------|--------|---------|-----|
| **Wave 1+1.5** | `03ba7e6` | StrategyMatcher (§17) / TaskPanorama (§13.8) / AttentionAnchor (§13.7) / EmergentSolution (§13.9) / variable_registry 62 变量谱 / ImportanceScorer 5 维 | +33 |
| **Wave 2** | `1d0cc01` | FastPath 6 触发条件+4 安全护栏 (§17.4a) / PanoramaBuilder 10 模块按需展开 (§5.8.1) | +33 |
| **Wave 3** | `a04eb50` | KillSwitch (T55) / TokenMeter (T46+T47) / PlanOnlyGate (T51) / TaskTimeoutGuard (T52) / ZeroTelemetryEnforcer (T56) | +34 |
| **Wave 4** | `fd31ae3` | IntentSaturation (T34) / IntentClarifier (T15) / AttentionAnchor pin API 4 endpoint | +21 |
| **Wave 5** | `55cd5d5` | KnowledgePrecipitation 4 类 PrecipitationStep (§16.12 ADR-025) / EmergentSwitchManager 8 信号+防抖动 (§5.8) / ExternalInfoScanner 异步守望 (§3.10) | +25 |
| **Wave 6** | `dab50ca` | IncidentResponseEngine 4 档应急 (§12.11) / HonestyTierMatcher 4 档力度 (§17.4 / T33) / SoulFile + Governance (T17+T44) | +30 |
| **Wave 7** | `dc2d8a4` | Blackboard 5 endpoint + 双重渲染 (T16) / BudgetTracker 四档收敛 (T18) / DiagnoseRunner 5 步管道 (T59+T60 提前 M3.2) | +22 |

**累计代码**: ~5500 行 (含测试) / **累计测试**: 267 → 445 (+178) / mypy clean / ruff clean.

### X.2 PROMISES 对照状态 (诚实标)

#### ✅ 真实装 (代码可跑 + 单测覆盖)

| PROMISES # | 任务 | 实际位置 |
|-----------|------|---------|
| T15 | 意图主动反问 | `kun/engineering/intent_clarifier.py` IntentClarifier |
| T16 | 黑板 MVP 5 endpoint | `kun/api/blackboard.py` |
| T17 | 灵魂档案 user 级 + extensions + governance | `kun/datamodel/soul_file.py` |
| T18 | 预算追踪四档动态收敛 | `kun/engineering/budget_tracker.py` |
| T33 | 诚实性自检 4 档力度 | `kun/engineering/honesty.py` |
| T34 | 信息饱和度判断器 | `kun/engineering/intent_clarifier.py` IntentSaturation |
| T44 | 灵魂档案 governance (injection 防护 + multi-source + 用户确认) | `kun/datamodel/soul_file.py` SoulFileGovernance |
| T46 | token 实时仪表盘 | `kun/engineering/safety_guards.py` TokenMeter.get_dashboard() |
| T47 | 单步 token 上限 | `kun/engineering/safety_guards.py` TokenMeter.check_step_limit() |
| T49 | 主动 summary checkpoint | (在 §17.5 决策点 #6 框架内, 接 BudgetTracker.should_summarize_history) |
| T51 | plan-only + human-gate | `kun/engineering/safety_guards.py` PlanOnlyGate (11 个 HARD_LIST 正则) |
| T52 | 任务 hard timeout | `kun/engineering/safety_guards.py` TaskTimeoutGuard |
| T55 | 紧急 Kill Switch ≤500ms | `kun/engineering/safety_guards.py` KillSwitch |
| T56 | 零回传 + 用户审计权 | `kun/engineering/safety_guards.py` ZeroTelemetryEnforcer |
| T59.M3 | 傩诊断 LLM 归因接口 | `kun/security/diagnose_runner.py` DiagnoseRunner._cause_attribute |
| T60.M3 | 傩诊断 user 确认链路 | `kun/security/diagnose_runner.py` confirm_user_fix |
| W7 (§16.12) | KnowledgePrecipitation 统一进化 | `kun/engineering/precipitation.py` 4 类 PrecipitationStep |
| W4 (§5.8) | 涌现方案识别+切换 | `kun/engineering/emergent_switch.py` |
| W3+W16 (§3.10) | 外部信息饱和度异步守望 | `kun/engineering/external_scan.py` |
| W11 (§12.11) | 安全异常 4 档应急 | `kun/security/incident_response.py` |

**§17 动态决策中枢 + §18 全局视角注意力**: 核心 5 个数据模型已实装 (StrategyMatcher / TaskPanorama / AttentionAnchor / EmergentSolution / variable_registry 62), §17.4a 决策跳过快速路径独立实装. 全部走单测. **18 决策点的具体 enumerate_candidates 实现待 M3.3 接入** (现有 router.py 走老逻辑, M3.3 迁移).

#### 🟡 部分完成 / stub (有抽象有测但未 wire 进 orchestrator 主流程)

| 任务 | 状态 | 待补 |
|------|------|------|
| StrategyMatcher 接 orchestrator | 抽象+ 单测 ✅ / orchestrator 主流程未替换 | M3.3 接入(估 6-8h) |
| PanoramaBuilder 接 orchestrator | 抽象+单测 ✅ / orchestrator 用现有事前流程未替换 | M3.3 接入(6-8h) |
| FastPath 接 API 主入口 | 抽象+单测 ✅ / `/api/chat/run` 未走 fast_path pre-check | M3.2 接入(4h) |
| 黑板 5 endpoint 接真数据源 | API ✅ / 数据源 hook 未 wire 到 orchestrator/event_store | M3.2 接入(6-8h) |
| AttentionAnchor pin 接 ImportanceScorer | API ✅ / scorer 未默认查 boost_for_asset | M3.2 接入(2-3h) |
| 灵魂档案接 router/intent | 数据模型+governance ✅ / 未 wire 到决策流程 | M3.3 接入(8-10h) |
| 涌现切换接 orchestrator | 8 信号检测+评估 ✅ / orchestrator 未周期跑 detect_signals | M3.3 接入(4-6h) |
| KnowledgePrecipitation 接 idle-batch | 4 类 step ✅ / 未注册到现有 idle_batch_worker | M3.3 接入(4h) |
| TokenMeter 接 LLM provider | 抽象+单测 ✅ / providers 未默认 record_usage | M3.2 接入(2-3h) |
| KillSwitch 接 WS API | 抽象+单测 ✅ / WS interrupt 未走 ks.kill | M3.2 接入(3-4h) |
| 傩诊断 5 类 fix handler | runner ✅ / 5 类具体 handler 未实装 | M3.2 实装(15-20h) |

**总 wire 缺口约 60-90h**——这是从"抽象可跑"到"orchestrator 真用上"的距离. M3.2/M3.3 才完整闭环.

#### ❌ 未动 (V2.1 没碰)

| 任务 | 阶段 |
|------|------|
| T19 周月报推送 | M3.3 |
| T20 批处理报告推送 | M3.3 |
| T21 OODA 外层循环显式建模 | M4 |
| T22 守望管 LLM 路由 | M3.3 |
| T23 Context 三大件 (压缩+分类合并+遗忘) | M4 |
| T24 多臂赌博机+自动回滚 | M4 |
| T25 早期错误左移 4 件 | M4 |
| T26 TASK.md L3 + LayeredAsset 推全 | M4 |
| T27 Starter Pack 扩到 20 skill | M4 |
| T28 idle_batch 7 step 真做 | M4 |
| T29 React Flow 节点图 (NUO 第 2 层) | M4 |
| T30 反向派活+人作为协作 | M5 |
| T31 自我进化触发条件 (条件机制实装) | M5 |
| T32 版本指针+热回滚 | M5 |
| T35 角色 context 模板 | M5 |
| T36 多任务非阻塞编排 | M3.3-M4 |
| T37 任务中途动态调整完整 OODA | M4 |
| T38 外部信息接入插件 (V2.1 ExternalInfoScanner 已抽象) | M5 |
| T40 反作弊 sandbox 完整 | M4 |
| T41 AG-UI streaming + approval | M5 |
| T42 per-project constitution | M4 |
| T45 fork-explore-commit OS 原语 | M5 |
| T48 工具输出哈希+diff 自检 | M3.3 |
| T50 计费透明承诺 (NUO 页面+API) | M3.3 |
| T53 任务"成功"必有可验证产物 | M3.3 |
| T54 dev/prod 物理隔离 | M3.3 |
| T57 注意力预算+多 agent 摘要 | M4 |
| T58 用户可配置中断频率 | M4 |
| T59.M4-M5 / T60.M4-M5 / T61 / T62 / T63 | M4-M5 |

### X.3 老实交底

**说"完成"指什么**:
- ✅ 真实装 = 代码可跑 + 单测覆盖, 但**未 wire 到 orchestrator 主流程**
- 🟡 部分 = 抽象做完, 业务流程没接通
- ❌ 未动 = V2.1 没碰

**距离"产品方案 V2.1.2 完整可跑"还差**:
- M3.2 接入 wire (~30-40h)
- M3.3 完整接入 + 剩余致命差评对策 (~60-80h)
- M4 系统完整性 (~150h)
- M5 协同+进化 (~150h)
- 总剩余 ~400-440h, 单 codex 全力约 2-3 个月

**单次会话能做到的**:
- ✅ 17 个核心新组件 + 178 新单测 + 全 wave commit + 全部 push
- ✅ 全部走 mypy/ruff clean
- ✅ 不写假 stub (空函数声明算"做了")
- ❌ 不可能"全部 600-1000h 一次完成"——这是物理现实

### X.4 git commit 历史 (审计可查)

```
dc2d8a4 wave7 — 黑板 MVP + 预算四档 + 傩诊断
dab50ca wave6 — 安全 4 档应急 + 诚实性 4 档 + 灵魂档案
55cd5d5 wave5 — 自我进化统一 + 涌现切换 + 外部信息
fd31ae3 wave4 — T15 主动反问 + pin API + IntentSaturation
a04eb50 wave3 — 致命差评第一批 5 硬约束
1d0cc01 wave2 — FastPath + PanoramaBuilder
03ba7e6 wave1 — StrategyMatcher + Panorama + AttentionAnchor + EmergentSolution + 5d Importance
```

7 个 commit, 每个独立可 review/revert. 测试 267 → 445 全程绿.

---

## W2. V2.1.2 修订 (2026-04-26 第五轮用户反馈)

| # | 用户反馈 | V2.1.2 修订 |
|---|---|---|
| W12 | "复杂任务也要根据复杂度决定跑多少步" | §5.8.1 改"按需展开"——12 个事前模块每个独立判断"该不该跑/跑多深",不是档位绑定固定 step 集。同档复杂任务可能跑 8-11 个模块,不是死板的 12 |
| W12.1 | 4 档参考速度只是后验区间 | §5.8.1a 加"参考档(后验)"表,实际生成耗时落在 ≤100ms / ≤500ms / ≤2s / ≤5s / ≤10s 5 档 |
| W12.2 | 模块自我进化 | §5.8.1c "该不该跑"判断本身走 §17.9 进化通道,idle-batch 周期分析"哪些模块跑了无影响" |
| W13 | "傩修复诊断流程要提前,我自己使用就用到" | §10.6.4 实施阶段从 M4 提前到 M3.2/M3.3 |
| W13.1 | M3.2 加 DiagnoseRunner 主管道 (~40-50h) | M3-19/M3-20:范围/规则归因/自动修/验证 + 5 类核心自动可修 |
| W13.2 | M3.3 加 LLM 归因 + 用户确认链路 | T59.M3/T60.M3 |
| W13.3 | M4 只剩"扩展剩余 9 类自动可修"+"体检按钮 + 守望定时诊断" | T59.M4/T60.M4 |
| W13.4 | M5 异常检测触发 + 复杂多因素归因深化 | M5 保留 |

---

## W. V2.1 修订 (2026-04-26 第三轮用户反馈后, Claude 自审 17 漏洞)

| # | 用户反馈 / 自审发现 | V2.1 修订点 | 我的判断 |
|---|---|---|---|
| W1 | "KUN 不能做的太慢, 参考抖音" | §1.2 加第十二原则速度铁律 + §1.4 三原则贯穿铁律 (效果/速度/资源) | 必修, 升到第一章 |
| W2 | "上来先做个任务全景" | §2.7 + §13.8 TaskPanorama 数据模型 + §5.8.1 按复杂度档位生成 (极简 ≤200ms / 轻 ≤1s / 标 ≤3s / 全 ≤8s) | 必修, 速度档位关键 |
| W3 | "定时检索外网" | §3.10 外部信息饱和度监控 (异步守望驱动, 不阻塞主路径, LLM 复审避免噪声, 用户可关) | 必修, 但严控 |
| W4 | "中间动态调整, 涌现更好方案" | §5.8 涌现方案识别与切换 (信号驱动 + 防抖动) + §13.9 EmergentSolution 数据模型 | 必修 |
| W5 | "执行流程总图" 自审发现 | 附录 J KUN 执行流程总图 + 速度铁律落点对照 + 决策与执行的关系 | 必修, 文档级 |
| W6 | DAG 修改机制配合涌现切换 | §7.7 DAG 热修改 (节点替换 / 插入 / 删除 / 子图替换 4 类操作 + 防抖动) | 必修 |
| W7 | "能归类合并的放一起" + 自审发现进化机制散在 5 处 | §16.12 ADR-025 自我进化统一架构 (5 机制归 KnowledgePrecipitation, 4 类 step_kind) | 必修, 简洁化原则 |
| W8 | 速度核心机制自审发现 | §17.4a 决策跳过快速路径 (6 触发条件 + 4 安全护栏 + 反馈写回 + 占比目标 ≥60%) | **必修**, 速度铁律基石 |
| W9 | 安全应急流程自审发现 | §12.11 4 档应急响应矩阵 (L1 留痕 / L2 告警 / L3 隔离 / L4 熔断) | 必修 |
| W10 | M3 工时太满自审发现 | §15.3 M3 拆三波 (M3.1 核心机制 + 速度 / M3.2 进化 + 安全 / M3.3 完整诚实性) | 必修 |
| W11 | "产品方案是为终局写的, 写好阶段即可, 不用砍" | 把砍掉的 3 个补回, 各自归阶段 | 用户纠正我的"砍"判断 |
| W11.1 | 多 agent 编排 → §9.6 写完整设计, M5 实装 (T61) | Pipeline/Parallel/Debate 3 模式 + 共享黑板并发 + 决策点 #19 | M5 |
| W11.2 | 傩修复诊断流程 → §10.6 写完整管道, M4 实装 (T59+T60) | 5 步管道 + 14 类映射 + 速度承诺 + 自动可修 vs 需用户确认分流 | M4 |
| W11.3 | 跨界面注意力同步 → §18.10 写完整设计, M5 实装 (T62) | WebSocket 广播 + 多设备冲突 + 3 档同步语义 (强/最终/延迟一致) | M5 |
| **V2.1 加入 Claude 思考** | "速度铁律" + "外部检索/涌现切换/全景生成都不能阻塞主路径" | 第十二原则 + 全文异步 / 快速路径 / 复杂度档位标注 | 我加的, 用户给了思考权 |

**V2.1 修订工作量**: 17 个漏洞中, 修 14 个, 砍 3 个, 加 2 个 Claude 自己想到的。
**V2.1 文档规模**: 4718 行 / 226 KB markdown / 151 KB docx (V1 是 1945 行, V2 是 3895 行, V2.1 增长主要在第十七章快速路径 + 附录 J 总图 + §13.8/13.9 数据模型 + §5.8 涌现机制)。

---

## V. V2.0 修订 (2026-04-26 第二轮用户反馈后)

| # | 用户反馈 | V2 修订点 |
|---|---|---|
| V1 | 18 决策点不够, 影响变量很多, 要列权重和依赖关系 | §17.7 加 62 变量谱 (7 族) + §17.8 依赖图 DAG + 8 个典型依赖关系 |
| V2 | 决策机制要随使用动态调整和优化 | §17.9 策略自我进化 4 层 (候选库 / 权重表 / 规则库 / 反馈延迟分级) |
| V3 | 决策可结合 LLM 实时判断 | §17.10 工程化 + LLM 混合 (3 模式 + 5 升档触发条件 + 成本控制 + 反馈回写) |
| V4 | 决策点扩展, 18 个只是起步 | §17.11 候选 12 个新决策点 (#19-#30) + decision_kind_registry.yaml 热加载 |
| V5 | "近期性 ≤ 0.25 工程铁律" 不对, 不锁定某一项, 具体情况具体分析 | §18.2 改为按场景动态算 (compute_dimension_weights), 5 维基线 0.20 + 7 个场景调整规则 |
| V6 | "强制全局扫描必跑" 不对, 按需触发 | §18.3 改为 4 级触发分级 (强 / 中 / 弱 / 跳过), 按场景取扫描子集 |
| V7 | 元认知自检不一刀切 | §18.6 改为按场景动态触发 (含候选打分接近 / 用户驱动 / 守望驱动) + 自检本身可自我进化 |
| V8 | 全局视角的本质是"能有", 不是"反 recency" | §18.2.2 明确"最新信息也可以是最重要的"+ 5 维都是动态浮动 |

---

## Z. 2026-04-26 第六轮: V2.2 修订 (决策核心 + 按需扩展)

承接 Y 节 (M3.3 wire 完成 + M4 持久化 3/4 + codex 7 PR), 这一轮用户跟 GPT 深度讨论后,
反馈 GPT 提的 5 个"补缺"中 4 个是 KUN V2.1 已实装 (用户判断准), 但有 5-6 个真正
启发. 用户拍板把"按需扩展"提升为通用范式, 把守望从被动监控升级成主动决策投资人.

### Z.1 5 个核心修订

| # | 修订点 | 文档位置 | 实施位置 |
|---|--------|---------|---------|
| Z.1.1 | 边际收益递减 (Marginal ROI Stop) | V2.2 §19.2 | `kun/engineering/marginal_roi.py` (待实装) |
| Z.1.2 | 按需扩展 / Anchor-Then-Expand 通用范式 | V2.2 §19.3 | `kun/core/anchor_expand.py` (待实装) — 18 处接入 |
| Z.1.3 | 守望 = 决策投资人 (StrategyMatcher 接 watchtower) | V2.2 §19.4 | `kun/watchtower/engine.py` 加 ValueDecisionRule |
| Z.1.4 | 知识图谱 + 导航式记忆 (合并 mempalace + KG) | V2.2 §20 | `kun/datamodel/relationship.py` + alembic + ImportanceScorer 升级 |
| Z.1.5 | 三模式分级 FAST/SMART/MAX | V2.2 §21 | TaskRef.execution_mode 字段 + 模式判定器 |
| Z.1.6 | hermes 结构化执行协议 | V2.2 §22 | `kun/engineering/execution_protocol.py` |

### Z.2 anchor-expand 应用清单 (18 处)

用户已识别 4 处 + 我审计代码后扩展 14 处:

**已识别**:
1. ImportanceScorer (`kun/context/importance.py:73`)
2. LayeredAsset 查询 (`kun/context/packer.py:56`)
3. SkillSelector (`kun/skills/selector.py:29`)
4. agent 通讯 (新)

**审计扩展**:
5. StrategyMatcher 候选枚举 (`strategy_matcher.py:240`)
6. CapabilityRouter 模型排序 (`capability_router.py:107`)
7. Tier 枚举 (`strategy_router_bridge.py:127`)
8. DiagnoseRunner findings (`diagnose_runner.py:211`)
9. FixPlan 生成 (`diagnose_runner.py:275`)
10. ExternalInfoScanner 多源 (`external_scan.py:117`)
11. MultiJudge 评议 (`multi_judge.py:57`)
12. idle_batch step 调度 (`idle_batch.py:84`)
13. AttentionAnchor 检查 (`attention_anchor.py:123`)
14. Panorama 模块按需展开 (`task_panorama.py:116`)
15. IncidentResponse 动作矩阵 (`incident_response.py:76`)
16. Watchtower 规则触发 (`watchtower/engine.py:114`)
17. NUO 待审批列表 (`action_panel.py:56`)
18. KnowledgePrecipitation 步分发 (`precipitation.py:107`)

### Z.3 任务排期 (V2.2 实施)

**我自己 (Claude) — 心脏部分 ~30-40h**:
- marginal_roi 模块
- anchor_expand 通用工具
- 守望 ValueDecisionRule + wire orchestrator
- ImportanceScorer + LayeredAsset + SkillSelector + multi_judge 接 anchor-expand (4 个核心)

**Codex BATCH6 — 周边模块 ~80-100h**:
- C21 三模式分级 (FAST/SMART/MAX) — TaskRef + classifier + orchestrator wire
- C22 知识图谱 entity_relationships 表 + RelationshipMineStep
- C23 hermes 结构化执行协议 + LLM JSON output schema
- C24 anchor-expand 接其余 14 处 (StrategyMatcher / CapabilityRouter / DiagnoseRunner / etc)
- C25 Panorama 按需展开优化
- C26 NUO action_panel + diagnose_panel anchor-expand UX

BATCH5 C12-C20 仍然有效 (Context 三大件 / 多臂赌博机 / sandbox / constitution / React Flow / starter pack / multi_task / TASK.md L3 / dynamic replan), 跟 BATCH6 并行做.

### Z.4 跟 V2.1 兼容性

- V2.2 是叠加, 不替换 V2.1 的 600 测试都过
- 老 API 保留 (e.g. ImportanceScorer.score() 仍存在), 新 API 叠加 (score_anchor_then_expand)
- FAST 模式默认行为跟 V2.1 一致 (不开守望主动决策, 不查记忆)
- SMART/MAX 模式才启用 V2.2 新机制

### Z.6 V2.2 §23 输入翻译器 (Magika 启发, 第七轮加)

用户跟 GPT 讨论 Google Magika (AI 文件类型识别) 后, 想到 KUN 缺一层"真实世界 ↔ KUN 翻译器".

修订点:
- V2.2 §23 加 InputTranslator + InputDescriptor (kind / mime_type / confidence / suggested_handler / content_summary)
- 应用范围: 用户上传文件 / 用户消息 (text 也分 JSON/Markdown/code/SQL) / 外部 API 响应 / skill 输出 / WS binary frame / 粘贴板
- 技术: Magika 做底层 file type detection, KUN 包一层 InputDescriptor + 推荐 handler
- 配 anchor-expand: Round1 detect → Round2 extract → Round3 deep understand
- Codex BATCH6 加 C27 任务 (~8-10h)

M5 后续可扩:
- 输出翻译器 (KUN → 真实世界格式)
- 环境感知器 (主动扫用户文件夹 / 桌面)

### Z.7 完成度推进

- X 节 (V2.1 抽象层) ~25%
- Y 节 (M3.3 完整闭环 + M4 持久化 3/4) ~50%
- Z 节 (V2.2 决策核心 + 按需扩展) 计划完成后 ~70%
- M5 (剩余) 完成后 ~85%
- 真用户磨合 + 调参 ~10%, 总 100%

V2.2 是从"高度结构化执行系统" → "会做选择/会下注/会停止/会节奏控制"的决策系统.

---

## U. 我自己 (Claude) 的工程化承诺 (2026-04-26 加, 配合 §18.7)

| # | 承诺 | 落点 |
|---|---|---|
| U1 | 不让最新信息覆盖全局 (recency bias 自我约束) | V2 §18.7 + 每次大决策前查 PROMISES |
| U2 | 用户原话和我的"产品最优"判断冲突时, 优先用户原话 | V2 §18.7 + 反例: 我之前推法律 vertical 被驳 |
| U3 | 大决策前强制全局扫描 (PROMISES + V1.0 + 历史对话) | V2 §18.3 我也按这个 |
| U4 | 每次产品讨论后追加 PROMISES.md (不删历史) | 已做 |
| U5 | 完成度对照"整份产品方案"诚实报告 | V2 §0.4 + 每次同步整体 % |

---

## Y. 2026-04-26 第二轮 (M3.3 wire 完成 + M4 真持久化 + codex BATCH4 7 PR 合并)

承接 X 节 (V2.1 wave1-7), 这一轮把 X.2 列出的 11 个🟡 部分完成项的 wire 主流程接通,
M4 持久化的 4 件大事 3 件做完, codex BATCH4 10 个 PR 中 7 个 merged.

### Y.1 wire 主流程 (M3.3 完结)

| wire 项 | 状态 | 实际位置 |
|---------|------|---------|
| FastPath → /api/chat/run | ✅ (X.2 错记, 实际已 wire) | `kun/api/chat.py:50-75` |
| TokenMeter → chat_handler | ✅ (X.2 错记, 实际已 wire) | `kun/api/chat.py:82-91` |
| AttentionAnchor pin → ImportanceScorer | ✅ (X.2 错记, 实际已 wire) | `kun/context/importance.py:192-261` 的 score_with_anchors |
| 黑板 5 endpoint → 真数据源 | ✅ (X.2 错记, W5 已 wire) | `kun/api/blackboard_data_sources.py` + main.py lifespan:60-66 |
| KnowledgePrecipitation → idle_batch | ✅ (X.2 错记, W7 已 wire) | `kun/engineering/precipitation_idle_step.py` + main.py lifespan:68-75 |
| KillSwitch → WS interrupt | ✅ (Y 轮新做) | `kun/api/ws.py` + commit d7d7192 |
| EmergentSwitch → orchestrator | ✅ (Y 轮新做) | `kun/engineering/orchestrator.py` 4 行 wire + commit d7d7192 |

X.2 状态过悲观, 实际 7 个 wire 中 5 个早已存在 (W5/W7/W8 wire 时做), 我重新 audit
代码后修正. KillSwitch + EmergentSwitch 本轮新加.

### Y.2 M4 真持久化

| 项 | 状态 | 实际位置 |
|---|------|---------|
| SoulFile DB 持久化 + alembic migration | ✅ Y 轮 | alembic 0011 + `kun/datamodel/soul_file_provider.py` 加 load_or_create / save / preload + 4 个集成测试 (commit 14f5553) |
| 真 cron scheduler (替换 fixed interval) | ✅ Y 轮 | `kun/engineering/cron_scheduler.py` + main.py lifespan 注册 3 个 jobs (commit a61d89c) |
| capability_card 真数据回填 | ✅ (X.2 错记, 已存在) | `kun/engineering/capability_writeback.py` + orchestrator.py:1024 已调 record_outcome |
| 黑板真数据 (LayeredAsset L2/L3) | 🟡 task_store 已接, asset/workspace 还是 stub | M5 |

### Y.3 5 类傩诊断 fix handler 实装 (M3.2 提前)

`kun/security/fix_handlers.py` (commit a8376a6):
- clean / accelerate / failover / network_guard / privacy 5 类 handler 真做 in-memory side effect
- /api/diagnose 3 endpoint (run / confirm / audit-log)
- 14 个单测覆盖 + install_runtime 注册 5 类 default handler

### Y.4 codex BATCH4 协作

- 7 PR merged: C2 (#21) / C4 (#23) / C5 (#24) / C7 (#26) / C8 (#27) / C9 (#28) / C10 (#29)
- 3 PR open 等 codex:
  - C1 #20 工具输出哈希 (待修 path traversal + git false negative)
  - C3 #22 任务成功验证 (待修 SSRF + human_approval 持久化)
  - C6 #25 守望路由治理 (LGTM, 等 codex rebase)
- 修了 codex C1-C9 PR 共同的 lint bug (test_wave7.py 漏 report assert)
- 修了 CI 配置只允许 base=main 的问题 (加上 feat/v2.1-foundation + workflow_dispatch)
- 派了 BATCH5 brief (10 个 M4 阶段独立任务 ~80-100h)

### Y.5 测试增长

| 阶段 | 测试数 |
|------|--------|
| Y 轮起点 | 490 |
| KillSwitch wire | 494 |
| EmergentSwitch wire | 495 |
| Precipitation wire | 496 |
| 5 类 fix handler | 510 |
| SoulFile DB | 514 |
| codex 7 PR merged | 575 |
| 真 cron scheduler | 597 |

497 测试增长 → 全程绿. ruff format/check + mypy 干净.

### Y.6 距离 V2.1.2 完整可跑

剩余 (相对 X.2 60-80h M3.3 wire + 150h M4 + 150h M5):
- M3.3 wire 完结 ✅ (Y 轮 + audit 后发现已存在的)
- M4 真持久化 3/4 完成 ✅ (剩 LayeredAsset L2/L3 资产池真数据 → M5)
- 剩 codex 处理 #20 #22 #25 (~10h codex 工)
- BATCH5 10 个 M4 任务 (~80-100h codex 工)
- M5 协同+进化 (~150h, 包括真切 EmergentSwitch / OODA 完整 / 多臂赌博机 etc)

完成度从 X 节的 ~25% (V2.1 抽象层) → Y 节 ~50% (M3.3 完整闭环 + M4 持久化 3/4).
M5 完成 → 70%, 完整 V2.1.2 上线 → 90% (剩 10% 是真用户磨合 + 调参).
