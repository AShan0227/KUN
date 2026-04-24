# 另一台电脑上部署 KUN

> 本文件说明从零开始在一台新 Mac/Linux 上拉下仓库、配上依赖、让 KUN 跑起来.

---

## 前置条件

| 工具 | 最低版本 | 安装 |
|------|---------|------|
| Docker Desktop | 25+ | https://www.docker.com/products/docker-desktop |
| `uv` (Python 工具链) | 0.7+ | `brew install uv` 或 `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| `gh` (GitHub CLI, 可选) | 2.80+ | `brew install gh` |
| Python | 3.13 自动由 uv 拉 | — |
| Node.js (可选, 前端才需要) | 20+ | `brew install node` 或 nvm |

---

## 步骤

### 1. 克隆仓库

```bash
# HTTPS
git clone https://github.com/AShan0227/KUN.git
cd KUN

# 或 SSH (需要配过 SSH key)
git clone git@github.com:AShan0227/KUN.git
```

### 2. 一键 bootstrap

```bash
./scripts/bootstrap.sh
```

该脚本会:
1. 检查 `uv` + `docker`
2. `cp .env.example .env` (只在 .env 不存在时)
3. `uv sync --extra dev`
4. `docker compose -f docker-compose.dev.yml up -d` 起 10 个容器
5. 等 postgres 就绪
6. `uv run alembic upgrade head` 建表
7. 跑单测

> 已有旧 `.env` 的机器注意：Postgres 运行时账号已经切到非超级用户 `kun_app`，否则 RLS 会被 superuser 绕过。确认 `.env` 里是：
>
> ```bash
> KUN_PG_DSN=postgresql+asyncpg://kun_app:kun_app@localhost:55432/kun
> KUN_PG_ADMIN_DSN=postgresql+asyncpg://kun:kun@localhost:55432/kun
> ```

### 3. 配置 LLM

**推荐: CLI OAuth (无需 API key, 走订阅)**

KUN 默认优先尝试两个 CLI OAuth 会话:

```bash
# a) Claude Code CLI (Anthropic 订阅 → Opus/Sonnet/Haiku)
claude --version             # 确认已安装 (应 ≥ 2.1)
claude /login                # 交互登录, 浏览器点一下

# b) Codex CLI (ChatGPT 订阅 → GPT-5 / GPT-5.5 for coding)
codex --version              # 确认已安装 (应 ≥ 0.111)
codex login                  # 交互登录, 浏览器点一下
# 如果之前登录过但 refresh token 失效, 也跑这个 — 会自动刷新
codex login status           # 应看到 "Logged in using ChatGPT"
```

登进去之后什么 key 都不用填, 直接跑 KUN. 每次调用真实开销 = **订阅 $0** (成本口径:
`cost_usd_actual=0` / `cost_usd_equivalent=<CLI 报告的等效 API $>`, 方便透明化展示).

**补充: MiniMax API (fallback / 中国直连)**

```bash
# 在 .env 里填:
MINIMAX_API_KEY=sk-cp-...
MINIMAX_API_URL=https://api.minimax.chat/v1
MINIMAX_MODEL=MiniMax-M2.7
```

**完整路由优先级** (kun/interface/llm/router.py, 2026-04-24 版):

```
top / strong / cheap:
  1. Claude Code CLI (OAuth 订阅) ← 当前默认
  2. Anthropic API (KUN_OFOX_API_KEY / ANTHROPIC_API_KEY)
  3. MiniMax 替代 (MINIMAX_API_KEY)
  4. Stub (测试)

coding:
  1. Codex CLI (OAuth ChatGPT 订阅) ← 当前默认
  2. OpenAI API
  3. Claude Code CLI fallback
  4. MiniMax 替代
  5. Stub

fallback (主链失败时):
  1. MiniMax (直连 API)
  2. Stub
```

设 `KUN_DISABLE_CLI_OAUTH=1` 可强制跳过 CLI 探测. 所有档位都缺时一切走 Stub (确定性, 适合无网测试).

### 4. 起服务

```bash
make serve    # API @ :8000
```

访问:
- http://localhost:8000 — API root
- http://localhost:8000/docs — OpenAPI
- http://localhost:8000/health/ready — 依赖健康
- ws://localhost:8000/ws — 对话 WebSocket
- http://localhost:8000/nuo/health/summary — 傩健康面板 JSON
- http://localhost:3011 — Grafana (admin/admin)
- http://localhost:16686 — Jaeger (traces)
- http://localhost:9090 — Prometheus
- http://localhost:19001 — MinIO console (minio/minio123)

### 5. 起前端 (可选, 傩管家 UI)

```bash
cd frontend
npm install
npm run dev     # @ :3000
```

打开 http://localhost:3000 进对话框, http://localhost:3000/nuo 看管家.

### 6. 试跑

```bash
# CLI 端到端 smoke (不需要前端)
uv run kun run "用一句中文介绍你自己"

# HTTP API
curl -sS -X POST http://localhost:8000/api/chat/run \
  -H 'Content-Type: application/json' \
  -d '{"message":"用一个成语形容目标远大"}' | jq

# 跑一次 idle-batch (用户闲置时的后台任务)
uv run kun idle-batch --only health_report
```

---

## 端口映射

为避免和 Genesis / dreamapp 的旧容器冲突, KUN 的 host 端口全部加 10000-ish 偏移:

| 服务 | KUN host 端口 | 容器内端口 |
|------|-------|-------|
| Postgres | 55432 | 5432 |
| Redis | 6379 | 6379 (不冲突, 保持原值) |
| Qdrant HTTP | 16333 | 6333 |
| Qdrant gRPC | 16334 | 6334 |
| NATS client | 4222 | 4222 |
| NATS mon | 8222 | 8222 |
| MinIO S3 | 19000 | 9000 |
| MinIO console | 19001 | 9001 |
| OTel gRPC | 14317 | 4317 |
| OTel HTTP | 14318 | 4318 |
| Prometheus | 9090 | 9090 |
| Jaeger UI | 16686 | 16686 |
| Loki | 3100 | 3100 |
| Grafana | 3011 | 3000 |
| **KUN API** | **8000** | 8000 |

如果另一台电脑没跑 Genesis/dreamapp, 这些 host 端口可以改回默认 (编辑 `docker-compose.dev.yml` + `.env`).

---

## 做 PR 的流程

```bash
# 1. 创建分支
git switch -c feat/your-change

# 2. 改代码 / 加测试
# ...

# 3. 本地验证
make test       # 单测
make lint       # ruff
make format     # 自动修复

# 4. 提交
git add -A
git commit -m "feat: your change"

# 5. 推分支
git push origin feat/your-change

# 6. 开 PR
gh pr create --fill
```

---

## CI 工作流

`scripts/push-workflow.sh` 可以补装 GitHub Actions 工作流 (初次推送时因 OAuth scope 缺失被 strip 了).

```bash
./scripts/push-workflow.sh
```

该脚本会让你在浏览器里给 `gh` 加 `workflow` scope, 然后自动 commit + push `.github/workflows/ci.yml`.

CI 做的事:
- ruff check + format --check
- mypy (soft, 不阻断)
- pytest tests/unit
- pytest tests/integration (带 postgres/redis/nats)
- REUSE 许可证扫描 (skills/)

---

## 故障排查

| 症状 | 原因 | 解决 |
|------|------|------|
| `make up` 报 port already allocated | Genesis/dreamapp 占了同名端口 | 改 docker-compose.dev.yml 的 host 端口 |
| `make migrate` 报 connection refused | postgres 容器没起 | `docker compose -f docker-compose.dev.yml ps` 看状态; `docker compose logs postgres` 看日志 |
| `kun run` 报 401 Unauthorized (MiniMax) | API key 不对 | 确认 `.env` 里 MINIMAX_API_KEY 填对 |
| `kun run` 用到了 claude CLI 但报 "Not logged in" | Claude Code OAuth 会话过期 | `claude /login` 重登 |
| `kun run` 用到了 codex CLI 但报 "refresh_token_reused" | Codex OAuth refresh token 失效 | `codex login` 重登 (浏览器点一下) |
| `kun run` 一直走 stub | 没配任何 LLM 源 | 登 claude CLI, 或登 codex CLI, 或填 `.env` 里的 key |
| CI 推不上去 (OAuth scope) | gh 没 workflow scope | `./scripts/push-workflow.sh` 或 `gh auth refresh -s workflow` |

---

## 两台机器之间同步

假设你在 Mac A 开发, Mac B 部署/做 PR:

```bash
# Mac A → push
git push origin feat/xxx

# Mac B → pull
git pull --rebase
```

**不要把 `.env` commit**. 各机器各自填 API key.

数据 (Postgres / Qdrant / MinIO) 默认在 Docker volume 里, 不跨机. 想同步: 用 `pg_dump` 导出再 `pg_restore`, 或直接 `docker cp` volume.
