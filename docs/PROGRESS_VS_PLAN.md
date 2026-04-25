# KUN 全局开发进度（对照《汇总版 V2》产品方案）

> 单一真相源：`docs/汇总版V2.docx`（产品方案）。这份文档是它的**进度尺**。
> 每次推进都更新这份；用户随时知道"对照整份方案"完成度。

**最近更新**：2026-04-25（主动用工具 4 层全接通 + 小尾巴 A/B/C/D 全清）
**整体加权完成度**：**~42%**（+3 来自这一波：4 层主动用工具 + planner/router/step OTel span + router A/B 切流 + NATS 跨进程订阅 + agent loop 端到端验证）

---

## 一句话现状

骨架（基础设施 / 路由 / 数据模型 / 接入）扎实，**约 70-80%**；
工程化（事前 / 事中 / 事后）和评估进化薄弱，**约 25-30%**；
产品门面（傩第 2/3 层 / 黑板 / 透明化）大块缺口，**约 20%**；
协同（OODA / 人协作 / 翻译适配器）几乎没起头，**< 10%**。

---

## 按章节进度

| 章 | 章节名 | 完成度 | 关键缺口（按优先级） |
|---|---|---|---|
| 一 | 产品定位与第一性原则 | 60% | 第六条原则"个性化工作台"（用户偏好库）❌；工程化可迭代 ❌；合并简化原则只在数据模型层做了 |
| 二 | 整体架构（三元 / 两脑 / 黑板） | 55% | **黑板（双向交互式）❌**；**第三个大脑：LLM 路由归守望管 半做**（playbook 在但守望规则没用）；事件总线**双向链路接通** ✓（NATS 订阅者 + outbox） |
| 三 | Context 子系统 | 25% | **中央重要度打分器 ❌**；**压缩器 LLMLingua ❌**；**遗忘器 FadeMem ❌**；分类合并 ❌；反查 ❌；热更新 ❌；三级披露**只在资产做了**，skill / 通讯没统一 |
| 四 | 接入层 | 35% | HTTP/WS ✓ + 4 个 LLM provider ✓ + MCP ✓；**翻译适配器层 ❌**；**A2A ❌**；硬件协议 N/A |
| 五 | 工程化子系统（按时序） | 50% | 事前 75%（**主动用工具 4 层全通** ✓ keyword/yaml/SKILL.md/反馈；三维风险预估 ❌、Context 预热 ❌）；事中 30%（预算追踪四档 ❌、早期错误左移 ❌、自我修复 ❌）；事后 20% |
| 六 | 守望子系统（隐藏大脑） | 25% | RuleEngine 骨架 ✓；4 条规则**只 1 条真接通**；分级自治四级**只到第 3 级**；**守望管 LLM 路由 ❌**；夜间作业 placeholder |
| 七 | 任务执行大脑 | 35% | 意图 ✓；**拆解 ❌（TaskPlanner 是占位）**；路由 ✓ 4 层 + **A/B 切流挑战者** ✓；agent loop ReAct **端到端验证** ✓；模型能力地图 半做（playbook 是手册，capability_card 收集中） |
| 八 | 评估与进化 | 15% | tier 矩阵简版 ✓；**多判官投票 ❌**；**iMAD 辩论 ❌**；**AB 4 阶段 ❌**；**惊喜反馈 ❌**；基准测试 ❌ |
| 九 | 协同机制 | 13% | 事件溯源 ✓；**事件总线双向链路** ✓（NATS subscriber + queue group）；**黑板（双向）❌**；**OODA 外层循环 ❌**；任务指挥自适应 ❌；**人作为协作实体 ❌** |
| 十 | 产品门面（傩 NUO） | 25% | NUO 第 1 层（健康 / 预算 / 待审批 / 模型画像）✓；**第 2 层节点图 ❌**；**第 3 层深度编辑 ❌**；影响面分档 ❌；**外部 agent benchmark ❌** |
| 十一 | 透明化与用户协作 | 23% | 实时消费（cost_tick）✓；**OTel 业务 span 全 4 个**（intent/planner/router/step）✓；**批处理报告 ❌**；**周月报 ❌**；用户作为生态调度中心 ❌ |
| 十二 | 安全 / 权限 / 合规 | 45% | RLS + 多租户 ✓；scopes wire ✓ 但还没在 endpoint 强制；**沙箱三档**只 file-io 简版；**红队测试 ❌**；版本管理只数据模型 |
| 十三 | 核心数据模型 | 70% | TASK.md L1+L2 ✓ **L3 ❌**；能力卡 ✓；交接协议数据模型 ✓ **但实际跨角色没用**；RuntimeState ✓；Starter Pack 半（6 skill 太少；20 角色模板 ❌） |
| 十四 | 技术栈与选型 | 80% | Python/Postgres/Qdrant/Redis/MinIO/NATS/OTel/Grafana ✓；Firecracker（N/A 后期）；GrowthBook（N/A 后期） |
| 十五 | 开发优先级（M1-M5） | M1 70%, M2 50%, M3 15%, M4 20%, M5 0% | M1 缺：拆解层真做；M2 缺：评估真跑；M3 几乎没起头 |

**加权（按章重要度）≈ 37%**

---

## 你 11 处批注的对齐情况

见 [`docs/PRODUCT_FEEDBACK_ALIGNMENT.md`](./PRODUCT_FEEDBACK_ALIGNMENT.md)（拆出去做单独追踪）。
摘要：
- 1 条对齐 ✓
- 5 条半做 🟡（需补完整）
- 5 条没起头 ❌

---

## 下个阶段（按价值密度排）

**阶段 A — 把"主动用工具"做实** ✅ DONE
- TASK.md 加 `required_tools` 字段 ✓
- 意图识别阶段预填 ✓
- orchestrator 启动时先 dispatch、结果塞 user message ✓
- 守望 3-5 条强匹配规则 ✓ (rules/proactive/triggers.yaml, layer 2)
- SKILL.md 加 `auto_trigger_when` 字段 ✓ (layer 3)
- 失败回看驱动主动性 ✓ (task.tool_skipped 事件, layer 4 — 写进 capability_card 等下一阶段消费)

**阶段 B — 4 个 follow-up 清尾** ✅ DONE
- A: 真跑一次 agent loop 让 LLM 输出 `<skill>` 验证 ✓ (e2e 单测靠 stub builder)
- B: router 加第二个候选模型做 A/B ✓ (KUN_AB_RATIO + ab_alternates)
- C: NATS 跨进程订阅 ✓ (kun.core.nats_subscriber + queue group)
- D: OTel 自定义业务 span ✓ (intent/planner/router/step 全部接通)

**阶段 C — 我做：核心主线**（10-15 小时）
- 任务拆解层 TaskPlanner 真做
- 守望管 LLM 路由（联动 capability_card）
- 个性化工作台（用户偏好库）
- 三级披露统一抽象（skill / handoff / message 都继承 LayeredAsset）
- 预算追踪四档 + 早期错误左移

**阶段 D — 给 codex 并行**（独立模块，不踩主线）
- 中央重要度打分器（Context 三大件之一）
- 翻译适配器层（接入层扩展）
- 多判官投票（评估扩展）
- 故障转移（provider 切换策略）
- 红队测试机制
- 外部 agent benchmark

**阶段 E — 大块**（20+ 小时）
- 黑板（双向交互式）
- 节点图编辑（第 2 层交互）
- 协作编排器（人作为协作实体）
- AB 4 阶段框架真跑
- 批处理报告 + 周月报

---

## 进度更新机制

每次推进，**这份文档跟着更新**，再 commit。提交信息里同步标整体百分比变化（例：`feat(...): 进度 37% → 41%`）。
