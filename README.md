# 鲲 · KUN

> Agent OS / Agent 管家，向自运营 agent 协作调度平台演进。

权威文档：
- [`KUN-V1.md`](./KUN-V1.md) — 开发方案
- [`decisions.md`](./decisions.md) — ADR 决策记录（权威）

## Quickstart

```bash
# 1. 安装 uv (如未安装)
brew install uv

# 2. 装依赖
make install     # 等价于 uv sync --dev

# 3. 配置环境
cp .env.example .env
# 编辑 .env 填入 KUN_OFOX_API_KEY / MINIMAX_API_KEY 等

# 4. 拉起基础设施 (Postgres/Redis/Qdrant/NATS/MinIO/OTel/Jaeger/Prometheus/Grafana/Loki)
make up

# 5. 跑数据库迁移
make migrate

# 6. 起 API 服务
make serve

# 或直接
uv run kun serve --reload
```

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
- Grafana: http://localhost:3001

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
