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

*ADR 记录自 2026-04-23 起，追加式维护。*
