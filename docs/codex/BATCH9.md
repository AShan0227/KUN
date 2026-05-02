# BATCH9 brief — KUN-Lab 周边模块 + V2.2 §21 ExecutionMode 第 4 档

派给 codex. 总量 ~50-70h. 全部基于 `feat/v2.1-foundation` (现 head `42e0b77`).

## 背景 — Claude 这一轮做完的范围 (Wire 19-29)

Claude 已经把 KUN-Lab 心脏 + 闭环 + 反哺主仓库决策全部接通:
- Wire 19-26: lab MVP → events bus → idle_batch → KP → registry → ExecutionMode classifier 真用 lab hint (env `KUN_LAB_BRIDGE_ENABLED=1` opt-in)
- Wire 27: cost-cap hard 执行 (累积超 budget cancel 剩余 path)
- Wire 28: 7 lab Prometheus metrics
- Wire 29A: hermes prompt 接 LabRecipeRegistry (chain_of_thought / diverse_perspective / tier_top_low_temp)
- Wire 29B: cursor_storage Protocol (InMemoryCursorStorage + SqlCursorStorage 自包含 CREATE TABLE)
- Wire 29C: BATCH8a/b 升级 — debugger 接 DiagnoseRunner / reviewer 接 multi_judge.jury_evaluate
- Wire 29D: 2 个 watchtower rule (lab_budget_cap_spike / lab_recipe_promotion_burst) + Grafana dashboard JSON 8 panel

**测试: 1143 全过. ruff/mypy 干净.** 完整闭环数据流见 `docs/PROMISES.md` Z.12 节.

BATCH9 是周边补全 + 把 lab 真用到生产 task. **不动心脏 / 不改 Wire 19-29 已有接口** (Claude 责任).

---

## C29 — ExperimentLog DB 持久化 (~6-8h)

**现状**: `kun/lab/experiment_log.py` 是 in-memory `ExperimentLog`. 进程重启清空. CLI `kun lab stats` 跨进程不可用.

**任务**:
1. 加 alembic migration `0013_lab_experiments.py` — `lab_experiments` 表 (experiment_id PK, task_type, prompt_hash, ensemble_result JSONB, created_at)
2. 加 ORM `LabExperimentRow` 在 `kun/core/orm.py`
3. 加 `kun/lab/experiment_log_db.py: SqlExperimentLog` 实现跟现有 `ExperimentLog` 同接口 (record / list_all / by_task_type / recipe_stats / best_recipe_for / total_lab_cost_usd / reset)
4. 默认仍用 in-memory (向后兼容); env `KUN_LAB_DB_BACKED=1` → install_runtime 切 SqlExperimentLog
5. 测试: round-trip + 跨 process 持久 + by_task_type 过滤 + recipe_stats 跟 in-memory 等价

**注意**: 不能改 `ExperimentLog` 公共 API (Wire 19-22 + lab CLI 在用). 用 Protocol / 替换 singleton.

---

## C30 — kun lab inspect/explain/replay CLI 子命令 (~6-8h)

**现状**: `kun lab run/stats/promote` 三个子命令 (Wire 22). 用户跑完看不到详细路径输出, 也不能复现.

**任务**:
1. `kun lab inspect <experiment_id>` — 看具体一次实验:
   - 5 path 各自 strategy/tier/temperature/output 全文/score/cost/error
   - winner 高亮
   - 总耗时 / 总 cost / budget 状态
2. `kun lab explain <experiment_id>` — LLM 解释:
   - 调 LLMRouter (cheap tier) 给 5 path 输出 + 用户 prompt → "为什么 winner 比其他更好?"
   - 输出一段 markdown 解释
3. `kun lab replay <experiment_id>` — 同 prompt + 同 EnsembleConfig 重跑:
   - 拉原 experiment 的 prompt + config
   - 跑 EnsembleExecutor (新 experiment_id)
   - 比较: winner_idx 是否变 / score 变化 / cost 变化
4. 测试: 4-6 个 (mock invoker, in-memory log)

**依赖**: 用 C29 (DB 持久化) — 否则 inspect 跨进程不可用. 但可先做 in-memory 版, C29 完成后自动跨 process.

---

## C31 — /api/lab/* HTTP endpoints (~8-10h)

**现状**: 只有 CLI. 没 HTTP API. 跟 blackboard / capability_card endpoints 不一致.

**任务** (跟 `kun/api/blackboard.py` 同模式):
1. `GET /api/lab/experiments?task_type=X&limit=20` — list (从 ExperimentLog)
2. `GET /api/lab/experiments/{id}` — detail (含全 path)
3. `GET /api/lab/recipes` — LabRecipeRegistry 当前 dump (tenant 隔离)
4. `GET /api/lab/recipes/{task_type}` — by task_type filter
5. `POST /api/lab/run` — 跑 ensemble (要求 `KUN_LAB_MODE=1`, body: prompt + EnsembleConfig)
6. `POST /api/lab/promote` — 触发 RecipePromoter.promote_eligible (admin only)
7. WebSocket `/ws/lab/experiment/{id}/stream` — 跑 ensemble 时实时 path 进度 (类似 chat WS)
8. 测试: TestClient 8-10 个 (含 KUN_LAB_MODE=0 时 POST run 拒绝)

**依赖**: install_runtime 已经装 (Wire 26), 直接拉 app.state.lab_recipe_registry.

---

## C32 — ExecutionMode 加 ENSEMBLE 第 4 档 (V2.2 §21) (~10-12h)

**现状**: ExecutionMode = FAST | SMART | MAX. KUN-Lab EnsembleExecutor 只在 lab 跑 (env-gated).

**任务**: 让生产 task 在高 stakes 时走 ENSEMBLE (V2.2 §21):
1. `kun/api/execution_mode_classifier.py` 加 `"ENSEMBLE"` 字面量
2. 决策规则 (优先级 risk_critical > complexity > lab_recipe > default):
   - SoulFile.execution_mode_preference 加 `always_ensemble_kinds: list[str]`
   - `risk_level=critical AND user_can_wait=True` → ENSEMBLE (而不是 MAX)
   - `complexity_score > 0.9 AND estimated_cost > approval_threshold * 0.8` → ENSEMBLE
3. orchestrator (`kun/engineering/orchestrator.py`) 见到 mode=ENSEMBLE → 走 EnsembleExecutor (复用 Wire 19/20 心脏) 而不是单 LLM
4. 用户体验: ENSEMBLE 走 5 path → multi_judge 选最优 → 给用户 winner + "5 个方案对比" 可选展开
5. 测试: classifier 决策规则 8-10 + orchestrator 集成 4-5

**依赖**: 用 Claude Wire 20 LLMRouterEnsembleAdapter 真接 LLMRouter (现成).

---

## C33 — TaskBoundaryGuard 接 OffTopicEval benchmark (~6-8h)

**现状**: Wire 18 TaskBoundaryGuard 是启发式 + LLM judge 二合, 没 benchmark 验证 reject rate.

**任务**:
1. 拉 OffTopicEval (Lambda, ICLR 2026 Agents in the Wild Workshop) 数据集 (~500 题)
2. 加 `kun/security/task_boundary_benchmark.py: run_benchmark(guard, dataset) → BenchmarkReport`
3. idle_batch 加 `task_boundary_eval` step (weekly)
4. Prometheus: `kun_task_boundary_reject_rate{dataset}` gauge
5. CLI: `kun security task-boundary-benchmark`
6. 测试: 8-10 (mock dataset + benchmark)

**依赖**: Wire 18 TaskBoundaryGuard 现成接口 `.check(task_meta, scope) → BoundaryDecision`.

---

## C34 — ThoughtActionConsistency 接真 multi_judge (~6-8h)

**现状**: Wire 17 ThoughtActionConsistency 是启发式 + 单 LLM judge. SMART/MAX 模式应该走多 judge 投票.

**任务**:
1. `kun/engineering/execution_protocol.py: ThoughtActionConsistency.check_with_jury(thought, action, mode, *, router) → ConsistencyVerdict`
2. 内部调 `multi_judge.jury_evaluate` (3 judges, MAX 用 5 judges)
3. 当 mode=MAX 时 generator 调 check_with_jury (而不是 check 启发式)
4. rethink_count 触发条件: jury_verdict.pass_=False → rethink (max 2 次)
5. 测试: 8-10 (mock router + jury fixture)

**依赖**: Wire 17 ThoughtActionConsistency 接口 + `kun/engineering/multi_judge.py` (现成).

---

## C35 — Lab Grafana dashboard provision (~2-3h)

**现状**: Wire 29D 加了 `kun/infra/grafana-dashboard-kun-lab.json`, 但需要手动 import 进 Grafana.

**任务**:
1. `kun/infra/grafana-dashboards-provision.yaml`:
   ```yaml
   apiVersion: 1
   providers:
     - name: 'kun-lab'
       folder: 'KUN'
       type: file
       options:
         path: /var/lib/grafana/dashboards
   ```
2. docker-compose / Tilt 把 dashboard JSON 挂到 `/var/lib/grafana/dashboards/`
3. 文档: `docs/ops/grafana-lab-dashboard.md`
4. 测试: 1-2 个 yaml 加载 sanity

---

## C36 — alembic 0013_lab_adoption_cursor (~3-4h)

**现状**: Wire 29B `SqlCursorStorage` 自管 CREATE TABLE IF NOT EXISTS. 应该走 alembic 标准化.

**任务**:
1. `alembic/versions/0013_lab_adoption_cursor.py`:
   - upgrade: CREATE TABLE lab_adoption_cursor (cursor_name PK, last_adopted_at TIMESTAMPTZ, adopted_ids JSONB, updated_at)
   - 加 index on updated_at (后续 truncate cron 用)
   - downgrade: DROP TABLE
2. `kun/lab/cursor_storage.py: SqlCursorStorage._ensure_table` 改为 no-op (alembic 已建表) + 加注释 "now alembic-managed"
3. 加 cron job (`kun lab cursor-truncate` CLI 子命令): 删超 30 天的 cursor (按 updated_at)
4. 测试: alembic upgrade/downgrade round-trip + truncate 行为

**依赖**: Wire 29B SqlCursorStorage 已存在.

---

## 排期建议

按依赖关系:
- 第 1 批 (并行): C29 / C30 / C36 (lab 持久化基础)
- 第 2 批 (依赖 C29): C31 (HTTP endpoints) / C32 (ENSEMBLE 第 4 档)
- 第 3 批 (独立): C33 / C34 / C35

**全部基于 feat/v2.1-foundation, 不动心脏代码. CI 全绿后开 PR 让 Claude 审 + 决策合并.**

跟之前 BATCH 一样: Claude 不抢 codex 周边模块工作; codex 不动 Wire 19-29 已有接口.
