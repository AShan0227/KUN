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

- **状态**：accepted
- **决策**：严格按用户指定顺序
  - 主力 = Opus 4.7（个人订阅）
  - 次力 = Codex 5.3（个人订阅，编程专项）
  - 便宜档 = Claude 系列（Haiku 4.5 默认，Sonnet 4.6 中档备选）
  - Fallback = MiniMax M2.7（API，兜底）
- **调用实现**：走 `api.ofox.ai` proxy 做统一调用入口；探测限流信号 → 自动 fallback；记录降级事件到 NUO 告警通道
- **影响**：`LLMProvider` 抽象以"能力标签 + 优先级链"为核心；路由引擎按这个链做尝试
- **上线后**：替换为 Anthropic / OpenAI / MiniMax 官方 API（不改路由逻辑，只换 adapter）

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

## 需要后续补充的决策（占位，开发中定）

- ADR-019：日志格式 / 结构化字段 / 敏感数据屏蔽策略
- ADR-020：秘钥管理（本地 dev / 生产期）
- ADR-021：前端状态管理方案（Zustand / Redux / Context API）
- ADR-022：测试数据 fixture 构造方式

---

*ADR 记录自 2026-04-23 起，追加式维护。*
