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

### 上线前硬检查

```bash
uv run kun ops preflight
uv run kun ops preflight --json
uv run kun ops secret-audit
uv run kun ops secret-audit --json
uv run kun ops readiness --skip-alembic
uv run kun ops readiness --include-dogfood --skip-alembic
```

它检查的是“能不能安全上线”，不是产品宣传：

- production 下默认租户必须为空，不能静默落到 `u-sylvan`
- `KUN_AUTH_SECRET` 或 `KUN_AUTH_SECRETS` 必须至少包含一个足够长的 secret
- `KUN_PG_DSN` 必须用 `kun_app` 这种非管理员应用账号
- S3/MinIO 不能用默认密钥
- `secret-audit` 会展开看默认数据库密码、单密钥轮换风险、半启用外部 handler、企业 API 认证头缺失
- alembic 必须单 head
- 备份脚本和恢复 smoke 脚本必须存在
- delivery status 不能把没接入主流程的能力标成 ready

有 blocker 时命令返回非零退出码，CI/release 应该直接拦住。

`readiness` 是给正式测试前用的一条总命令：它把 preflight、secret-audit、delivery-status 汇总到一份报告；加 `--include-dogfood` 时再跑低风险 dogfood。

### 能力边界自检

```bash
uv run kun ops delivery-status
uv run kun ops delivery-status --json
uv run kun ops delivery-status --fail-on-not-ready
```

这条命令回答一个很朴素的问题：KUN 现在到底哪些能力真能承诺，哪些只是 partial，哪些还 not_ready。

`--fail-on-not-ready` 适合 release gate。现在它会失败是正常的，因为生产级部署和完整真实世界能力还没完成。

### 备份/恢复演练

```bash
uv run python scripts/backup_restore_drill.py create --output-dir backups
uv run python scripts/backup_restore_drill.py restore-dry-run backups/kun-backup-drill-*.manifest.json \
  --restore-root /tmp/kun-restore-dry-run
```

这条演练只验证本地关键配置/目录的“可打包 + manifest 可校验 + restore dry-run 能发现缺失/覆盖风险”。它会生成 tar.gz 和 manifest，记录文件数、sha256、时间和路径白名单；restore dry-run 不写文件、不覆盖目录。Postgres 真实数据仍按下面生产部署里的 `backup_postgres.sh` / `restore_postgres_smoke.sh` 单独演练。

### 租户启动包

生产模式不再信任裸 `X-Tenant-Id`。给一个租户发起 dogfood 前，先生成签名 token：

```bash
export KUN_AUTH_SECRET="至少 32 位的随机字符串"
# 轮换期也可以:
# export KUN_AUTH_SECRETS="新 secret,旧 secret"
uv run kun ops onboard-tenant \
  --tenant u-sylvan \
  --user petrarain \
  --scopes world:approve,world:dispatch \
  --output /tmp/kun-u-sylvan-onboarding.json
```

这只是当前阶段的安全启动包，不是完整账号体系。它会输出 Bearer token、smoke curl，以及还缺什么：注册登录、组织成员、账单、集中密钥轮换。

### V4 低风险 dogfood

```bash
uv run kun ops dogfood --tenant u-sylvan
uv run kun ops dogfood --tenant u-sylvan --json
# 显式多跑 Mission / RuntimeState / Orchestrator runner 的真实数据库续跑 smoke:
uv run kun ops dogfood --tenant u-sylvan --include-db-mission
```

这条命令只跑低风险、可重复的验收：

- preflight 能跑，且没有 blocker
- delivery status 没把半成品冒充 ready
- 租户启动 token 能验签
- WorldGateway 的低风险 `local_file.write` handler 可预览、可执行、可审计
- 关键边界仍然诚实暴露：生产级部署是 `not_ready`，长周期任务是 `partial`

它不是“鲲已经能自动运营公司”的验收。真正长周期 dogfood 要另起真实 Mission，用用户目标、预算、外部动作和复盘跑完整周期。

### MoE / 策略信用报告

```bash
uv run kun ops credit-report --tenant u-sylvan
uv run kun ops credit-report --tenant u-sylvan --kind skill --json
```

这条命令看的是 `resource_credit_stats`：哪些 memory、skill、model、decision、WorldGateway handler 真的在历史任务里拿到过贡献分。

它不是“自动证明策略已经最优”，只是把贡献数据露出来，方便傩、守望和后续 dogfood 校准路由阈值。

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
KUN_ENV=production
KUN_PG_DSN=postgresql+asyncpg://kun_app:<app-password>@postgres:5432/kun
KUN_PG_ADMIN_DSN=postgresql+asyncpg://kun_admin:<admin-password>@postgres:5432/kun

# NATS
KUN_NATS_URL=nats://nats:4222

# S3 / MinIO artifacts
KUN_S3_ENDPOINT=https://objects.example.com
KUN_S3_ACCESS_KEY=<object-store-access-key>
KUN_S3_SECRET_KEY=<object-store-secret-key>
KUN_S3_BUCKET=kun-artifacts

# LLM provider (默认: Codex MCP / GPT-5.5; Claude 只作为显式 opt-in)
KUN_LLM_PRIMARY=codex
KUN_CODEX_MCP_MODEL=gpt-5.5
KUN_DISABLE_CLAUDE_CLI=1
OPENAI_API_KEY=sk-...        # 可选 direct API fallback; ChatGPT 账号优先走 Codex MCP
KUN_OFOX_API_KEY=...         # 可选 Anthropic/OFOX fallback
ANTHROPIC_API_KEY=sk-ant-... # 可选 Anthropic fallback
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
KUN_AUTH_SECRET=<32+ random chars, do not commit> # production 必须有；API 不再信任裸 X-Tenant-Id
KUN_AUTH_SECRETS=new-secret,old-secret  # 可选；轮换期同时验多个 secret

# 低峰期 context/memory 瘦身
KUN_CONTEXT_MAINTENANCE_ENABLED=1
KUN_CONTEXT_MAINTENANCE_MUTATE=0        # 默认 dry-run；确认后再改 1
```

### 启动顺序

```bash
docker compose -f docker-compose.prod.yml up -d postgres nats

# 2. alembic 迁移到 head
docker compose run --rm api uv run alembic upgrade head

# 2.5 发布前做一次备份 + restore smoke
docker compose run --rm api scripts/backup_postgres.sh backups
KUN_RESTORE_TEST_DSN=postgresql://... scripts/restore_postgres_smoke.sh backups/kun-postgres-*.dump

# 3. 启 API (含 install_runtime 装 hermes/verification/lab bridge etc.)
docker compose -f docker-compose.prod.yml up -d api

# 4. 启 outbox_worker (events bus → NATS publish)
docker compose -f docker-compose.prod.yml up -d outbox_worker

# 5. 启 idle_batch_worker (低峰期 lane scheduler + anchor-expand 维护链)
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

### 2b. API 401 / 租户头不生效

**症状**: production 里带 `X-Tenant-Id` 还是 401。

**原因**: production 不再信任裸 header，必须用签名 Bearer token。  

**处置**:

```bash
export KUN_AUTH_SECRET=...
TOKEN="$(scripts/mint_auth_token.py --tenant u-sylvan --user sylvan --scopes world:approve,world:dispatch)"
curl -H "Authorization: Bearer $TOKEN" https://your-kun/api/missions
```

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
