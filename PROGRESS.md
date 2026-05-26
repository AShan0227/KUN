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

## L1 · 基础能力 + 路由闭环 + Anti-drift 基础（进行中）

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

### Phase 1 · 工程化（10 改动）

- [ ] 目录重组：建 `kun/agents/<role>/` × 7 + `kun/governance/` × 5 文件
- [ ] 拆 `orchestrator.py` (1524 LOC) 到 Director + Executor + Tester
- [ ] alembic 0011：6 张数据脊柱表 + evidence_ledger 加 3 字段
- [ ] ConcurrencySafety 真合并：删 `kun/control_plane/concurrency.py` 副本
- [ ] 删 KnowledgePrecipitation 抽象（0 调用方，ADR-024 替代）
- [ ] 接 `capability_router` 进 `LLMRouter.decide()`（写了 1 年没用，最大形式化案例）
- [ ] Director 输出 complexity + priority_profile + GoalAnchor + Input Classification
- [ ] Long-task mode 探测 + GoalAnchor 顶部 pinning
- [ ] Anti-sycophancy system prompt 加进 Executor
- [ ] L1 验收：跑测试 + 验证清单

---

## L2 · 监督线启动 + RCDH + 第一条 RSI 闭环（待启动）

**交付标志**：
1. External Supervisor 独立进程跑本地模型（qwen2.5-32b via ollama），发现 ≥ 1 个真自嗨案例
2. RCDH 走完 4 级一次，定位到 L1 案例（功能区没激活）
3. 第 1 条 RSI 实例（LLM 路由优化）跑完 10 步：异常 → Strategist 提候选 → Executor 跑实验 → Tester 评估 → Gate 启用 → Executor 下次任务用新路由
4. 数据脊柱 6 张表都有真实写入

### 计划任务

- [ ] Supervisor service 真做：事件流订阅 + 异常阈值 + 写 strategy_search_request
- [ ] Input Classifier 6 类（Director 持有）
- [ ] Periodic Plan Review Heartbeat（每 3 步 / 5 分钟）
- [ ] External Supervisor 独立进程化（docker-compose 加 service）
- [ ] LocalLLMProvider 接入（ollama / llama.cpp）
- [ ] External Supervisor Mode A 同步监管 + Mode B 任务尾复盘
- [ ] 自嗨/假通过检测每次必跑
- [ ] RCDH 诊断层级 + diagnostic_records 表 + narrow_scope 工具
- [ ] Strategist on-demand + 第一次 RSI 实例（LLM 路由优化）
- [ ] Gate 准入门禁：读 TestReport + RCDH report + Debrief，写 runtime_capabilities

---

## L3 · 闭环扩散 + Forward/Backward 双策略（待启动）

**交付标志**：3 条 RSI 实例并行跑；自指限制生效；ADR-018 半合并补齐到 ≥ 3 调用方。

### 计划任务

- [ ] 第 2 条 RSI 实例：context 压缩策略
- [ ] 第 3 条 RSI 实例：skill 选择启发式
- [ ] Forward / Backward 双修复策略（Strategist auto-select）
- [ ] 监督线三级阈值 + 4 级升级路径真接
- [ ] 自指限制（Strategist 改自己强制 L4 人审）
- [ ] ADR-018 半合并补齐：ValidationPipeline / NotificationLayer / GuardPolicy / GuardRule

---

## L4 · 多实例资源治理（待启动）

**交付标志**：启 Pool / 傩 Pool / External Supervisor Pool 多实例并行；合议层处理 dedup / cluster / 优先级；资源不爆。

### 计划任务

- [ ] Strategist Explorer Pool 配置化（默认 3 模式）
- [ ] Supervisor Pool 多实例（不同 audit 维度）
- [ ] External Supervisor Pool 配置化（按 audit mode 分实例）
- [ ] 合议层：dedup（embedding 相似度 > 0.85 合并）/ cluster / 排序
- [ ] Resource quota（token / 时间 / dedup_key cooldown）
- [ ] 探索惩罚（失败候选 3 次内不重复 / similar 策略合并）

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
