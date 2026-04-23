# KUN 开发进度

> 按交付标志推进 (ADR-001). 本文档追踪 milestone 进度 + 重要决策.

---

## M1 · 能跑通一次任务

**交付标志**: 从用户对话 → 拆解 → 分配角色 → 执行 → 返回结果的完整链路打通,
6 个校准任务能跑过 4 个以上.

### ✅ 已完成

- [x] 项目骨架 / Docker Compose / Alembic / CI 配置
- [x] 核心数据模型: TASK.md / RuntimeState / CapabilityCard / Handoff L1-L4 / Event / Notification / ScoreDescriptor
- [x] 多租户 ambient context + Postgres RLS-ready schema (ADR-007)
- [x] Outbox pattern + NATS JetStream 通知 (ADR-005)
- [x] LLMProvider 抽象 + Anthropic / OpenAI / MiniMax / Stub adapters (ADR-002)
- [x] LLM Router tier-based fallback (ADR-002)
- [x] Watchtower rule engine (YAML + Python handlers, ADR-004)
- [x] Context 子系统 scaffold (LayeredAsset + ImportanceScorer, ADR-018 §16.1/§16.7)
- [x] Task brain: Intent → Planner → Router
- [x] Orchestrator walking skeleton (§5.1-5.3)
- [x] FastAPI + WebSocket 对话协议 (ADR-010)
- [x] 傩 (NUO) API 命名空间 + 独立 schema (ADR-012)
- [x] Typer CLI (kun serve / run / calibrate / rules)
- [x] 6 个校准任务 (ADR-011)
- [x] 2-5 个 starter skills (ADR-014)
- [x] Experiments SDK + 状态机 (ADR-009)
- [x] ConcurrencySafety (IdempotencyKey + ResourceGuard, ADR-018 §16.5)
- [x] ValidationPipeline + SingleJudge + MultiJudge + DebateValidator (ADR-018 §16.2)
- [x] NotificationLayer (ADR-018 §16.3)
- [x] Prometheus metrics (ADR-016)
- [x] Next.js 前端 scaffold (主对话框 + 傩面板)
- [x] GitHub Actions CI (lint / typecheck / unit / integration / license-scan)
- [x] Skill loader (Anthropic 兼容 SKILL.md) + SkillSelector (§2.1)
- [x] Orchestrator 完整 pipeline: intent → plan → route → skill-select → execute → validate → writeback
- [x] Capability card writeback (运行时) + surprise_score EMA
- [x] Context storage: InMemoryAssetStore + RedisAssetStore
- [x] idle-batch scheduler + 7 个 step 类型 (placeholder + real health_report)
- [x] Dockerfile (multi-stage + healthcheck) + Makefile (dev 目标) + .dockerignore
- [x] 90 unit + integration tests passing

### ⏳ 进行中 / 待完善

- [ ] 跑起真的 docker compose, 验证 Orchestrator 端到端 (需要 .env 里的 API key)
- [ ] 接入真实 Anthropic / OpenAI / MiniMax (用户本地提供 API key)
- [ ] 冷启动 Starter Pack 填充到 50-80 个 skill (ADR-014)
- [ ] Grafana dashboard JSON 定义
- [ ] 前端装 node_modules + 验证 npm run dev
- [ ] idle-batch 的 task_replay / methodology_distill 实装 (当前 placeholder)
- [ ] Qdrant 向量检索接入 (Context 子系统的真实后端)
- [ ] 接入 MCP / A2A 协议
- [ ] testcontainers 集成测试 (需 Docker 运行)

### 🔬 当前可验证的能力

```bash
# 跑所有测试
uv run pytest tests/ -q
# → 85 passed

# 列出加载的规则
uv run kun rules
# → 4 YAML 规则

# 列出加载的 skills
uv run kun skills
# → 5 starter skills

# 端到端跑 (stub providers, 不需要外部 API)
uv run kun run "Say hi to the world"
# → thinking → action_plan → action → cost_tick → answer → done

# 跑一次 idle-batch
uv run kun idle-batch --only health_report
# → 健康报告

# 跑校准任务集
uv run kun calibrate --type role_template --id rt-default
# → 6 个校准任务的结果表
```

### 📊 当前代码规模

| 模块 | 行数 / 数量 |
|------|-----:|
| kun/ (Python) | 6714 |
| tests/ | 1282 |
| rules/ | 4 YAML |
| skills/ | 5 starter |
| frontend/ | Next.js scaffold (dialog + NUO) |
| alembic/ | 2 migrations |
| ADR + 方案 | decisions.md (ADR-001 ~ 018) + KUN-V1.md |

---

## M2 · 能自己评估 (pending)

**交付标志**: 系统能自动评估任务成败并按规则升级处理, 能力卡进入 warming_up.

### 待做

- 将 ValidationPipeline 挂到 Orchestrator 事后阶段
- 能力卡回写逻辑接入每个任务完成
- 熔断规则细化 (ADR-015 surprise_score 公式)
- 分级自治 4 级执行链路 (§6.2)

---

## M3 · 能自己进化 (pending)

**交付标志**: idle-batch 跑完一轮后能自动优化路由和 skill, 并有可读的进化报告.

### 待做

- idle-batch 调度器 (dispatcher 骨架)
- 夜间任务回放 + AB 决策汇总
- 路由规律涌现发现 (聚类 + 关联规则)
- 方法论蒸馏 pipeline
- 进化验收三层门

---

## M4 · 有个好看的壳 (pending)

### 待做

- 节点图编辑 (React Flow, 第 2 层交互)
- 深度编辑 / 版本历史 (第 3 层)
- 透明化三层报告 (实时 / idle-batch / 周月)
- 协作编排器 (人作为协作实体)
- 翻译适配器层
- 故障转移 (多供应商自动切换)

---

## M5 · 持续进化 (pending)

### 待做

- 路由引擎自进化回路
- 双语 UI 切换
- 红队测试稳定通过
- 多租户从 hardcoded 切到 auth-resolved
