# KUN Ops Runbook

> 部署 + 故障排查 + 应急处置. 给 ops/oncall 用.

## 目录

- [快速启动](#快速启动)
- [生产部署](#生产部署)
- [常见故障](#常见故障)
- [Watchtower alerts 应急处置](#watchtower-alerts-应急处置)
- [LLM provider 切换](#llm-provider-切换)
- [Lab dogfood 验证](#lab-dogfood-验证)
- [Rollback 流程](#rollback-流程)

---

## 快速启动

### 本地 dev

```bash
./scripts/bootstrap.sh   # 一键: uv sync + docker postgres + alembic + tests + serve
```

### 验证 healthz

```bash
curl http://localhost:8000/healthz
# 期望: {"status": "ok", "version": "..."}
```

### 跑一次 task

```bash
curl -X POST http://localhost:8000/api/chat/run \
  -H "Content-Type: application/json" \
  -H "X-User-Id: u-test" \
  -d '{"message":"hello"}'
```

---

## 生产部署

### 必备 env (docker-compose.prod.yml 注入)

```bash
# DB
DATABASE_URL=postgresql+asyncpg://kun:secret@postgres:5432/kun
KUN_ENV=production

# NATS
NATS_URL=nats://nats:4222

# LLM provider (优先级: claude-cli > anthropic api > minimax > stub)
KUN_OFOX_API_KEY=...        # 或
ANTHROPIC_API_KEY=sk-ant-...
OPENAI_API_KEY=sk-...        # for codex
MINIMAX_API_KEY=...          # 兜底

# V2.2 wire 开关 (默认开)
KUN_HERMES_ENABLED=1                    # Wire 11/31/32/33
KUN_HERMES_CONSISTENCY_THRESHOLD=0.5    # Wire 35
KUN_HERMES_MAX_RETHINKS=2               # Wire 35
KUN_VERIFICATION_ENABLED=1              # Wire 36
KUN_VALUE_GATE_ENABLED=1                # V2.2 §19
KUN_STRATEGY_MATCHER_ENABLED=0          # V2.1 §17 (opt-in, prod 默认 off)
KUN_LAB_MODE=0                          # KUN-Lab 跑实验 (默认 off, 走 lab CLI 时手动 export)
KUN_LAB_BRIDGE_ENABLED=0                # 主仓库消费 lab recipe (默认 off, 第 1 周生产观察后再开)

# 任务超时 (默认 30min)
KUN_TASK_MAX_DURATION_SEC=1800

# 多租户 (production 必须显式, dev 可设 default)
KUN_DEFAULT_TENANT_ID=                  # production 必须空
```

### 启动顺序

```bash
# 1. Postgres + NATS 起来
docker compose -f docker-compose.prod.yml up -d postgres nats

# 2. alembic 迁移到 head
docker compose run --rm api uv run alembic upgrade head

# 3. 启 API (含 install_runtime 装 hermes/verification/lab bridge etc.)
docker compose -f docker-compose.prod.yml up -d api

# 4. 启 outbox_worker (events bus → NATS publish)
docker compose -f docker-compose.prod.yml up -d outbox_worker

# 5. 启 idle_batch_worker (cron 跑 7 个 step)
docker compose -f docker-compose.prod.yml up -d idle_batch

# 6. 监控 (Prometheus + Grafana)
docker compose -f docker-compose.prod.yml up -d prometheus grafana
```

### Grafana dashboards (auto provision via docker volume)

```yaml
# kun/infra/grafana-datasources.yml — Prometheus datasource
# kun/infra/grafana-dashboards-provision.yaml — codex C35 BATCH9
# - kun-lab dashboard (V2.2 §26, 8 panel)
# - knowledge-graph dashboard (V2.2 §20, 6 panel — codex C39 #56)
```

打开 http://localhost:3000 (admin/admin), 看面板.

---

## 常见故障

### 1. `Event loop is closed` / asyncpg cancel warning

**症状**: 测试或 prod 偶发 `RuntimeWarning: coroutine 'Connection._cancel' was never awaited`.

**原因**: asyncpg pool 在 event loop 关闭后仍 try 清理 connection. 已知问题, 不影响功能.

**处置**: 忽略 warning. 如果是测试间事件循环冲突, 检查 fixture 是否 reset event loop (e.g. `tests/unit/test_lab_cli.py::_isolate_lab_state`).

### 2. LLM provider unavailable

**症状**: `router.invoke` log "primary_failed", 然后 `fallback_engaged`.

**处置**:
- 看 `kun_llm_fallback_total{from_provider, to_provider, reason}` metric — 哪条 fallback 触发频繁
- 看 `kun_llm_request_total{provider, model}` — 是否真切到 fallback (MiniMax / Stub)
- 如 Anthropic API 配额耗尽 → 看 `kun.core.quota_tracker` 日志, 等 5h rolling window 重置
- 如 ChatGPT subscription 过期 → 重新 login claude-code CLI: `claude login`

### 3. Task timed out

**症状**: `task.timed_out` event + `error: task_timed_out`.

**处置**:
- 默认 1800s (30min). 超过应该是真有问题.
- 看 task 的 step_records — 哪个 step 慢了? (Grafana span: `kun.orchestrator.step`)
- 如某个 LLM provider 慢 → 看 `kun_llm_latency_seconds_bucket{provider, model}` p95
- 临时延长: TaskProfile.max_duration_sec 或 KUN_TASK_MAX_DURATION_SEC

### 4. Verification failed (Wire 36)

**症状**: task 状态是 `failed` 而不是 `done`, OrchestratorEvent kind=`verification_done` failed=True.

**处置**:
- 看 `verification_done.results` — 哪个 spec 失败
- 如是 `exact_output` mismatch → 用户 LLM 答案不匹配 spec, 检查 spec 是否合理
- 如是 `test_pass` 命令失败 → 看 stderr (PR diff log)
- 如是 `human_approval` pending → 用户没 approve, 走 NUO action panel
- 临时禁用: `KUN_VERIFICATION_ENABLED=0` (不推荐, 失去防护)

### 5. KUN-Lab budget cap 触发频繁 (Wire 27)

**症状**: `kun_lab_budget_cap_total{task_type}` rate > 0.3/h, OR Watchtower alert "lab_budget_cap_spike" 触发.

**处置**:
- 看是否最近改了 invoker 让单 path cost 变大
- 看 EnsembleConfig.n_paths 是否需要降 (e.g. 5 → 3)
- 看 EnsembleConfig.cost_budget_total_usd 是否需要调高 (lab 单独预算)
- 真 ops 层面: 查 `kun_lab_path_total{strategy, status=cancelled}` — 哪 path strategy 总被 cap

### 6. Hermes rethink loop (Wire 35) 频繁触发

**症状**: 多个 task 的 `hermes_step.rethink_count > 0`, latency 增加.

**处置**:
- 看 `KUN_HERMES_CONSISTENCY_THRESHOLD` (默认 0.5) — 太高 → 触发频繁
- 看 ThoughtActionConsistency._EXPECTED_KEYWORDS — keyword 是否合理 (中文 / 英文 cover)
- 临时降阈值: `KUN_HERMES_CONSISTENCY_THRESHOLD=0.3`
- 关掉 rethink: `KUN_HERMES_MAX_RETHINKS=0`

### 7. ValueGate 频繁 stop / escalate (Wire 2 / V2.2 §19)

**症状**: `value_gate_intervention` event 频, task 大量 paused/done early.

**处置**:
- 看 `gate_decision.expected_value` — production estimator 是否 too pessimistic
- 看 `kun_watchtower_intervention_total{rule_id=value_gate}` 趋势
- 临时禁用: `KUN_VALUE_GATE_ENABLED=0`
- 调阈值: ValueGate.min_value_threshold (默认 0.20)

### 8. Lab recipe 推到 main 导致 ExecutionMode 决策抖动 (Wire 25/26)

**症状**: 用户报告同 task_type 的 mode 决策不稳 (一会 FAST 一会 MAX).

**处置**:
- 看 `kun_lab_promotion_total{task_type, target_module}` — promote 是否过频
- 看 `kun_lab_registry_size` 是否飞涨
- 临时关 lab → main 桥: `KUN_LAB_BRIDGE_ENABLED=0`
- 看 RecipePromoter min_total / min_winrate 阈值是否需要调严

---

## Watchtower alerts 应急处置

每条 watchtower rule 触发时的 SOP:

### `cost_runaway` (rules/guard/cost_runaway.yaml)

任务 cost 超 estimated × 1.2.

1. 看 task_id + accumulated_cost
2. 决定: pause + 问用户 (默认) / 强制停 / 调高 estimated
3. 长期: 看 `kun_llm_cost_runaway_total{tenant_id}` 找 chronic 超预算 task_type

### `cross_tenant_attempt` (rules/guard/cross_tenant_attempt.yaml)

跨 tenant 访问尝试 — CRITICAL.

1. 立即看 `tenant_cross_access_attempt_total{from_tenant, to_tenant}`
2. log 全调用栈 (找 source code path)
3. revoke 涉及 user_id 权限
4. post-mortem: 改 RLS policy / 修代码 bug

### `llm_fallback_spike` (rules/anomaly/llm_fallback_spike.yaml)

10min rolling fallback rate > 10%.

1. 看 from_provider / to_provider / reason
2. 主 provider 挂了? 看 status page
3. quota 耗尽? 等 reset 或换 provider
4. 网络问题? curl 测试 Anthropic / OpenAI endpoint

### `lab_budget_cap_spike` (rules/anomaly/lab_budget_cap_spike.yaml)

Lab Wire 27 cap 触发 (1h rolling > 30%).

详见上面 "5. KUN-Lab budget cap 触发频繁".

### `lab_recipe_promotion_burst` (rules/anomaly/lab_recipe_promotion_burst.yaml)

5min rolling > 5 个 recipe 推送.

详见上面 "8. Lab recipe 推到 main".

---

## LLM provider 切换

### 临时切到 fallback (MiniMax)

```bash
# 关 Anthropic + Codex CLI, 让 MiniMax 兜
export KUN_DISABLE_CLI_OAUTH=1
unset ANTHROPIC_API_KEY
# MINIMAX_API_KEY 必须有
```

### 临时切到 Stub (无外部依赖, 测试场景)

```bash
unset ANTHROPIC_API_KEY OPENAI_API_KEY MINIMAX_API_KEY KUN_OFOX_API_KEY
export KUN_DISABLE_CLI_OAUTH=1
```

### 切回 production 主链

```bash
# 用户 OAuth subscription 优先
unset KUN_DISABLE_CLI_OAUTH
# 验证 claude-code CLI 登录
claude --version
```

---

## Lab dogfood 验证

跑一次完整闭环验证 (V2.2 §26):

```bash
./scripts/dogfood_run.sh
```

详见 `docs/ops/dogfood-checklist.md`.

---

## Rollback 流程

### 单 PR rollback

```bash
git revert <commit-sha>
git push origin feat/v2.1-foundation
```

### 紧急关 V2.2 wire

每个 wire 都有 env 开关:

```bash
# 关 hermes (Wire 11/31/32/33/35) → 走 V2.1 single-step
export KUN_HERMES_ENABLED=0

# 关 verification (Wire 36) → 跳过 verification, 直接 mark done
export KUN_VERIFICATION_ENABLED=0

# 关 ValueGate (V2.2 §19) → 不主动 stop step
export KUN_VALUE_GATE_ENABLED=0

# 关 KUN-Lab 主仓库消费 (Wire 26)
export KUN_LAB_BRIDGE_ENABLED=0

# 关 KUN-Lab 跑 (Wire 19+)
export KUN_LAB_MODE=0
```

重启 API 后生效.

### alembic rollback

```bash
# 看当前 migration
uv run alembic current

# downgrade 一步
uv run alembic downgrade -1

# downgrade 到指定 revision
uv run alembic downgrade <rev>
```

⚠️ 如果有 codex BATCH9 C36 (alembic 0013_lab_adoption_cursor) 要 rollback: 同时改 `kun/lab/cursor_storage.py:_CREATE_TABLE_SQL` 重新启用 self-managed 路径.

### 全量 rollback (灾难)

```bash
# 1. checkout 上一个稳定 tag
git checkout v2.1.0   # 假设 tag 已打

# 2. alembic downgrade 到 v2.1.0 对应 revision
uv run alembic downgrade <pre-v2.2-rev>

# 3. 重启
docker compose -f docker-compose.prod.yml up -d
```

---

## 联系人

- Claude (心脏 / wire / V2.2 spec): GitHub issues
- codex (周边模块 / BATCH 9-11 任务): GitHub issues
- 用户 (产品决策 / V2.3 方向): @petrarain

## 参考

- `docs/v2/V2.2-implementation-audit.md` — V2.2 实装状态全图
- `docs/v2/KUN-V2.2-revisions.md` — V2.2 spec 原文
- `docs/PROMISES.md` — 历史承诺清单 (Y/Z 节)
- `docs/ops/dogfood-checklist.md` — dogfood 验证清单
- `docs/codex/BATCH*.md` — codex 协作 brief
