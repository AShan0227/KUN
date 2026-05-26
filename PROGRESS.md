# KUN 开发进度

> 按交付标志推进。本文档追踪能力等级（L0-L6）+ 重要决策。
>
> **历史里程碑 M1-M5 命名自 2026-05-26 起退役**（ADR-020 决定），改用 L0-L6 能力等级。
> 历史 M1-M5 章节归档到 §历史里程碑（保留为引用）。

---

## L0 · 通路打通 (✅ 已达成)

**交付标志**：用户对话 → 拆解 → 路由 → 执行 → 返回结果的完整链路打通。

### 已完成核心能力

- 三元要素骨架（Context / 接入层 / 工程化）
- LLMProvider 抽象 + 6 个 provider (Anthropic / OpenAI / MiniMax / Stub / ClaudeCode CLI / CodexMcp)
- LLM Router tier-based fallback + KUN_CODEX_ONLY 单 provider 模式
- Watchtower 规则引擎骨架 (YAML + Python handler)
- Context 子系统骨架 (LayeredAsset + ImportanceScorer)
- TASK.md / RuntimeState / CapabilityCard 数据模型
- 多租户 schema + Postgres RLS (kun_app 角色)
- Outbox + NATS JetStream + idle-batch 调度
- FastAPI + WebSocket 对话协议
- 傩 (NUO) 独立 schema + API namespace
- 760 unit tests passing + ruff clean
- Docker compose 10 服务 (postgres / redis / qdrant / nats / minio / otel-collector / prometheus / grafana / jaeger / loki)

### 已修复的审计发现（2026-05-26 全盘审计 + v3 方案前期）

- C1 WS auth fail-closed 闸门
- C2 production 启动检查拒绝 dev 默认凭证
- C3 outbox at-least-once 语义文档化
- C4 idle-batch stub steps 显式标 stub=True
- C5 cross_tenant_attempt 死规则删除
- C6 validation 失败推送 NUO alert
- 路由 + Codex MCP 路径完整 (KUN_CODEX_ONLY)
- 大量 reliability 改进 (见 commits cfbedfa ~ 3d5e702)

---

## L1 · 基础能力 + 路由闭环 + Anti-drift 基础（✅ 已达成 2026-05-27）

**交付标志**：
1. 7 agent 目录就位 + 6 张数据脊柱表建立
2. 第一条真闭环跑通（capability_router → LLMRouter → 能力卡 → 下次路由）
3. 长任务有 GoalAnchor pinning + Anti-sycophancy system prompt
4. KUN 能在 3 类开发任务上各成功 N 次（N 自适应，95% CI 下界 ≥ 0.7）→ Phase 1a 基础能力闸门

### Phase 0 · 文档先行（前置）

- [x] ADR-019 Auth posture（短/中/长三阶段）
- [x] ADR-020 五层架构 + 7 agent 角色 + 主线/监督线双线
- [x] ADR-021 RCDH 强制诊断层级
- [x] ADR-022 Anti-drift 长任务防漂移
- [x] ADR-023 External Supervisor 独立进程 + 本地模型
- [x] ADR-024 RSI 闭环 + 6 张数据脊柱表
- [ ] KUN-V1.md v2（修订章节 + 退役标记）
- [ ] PROGRESS.md L0-L6（本文件，本次更新）

### Phase 1 · 工程化（10 改动 — 全部完成 ✅）

- [x] **L1.1** 目录重组：`kun/agents/<role>/` × 7 + `kun/governance/` × 5（commit 80c5c34）
- [x] **L1.2** 拆 `orchestrator.py` — 5 个 commit A-E 串行迁移（intent / planner / role_router / validation+multi_judge / capability_writeback）
- [x] **L1.3** alembic 0011：**7 张**数据脊柱表 + ORM Row 类 + RLS + 部分索引（commit d4806c2）
- [x] **L1.4** ConcurrencySafety 解歧义：`control_plane/concurrency.py` → `work_item_governance.py`（commit cf3fc1f）
- [x] **L1.5** 删 KnowledgePrecipitation 抽象注释引用（commit cf3fc1f）
- [x] **L1.6** **接 `capability_router` 进 `LLMRouter.invoke()`** — **第一条真闭环激活**（commit 42f57a5）
- [x] **L1.7** Director 输出 complexity + priority_profile + estimated_steps + GoalAnchor（commit 39d9333）
- [x] **L1.8 + L1.9** Long-task mode + GoalAnchor 顶部 pinning + Anti-sycophancy system prompt（commit 25d7d06）
- [x] **L1.10** L1 验收：768/768 unit tests pass + ruff clean + `L1-retrospective.md`（9 段）+ 4 份新 methodology seeds 蒸馏

---

## L2 · 监督线启动 + RCDH + 第一条 RSI 闭环（✅ 已达成 2026-05-27）

**交付标志**（service-layer 基础设施）：
1. External Supervisor 独立进程化 + 本地模型 (ollama) 适配器就位
2. RCDH 4 级诊断走完一次，工程化实装 23 个 test 全绿
3. 第 1 条 RSI 实例 (LLM 路由优化) 的核心服务层就位（Supervisor 检测 → Strategist 候选 → Gate 准入 + capability_writer）
4. 数据脊柱 7 张表的 service-layer 写入路径全部就位
5. methodology_distill 真做 — 扫真 dev_logs 跑出 73 novel candidates

### 实施细节

- [x] **L2.1** Supervisor service 真做：事件流订阅 + 异常阈值 + 写 strategy_search_request（commit 67b960c）
- [x] **L2.2** Input Classifier 6 类（Director 持有）（commit e8af322）
- [x] **L2.3** Periodic Plan Review Heartbeat（每 3 步 / 5 分钟）（commit 9cee6c9）
- [x] **L2.4** External Supervisor 独立进程化 + LocalLLMProvider 接入（ollama）（commits 5bbf486 + 6330d6e + cb64c2f）
- [x] **L2.5** External Supervisor Mode A 同步监管 + Mode B 任务尾复盘 + 自嗨检测（commit 92a258e）
- [x] **L2.6** RCDH 诊断层级 + diagnostic_records 表 + narrow_scope 工具（commit ea8d8fb）
- [x] **L2.7** Strategist on-demand + 第一次 RSI 实例（LLM 路由优化）（commit a2bd5fd）
- [x] **L2.8** Gate 准入门禁：读 TestReport + RCDH report + Debrief，写 runtime_capabilities（commit 15a537f）
- [x] **L2.9** methodology_distill step 真实现（commit 211869a）
- [x] **L2.10** L2 验收 + retrospective + 3 新 methodology seeds（commit pending）
  - engineering_first_with_llm_fallback
  - frozen_dataclass_agent_io_contract
  - word_boundary_regex_for_nl_keywords

---

## L3 · 闭环扩散 + Forward/Backward 双策略（✅ 已达成 2026-05-27）

**交付标志**：3 条 RSI 实例并行跑；自指限制生效；ADR-018 半合并补齐到 ≥ 3 调用方。

### 实施细节

- [x] **L3.1** 第 2 条 RSI 实例：context 压缩策略（commit f03a5bf · +10 tests）
- [x] **L3.2** 第 3 条 RSI 实例：skill 选择启发式（commit 0476315 · +9 tests）
- [x] **L3.3** Forward / Backward 双修复策略（Strategist auto-select）（commit 5276801 · +14 tests）
- [x] **L3.4** 监督线三级阈值 + 4 级升级路径真接（commit 0c8bc9a · +22 tests）
- [x] **L3.5** 自指限制强化（governance + Strategist + Gate 双重）（commits a496a9f + 5d30395 · +13 tests）
- [x] **L3.6** ADR-018 半合并补齐（commits 84308fa + 98c890a · +7 tests）
- [x] **L3.7** L3 验收 + retrospective + 3 新 methodology seeds（commit pending）
  - rsi_explorer_pool_three_modes
  - forward_backward_repair_auto_select
  - caller_count_is_a_guardrail_not_a_kpi

---

## L4 · 多实例资源治理（推进中）

**交付标志**：启 Pool / 傩 Pool / External Supervisor Pool 多实例并行；合议层处理 dedup / cluster / 优先级；资源不爆。

### 计划任务

- [x] **L4.1** Strategist Explorer Pool 配置化（默认 3 模式）（commits b6c560c + 65180f2）
- [x] **L4.2** Supervisor Pool 多实例（不同 audit 维度）（commit 173d3c1）
- [x] **L4.3** External Supervisor Pool 配置化（按 audit mode 分实例）（commit a472331）
- [x] **L4.4** 合议层：dedup（Jaccard 相似度）/ cluster / 排序（commit pending）
- [ ] **L4.5** Resource quota（token / 时间 / dedup_key cooldown）
- [ ] **L4.6** 探索惩罚（失败候选 3 次内不重复 / similar 策略合并）
- [ ] **L4.7** L4 验收 + retrospective + ≥3 新 methodology seeds

---

## L5 · 自创任务（待启动，RSI 真闭合的标志）

**交付标志**：监督线发现的系统性问题 → 自动转 Strategist 任务（无需人提）→ 闭环跑完产出 capability → 自动晋级。

### 计划任务

- [ ] Supervisor 异常聚类 → 自动写 strategy_search_request
- [ ] 自动晋级 promotion_queue 超时规则（候选 > N 天未晋级 → 重审）
- [ ] 监督线给 Strategist 的高优触发通道

---

## L6 · Phase 2 商业化（条件成熟才进）

**前置条件**：L5 跑稳 + 用户决定切 Phase 2。

### 计划任务

- [ ] 垂直行业选定（电商 / 投放 / 内容分发 / CRM 选一）
- [ ] 接入层 adapter（API / SDK / browser automation）
- [ ] 业务能力卡 + 行业评测集
- [ ] ADR-019 Auth posture 升级到 Phase 2 / Phase 3

---

## 北极星指标

- **Phase 1 (L0-L5)**：鲲对鲲（自开发 + 自进化），目标 RSI 真闭合
- **Phase 2 (L6+)**：鲲对用户（商业化）

---

## 关键决策记录

| 决定 | ADR | 日期 |
|------|------|------|
| LLM 路由（CLI OAuth 走订阅，不走 API key） | ADR-002 | 2026-04-24 |
| 多租户 schema-ready + runtime 单租户默认 | ADR-007 | 2026-04-23 |
| Outbox + NATS JetStream | ADR-005 | 2026-04-23 |
| 守望规则引擎 YAML + Python handler | ADR-004 | 2026-04-23 |
| ADR-018 8 项合并方向 | ADR-018 | 2026-04-23 |
| **Auth posture 短/中/长三阶段** | ADR-019 | 2026-05-26 |
| **五层架构 + 7 agent + 双线运行（取代两个大脑表述）** | ADR-020 | 2026-05-26 |
| **RCDH 强制诊断层级** | ADR-021 | 2026-05-26 |
| **Anti-drift 长任务防漂移** | ADR-022 | 2026-05-26 |
| **External Supervisor 独立进程 + 本地模型** | ADR-023 | 2026-05-26 |
| **RSI 闭环 + 6 张数据脊柱表** | ADR-024 | 2026-05-26 |

---

## 历史里程碑（M1-M5）— 归档

以下 M1-M5 命名自 2026-05-26 起退役（ADR-020），保留供历史引用。

### M1 · 能跑通一次任务（≈ L0，已完成）
### M2 · 能自己评估（≈ L2 部分）
### M3 · 能自己进化（≈ L3 / L4）
### M4 · 有个好看的壳（跨 Phase 基础设施，按需做）
### M5 · 持续进化（≈ L5）

---

*最后更新：2026-05-26（v3 方案落地中）*
