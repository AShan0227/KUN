# KUN-Lab dogfood 验证清单 (V2.2 §26 完整闭环)

> 给用户/ops 验证 V2.2 §26 KUN-Lab 真上线了 — Wire 19-37 全链路 working.

## 前置条件

- [ ] `./scripts/bootstrap.sh` 跑过, 测试全过 (1231+)
- [ ] Postgres 起来 (docker compose up postgres)
- [ ] alembic upgrade head 跑过
- [ ] LLM provider 配好 (claude-code CLI 或 ANTHROPIC_API_KEY 或 KUN_DISABLE_CLI_OAUTH=1 + StubProvider)

## 一键 dogfood

```bash
./scripts/dogfood_run.sh
```

不想连真实 LLM / DB 时，可以先跑 in-process demo（默认 5 类任务：写作 / 决策 / 编程 / 分析 / 创意），并输出报告文件：

```bash
uv run kun lab dogfood --enable --paths 3 --report-path .kun-dogfood-report.json
```

跑完看输出:
- [ ] Step 1: 3 个 ensemble 都跑完, 看到 `winning_path_idx` + `total_cost_usd`
- [ ] Step 2: `kun lab stats` 显示 3 条 experiment + recipe_stats 表
- [ ] Step 3: `kun lab promote --apply` 推 ≥1 条 recipe
- [ ] Step 5: Prometheus metrics 端点列出 7 个 lab 指标
- [ ] 如用了 `--report-path`，报告里有 `experiment_count`、`top_recipes`、`registry`、`classifier_decisions`

## 单点验证 (深入跑各 wire)

### V2.2 §26 KUN-Lab MVP (Wire 19-22)

```bash
# 1. lab 跑一次
KUN_LAB_MODE=1 uv run kun lab run "测试任务" --paths 5 --enable

# 2. 看 ExperimentLog
uv run kun lab stats

# 3. 推 recipe (需 N 次, 用 --min-total 调低)
uv run kun lab promote --min-total 1 --apply
```

预期:
- [ ] `winning_output` 跟 prompt 相关 (LLM 真接通)
- [ ] `total_cost_usd > 0` (真 LLM call)
- [ ] `recipe_stats` 表显示 strategy + win_rate
- [ ] promote 输出 "推升 N 条 recipe → events bus"

### V2.2 §26 闭环到主仓库 (Wire 23-26)

```bash
# 启用主仓库消费
export KUN_LAB_BRIDGE_ENABLED=1

# 启 API
uv run kun serve &

# 跑一次主仓库 task (intent 走 SMART/MAX → hermes 启用)
curl -X POST http://localhost:8000/api/chat/run \
  -H "Content-Type: application/json" \
  -H "X-User-Id: u-test" \
  -d '{"message":"为登录接口写一段测试用例"}'

# 看 idle_batch 日志 (lab_recipe_adoption step)
# 真 prod 装 cron scheduler, dev 手动跑 idle_batch
uv run kun idle-batch --tenant u-sylvan
```

预期:
- [ ] task 完成, 返 winning answer
- [ ] idle_batch 输出含 `lab_recipe_adoption` step (status=ok 或 noop)
- [ ] 多次跑后 LabRecipeRegistry 累积 entries (跨 task_type)

### V2.2 §22 Hermes 5 action_type (Wire 31-33)

跑同一个 task, 改 system prompt 让 LLM 决定不同 action_type:

```bash
# 看 chat WS 端的事件流
uv run kun run "请帮我先回顾历史架构" --tenant u-sylvan
# 应该看到 hermes_step (action_type=use_memory) + hermes_memory_injected
```

预期 OrchestratorEvent:
- [ ] `hermes_step` (含 action_type)
- [ ] `hermes_skill_override` (use_skill / web_search)
- [ ] `hermes_memory_injected` (use_memory + payload.query)
- [ ] `hermes_ask_user` (ask_user → status=paused)

### V2.2 §27 Inference-Time Rethinking (Wire 35)

```bash
# 强制 MAX 模式 + 让 LLM 故意答得 thought 跟 action 不一致
# 看 hermes 是否触发 retry
KUN_HERMES_CONSISTENCY_THRESHOLD=0.7 KUN_HERMES_MAX_RETHINKS=3 \
  uv run kun run "MAX 模式测试" --user max-test
```

预期:
- [ ] `hermes_step.rethink_count > 0` 出现至少一次 (consistency 不足触发 retry)
- [ ] `thought_action_consistency` 字段反映启发式分

### V2.2 §20 mempalace (Wire 30)

```bash
# 1. 先让 RelationshipMineStep 跑 (需要 events 数据)
# 跑几次 task → emit task.completed events
uv run kun run "task1" --user mempalace-test
uv run kun run "task2" --user mempalace-test
uv run kun run "task3" --user mempalace-test

# 2. 跑 idle_batch 触发 RelationshipMineStep daily 节奏
uv run kun idle-batch --tenant u-sylvan

# 3. 检查 entity_relationships 表
psql -d kun -c "SELECT relation_type, COUNT(*) FROM entity_relationships GROUP BY relation_type;"
```

预期:
- [ ] entity_relationships 表非空 (至少 co_occurs 关系)
- [ ] relations.confidence ∈ [0.3, 0.9]
- [ ] 跨 tenant 隔离 (其他 tenant 看不到)

### V2.2 §21 ExecutionMode classifier (Wire 25 lab hint)

```bash
# 1. 先让 lab 推 recipe
KUN_LAB_MODE=1 uv run kun lab run "task" --task-type biz_plan --enable
KUN_LAB_MODE=1 uv run kun lab promote --min-total 1 --apply

# 2. 跑同 task_type 的真 task → classifier 应该用 lab hint
KUN_LAB_BRIDGE_ENABLED=1 uv run kun run "biz plan task" --user e-test
```

预期:
- [ ] classifier rationale 含 `lab_recipe:tier_top_low_temp(win_rate=0.85)`
- [ ] mode 跟 lab strategy 推荐一致 (top → MAX, cheap → FAST)

## metrics 看可观测性

启动 Prometheus + Grafana (docker compose 配好):

```bash
docker compose -f docker-compose.dev.yml up -d prometheus grafana
```

打开 Grafana: http://localhost:3000

预期 dashboards:
- [ ] **KUN-Lab (V2.2 §26)** — 8 panel: 实验吞吐 / cost / latency / path / budget cap / recipe 推送 / registry size / top strategies
- [ ] **Knowledge Graph (V2.2 §20)** — 6 panel (codex C39 #56 加的)
- [ ] **lab_budget_cap_spike** alert 配置 (cost cap rate > 0.3 触发)

## 失败模式 (验防御性)

跑这些应该 graceful degrade, 不挂:

- [ ] `KUN_LAB_MODE=0` 跑 `kun lab run` → 红字"未启用" + exit 2
- [ ] LLM provider 不可用 → fallback to MiniMax / Stub
- [ ] DB 不可用 → in-memory ExperimentLog 仍 work, 跨进程不共享
- [ ] verification spec 写错 (kind 拼错) → required failed → task mark failed (不挂 orchestrator)
- [ ] hermes 返不可解析 JSON → fallback step 直接返 (不 rethink loop 死循环)
- [ ] graph_traversal 没数据 → fallback score 降序 (不破 ImportanceScorer)

## 跑完后清理

```bash
# 清 in-memory 状态 (Wire 19 ExperimentLog / Wire 25 LabRecipeRegistry)
# 重启 API 服务即清

# 清 entity_relationships 表 (重置 mempalace 数据)
psql -d kun -c "DELETE FROM entity_relationships WHERE tenant_id='u-sylvan';"

# 清 events 表 outbox (重启 outbox_worker)
```

## 报告模板

跑完 dogfood 后填这份给 PM/team:

```
跑 dogfood 时间: __________
LLM provider: __________
完整链路 working: yes / no (如 no, 哪步卡)
预期 metrics 显示: yes / no
预期 events 触发: yes / no
mempalace 数据: __ relations, __ co_occurs
classifier lab hint 真生效: yes / no
建议 V2.3 优先级: __________
```

## 参考

- `docs/v2/V2.2-implementation-audit.md` — 完整 V2.2 状态 audit
- `docs/PROMISES.md` — 历史承诺清单 (Z 节为 V2.2)
- `docs/v2/KUN-V2.2-revisions.md` — V2.2 spec 原文
