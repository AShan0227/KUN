# KUN 设计决策记录（ADR）

> 本文档记录审查后的**绑定决策**。开发方案.md 是设计文档，decisions.md 是权威决策清单——冲突时以本文件为准。
> 每条决策带 `ADR-编号` 便于引用。新增/修改决策都追加到此，不删不改（append-only）。

---

## 方法论

- 每条决策 = 背景 + 选项 + 选择 + 影响
- 状态：`proposed` / `accepted` / `superseded-by:ADR-XXX` / `deprecated`
- 所有开发动作需能追溯到某个 `accepted` 的 ADR

---

## ADR-001：开发节奏与里程碑不按正常时间估算

- **状态**：accepted
- **背景**：单人开发 + Claude Code 辅助，效率远超传统人月估算
- **决策**：M1-M5 里程碑保持原方案 P0-P4 全部范围不做收缩。Claude Code 作为主力编程辅助。
- **影响**：时间估算以交付标志为准，不按"人月"换算。每个里程碑的验收标志必须硬达成。

## ADR-002：开发期 LLM 路由

- **状态**：accepted（2026-04-24 修订：改走 CLI OAuth）
- **决策**：
  - 主力 = Opus 4.7（Claude Code CLI OAuth，走 Pro/Max 订阅）
  - 次力 = Codex 5.3 / GPT-5.5（Codex CLI OAuth，走 ChatGPT 订阅）
  - 便宜档 = Claude 系列（Haiku/Sonnet 4.6，同 Claude Code CLI 内部自动选）
  - Fallback = MiniMax M2.7（直连 API，兜底）
- **调用实现**（修订）：
  - CLI OAuth 不用 API key，spawn `claude -p` / `codex exec --json` 子进程，解析 JSON/JSONL 输出
  - 成本口径：`cost_usd_actual=0`（订阅已付），`cost_usd_equivalent=<CLI 报告的等效 API $>`（ADR-008 一致）
  - 探测限流 / 登出 → 自动 fallback 到 MiniMax；记录降级事件到 NUO 告警通道
- **影响**：
  - 新增 `ClaudeCodeProvider` / `CodexCliProvider` 两个 subprocess adapter
  - `LLMProvider` 抽象不变（能力标签 + 优先级链）
  - 路由链：**CLI OAuth → API key（如果有）→ MiniMax → Stub**
- **上线后**：
  - 订阅路径只适合单机 dev. 多租户生产走 Anthropic/OpenAI/MiniMax 官方 API（只换 adapter, 不改路由逻辑）

## ADR-003：对话 → TASK.md 编译由 Claude Code 能力托底

- **状态**：accepted
- **决策**：不单列"对话 → TASK.md 编译器"模块。Claude Code 本身就能一步到位把自然语言拆成结构化任务。
- **影响**：意图理解层（§7.1）直接调主力模型，prompt 模板输出 TASK.md YAML；工程层只做校验和补全。

## ADR-004：守望规则引擎 = YAML 声明 + Python handler

- **状态**：accepted
- **选项**：(a) 纯 Python 装饰器 (b) YAML DSL + Python hook (c) 第三方规则引擎（durable-rules 等）
- **决策**：采用 **(b) YAML 声明规则 + Python handler 注册**。Prometheus alerting rules 风格。
- **规则文件样例**：
  ```yaml
  # rules/cost_runaway.yaml
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
- **事件条件表达式**：用 `simpleeval` 或 Python AST 白名单求值，禁用危险操作
- **理由**：YAML 好版本化、好审计、无代码回滚；复杂逻辑仍落到 Python handler 里；不用引入重型规则库

## ADR-005：事件存储 = Postgres Outbox + NATS 通知

- **状态**：accepted
- **决策**：Postgres `events` 表是**唯一真理源**（append-only + 按租户分区）；业务写入和事件写入在同一事务完成；后台 poller 读新事件 publish 到 NATS；消费者收到 NATS 通知后按 `event_id` 回 Postgres 拉完整事件。
- **Schema**：
  ```sql
  CREATE TABLE events (
    event_id BIGSERIAL PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,        -- task.started / route.feedback / ...
    subject TEXT NOT NULL,           -- NATS subject
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ DEFAULT now(),
    published_at TIMESTAMPTZ         -- NATS 发出时间戳
  );
  CREATE INDEX idx_events_unpublished ON events(event_id) WHERE published_at IS NULL;
  ```
- **NATS subject 命名**：`kun.{tenant}.{domain}.{event}` 例如 `kun.u-sylvan.task.started`
- **一致性语义**：at-least-once 交付；消费者必须幂等处理（用 `event_id` 去重）

## ADR-006：本地开发沙箱 = Docker Desktop Linux VM 内的容器

- **状态**：accepted
- **决策**：所有 KUN 后端组件（业务服务 + Postgres + Redis + Qdrant + NATS + MinIO）都跑在 **Docker Compose** 里，它们共享 Docker Desktop 的 Linux VM。沙箱策略在容器层实现（seccomp profile + capability drop + 网络 policy）。
- **macOS 宿主**：只跑前端开发服务器（Next.js dev server）和 Claude Code。不直接跑业务逻辑。
- **生产期规划**：Linux K8s 集群 + gVisor runtime；高风险任务 Firecracker。本地不模拟 gVisor（代价高、收益低）。

## ADR-007：多租户 = Schema 多租户就绪 + Runtime 单租户默认

- **状态**：accepted
- **决策**：
  - 所有业务表 **day 1 就带 `tenant_id` 列**（非空，有索引）
  - Postgres Row Level Security (RLS) 策略**立即启用**
  - `TenantContext` 在应用层作为 ambient context，默认值 `"u-sylvan"`
  - 未来从 auth token 解析 → 改 `TenantContext` 初始化方式即可，业务代码零改动
- **理由**：以后不用做迁移；开发期没负担（默认值 hardcoded）；多租户开关在 auth 层不在业务层

## ADR-008：费用展示前期用等效价格（内部）+ 上线后切真 API 计费

- **状态**：accepted
- **决策**：
  - 开发期（自用）：订阅模型按"等效 API 价格"估算（用 Anthropic 官方 API pricing 表映射），MiniMax 用真 token 价
  - 给用户版（上线）：全部走 API 调用，展示真实成本
  - 内部字段：`cost_usd_actual`（真花的）+ `cost_usd_equivalent`（估算的）并存；展示层按模式选择
- **数据**：成本字段在 RuntimeState 和 event payload 里保留两个

## ADR-009：Feature Flag = Postgres experiments 表 + 状态机

- **状态**：accepted
- **决策**：
  - 静态开关（on/off）→ YAML 配置文件
  - 带状态的实验（新 skill / 新路由规则 / 新 prompt）→ Postgres `experiments` 表
- **状态机**：`draft → shadow → canary → rollout → stable`（可 `rolled_back`）
- **流量分配**：`hash(tenant_id + experiment_id) % 100 < rollout_percent`（consistent hash，用户体验稳定）
- **Schema**：
  ```sql
  CREATE TABLE experiments (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                  -- skill / route_rule / prompt / ...
    status TEXT NOT NULL,                -- draft / shadow / canary / rollout / stable / rolled_back
    rollout_percent INT DEFAULT 0,
    control_variant JSONB,
    treatment_variant JSONB,
    guardrails JSONB,                    -- 指标护栏
    metrics JSONB,                       -- 当前指标快照
    created_at TIMESTAMPTZ DEFAULT now(),
    promoted_at TIMESTAMPTZ
  );
  ```
- **Python SDK**：`with experiment("new_router_rule_v2", tenant_id) as variant:`，自动记录 metrics

## ADR-010：对话协议 = WebSocket 自定义消息块（KUN 风格）

- **状态**：accepted
- **决策**：WebSocket 双向流，消息分块格式借鉴 Anthropic Messages API 但做 KUN 扩展
- **KUN 特色**：
  1. **双通道**：main（对话主流）+ side（费用 / 批处理 / 惊喜 / 告警推送）
  2. **纠偏即说**：用户文本里说"不是这样做"类引导词自动被标记为 correction，不用点按钮
  3. **费用透明**：每次 LLM 调用后 emit `cost_tick` 块，累计显示当次任务成本
  4. **惊喜分享**：系统发现更优路径/意外收获时推 `insight` 块到 side channel
  5. **多模态一等公民**：text / file / image / code 都是 content block
- **消息块类型**：
  ```
  main:  user_message | thinking | action_plan | tool_call | tool_result |
         assistant_message | answer | ask_user | correction_ack | error
  side:  cost_tick | evolution_note | insight | surprise | alert |
         idle_batch_report | guard_intervention
  ```
- **流式**：server → client 走 `delta` 增量；client → server 支持 `interrupt` 消息打断

## ADR-011：能力卡校准任务集 = 6 个内置任务（M1 必交付）

- **状态**：accepted
- **决策**：6 个多样化任务，覆盖主要能力维度。每任务含预期输出 + 评分 rubric + 成本/时长预估。详见附录 C。
- **维度**：coding / writing / research / data / reasoning / multi-step orchestration
- **用途**：新实体接入时自动跑这 6 个 → 初始化能力卡 → 标 `maturity: cold_start`

## ADR-012：傩数据层独立（Schema 级隔离，不影响 KUN 使用）

- **状态**：accepted
- **决策**：
  - Postgres 里傩用独立 schema（`nuo.*`），KUN 主业务用 `public.*` 或 `kun.*`
  - 两者**只通过事件总线交互**，不跨 schema JOIN
  - KUN 主业务 down 时傩可读历史数据继续展示；傩 down 时 KUN 正常跑只是看板不可用
- **影响**：未来整体抽出傩时只需迁移 `nuo.*` schema，KUN 业务零改动

## ADR-013：CI/CD 护栏分级 + 人审阈值

- **状态**：accepted
- **决策**：按改动影响面（自动识别）分三档
  - **小改**：自动 merge（CI 自动化护栏：lint + 单测 + 评估冒烟 20 任务 + 成本不涨 + 延迟不涨）
  - **中改**：CI 通过 + 你二次确认（一个 "approve" 点按）
  - **大改**：CI 全套 + 影子模式跑 3 天 + 你审阅批处理报告 + 明确批准
- **影响面识别**：git diff 文件路径匹配
  - 小：`tests/**`、`docs/**`、单 skill 增加、UI 组件 level
  - 中：改业务服务单模块、单个子系统内部
  - 大：改 `core/` 下的抽象（LLMProvider / 能力卡 / 交接协议 / 守望引擎）、数据库 schema 变更、安全/权限策略变更
- **紧急通道**：生产告警需立即回滚时，走"一键 revert"走金丝雀回滚路径，跳过护栏但事后必须补审
- **工具**：配置文件 `.kun/ci-tiers.yaml` 定义路径 → 档位映射

## ADR-014：Starter Pack 许可归属

- **状态**：accepted
- **决策**：
  - 从 Anthropic 140+ 开源 skill 精选时，每个 skill 的 SKILL.md 文件头**保留原作者 + 原 LICENSE**
  - 新增一行 frontmatter：`curated_by: KUN`
  - 仅收录 MIT / Apache-2.0 / BSD 许可的 skill
  - 发布清单（`skills/STARTER_PACK.md`）标明来源 URL + 版本 tag
- **合规扫描**：CI 里跑 `reuse lint`（SPDX 许可检查），发现不符合许可的 skill 阻断 merge

## ADR-015：意外度（surprise_score）公式

- **状态**：accepted
- **公式**：
  ```
  surprise_score = 0.35 · cost_dev + 0.20 · step_dev + 0.25 · path_novelty + 0.20 · quality_dev

  cost_dev      = max(0, actual_cost / estimated_cost - 1)        # 只看超支
  step_dev      = max(0, actual_steps / estimated_steps - 1)
  path_novelty  = 1 - jaccard(actual_skill_set, typical_skill_set_for_task_type)
  quality_dev   = abs(actual_quality - expected_quality) / max_quality
  ```
- **分档**：
  - `< 0.30` → low（只入流水账）
  - `0.30 – 0.60` → medium（中度分析 + 记入短期层）
  - `≥ 0.60` → high（深度分析 + 写入方法论 + 考虑推送 insight / surprise）
- **权重演化**：初始手定，idle-batch 期间基于历史标注微调权重（监督式）

## ADR-016：缓存命中率监控 + 动态 TTL 策略

- **状态**：accepted
- **决策**：
  - 指标：`kun.cache.hit_rate{tier}`（tier = permanent/stable/semi-stable/volatile）、`kun.cache.cost_savings_usd`（累积节省）
  - 告警阈值：
    - 永久段命中率 < 80% → warn（可能缓存 key 构造错）
    - 稳定段 < 40% → warn
  - 动态 TTL：命中率持续低时自动切换到 Anthropic extended 1-hour cache beta（有一定成本但命中率高）
  - 决策由守望子系统执行，按 ADR-004 规则引擎配置

## ADR-017：术语统一

- **状态**：accepted
- **决策**：全文统一术语（代码 / 文档 / 日志）
  - "夜间作业" / "批处理调度器" / "闲置时批处理" → **idle-batch**
  - "agent 实例" / "角色实例" → **role instance**
  - "角色模板" → **role template**
  - "外部 agent" / "外部实体" → **external agent**（若特指企业用 `company`）
  - "辩论" → **debate**（代码）/ "辩论" （中文文档）
  - "守望子系统" → **watchtower**（代码）
  - "任务说明书" → **TASK.md**（不改叫法）
  - "意外度" → **surprise_score**

## ADR-018：沿用 13 条思路的系统性合并（详见 §16）

- **状态**：accepted
- **背景**：用户明确要求"工程简洁工整，避免越维护越复杂"
- **合并清单**（详见 §16）：
  1. `ScoreDescriptor` 基类统一三套打分
  2. `ValidationPipeline` 统一评估 / 辩论 / AB / 红队 / 进化验收
  3. `NotificationLayer` 统一三层透明化 / 惊喜 / 告警 / 批处理推送
  4. `KnowledgePrecipitation` 统一能力卡回写 / 评分更新 / 方法论蒸馏
  5. `ConcurrencySafety` 统一锁 / 幂等 / 冲突检测 / 版本号
  6. `GuardPolicy` 统一硬熔断 / 自动回滚 / 升级给人
  7. `LayeredAsset` 统一三级渐进披露的所有资产存取接口
  8. `GuardRule` 单一规则引擎承载守望 / 评估触发 / CI 护栏 / 异常检测

---

## ADR-019：Auth posture（短期 / 中期 / 长期）

- **状态**：accepted (2026-05-26)
- **背景**：2026-05-26 审计发现 DB 层 (ADR-007 RLS + kun_app 角色 + tenant_id 主键) 做到产品级硬背书；但 API 层完全没 auth — HTTP 读 `X-Tenant-Id` header 不验签、WS 读 query param 不验签。审计后已加 `KUN_WS_REQUIRE_AUTH_HEADER` fail-closed 闸门和 production 拒启动 dev 默认 (commit f78df22)，但仍不是真 auth。
- **决策**：分三阶段补齐，不强行一步到位。

### 阶段 1 · 短期（当前阶段，鲲对鲲）

- 单机部署，`KUN_ENV=dev`，跳过真 auth
- production 启动检查（已实现）拒绝 dev 默认凭证、拒绝 `default_tenant_id` 未清空、CORS 拒绝 `*`
- WS 加 `KUN_WS_REQUIRE_AUTH_HEADER=1` 闸门（已实现），生产模式不允许 query 参数租户
- **状态**：✅ 已落地

### 阶段 2 · 中期（多用户进入前）

- 部署反向代理（Caddy / nginx / Cloudflare Access）做 OAuth proxy
- Proxy 在 verified session 后注入：
  - `X-Forwarded-User`（用户身份）
  - `X-Forwarded-Tenant`（用户所属租户）
  - `X-Forwarded-Signature`（HMAC-SHA256 签名，KUN middleware 验签）
- KUN middleware：
  - 不再信任 `X-Tenant-Id`（旧 header 仅 dev 模式允许）
  - 改信任 `X-Forwarded-Tenant`，前提是签名通过
  - WS 同样改走 proxy 注入的 cookie / signed header，不再走 query
- 签名 secret 通过 `KUN_FORWARD_AUTH_SECRET` 环境变量配置
- 触发条件：用户数 ≥ 2 或非本地部署

### 阶段 3 · 长期（真 SaaS）

- 完整 OAuth2 / OIDC + JWT Bearer
- RBAC（基于 capability card 的角色权限）
- 审计日志（每次身份验证写 audit_log）
- 多因素认证（敏感操作）
- 触发条件：商业化（Phase 2 L6）

### 实施约束

- 阶段切换时**不允许跳级**（短期 → 中期 → 长期），避免半套 auth
- 阶段 2 上线前必须有 ADR-019 修订记录确定 proxy 选型 + secret 管理方案
- 任何阶段都**不允许**禁用 production 启动检查（ADR-019 阶段 1 已实现的 `_production_safety`）

### 影响

- 短期对 RSI 闭环开发不阻断（用户 = 1，鲲自跑）
- 阶段 2 启动时需补一份 ADR-019 续篇，定 proxy 具体选型
- 阶段 2 触发后，所有 API endpoints 加 `require_signed_tenant()` middleware

---

## ADR-020：五层架构 + 7 个 agent 角色 + 主线/监督线双线

- **状态**：accepted (2026-05-26)
- **背景**：2026-05-26 全盘审计发现 KUN-V1.md §2 "三元要素 + 两个大脑" 描述与实现存在 10 处内在矛盾（守望大脑实际是空壳；agent 是隐式实体但 control_plane/ 22k LOC 在管 agent；多个 ADR-018 合并是壳；"学习放每一面" 7 个 step 中 6 个 stub）。需要明确一套自洽的架构作为后续 L1-L6 实施基准。
- **决策**：KUN 采用**五层架构 + 主线/监督线双线 + 7 个 agent 角色（按需扩展）**。

### 五层架构

| 层 | 回答什么 | 内容 |
|---|---|---|
| L1 · 架构层 | KUN 由什么构成 | 三元要素：Context / 接入层 / 工程化子系统 |
| L2 · 运行层 | 执行与监督如何并行 | 主线 + 监督线双线 |
| L3 · 实例层 | 谁干活 | 7 个 agent 角色（按需扩展） |
| L4 · 升级层 | 异常往哪儿升 | 4 级自治（角色 / 任务 / 监督线 / 人） |
| L5 · 治理层 | 改进如何沉淀为能力 | RSI 闭环 + RCDH（ADR-021）+ Anti-drift（ADR-022） |

每层只回答该层问题，**不越界**。

### 主线 / 监督线双线

```
主线 (串行 5 阶段):
   用户目标 → Director (拆解) → Executor (执行) → Tester (验证) → Gate (准入)
                                       ↕
监督线 (与主线并行旁路):
   Supervisor (傩观察 / 归因)
   Strategist (启 on-demand 策略搜索)
   External Supervisor (独立进程 + 本地模型, 见 ADR-023)
```

两线唯一交互：监督线发现问题 → 沿 L4 四级升级路径触发动作。

### 7 个 Agent 角色

| 角色 | 类型 | 线 | 模型 | 主要职责 | 闭哪个环 |
|------|------|----|------|---------|---------|
| Director | 常驻 service | 主线入口 | 远程 (gpt-5.5) | 拆解任务 + 输出 TaskSpec + GoalAnchor + complexity + priority_profile + input classification | 主线起点；接 anti-drift |
| Executor | task-bound | 主线主体 | 远程 (gpt-5.5) | 执行任务 + 写 capability card + 读 runtime_experiments / runtime_capabilities | 路由 → 能力卡 → 路由 |
| Tester | task-bound | 主线尾 | 远程 (gpt-5.5) | 跑 ValidationPipeline + 输出 TestReport | Gate 准入证据 |
| Gate | 常驻 service | 主线出口 | 远程 (gpt-5.5) | 治理决策 + 写 runtime_capabilities + 维护 evidence_ledger + promotion_queue | capability 晋级 → 实际启用 |
| Supervisor | 常驻 service | 监督线 | 远程 (gpt-5.5) | 订阅 event stream + 异常归因 + 走 RCDH + 写 strategy_search_request | RSI 触发起点 |
| Strategist | on-demand | 监督线 | 远程 + 本地（Explorer Pool 3 模式）| 候选策略生成 + 实验设计 + 写 StrategyExperiment | 实验 → runtime_experiments → 启用 |
| External Supervisor | **独立进程** | 监督线 | **本地模型** | Mode A 同步监管 + Mode B 任务尾复盘 + 自嗨/假通过检测必跑 | Gate 入门禁；详见 ADR-023 |

**扩展规则**：当前 7 个不是上限。若发现需要 Memory Curator / Skill Sourcer / Adapter Monitor，按"闭哪个环 / 谁读它 / 删了断什么"三连问通过即可加。**任何不能闭环的角色不允许新增**。

### 目录组织

L1 工程化阶段重组为：

```
kun/
├── core/                 # 共享底层（不变）
├── context/              # Context 子系统（L1）
├── interface/            # 接入层（L1）
│   └── llm/local_provider.py  # 新增本地模型接入
├── agents/               # 7 个角色（L3）
│   ├── director/
│   ├── executor/
│   ├── tester/
│   ├── gate/
│   ├── supervisor/
│   ├── strategist/
│   └── external_supervisor/
├── governance/           # L5 治理层
│   ├── rsi_loop.py
│   ├── rcdh.py
│   ├── evidence_ledger.py
│   ├── promotion_queue.py
│   └── diagnosis_scope.py
├── domains/              # Phase 2 业务模块
└── api/                  # FastAPI + WS + NUO
```

`kun/control_plane/` `kun/brain/` `kun/engineering/orchestrator.py` 三处现有目录/文件**在 L1 阶段重组后消失**，功能分散到上面对应位置。

### 退役

以下 KUN-V1.md 原表述自本 ADR 起退役（文档不删、但不再作为现行架构依据）：

- 「两个大脑」二元概念 → 改"主线 + 监督线"双线
- 「守望子系统是系统隐藏大脑」 → 改"监督线由多 agent 协作组成"
- 「不存在管理 agent」原则 → 改"agent 是显式实体，按需扩展"
- 「黑板组件化」 → 黑板只作心智模型，不建 BlackboardView 组件
- 「冷启动上来就是完整版」承诺 → 改"骨架完整，跑 N 任务后展示能力"
- M1-M5 里程碑命名 → 改 L0-L6（见 PROGRESS.md）

### 影响

1. ADR-001 的 M1-M5 命名继续保留为历史引用，但实际开发按 L0-L6 推进
2. ADR-018 的 8 项合并按 "≥3 调用方真合并" 原则重新定调（KnowledgePrecipitation 删；ConcurrencySafety L1 真合并；其他半合并待补）
3. 后续 ADR-021 / 022 / 023 / 024 基于本 ADR 的架构展开
4. 任何与本 ADR 矛盾的旧文档段落，以本 ADR 为准

### 引用

- ADR-018 §16（保留方向，合并执行调整）
- ADR-021 RCDH 强制诊断（运行机制）
- ADR-022 Anti-drift（运行机制）
- ADR-023 External Supervisor（角色实现）
- ADR-024 RSI 闭环 + 数据脊柱（治理层落地）

---

## ADR-021：RCDH 强制诊断层级（Root-Cause Diagnostic Hierarchy）

- **状态**：accepted (2026-05-26)
- **背景**：当前 KUN（以及业内多数 RSI 系统）的失败模式是「AI 看最后结果找原因 → 只调最后节点 → 表面通过 → 同问题复发」。例：用户视频音画不同步，AI 只调编辑器让那一帧同步，整条 pipeline 没修，下次还出。需要工程化在外部强制 AI 走根因分析。
- **决策**：任何修复行为发起前，**必须**按 4 级顺序排查；不允许越级。

### 4 级诊断层级

| 层 | 检查问题 | 检查方式 | 决策权 | 修复动作 |
|---|---|---|---|---|
| L0 · 产品设计层 | 这个 feature 本身是不是设计错了？ | 对比 ADR / PROMISES.md / 设计方案 | Director + 人 | 不修代码，开新设计任务 |
| L1 · 功能区激活层 | 对应功能区是不是没被激活？ | 扫 runtime_capabilities / feature flag / config / 调用链 | Gate | 改配置，不动代码 |
| L2 · 功能区开发层 | 是不是某个模块写错了？ | 模块级隔离测试 + 模块边界 trace | Supervisor + Strategist | 模块进 RSI 闭环 |
| L3 · 代码层 | 是不是具体代码错了？ | 单元级 trace + 局部 debug | Executor | bug fix |

### 工程化护栏

1. **修复请求必须有 `diagnostic_id`**：Gate 拒绝接收无 RCDH 报告的裸修
2. **`StrategyExperiment` 必须声明 `target_level`**：Strategist 提的实验明确针对哪一级
3. **重复 ≥ 3 次的同症状强制升 L0/L1**：不允许在 L3 再修一次
4. **诊断范围圈定 ≤ 5 模块**：`narrow_scope(symptom, evidence) → list[module]` 是强制入口；不允许"喂全 codebase 给 LLM"
5. **`evidence_ledger.diagnostic_level_reached` 字段必填**：每次修复留痕到了几级

### 数据结构

```python
class DiagnosticRecord(BaseModel):
    diagnostic_id: str  # ULID
    triggered_by_event_id: str
    symptom_summary: str
    repeat_history_count: int  # 同症状之前出现几次
    
    level_0_check: LevelCheckResult  # 产品设计层
    level_1_check: LevelCheckResult  # 激活层
    level_2_check: LevelCheckResult  # 模块层
    level_3_check: LevelCheckResult  # 代码层
    
    root_cause_level: int  # 0/1/2/3
    recommended_action: Literal["redesign", "activate", "module_rsi", "code_fix"]
    scope_modules: list[str]  # narrow_scope 圈定的 ≤5 模块
    created_at: datetime
    
class LevelCheckResult(BaseModel):
    skipped: bool  # 是否跳过（如：症状明显不属于该层）
    skip_reason: str | None
    is_root_cause: bool
    evidence: list[dict]  # 支持判定的证据
```

### Forward / Backward 双修复策略

确认根因层级后，Strategist 选修复方向：

```
Forward · 正向重跑
  条件：任务幂等 + 任务步数 < 5 + 现有结果"差"而非"灾难"
  动作：重跑任务（可能换 model / 换 strategy）
        结果对比：新 > 旧 → 覆盖
        结果对比：新 ≤ 旧 → 自动切到 backward

Backward · 逆向回溯
  条件：任务非幂等（已发邮件 / 合并代码 / 调外部 API）
        或任务步数 ≥ 5
        或有完整 evidence_ledger trace
  动作：沿 trace 找具体 step 的失败点
        走 RCDH 找层级
        局部修复

Auto-select rule (Strategist 持有):
  if task.idempotent AND task.steps < 5: forward
  elif task.has_side_effects: backward (forward 太危险)
  elif first forward 没改善: switch to backward
  else: forward first, fallback to backward
```

### 与其他 ADR 的关系

- **ADR-020 角色对接**：Supervisor 触发 RCDH；Gate 验诊断报告；Strategist 选修复方向；Executor 执行修复
- **ADR-022 关系**：Anti-drift 检测出的偏移信号也走 RCDH（多数会归为 L1 激活层或 L2 开发层）
- **ADR-023 关系**：External Supervisor Mode B 复盘报告会推荐 RCDH 层级

### 影响

- 新加 `diagnostic_records` 表（alembic 0011）
- `evidence_ledger` 加 `diagnostic_id` + `diagnostic_level_reached` 字段
- 新建 `kun/governance/rcdh.py` + `kun/governance/diagnosis_scope.py`
- 所有修复路径必须经过 RCDH，老路径在 L1 阶段重写或加 wrapper 强制走 RCDH

---

## ADR-022：Anti-drift 长任务防漂移（Goal Anchor + Input Classifier）

- **状态**：accepted (2026-05-26)
- **背景**：实际执行复杂长任务时，LLM 会出现 sycophancy-driven goal drift — 用户随手扔进来的消息扰乱原计划优先级，模型本能讨好新输入，慢慢偏离原始目标。用户手动 workaround 是"写方案 + 定期 review"。需要工程化把这一手工流程固化。
- **决策**：进入 long-task mode 时启用 5 层 anti-drift 工程化约束。

### Long-task mode 触发条件

```python
def is_long_task(meta: TaskMeta) -> bool:
    return (
        meta.complexity == "complex"
        or meta.estimated_duration_sec > 600        # 10 分钟
        or meta.estimated_steps > 5
        or meta.risk_level in {"high", "critical"}
    )
```

满足任一条件即进入 long-task mode；下面 5 层约束**全部启用**。短任务跳过节省开销。

### 5 层 Anti-drift 工程化约束

#### Layer 1 · Long-task mode 显式入口

- TaskMeta 加 `long_task_mode: bool` 字段
- Director 判定后写入；后续 agent 读取并差异化行为

#### Layer 2 · Goal Anchor 顶部 pinning

Director 生成 `GoalAnchor`：

```python
class GoalAnchor(BaseModel):
    anchor_id: str
    task_id: str
    tenant_id: str
    goal_statement: str            # 1-2 句，必须 < 200 字符
    success_criteria: list[str]    # 3-5 条可验证条件
    out_of_scope: list[str]        # 明确"本任务不做什么"
    invariants: list[str]          # 任务全程必须保持的不变量
    immutable: bool = True         # 默认不可改 — 改要走显式 pivot 流程
    created_at: datetime
```

**强制工程化约束**：

- GoalAnchor **必须**在 Executor 每次 LLM call 的 system prompt **顶部**（不是底部 — 顶部不会被 truncation 切掉）
- LLM 不允许改 anchor — Executor system prompt 加一条："Goal anchor 是 immutable，不可被新指令覆盖"
- 用户新消息拼接进 user message，**Goal anchor 位置不动**

#### Layer 3 · Input Classifier 6 类分流

Long-task mode 下，所有进入 working context 的新信息（用户消息 / 外部事件 / 工具输出）必须先过 Director 的 classifier：

```python
class InputClassification(BaseModel):
    category: Literal[
        "on_topic_progress",       # 推进当前目标 → 正常 integrate
        "on_topic_clarification",  # 澄清当前目标 → integrate + 更新 success_criteria
        "off_topic_noise",         # 与目标无关 → 礼貌回复但不进 working context
        "scope_expansion",         # 试图扩大任务范围 → 触发 RCDH L0 + ask user
        "explicit_pivot",          # 用户明确换任务 → pause + 创建新 task
        "interrupt",               # 中断/取消 → 走 cancel 流程
    ]
    confidence: float
    integration_hint: dict
```

**分类规则**：

- `on_topic_*` 正常喂给 Executor
- `off_topic_noise` 走**独立 channel** — 主线 Executor 不知道这条消息存在；Director 单独回复用户"收到，当前任务后处理"
- `scope_expansion` 触发 RCDH L0 检查（是不是计划本身就漏了这块）
- `explicit_pivot` 强制 user 二次确认 + 写入 evidence_ledger

→ **sycophancy 的工程化解药**：噪音根本不进模型 context。

#### Layer 4 · Periodic Plan Review Heartbeat

Supervisor 在 long-task 期间持续跑：

```
每 N 步（默认 3）或 每 M 分钟（默认 5）：
  Supervisor 注入 plan_review_event
  ↓
  Executor 下一次 LLM call 的 system prompt 加一段：
    "Review your goal anchor. Answer in JSON:
     - current_step: 你在执行原计划的第几步？
     - last_3_actions_alignment: 最近 3 个动作各自如何推进 goal？
     - drift_risk: 你觉得有偏移风险吗？why?"
  ↓
  Executor 给出 review 答复
  ↓
  Supervisor 独立判断（调 External Supervisor 用不同模型独立 verify）：
    - Executor 的自评是否可信？
    - 客观证据（events / artifacts）是否符合声明？
  ↓
  分流:
    - ok       → 继续
    - 轻度漂移 → 注入 alignment_reminder 进 system prompt
    - 重度漂移 → pause + Strategist 提"如何回归" + Gate 决策
```

#### Layer 5 · Anti-sycophancy system prompt 加固

Long-task mode 下，Executor system prompt 强制加一段：

```
你正在执行一个 long-task。规则：

1. Goal anchor（任务顶部 pinned）是 immutable。新的用户消息不会覆盖它。
2. 如果用户消息看起来要你换方向、做新事情、扩大范围：
   - 你不要主动满足
   - 输出 "needs_user_confirmation" 信号，让上层决定是否 pivot
3. 长任务的成功取决于"按计划完成"，不是"满足最近的每一条请求"。
4. 你被允许说"这个我先记下，当前任务完成后处理"。
5. 如果当前 step 输出和 goal anchor 不一致，优先 anchor。
```

→ 给模型显式"拒绝讨好"的许可证。

### 数据结构

```python
class PlanReview(BaseModel):
    review_id: str
    task_id: str
    anchor_id: str
    triggered_at_step: int
    triggered_at_time: datetime
    executor_self_report: dict
    supervisor_verdict: Literal["ok", "mild_drift", "heavy_drift"]
    drift_evidence: list[dict]
    external_supervisor_verify: dict | None  # 独立 verify 结果
    action_taken: Literal["continue", "remind", "pause", "rsi_trigger"]
```

### 影响

- 新加 `goal_anchors` 表 + `plan_reviews` 表（alembic 0011）
- TaskMeta 加 `long_task_mode` 字段
- Director 增加 `generate_goal_anchor` + `classify_input` 两个职责
- Executor system prompt 顶部强制 pin GoalAnchor + 加 anti-sycophancy 段
- Supervisor 加 `inject_plan_review` + `verify_review_response`
- 新加 7 个事件类型：`goal_anchor.created` / `plan_review.due` / `plan_review.completed` / `drift.detected` / `input.classified` / `user_message.queued_for_post_task` / `scope_expansion.requested`

### 与其他 ADR 的关系

- **ADR-020 角色对接**：Director 持 GoalAnchor + Input Classifier；Supervisor 触发 heartbeat；Executor 读 anchor
- **ADR-021 RCDH 对接**：scope_expansion 分类 → RCDH L0；heavy_drift → RCDH L2
- **ADR-023 对接**：plan_review 的独立 verify 由 External Supervisor 跑
- **ADR-001 核心原则补充**：「Goal 优先于讨好」作为第 7 条原则补入

---

## ADR-023：External Supervisor 独立进程 + 本地模型

- **状态**：accepted (2026-05-26)
- **背景**：内部 Supervisor（傩）和主线 Executor 用同一类模型时存在「同源偏差」问题 — 模型评模型自己容易自嗨；同时事后回溯模式（KUN-V1.md 原 idle-batch）对长任务而言来得太晚。需要一个独立视角 + 实时介入的监督机制。本地模型 (qwen2.5-32b 等) 的零边际成本进一步让"持续监管"成为可能。
- **决策**：External Supervisor 是 7 个 agent 角色之一，但与其他 6 个角色架构上不同：**独立进程 + 本地模型 + 双模式 (实时同步 + 任务尾复盘)**。

### 独立性要求

3 条硬约束，启动时 fail-close：

```python
# kun/agents/external_supervisor/__init__.py
class ExternalSupervisor:
    def __init__(self):
        # 必须独立进程 (multiprocessing.Process, 不能 asyncio.Task)
        assert isinstance(self.process, multiprocessing.Process)
        
        # 必须显式配 model
        assert os.getenv("KUN_SUPERVISOR_MODEL"), \
            "External supervisor requires explicit KUN_SUPERVISOR_MODEL"
        
        # 必须不同 model family (不允许用同 family 监督自己)
        assert self.model_family != main_line_model_family, \
            "External supervisor must use different model family from main line"
```

任一条件不满足，启动失败。**不允许 silent 跑同模型监督自己**。

### 双模式

```
Mode A · 同步监管 (during task)
  
  订阅 NATS subject: kun.task.*
  每 N event (默认 5) 或 T 秒 (默认 30) 判一次:
  
    审计维度:
      - 漂移 (与 GoalAnchor 偏离)
      - 自嗨/假通过 (声明完成但证据不足)
      - 阻塞 (主线无新事件超时)
      - context 污染 (Executor 反复读取无关 asset)
  
    三级响应:
      weak  → 写 supervisor_observations 表 (留痕不打扰)
      mid   → push 到主线 channel (Executor 看得见但不强制)
      high  → 强制 Gate pause 主线 (主线必须停)


Mode B · 任务尾复盘 (end of task, ≤30s)
  
  Gate 准入门禁前必跑:
  
    输出 1 页 debrief:
      - 任务真完成 vs 假通过 (含证据对比)
      - 关键决策点合理性
      - silent failure / dead loop / 跳过的 step
      - RCDH 层级建议 (如果有问题)
    
    写 evidence_ledger:
      - external_supervisor_debrief_id 字段必填
    
    Gate 准入规则:
      - debrief 不过关 → Gate 拒绝准入 (即使 Tester 给了 pass)
```

### 4 种 Audit Mode（按触发激活，不全跑）

| Mode | 触发条件 |
|---|---|
| 产品体验审计 | Artifact 含用户可见输出 (UI / 文本 / 对外消息) |
| 工程质量审计 | 改了代码 / config / migration |
| 用户目标对齐审计 | Director 判 complexity ≥ medium |
| **自嗨 / 假通过审计** | **每次必跑** — RSI 不退化的命门 |

大部分简单任务只跑自嗨审计 1 种；复杂任务跑 3-4 种。

### 本地模型接入

```python
# kun/interface/llm/local_provider.py (新增)
class LocalLLMProvider(LLMProvider):
    """通过 ollama / llama.cpp / vllm 接入本地模型"""
    name = "local"
    
    def __init__(self, model_id: str, endpoint: str):
        # 默认: ollama 在 localhost:11434
        self.endpoint = endpoint or "http://localhost:11434"
        self.model_id = model_id  # e.g. "qwen2.5:32b"
```

部署：

```yaml
# docker-compose.dev.yml 加 service
external-supervisor:
  build: .
  command: uv run kun external-supervisor
  depends_on: [nats, postgres, ollama]
  environment:
    KUN_SUPERVISOR_MODEL: local:qwen2.5-32b
    KUN_SUPERVISOR_ENDPOINT: http://ollama:11434
    KUN_NATS_URL: nats://nats:4222
    KUN_PG_DSN: ${KUN_PG_DSN}

ollama:
  image: ollama/ollama:latest
  ports: ["11434:11434"]
  volumes: [ollama_data:/root/.ollama]
```

### ROI

- 远程模型监管 100 次/天 × 3k token ≈ $3/day（用 gpt-5.5）
- 本地模型 = $0 marginal + 一次性硬件成本（M-series MacBook 即可跑 32B）
- 一年节省 ~$1000+，**更关键的是不为成本斤斤计较 — 监管密度可以拉满**

### 与其他 ADR 的关系

- **ADR-020**: External Supervisor 是 7 个 agent 角色之一（独立进程 + 本地模型是它特有的）
- **ADR-021 RCDH**: Mode B 复盘报告输出推荐的 RCDH 层级
- **ADR-022 Anti-drift**: Mode A 同步监管核心 audit 维度之一是 drift detection；plan_review 的独立 verify 由 External Supervisor 跑

### 影响

- 新增 `kun/agents/external_supervisor/` 目录
- 新增 `kun/interface/llm/local_provider.py`
- 新加 `docker-compose.dev.yml` 的 ollama + external-supervisor 两个 service
- 新增 `supervisor_observations` 表（Mode A 留痕）
- `evidence_ledger.external_supervisor_debrief_id` 字段
- Gate 准入逻辑必须读 debrief

---

## ADR-024：RSI 闭环 + 6 张数据脊柱表

- **状态**：accepted (2026-05-26)
- **背景**：KUN-V1.md §1.3 强调"学习放在每一面" — 但 2026-05-26 审计发现实际状态是 idle-batch 7 个 step 中 6 个 stub；capability_router 写了但没接进 LLMRouter；route_rule_mining / methodology_distill / debate 学习曲线全部零代码。"学习" 是叙事但落不下来。需要把 RSI 闭环用数据脊柱真正固化下来。
- **决策**：RSI 闭环以 **6 张数据脊柱表 + 10 步流程 + 3 条工程化约束** 落地。

### 6 张数据脊柱表（alembic 0011）

闭环 = 数据流动。**先建表后写代码**。

| 表 | 写入方 | 读取方 | 闭哪个环 |
|---|---|---|---|
| `runtime_capabilities` | Gate | Executor 启动 + 任务前 | capability 晋级 → 实际启用 |
| `runtime_experiments` | Strategist | Executor / Tester | 候选策略 → 真跑实验 |
| `strategy_search_requests` | Supervisor / External Supervisor | Strategist | 异常 → 自动触发探索 |
| `diagnostic_records` | Supervisor (走 RCDH) | Gate / Strategist | 诊断到层级 → 引导修复 |
| `goal_anchors` | Director | Executor (system prompt 顶部) | 长任务防漂移 |
| `plan_reviews` | Supervisor + Executor + External Supervisor | Gate | 漂移检测闭环 |

`evidence_ledger` 加字段：

```sql
ALTER TABLE evidence_ledger ADD COLUMN diagnostic_id ULID;
ALTER TABLE evidence_ledger ADD COLUMN external_supervisor_debrief_id ULID;
ALTER TABLE evidence_ledger ADD COLUMN diagnostic_level_reached INT;
```

### RSI 10 步闭环

```
1. 人类定义目标、安全边界、验收标准
   ↓
2. Director 启动
   - 拆任务 + 生成 GoalAnchor (ADR-022)
   - 定义验收标准
   - 输出 complexity + priority_profile + input classification
   ↓
3. Gate 建立任务账本
   - sandbox
   - resource lock (ConcurrencySafety)
   - rollback drill 准备
   - evidence_ledger 入条
   ↓
4. Executor 执行主线任务
   
   同时并行启动监督线 (ADR-020 双线):
   - Director 持续验收目标对齐 (anti-drift heartbeat, ADR-022)
   - External Supervisor 持续 Mode A 监管 (ADR-023)
   - Supervisor 持续抓异常信号 (anomaly threshold)
   
   ↓ 如果发现问题
   
5. Supervisor 走 RCDH 自评 (ADR-021)
   - L0 → L3 顺序排查
   - 写 diagnostic_record
   - 推荐修复方向
   
   ↓ 如果是 L2 (模块开发问题)
   
6. Strategist 做 Strategy Search
   - 多个本地 Explorer 并行 (ADR-020)
   - 多个候选策略
   - 每个策略带实验计划和验收指标
   - 写 StrategyExperiment 进 runtime_experiments
   
   ↓
   
7. Safe Experimentation
   - Strategist 编排实验 (forward / backward 由 ADR-021 决定)
   - Gate 强制 sandbox / lock / rollback
   - KUN Runner 实现候选方案
   - Tester / Browser / Replay runner 执行验证
   
   ↓
   
8. Learning Signal 独立收集
   - 自动测试结果
   - 浏览器试玩 (Phase 2 才有)
   - 历史 replay
   - External Supervisor 评分
   - 残差审计
   - 性能 / 失败率 / 质量指标
   
   ↓
   
9. 合议层
   - Gate 去重、聚类、排序
   - Director 判断任务相关性
   - Supervisor 判风险
   - Strategist 比较策略优劣
   - External Supervisor 给独立意见 (Mode B 复盘)
   
   ↓
   
10. Capability Governance (Gate 执行)
   - 失败：淘汰/归档/形成 known limit
   - 通过：合入 main，但默认 runtime_enabled=false
   - 自动进入 promotion 队列
   - replay / shadow / canary / rollback drill 序列
   - 达标后 Gate 写 runtime_capabilities.enabled=true
```

### 3 条工程化约束

#### 约束 1 · 自指限制（防退化）

Strategist 提的策略**如果改 Strategist / Supervisor / Director / Gate 自己** → 强制升 L4 人审。

```python
# kun/agents/strategist/__init__.py
SELF_REFERENTIAL_TARGETS = {"strategist", "supervisor", "director", "gate", "external_supervisor"}

def submit_experiment(self, exp: StrategyExperiment):
    if exp.target_module.split(".")[0] in SELF_REFERENTIAL_TARGETS:
        require_human_approval(exp)
```

→ RSI 允许全自动改 Executor / Tester；改"监督和治理自己" 必须人审。这是 termination condition。

#### 约束 2 · 基础能力闸门（防 RSI 假启动）

Phase 1 拆为 1a + 1b：

- **Phase 1a (基础能力)**：KUN 能可靠完成 3 类开发任务（写测试 / 修 typo bug / 加单 endpoint）
  - 判定：3 类任务最近 N 次成功率 95% CI 下界 ≥ 0.7
  - N 自适应：方差小 → N=10；方差大 → N=30+
  - **不需要 RSI**
- **Phase 1b (RSI 闭环)**：1a 通过后启用 RSI 在这 3 类任务上探索"做得更好的策略"
- **没跑通 1a 不许跑 1b**

#### 约束 3 · 候选能力晋级队列（防 dormant feature）

`runtime_capabilities` 表 + promotion_queue：

```python
class RuntimeCapability(BaseModel):
    capability_id: str
    target_module: str
    change_summary: str
    enabled: bool = False              # 合入 main 后默认 false
    promotion_state: Literal[
        "merged",                      # 合入 main
        "in_replay",                   # 历史 replay 中
        "in_shadow",                   # shadow 模式中
        "in_canary",                   # canary 模式中 (1% → 5% → 25%)
        "ready",                       # 达标待启用
        "enabled",                     # runtime_enabled=true
        "rolled_back",                 # 出问题回滚
        "expired",                     # 超时未晋级 → 重审
    ]
    promotion_started_at: datetime
    promotion_deadline: datetime       # 默认 N 天 (e.g. 14)
    rollback_on: list[str]             # 哪些指标超过什么阈值自动回滚
```

**超时规则**：候选能力 > N 天未晋级 → 自动写 strategy_search_request 重新评估（Strategist 判断"过期 / 污染 / 低价值" → 合并 / 降级 / 删除 / 重新排队）。

### 与其他 ADR 的关系

- **ADR-020**: 6 张表是 L5 治理层的数据脊柱；10 步流程贯穿 7 agent 协作
- **ADR-021 RCDH**: step 5 走 RCDH；diagnostic_records 是 6 张表之一
- **ADR-022 Anti-drift**: step 4 监督线包含 Director heartbeat；goal_anchors + plan_reviews 是 6 张表之二
- **ADR-023 External Supervisor**: step 4 / 8 / 9 都有 External Supervisor 参与
- **ADR-018 §16.4 KnowledgePrecipitation 删除**: 本 ADR 用 6 张表 + 10 步流程替代

### 影响

- alembic 0011 新建 6 张表 + evidence_ledger 加 3 字段
- 新建 `kun/governance/rsi_loop.py` 编排 10 步
- 新建 `kun/governance/promotion_queue.py` 处理 capability 晋级
- 新建 `kun/governance/evidence_ledger.py` 写入 / 查询封装
- 删除 ADR-018 §16.4 KnowledgePrecipitation 抽象
- 实施分 L2 / L3 两阶段（L2 跑通第 1 条 RSI；L3 扩散到 3 条并行）

---

## ADR-025：工程能力冷启动 — 通过开发日志蒸馏 Claude/Codex 的工程思维到 KUN

- **状态**：accepted (2026-05-26)
- **背景**：当前 KUN 有 0 条工程类 capability card / methodology。即使 RSI 闭环跑起来（ADR-024），从零样本起步发展工程能力要数百个任务周期。**更快路径**：捕获当前 AI 开发者（Claude / Codex CLI 等）的真实开发过程，蒸馏成 KUN 的 starter 工程能力，让 KUN 上来就有"过来人的手感"而不是从零摸索。
- **决策**：建立 **开发日志（dev log）→ methodology cards** 的捕获 + 蒸馏管道，作为 KUN 工程能力的冷启动来源。

### 捕获机制

每个**有意义的工作单元**完成后必须写 dev log：

| 工作粒度 | 日志类型 | 文件路径 |
|---|---|---|
| 单子任务（L1.1 等） | 微日志（3-5 句话） | `docs/dev_logs/L<N>-progress.md`（追加式） |
| L 里程碑（L1 / L2 等） | 完整回顾 | `docs/dev_logs/L<N>-retrospective.md`（一份独立） |
| 事故 / 失败 | 失败分析 | `docs/dev_logs/incident-<date>-<topic>.md` |

dev log 不是可选 — **是 commit 后强制动作**（写进 ADR-025 的工程约束）。

### Dev log 结构（强制模板）

每份 retrospective 必须包含 9 段：

```markdown
# Dev Log: <Task Name>

**Date / Phase / Duration / Commits**

## Goal           — 实际目标
## Approach       — 具体走法
## Key Decisions  — 决策 + 为什么
## Constraints Applied  — RCDH 走到哪级 / anti-drift 状态 / Forward 还是 Backward
## Patterns Used  — 可识别的工程模式
## What Failed    — 失败 + 根因 + 恢复
## What Worked    — 成功模式 + 复用条件
## Heuristics Extracted  — 可蒸馏的规则
## Methodology Card Candidates  — 给 KUN 蒸馏管道的输入
```

最后一段 **Methodology Card Candidates** 是 KUN 蒸馏的入口 — 必须是结构化的：

```yaml
- topic: <area>          # e.g. large_design_refactor
  trigger: <when>        # 什么场景适用
  action: <what to do>   # 具体做法
  rationale: <why>       # 原因 / 取舍
```

### 蒸馏管道（实施 L3）

KUN 的 idle-batch `methodology_distill` step（当前 stub）在 L3 阶段获得真实现：

```python
async def methodology_distill_real_impl(tenant_id: str) -> dict:
    """
    1. 扫 docs/dev_logs/*.md → 解析 Methodology Card Candidates 段
    2. 扫 seeds/methodologies/*.yaml → 现有 cards
    3. 对比：新 candidates vs 现有 cards 做 embedding 相似度
       - similarity > 0.85 → 合并（更新现有 card 的 evidence count）
       - similarity ≤ 0.85 → 写入新 card
    4. 写入 Context 子系统 (LayeredAsset, kind="methodology")
    5. emit event: methodology.card_distilled
    6. 返回统计：新增 N, 合并 M, 总数 T
    """
```

蒸馏出的 methodology cards 通过 ContextPacker + ImportanceScorer 被以下 agent 自动检索消费：

- **Director** 拆任务时（complexity ≥ medium）→ 检索相关 methodology 注入 GoalAnchor 的 `invariants`
- **Executor** 卡住时（连续 2 步无进展）→ 检索相关 methodology 注入 system prompt
- **Supervisor** 走 RCDH 时 → 检索相关 methodology 帮助归因
- **Strategist** 提候选策略时 → 检索 methodology 限制策略空间

### Seed methodology cards

`seeds/methodologies/*.yaml` 是冷启动种子，KUN 启动时加载。本 ADR 落地时随附 3-5 份种子（提取自 Phase 0 retrospective）。

后续 L1-L5 每完成一个里程碑，至少新增 3-5 份种子。

### 工程约束

1. **每个 commit 前必查 dev log** — 当前 L 阶段的 progress.md 是否同步更新（git pre-commit hook 可加，但当前阶段靠纪律）
2. **每个 L 里程碑结束必写 retrospective** — 没写 retrospective 不算完成
3. **失败必写 incident** — 任何被审计 / 自审发现的失败都要 incident 文件
4. **Methodology Card Candidates 必须可解析** — YAML 结构化，不允许散文式描述

### 与其他 ADR 的关系

- **ADR-018 §16.4 KnowledgePrecipitation 退役**：本 ADR 用具体的 dev log → methodology distill 管道替代抽象的"统一结果转知识"
- **ADR-022 Anti-drift**：methodology cards 可注入 GoalAnchor 的 invariants，帮长任务保持工程纪律
- **ADR-021 RCDH**：methodology cards 包含 "诊断 → 归因" 的样板，给 RCDH 提供启发式
- **ADR-020 Director / Supervisor / Strategist 角色对接**：上述 3 个 agent 在 L3 起读取 methodology cards
- **KUN-V1.md §1.3 "学习放每一面"原则**：这是该原则的第一个真实落地（之前全 stub）

### 影响

- 新建目录 `docs/dev_logs/` + `seeds/methodologies/`
- 新建 `docs/dev_logs/README.md`（模板 + 工作流）
- 每次 commit 必须配 dev log 同步更新（先靠纪律 + ADR 约束，后期可加 pre-commit hook）
- idle-batch `methodology_distill` step 在 L3 阶段从 stub=True 转为真实现
- Context 子系统加 `methodology` 资产类目（已在 AssetKind 中预留）
- ImportanceScorer / ContextPacker 接入 methodology 检索（L3 / L4 阶段）

### 北极星价值

- 短期（当前 /loop 自主推进期）：每个里程碑后产生 retrospective + 3-5 份 methodology seed → 半年后 KUN 自动启动时已有数十份"过来人手感"
- 中期（L3 蒸馏管道实装后）：KUN 不止读 seed，还读它自己的 dev log（任务回放产生的）→ 自我迭代工程能力
- 长期（L5 自创任务）：KUN 通过 RSI 发现 methodology 之间的差距 → 自动提任务补 methodology

> **关键认识**：让 KUN 完美的不是它独立从零学会一切，是它**站在 Claude / Codex CLI 这些当前最强 AI 开发者的工程肩膀上**起步。dev log → methodology distill 是这个肩膀的承接机制。

---

## ADR-026：Phase 2 接入层架构 — Browser-first hybrid

**状态**：accepted（2026-05-27）

### 背景

Phase 1 (L0-L5) 完成后, KUN 已经具备:
- 7 agent + Pool 多实例 + 监督线 + Strategist + Gate
- RSI 真闭环 (Supervisor → Strategist → Gate → runtime_capabilities)
- 1186 unit tests 全绿, 端到端 demo 跑通真 Postgres

Phase 2 商业化目标 (用户决策, 2026-05-27): "电商 / 投放 / 内容分发 / CRM **四个行业互通, 先不上线, 完善打磨产品**". 4 个行业本质都是 SaaS 操作场景, KUN 扮演 agent 替人在 Shopify / Meta Ads / 微信公众号 / Salesforce 等平台上做事.

### 决策

接入层架构: **Browser-first hybrid** —
- **Browser automation 是主路径**: Playwright + DOM 自愈, 覆盖度最大化 (~100%)
- **官方 API 是加速路径**: 同操作有 API 就走 API (快 10x), 缺时 fallback browser
- **不做 SDK**: KUN 不是开发者平台, 是 agent

### 不做 SDK 的理由

| 形态 | 适用 | 4 行业可用度 |
|------|------|------------|
| 官方 API | 已开放接口的平台 (Shopify, Meta Ads) | ~40% (微信公众号/小红书接口稀缺) |
| SDK | 给第三方开发者集成 KUN 的库 | 0 (与产品定位不符) |
| Browser 自动化 | 所有 SaaS (鲲扮演人) | ~100% |

KUN 产品定位是"自主 agent 替人做事", 不是"开发者集成在 KUN 上构建产品". SDK 是 platform-as-product 思路, 与 agent-as-product 不兼容.

### 架构

```
Director → Adapter Router → ┬─ Official API Adapter (Shopify/Meta/Salesforce/etc)
                            └─ Browser Adapter (Playwright pool + visual self-heal)
```

**核心模块** (`kun/interface/automation/`):
- `base.py` · `AutomationAdapter` Protocol + `Action` / `ActionResult` 数据模型
- `registry.py` · `AdapterRegistry` — 注册 `(platform, operation) → list[adapter]`
- `router.py` · `AdapterRouter` — capability_score-driven API / Browser 选择
- `api_base.py` · `APIAdapter` base — HTTP-based 适配器抽象 (子类实现 `_do_execute`)
- `browser_base.py` · `BrowserAdapter` base — Playwright 适配器抽象 (page_factory 注入)

**与现有 `kun/interface/adapters/` 区分**:
- `adapters/` 是 **输出翻译** (KUN 内部 → A2A / email / REST / markdown), L0 早期建
- `automation/` 是 **动作执行** (KUN 操作他人 SaaS), L6 起步

### Router 选择算法

1. `action.requested_kind` 显式 (`api` 或 `browser`) → 用它
2. 否则 API capability_score >= 0.7 → 用 API (快 + 稳)
3. API 不达标 / 不可用 → fallback Browser
4. 没注册的 (platform, operation) → `AdapterSelection.kind=None` + reason

`capability_score` 与 LLM Router (ADR-002) 同源 cold-start damping:
- `damped_score = 0.5 + min(1.0, sample_size/30) * (success_rate - 0.5)`
- `health_check` 失败 → 5 分钟 cooldown 不选

### Fallback policy

主路径 (API) 失败 → 切 `fallback_candidate` (Browser).
- 主路径 success → record_success → score 升
- 主路径 fail → record_failure + 切 fallback
- Fallback 也 fail → return result with `fallback_used=True`, caller 决定下一步

### Adapter 注册流程 (per 行业)

1. 选定行业 (e.g. 电商) → 选 platform (e.g. Shopify, Taobao, JD)
2. 每个 platform 实装 API + Browser 两个 adapter (有 API 的) 或仅 Browser
3. Adapter `supported_operations` 声明能干啥 ("create_product", "list_orders", ...)
4. 在 KUN 启动时 `registry.register(my_adapter)` 注入
5. KUN Director 拆 task → Executor 调 `router.route_and_execute(action)`

### 自我进化路径 (RSI 入口)

接入层本身可作 RSI 实例:
- **API 成功率监控**: 每个 platform/operation 维护 capability_card
- **失败 spike → strategy_search_request**: 同 (platform, op) 失败率 ≥ 阈值 → Supervisor 写
- **Strategist 候选**: e.g. "Shopify API 频繁 429 → 切 Browser primary" 或 "DOM 自愈规则更新"
- **Gate 准入 → runtime_capability**: 验证后写入, 影响下次 Router 选择

这与现有 RSI 闭环完全兼容, capability_card 是接口点.

### 不在本 ADR 实装

- Playwright 真依赖 (留给行业接入时 `uv add playwright`)
- DOM 自愈算法 (selector fallback / visual cv) — 各行业 adapter 自己实装
- 具体 platform adapter (Shopify / Meta / WeChat / Salesforce) — 选定行业后建
- 业务能力卡冷启动校准 task (见 ADR-011) — 选行业后补

### 实施路径

L6.A (本 ADR 同时落地) · Adapter Router framework + Registry + base classes + 26 unit tests
L6.B · 行业评测集框架 (industry-agnostic IndustryEvalSuite + GoldenTask)
L6.D (待行业选定) · 第一个 platform adapter 实装
L6.E · KUN 真接 Director.intent → Executor → Router → action execution e2e

### 影响

- 新 module `kun/interface/automation/` (5 个文件 + tests)
- 现有 7 agent 都不动 — 接入层是新加的 leaf
- Director 现有 `intent.py` 后续加 `action` 字段输出 (映射到 `Action(target_platform, operation)`)
- Executor 现有调用通过 LLM 完成, 后续加 `route_and_execute` 选项 (与 LLM 调用并存)
- Supervisor 增 `action_failure_spike` anomaly type (类似 `task_failure_spike` 但 per-platform)
- Strategist 增 `_candidates_for_action_failure_spike` generator

### 北极星价值

- **真正完成 KUN 闭环**: 不只能内部跑测试和单测; 能真去 Shopify 后台上架商品 / Meta Ads 后台调出价
- **4 行业互通**: Adapter Router framework 一次设计, 4 行业都用; 切换行业不动 KUN 核心
- **RSI 真改变世界**: 现在 RSI 改的是 LLM 路由 / context 压缩 / skill 选择 这些"内部"系统; L6 起 RSI 也能改"我在 Shopify 怎么操作更稳" 这种"外部"系统

> **关键认识**: Browser-first 不是"无奈选 browser 因为 API 不全", 是"承认 SaaS 操作的 ground truth 是浏览器 UI". API 是优化, 不是基线.

---

*ADR 记录自 2026-04-23 起，追加式维护。*
