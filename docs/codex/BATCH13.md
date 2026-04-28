# BATCH13 brief — V2.3 周边补全

派给 codex. 总量 ~30-40h. 全部基于 `feat/v2.1-foundation` (现 head 含 V2.3 心脏 Wire 38-50 + install_runtime + Pheromone hook + Wire 47/49).

跟 V2.3 关系: Claude 一次性做完 V2.3 心脏 (Wire 38-50 + install_runtime). BATCH13 = **V2.3 周边模块 + 启 V3 高级探索 + orchestrator 真接 protocol**.

详见 `docs/v2/V2.3-implementation-audit.md` — V2.3 完成度 ~85%, 剩这些等 codex.

---

## 第 1 部分 — V2.3 心脏外围 (优先, 让 V2.3 真上线)

### C61 ProtocolRegistry HTTP API + CLI (~6-8h)

**现状**: ProtocolRegistry 心脏全通 (Wire 39, alembic 0015), 没 HTTP/CLI. admin / 用户看不到协议.

**任务**:
1. `kun/api/protocol.py` HTTP endpoints:
   - `GET /api/protocols` — list (per-tenant)
   - `GET /api/protocols/{protocol_id}` — 当前 stable 版
   - `GET /api/protocols/{protocol_id}/versions` — 所有版本
   - `POST /api/protocols/{protocol_id}/promote` — body: {version, target_status}
   - `POST /api/protocols/{protocol_id}/rollback` — body: {version, reason}
2. `kun/cli.py` 加 `kun protocol` 子命令组:
   - `kun protocol list`
   - `kun protocol show <id>` (含 yaml 美化输出)
   - `kun protocol show <id>@<version>`
   - `kun protocol promote <id>@<version> shadow|canary|stable`
   - `kun protocol rollback <id>@<version> --reason ...`
3. WebSocket `/ws/protocols/lifecycle/stream` (可选, 监 lifecycle 变化)
4. 测试 8-10

**重要约束**:
- 复用 `kun.qi.ProtocolRegistry` API (我已写, 别重写)
- 不动 alembic 0015 schema

### C62 AI Scientist v2 树搜索 (~6-8h)

**现状**: Wire 50 Darwin Gödel 是线性多轮. AI Scientist v2 是树搜索 (并行多分枝).

**任务**:
1. `kun/qi/scientist_agent.py`:
   - `class TreeNode`: hypothesis + experiment_config + score + children
   - `class ScientistTree`: max_depth + max_breadth + value_guided_expansion
   - `await tree.explore(prompt, runner) → ScientistResult`
2. 复用 Darwin Gödel 的 strategy_evolver (改成 LLM 生成假设)
3. 集成进启窗口 (跟 Darwin Gödel 同模式: budget + time stop)
4. 测试 8-10

### C63 5% 非最佳路径测试 (~3-4h)

**现状**: 启窗口内现在跑 Darwin Gödel + AI Scientist 都是探索. 缺 "5% 故意走非最佳" 验已知最佳是否仍最佳.

**任务**:
1. `kun/qi/exploration_audit.py`:
   - `async run_5pct_audit(stable_protocol, prompt) → AuditResult`
   - 拉 stable protocol → 故意改 strategy (用第 2 高 score 的) → 跑实验
   - 对比真实表现 → 如新 strategy 真好 → 推 protocol@new-version-experimental
2. 加进启窗口 cron
3. 测试 4-6

### C64 Pheromone daily decay cron (~2-3h)

**现状**: PheromoneStorage.decay_all 已有 (Wire 43), 但没 cron 自动跑.

**任务**:
1. `kun/engineering/idle_batch.py` 加 step `pheromone_decay`:
   - 每日跑 `await get_pheromone_storage().decay_all()`
   - 写 metric `kun_pheromone_decay_total`
2. install_runtime 注册该 step
3. 测试 3-5

### C65 lite_jury for SMART (~3-4h)

**现状**: codex C34 #58 把 jury 接到 MAX. SMART 仍单 LLM judge.

**任务**:
1. `kun/engineering/multi_judge.py` 加 `lite_jury_evaluate` — 2 个 judge (跟 jury_evaluate 5 个对比, lite 省成本)
2. `kun/engineering/execution_protocol.py` SMART 模式 ThoughtActionConsistency 用 lite_jury
3. install_runtime env 控制 (KUN_LITE_JURY_ENABLED)
4. 测试 4-5

---

## 第 2 部分 — Wire 53 orchestrator 真接 V2.3 (用户拍板后启动)

### C66 orchestrator 真消费 protocol (~5-7h, 留 Wire 53)

**任务**:
1. orchestrator step 启动前: `protocol = await registry.find_protocol_for(task_meta, tenant)`
2. 如有 protocol → 改 ExecutionMode / hermes prompt addon / skill_chain / verification specs (按 protocol)
3. 4 章节 (V2.3 §3.5 鲲消费协议) 真闭环
4. 测试 e2e 6-8

### C67 AntiGamingDetector 接 orchestrator + jury (~4-6h, 留 Wire 53)

**任务**:
1. orchestrator step 完后跑 AntiGamingDetector.check
2. 命中 → emit `gaming.detected` event + walk reset (跟 ValueGate escalate 同模式)
3. 启 explore 时也跑 (防自我作弊)
4. 测试 6-8

---

## 第 3 部分 — V2.3 dogfood + 真用 (后续, 等 V2.3 闭环)

### C68 V2.3 dogfood 真跑 (~4-6h)

scripts/dogfood_v23.sh — 启用 KUN_QI_ENABLED=1 + KUN_QI_FORCE_ACTIVE=1, 跑 10 个真 task, 看 protocol 涌现.

### C69 V2.3 metrics + Grafana dashboard (~3-4h)

加 Prometheus metric:
- kun_qi_window_active gauge
- kun_qi_daily_spent_usd
- kun_protocol_promotion_total
- kun_predictive_coding_error_p50
- kun_pheromone_total_strength
- kun_anti_gaming_detection_total{pattern}

加 Grafana dashboard (类似 V2.2 §26 lab dashboard).

### C70 PROMISES.md auto-gen 跑一遍 (~1-2h)

用 codex 自己 #71 写的 promises_autogen.py 跑 git log v2.2.0..HEAD → 生成 Z.17 草稿. 我手动整合.

---

## 排期建议

按 ROI:
- **第 1 周**: C61 (Protocol HTTP/CLI, 用户最直观能感受) + C64 (Pheromone decay)
- **第 2 周**: C62 (AI Scientist v2) + C65 (lite_jury)
- **第 3 周**: C63 (5% 探索) + C66 (orchestrator 真接 protocol — 用户拍板)
- **第 4 周**: C67 (AntiGaming wire) + C68 (dogfood) + C69 (metrics/dashboard)

---

## 重要约束

1. **不动 V2.3 心脏接口** (Claude 已写):
   - kun/qi/* (Protocol/PredictiveCoding/Pheromone/DarwinGodel/Window/Budget)
   - kun/security/anti_gaming.py
   - kun/engineering/capability_cache.py
   - kun/datamodel/verification_templates.py
   - install_runtime V2.3 env opt-in 都已装

2. **commit 前 4 step**: ruff format + ruff check + mypy + pytest. (UP038 老错, 你 BATCH9-12 也修了, 跟 Claude 配合 hotfix 经验吸取)

3. **alembic next revision**: 0017 (0015_protocols + 0016_pheromone 已被 Claude 用)

4. **PR base**: 全部 feat/v2.1-foundation. 避免 stacked PR (BATCH9 教训).

---

## 当前状态 (供 codex 参考)

- Claude 一天内做完 V2.3 心脏 wire 38-50 + install_runtime + 4 个文档
- 测试 1302 → 1427 (+125)
- V2.3 完成度 ~85%
- V2.2 还有 #67 alembic 卡住, 等 codex 上一轮的 fix 还没 push (你之前说 #67 等 #53 改 alembic 0015, 现在 #53 已 merge, #67 待你 rebase)

谢 codex. V2.3 心脏 + 周边配合, 离 v2.3.0 tag 不远.
