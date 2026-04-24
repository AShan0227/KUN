# Claude Review Packet

给 Claude 审核用。审核范围建议直接看：

```bash
git diff origin/main...HEAD
git log --oneline origin/main..HEAD
```

当前要求：先审查，不合并，不推送。

## Review 后补丁

Claude 初审后点了 5 个问题。本分支已继续补了其中 3 个合并前应处理项：

1. **R-1 幂等 crash 窗口**
   - 新任务创建时，同事务写入初始 `RuntimeState(status='queued')`，避免只有 `TaskRow` 没有 runtime 的孤儿窗口。
   - 重复请求如果发现历史任务没有 cached result 且 runtime 缺失/陈旧 queued，会标记为 `failed` 并持久化结果，避免永远显示“在跑”。
   - 测试：`tests/unit/test_orchestrator_offline.py::test_orchestrator_duplicate_orphan_is_marked_failed`

2. **R-2 API 端口对齐**
   - API 默认端口、前端 rewrite fallback、`.env.example`、README/部署文档统一回 `8000`。
   - 这样和远端 main / Dockerfile / frontend 默认保持一致，减少 merge 后本地体验问题。

3. **R-5 pending action 审批 race**
   - `decide_pending_action()` 改成 `UPDATE ... WHERE status='pending_approval' RETURNING ...`。
   - 并发双击/重复审批时，只有一个请求能成功，其余返回 409。
   - approve 响应里明确说明：当前只是标记 approved，等待 side-effect executor，任务不会自动 resume。

## 这轮解决了什么

这批改动是围绕最早那 12 个风险点做的收口，目标是让 KUN 现在的后端护栏从“看起来有”变成“真的生效”。

### 1. Watchtower 规则真正生效

问题：API 启动时加载了规则，但真实 HTTP/WebSocket 请求用的是另一个空规则 `Orchestrator`。

已改：
- `kun/api/main.py` 在 lifespan 里加载 `RuleEngine`。
- `kun/api/runtime.py` 安装共享 `Orchestrator` 到 `app.state`。
- HTTP 和 WebSocket 都通过共享 runtime 取 orchestrator。

请重点看：
- `kun/api/runtime.py`
- `kun/api/main.py`
- `kun/api/chat.py`
- `kun/api/ws.py`

### 2. 重复请求不再 500

问题：幂等逻辑只查 `idempotency_keys`，但没有稳定写入/复用结果；重复请求会撞 `tasks(tenant_id, fingerprint)` 唯一约束。

已改：
- 新增 `task_results` 持久化最终结果。
- 重复请求优先返回旧 `TaskResult`。
- 创建 task 时写入 `idempotency_keys`。
- 如果历史 task 已存在但结果还没完成，会返回 “already running” 类的可解释响应，而不是 500。

请重点看：
- `kun/engineering/orchestrator.py`
- `alembic/versions/0004_task_results.py`

### 3. Outbox 多 worker 不重复发布

问题：多 worker 同时查 `published_at IS NULL` 会拿到同一批事件。

已改：
- outbox 查询改成 `FOR UPDATE SKIP LOCKED`。
- 发布后同事务标记 `published_at`。
- 单测检查 SQL 里包含 `SKIP LOCKED`。

请重点看：
- `kun/core/events.py`
- `tests/unit/test_events.py`

### 4. NATS 晚启动可以恢复

问题：worker 启动时连不上 NATS 后，会一直拿着 `None` 跑。

已改：
- outbox worker 每轮需要发布时都会尝试连接/重连。
- NATS 不可用时事件留在 outbox，NATS 恢复后继续发布。

请重点看：
- `kun/core/events.py`
- `tests/unit/test_events.py`

### 5. Capability writeback 并发更安全

问题：原来是读 JSON、改 JSON、写回；多个任务同时更新同一能力卡会丢更新或撞唯一约束。

已改：
- 已存在的能力卡使用 `SELECT ... FOR UPDATE` 行锁。
- 首次创建冲突时捕获 `IntegrityError` 并重试。
- RLS 开启后，`record_outcome(tenant_id, ...)` 显式把 `tenant_id` 传进 `session_scope`，避免 ambient tenant 不一致。

请重点看：
- `kun/engineering/capability_writeback.py`
- `tests/unit/test_capability_writeback_math.py`

### 6. RLS 从声明变成真实数据库隔离

问题：ORM 注释说有 RLS，但迁移没有安装 policy；而且本地 app 连接使用 superuser 会天然绕过 RLS。

已改：
- 新增 `0006_enable_rls.py`：对租户表启用并强制 RLS。
- 新增 `0007_app_role_rls.py`：创建/收紧 app DB role，默认 `kun_app`，并授予必要权限。
- 应用运行时默认 `KUN_PG_DSN=kun_app`。
- Alembic / 系统 worker 使用 `KUN_PG_ADMIN_DSN`。
- `session_scope()` 每次事务开始时设置 `app.tenant_id`。
- `/health/ready` 会检查 app role 是否是 superuser 或 BYPASSRLS，如果是就 degraded。

真实烟测结果：
- `kun_app` 不是 superuser，也不是 BYPASSRLS。
- tenant A 只能看到 tenant A 的测试 task。
- admin 能看到所有 tenant，用于系统 worker 和清理。

请重点看：
- `kun/core/db.py`
- `kun/core/config.py`
- `kun/api/health.py`
- `alembic/env.py`
- `alembic/versions/0006_enable_rls.py`
- `alembic/versions/0007_app_role_rls.py`
- `tests/unit/test_db_rls_context.py`

### 7. 类型检查和前端 lint 可以当护栏

问题：
- `uv run mypy kun` 原来有一批错误。
- 前端 `npm run lint` 原来会进入 Next 交互式配置，CI 会卡住。

已改：
- 后端 mypy 已清到 0。
- 前端迁到 ESLint CLI：`frontend/package.json` 里 `lint` 是 `eslint .`。

验证命令见下方。

### 8. NUO 面板按 tenant 过滤

问题：NUO 健康面板 outbox lag 原来是全局未发布事件数，多租户后会串视图。

已改：
- `events_outbox_lag`、任务数、pending actions 都按当前 tenant 过滤。

请重点看：
- `kun/api/nuo/health_panel.py`

### 9. WebSocket interrupt 真取消任务

问题：用户点停，后端只回 “已中断”，真实任务还在跑。

已改：
- 每个 WS 会话维护当前 `asyncio.Task`。
- `interrupt` 会 cancel 并等待 cancellation 被观察到。
- `correction` 会先 cancel 当前任务再启动新任务。

请重点看：
- `kun/api/ws.py`
- `tests/unit/test_ws_runtime.py`

### 10. 执行 prompt 信息更完整

问题：执行模型原来主要拿到 `success_criteria_short`，原始目标、约束、工具、风险、回退方案丢太多。

已改：
- 执行 prompt 现在带 TASK.md L1/L2 关键信息。
- 多步骤任务会带最近 3 步输出摘要。

请重点看：
- `kun/engineering/orchestrator.py`
- `tests/unit/test_orchestrator_prompt.py`

### 11. 事前冲突和高风险动作左移

已改：
- 新增 pending side-effect action queue。
- 对外发、删除、支付等高风险动作进入 pending approval。
- 任务启动前做资源冲突扫描。
- NUO 有 pending action 查询和 approve/reject/cancel 接口。

请重点看：
- `kun/engineering/concurrency.py`
- `kun/engineering/orchestrator.py`
- `kun/api/nuo/action_panel.py`
- `alembic/versions/0005_pending_actions.py`

### 12. 数据库约束进一步加固

已改：
- 已有：task risk/status、runtime status、experiment rollout、pending action status/risk、task result status。
- 新增 `0008_harden_core_constraints.py`：
  - 成本、时长、token、失败次数不能为负。
  - `surprise_score` / `overall_reliability` 范围必须合法。
  - capability entity_type / maturity 必须合法。
  - notification severity / channel 必须合法。
  - idempotency TTL 必须大于 0。

请重点看：
- `alembic/versions/0003_core_check_constraints.py`
- `alembic/versions/0004_task_results.py`
- `alembic/versions/0005_pending_actions.py`
- `alembic/versions/0008_harden_core_constraints.py`
- `kun/core/orm.py`

## 已跑验证

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy kun
uv run alembic upgrade head
uv run pytest tests/unit tests/integration -q
cd frontend && npm run lint && npm run typecheck -- --pretty false
```

另外做过一次真实 RLS 烟测：

- admin 插入 tenant A / tenant B 两条 task。
- app role 设置 `app.tenant_id=tenant A`。
- app role 只能查到 tenant A。
- admin 能查到 A/B 两条并清理。

## 还没做完的长期项

这些不是这轮“必须先改”的阻塞项，但 Claude 可以判断优先级：

1. **真实认证/授权还没有做完**
   - 现在租户主要来自 `X-Tenant-Id` 或 WS query。
   - RLS 能防 DB 串租户，但 API 层还需要真正的 authn/authz，不然 header/query 可以伪造。

2. **Pending action 目前是审批队列，不是完整执行器**
   - approve/reject/cancel 有了。
   - approved action 后续怎么安全执行外部副作用，还需要接入层执行器。
   - API 响应已明确提示 approve 不会自动 resume。

3. **HTTP 请求没有“用户主动取消”机制**
   - WebSocket interrupt 已经真 cancel。
   - HTTP `/api/chat` 是一次请求/响应，目前没有外部取消任务 API。

4. **Outbox/NATS 还缺真实多进程压测**
   - 代码层用了 `FOR UPDATE SKIP LOCKED`。
   - 单测检查 SQL 和重连逻辑。
   - 还需要 Docker/CI 下开两个 worker 做真实重复发布压测。

5. **Capability writeback 并发还缺真实 DB 竞争测试**
   - 行锁 + first-write retry 已有。
   - 还缺两个真实 async session 并发写同一 card 的集成测试。

6. **RLS app role 密码生产管理要接 Secret Manager**
   - 当前 `.env.example` 默认是本地开发密码 `kun_app`。
   - 生产必须由 secrets 注入，不要使用默认密码。

7. **pytest 还有一个 asyncpg cleanup warning**
   - 测试是绿的，但会出现 `coroutine 'Connection._cancel' was never awaited` warning。
   - 不影响当前功能，但后面最好查清楚连接关闭路径。

8. **CI 工作流还需要正式配置**
   - 本地命令都能跑。
   - GitHub Actions 里应该固定跑 backend + frontend + alembic migration + RLS smoke。

## Claude 审查建议

请优先审查这些点：

1. RLS 策略是否有绕过路径，特别是 `bypass_rls=True` 是否只在系统 worker 使用。
2. Alembic 0006/0007/0008 的 upgrade/downgrade 是否安全。
3. `session_scope()` 每次事务设置 `app.tenant_id` 是否足够，连接池复用是否会串。
4. `record_outcome()` 的行锁 + retry 是否足够防止能力卡丢更新。
5. `Orchestrator` 的幂等路径是否还有重复请求状态不一致的边界。
6. pending side-effect action 的“暂停而不是执行”是否符合产品预期。
7. 前端/NUO 面板是否有租户视图遗漏。
