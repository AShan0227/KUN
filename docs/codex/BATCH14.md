# BATCH14 brief — V2.3 真上线 + V2.4 起点

派给 codex. 总量 ~25-35h. 全部基于 `feat/v2.1-foundation` (当前 head 含 BATCH13 + Claude C64/C68).

跟 V2.3 关系: BATCH13 完, V2.3 完成度 ~95%. BATCH14 = **orchestrator 真消费 protocol (Wire 53) + AntiGaming 真接 + V2.3 metrics + dogfood 真跑 + V2.4 起点**.

详见 `docs/v2/V2.3-implementation-audit.md` — 还差最后 5%, 等 codex.

---

## 第 1 部分 — V2.3 闭环最后一公里

### C71 Wire 53 orchestrator 真消费 protocol (~6-8h)

**现状**: ProtocolRegistry 全通 (BATCH13 #73), HTTP/CLI 全通, 但 orchestrator step 启动时**没真去 ProtocolRegistry.find_protocol_for**. 协议 IP 还没真用.

**任务**:
1. `kun/engineering/orchestrator.py` 在 step 启动前:
   ```python
   if hasattr(app.state, "protocol_registry"):
       protocol = await registry.find_protocol_for(task_meta=task_ref.meta, tenant_id=tenant.tenant_id)
       if protocol:
           # 改 ExecutionMode (按 protocol.execution.mode)
           # 改 hermes prompt addon (按 protocol.hermes_template)
           # 改 skill_chain (按 protocol.skill_chain)
           # 改 verification specs (按 protocol.verification)
   ```
2. 4 章节 (V2.3 §3.5 鲲消费协议) 真闭环
3. 测试 e2e 6-8 (mock protocol, 验真改 prompt / skill / verification)
4. 加 metric: `kun_protocol_match_total{protocol_id, hit}` + `kun_protocol_apply_total`

**约束**:
- 默认 OFF (KUN_PROTOCOL_CONSUME_ENABLED=0). 用户 explicit 启用.
- 启用时, 没 protocol → 行为完全不变 (back-compat).
- protocol 改 prompt → emit `protocol.applied` event 让 Watchtower 看到.

### C72 AntiGamingDetector 接 orchestrator + jury (~4-6h)

**现状**: AntiGamingDetector 7 套路全 (Claude wire 44), 但没接 orchestrator 也没接 jury.

**任务**:
1. orchestrator step 完后跑 `AntiGamingDetector.check`:
   - 输入: prompt / answer / planned_steps / actual_steps / has_assets / used_skills
   - 命中 → emit `gaming.detected` event + walk reset
2. 启 explore 也跑 (防自我作弊):
   - DarwinGodelLoop 每轮完后跑
   - 命中 → walk drop (skip this round)
3. KUN_ANTI_GAMING_ENABLED=0 (default) → 关
4. 测试 6-8

### C73 V2.3 Prometheus metrics + Grafana (~3-4h)

**现状**: V2.3 全装上, 没 metric 没 dashboard.

**任务**:
1. 加 metric (`kun/core/metrics.py`):
   - `kun_qi_window_active` (gauge)
   - `kun_qi_daily_spent_usd{tenant}` (gauge)
   - `kun_protocol_promotion_total{from_status, to_status}` (counter)
   - `kun_predictive_coding_error_p50` (histogram)
   - `kun_pheromone_total_strength` (gauge)
   - `kun_anti_gaming_detection_total{pattern}` (counter)
   - `kun_capability_card_cache_hit_rate` (gauge)
2. Grafana dashboard JSON (类似 V2.2 §26 lab dashboard) → `docs/observability/v23_dashboard.json`
3. 测试 3-5

---

## 第 2 部分 — V2.3 真用户磨合

### C74 V2.3 dogfood 真跑 + 真数据反推 (~3-4h)

**现状**: scripts/dogfood_v23.sh 写好 (Claude C68), 但**没真跑过**. 需 codex/用户跑一次, 看 protocol 涌现 / Pheromone 强化 / PC error 趋势.

**任务**:
1. 跑 `./scripts/dogfood_v23.sh`
2. 看 logs/events: `pheromone_decay` / `pc_error` / `protocol.match` 等
3. 写 `docs/v2/V2.3-dogfood-report.md`:
   - 跑了几个 task
   - 涌现的 protocol (如有)
   - Pheromone 加强的 chain (top 5)
   - PC error 真分布 (p50/p95)
   - 发现的问题 / 改进点
4. 真数据 → V2.4 spec 起点

### C75 V2.4 spec 草稿 (~4-6h)

**任务**:
1. 基于 C74 dogfood 报告, 写 `docs/v2/KUN-V2.4-spec.md`:
   - V2.3 留的问题
   - V2.4 改进方向 (3-5 个主题)
   - 跟 V2.3 兼容性
2. 不动 V2.3 心脏

---

## 第 3 部分 — V2.3 收尾

### C76 PROMISES.md auto-gen 跑一遍 (~1-2h)

用 codex #71 写的 promises_autogen.py 跑 git log v2.2.0..HEAD → 生成 Z.18 草稿. Claude/用户手动整合.

### C77 v2.3.0 release 准备 (~3-4h)

**任务**:
1. CHANGELOG.md 更新
2. release notes 写 (类似 v2.2.0)
3. tag v2.3.0
4. push tag → GitHub release

---

## 排期建议

按 ROI:
- **第 1 周**: C71 (orchestrator 真消费 protocol — 协议 IP 真用) + C72 (AntiGaming wire)
- **第 2 周**: C73 (metrics + dashboard) + C74 (dogfood 真跑)
- **第 3 周**: C75 (V2.4 spec) + C76 (PROMISES auto-gen) + C77 (v2.3.0 release)

---

## 重要约束

1. **不动 V2.3 心脏 + BATCH13 核心**:
   - kun/qi/* (Protocol/PredictiveCoding/Pheromone/DarwinGodel/Window/Budget/AIScientist)
   - kun/security/anti_gaming.py
   - kun/engineering/capability_cache.py (codex 的 CapabilityCardCache)
   - kun/api/protocols.py
   - kun/cli.py protocol 子命令

2. **commit 前 4 step**: ruff format + ruff check + mypy + pytest. 跟 BATCH13 一样, mypy 要用 `uv run --extra dev mypy kun` (pre-commit hook 没进 dev env, 必要时 --no-verify).

3. **alembic next revision**: 0017 (0015_protocols + 0016_pheromone 已用)

4. **PR base**: 全部 feat/v2.1-foundation. 避免 stacked PR.

5. **C71 是 V2.3 真核心**: protocol IP 真用 → V2.3 才真差异化. 优先做.

---

## 当前状态 (供 codex 参考)

- BATCH13 #73 已 merged (Claude commit 3a41096 解决冲突)
- Claude C64 (Pheromone decay step) + C68 (dogfood 脚本) 已 push (commit 08e3ade)
- 测试 1427 全过, ruff/mypy 干净
- V2.3 完成度: ~95% (剩 C71-C75 真上线 + dogfood + V2.4 起点)
- V2.2.0 tag 准备中 (codex #72)

谢 codex. BATCH14 完 → V2.3 真闭环 + V2.4 起点 + v2.3.0 tag.
