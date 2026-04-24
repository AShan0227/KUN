# 鲲 · KUN

> Agent OS / Agent 管家，向自运营 agent 协作调度平台演进。

权威文档：
- [`KUN-V1.md`](./KUN-V1.md) — 开发方案
- [`decisions.md`](./decisions.md) — ADR 决策记录（权威）
- [`PROGRESS.md`](./PROGRESS.md) — 开发进度
- [`docs/DEPLOY.md`](./docs/DEPLOY.md) — 另一台机器如何部署 + 做 PR

GitHub: https://github.com/AShan0227/KUN (private)

## Quickstart

**一键启动** (首次):

```bash
brew install uv        # 如未安装
./scripts/bootstrap.sh # 检查 toolchain + uv sync + docker up + migrate + run tests
```

**手动步骤**:

```bash
make install     # uv sync --extra dev
cp .env.example .env && edit .env    # 填入 KUN_OFOX_API_KEY / MINIMAX_API_KEY
make up          # docker compose up -d (infra)
make migrate     # alembic upgrade head
make serve       # 启 API (autoreload)
```

Postgres 现在分两条连接：
- `KUN_PG_DSN`：应用运行时使用 `kun_app` 非超级用户，RLS 会真正生效。
- `KUN_PG_ADMIN_DSN`：Alembic 迁移和系统级后台任务使用 admin。

如果你已有旧 `.env`，确认把 `KUN_PG_DSN` 从 `kun:kun` 改成 `kun_app:kun_app`，并补上 `KUN_PG_ADMIN_DSN`。

### 常用命令

```bash
make test         # 跑所有测试
make lint         # ruff + format 检查
make format       # 自动修复格式
make rules        # 看加载的守望规则
make skills       # 看加载的 starter skills
make idle-batch   # 跑一次 idle-batch (health_report 子集)
make run-cli      # 对话框 CLI smoke
```

访问：
- API: http://localhost:8000
- Docs: http://localhost:8000/docs
- WebSocket dialog: `ws://localhost:8000/ws`
- Jaeger (traces): http://localhost:16686
- Prometheus: http://localhost:9090
- Grafana: http://localhost:3011

## 目录结构

```
kun/
├── core/              # 共享抽象 (ScoreDescriptor, TenantContext, ids, guard)
├── datamodel/         # Pydantic 模型 (TASK.md, CapabilityCard, Handoff, RuntimeState, Notification)
├── context/           # Context 子系统 (资产池, 压缩, 遗忘, 三级披露)
├── interface/         # 接入层 (LLM providers, MCP, A2A, 人类 UI)
├── engineering/       # 工程化子系统 (事前/事中/事后/跨阶段)
├── watchtower/        # 守望子系统 (规则引擎 + handler)
├── brain/             # 任务大脑 (意图/拆解/路由)
├── api/               # FastAPI + WebSocket
└── infra/             # OTel/Prometheus/Grafana 配置
rules/
├── guard/             # 守望干预规则
├── validation/        # 评估触发规则
├── ci/                # CI 护栏规则
└── anomaly/           # 异常检测规则
skills/                # Starter Pack (Anthropic 开源 skill + 自建)
tests/
├── unit/              # 单元测试
├── integration/       # 集成测试 (需 docker 起服务)
└── e2e/               # 端到端测试
alembic/               # DB 迁移
```

## 开发铁律

1. **效果 > 成本 > 速度**：不为省钱牺牲效果
2. **学习放在每一面**：任何"数据足够即可优化"的地方有自我进化回路
3. **影响面分档**：修改按 `.kun/ci-tiers.yaml` 自动分档护栏（ADR-013）
4. **决策留痕**：新抽象需要 ADR；冲突时 `decisions.md` 为准
5. **简洁优先**：新功能上线前过一遍"不加这个行不行"

## 许可证

Proprietary. Starter Pack 中引用的第三方 skill 保留原许可证，见 `skills/STARTER_PACK.md`。
