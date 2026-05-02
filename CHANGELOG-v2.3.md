# KUN v2.3.0 release notes (草稿)

**版本**: v2.3.0
**Tag 日期**: 2026-04-28 (待真用户跑 dogfood 后正式 tag)
**主题**: 启 (Qi) — KUN 子模式: 协议涌现 / Predictive Coding / Pheromone / 神经符号

## 主线: V2.3 心脏 + 真上线

V2.3 给 KUN 加了 "启 (Qi)" 子模式 — 鲲日常稳定 + 启窗口探索 + 沉淀协议给鲲消费. 这是 KUN 跟 LangChain/Devin 等 agent 框架真差异化的一步.

### 启 (Qi) 核心架构

```
┌──────────────────────────────────────────────────────────┐
│ 鲲 (主, 用户日常用, 100% 稳定)                            │
│ - Orchestrator: 真消费 protocol (KUN_PROTOCOL_CONSUME)    │
│ - Orchestrator: post-step AntiGaming (KUN_ANTI_GAMING)    │
│ - PC hook: prediction_provider/model_updater (插件)        │
│ - skill_selector: prior_skill + Pheromone 加成 (蚁群)     │
└───────────────────┬──────────────────────────────────────┘
                    ↑↓ 协议 / Pheromone / PC error
┌───────────────────▼──────────────────────────────────────┐
│ 启 (Qi, 鲲子模式, 默认 OFF, KUN_QI_RUNTIME_ENABLED=1)      │
│ - QiWindowConfig (时间窗口) + QiDailyBudget (日预算)      │
│ - DarwinGodelLoop (多轮探索 + 4 stop)                     │
│ - AIScientistTreeSearch (V2 树搜索)                       │
│ - PredictionTrainer (PC 模型训练)                          │
│ - Pheromone reinforce + decay (蚁群涌现)                  │
│ - ProtocolRegistry (experimental → shadow → canary →      │
│   stable lifecycle)                                       │
└──────────────────────────────────────────────────────────┘
```

### 主要新增

#### 1. ProtocolRegistry (V2.3 核心 IP)
- 完整 lifecycle: experimental → shadow → canary → stable / rolled_back
- HTTP API (`/api/protocols/*`) + CLI (`kun protocol list/save/get/promote/rollback/match`)
- alembic 0015_protocols schema (per-tenant unique on protocol_id+version)
- orchestrator 真消费 (KUN_PROTOCOL_CONSUME_ENABLED=1) → 改 ExecutionMode + verification specs

#### 2. Predictive Coding (V2.3 §5)
- Plugin-style hook on Orchestrator (prediction_provider + model_updater)
- 默认 None → 鲲行为完全不变
- 启训练 pipeline: PredictionLog → PredictionTrainer → PredictionModel save/load
- env: KUN_PREDICTIVE_CODING_ENABLED=1 (default ON), KUN_PC_MODEL_PATH

#### 3. Pheromone (V2.3 §6 蚁群涌现)
- alembic 0016_pheromone (entity_relationships 加 pheromone_strength + last_reinforced_at)
- reinforce in orchestrator post-step (skill chain 自动加强)
- daily decay step in idle_batch (~30 天遗忘)
- skill_selector 用 Pheromone 加成 (prior_skill 参数)

#### 4. Darwin Gödel + AI Scientist v2 (V2.3 §4)
- DarwinGodelLoop 多轮探索 (4 stop conditions)
- AIScientistTreeSearch (beam search + 预算 stop)

#### 5. AntiGamingDetector (V2.3 §7)
- 7 套路: copy_prompt / answer_off_topic / copy_prior_answer / skip_step / fake_completion / over_spec / fake_data
- orchestrator post-step 自动跑 (KUN_ANTI_GAMING_ENABLED=1)
- emit gaming.detected event

#### 6. Verification 模板 (V2.3 §3.4)
- 4 task_type 默认: writing/coding/decision/research
- merge_with_default(task_type, llm_provided) 自动并存

#### 7. 用户 feedback API (V2.3 §8)
- POST /api/tasks/{task_id}/feedback (rating 1-5 + comment + tags)
- emit user.feedback event

#### 8. CapabilityCardCache (V2.3 §8.5)
- 实时 hot path (30s TTL) + per-tenant invalidate
- 跟 capability_card writeback 联动 (writeback 后失效 cache)

#### 9. 5% 非最佳路径探索 (V2.3 §3.6)
- ENSEMBLE 模式 KUN_ENSEMBLE_NON_BEST_EXPLORATION_RATIO env (default 0)

#### 10. lite_jury for SMART (V2.3 §22)
- SMART 模式 2-judge lite jury (MAX 仍 5-judge full)

### CLI 新增

```
kun protocol list / save / get / promote / rollback / match
kun qi status / start / stop
```

### Metrics 新增 (Prometheus)

- `kun_qi_window_active`, `kun_qi_daily_spent_usd`
- `kun_protocol_match_total`, `kun_protocol_promotion_total`
- `kun_predictive_coding_error` (histogram)
- `kun_pheromone_total_strength`, `kun_pheromone_decay_step_total`
- `kun_anti_gaming_detection_total`
- `kun_capability_card_cache_hit_rate`

Grafana dashboard: `kun/infra/grafana-dashboard-kun-v23.json` (auto-mounted).

### env 总览 (V2.3 新增)

| Env | Default | 说明 |
|---|---|---|
| KUN_PREDICTIVE_CODING_ENABLED | 1 | PC hook + log + updater |
| KUN_PC_MODEL_PATH | (empty) | PredictionModel JSON 路径 |
| KUN_QI_RUNTIME_ENABLED | 0 | 启 V2.3 runtime master switch |
| KUN_QI_ENABLED | 0 | 启 (Qi) window 启用 |
| KUN_QI_FORCE_ACTIVE | 0 | 强制窗口活跃 (debug) |
| KUN_QI_DAILY_BUDGET_USD | 5.0 | 启日预算上限 |
| KUN_QI_PROTOCOL_DB_ENABLED | 0 | 用 SQL 而非 InMemory |
| KUN_QI_PHEROMONE_DB_ENABLED | 0 | 用 SQL 而非 InMemory |
| KUN_PROTOCOL_CONSUME_ENABLED | 0 | orchestrator 真消费协议 |
| KUN_ANTI_GAMING_ENABLED | 0 | orchestrator post-step AntiGaming |
| KUN_PHEROMONE_DECAY_ENABLED | 1 | idle_batch daily decay |
| KUN_PHEROMONE_DECAY_RATE | 0.95 | 衰减率 (~30 天近零) |
| KUN_HERMES_SMART_LITE_JURY_ENABLED | 1 | SMART 2-judge lite (MAX 仍 5) |
| KUN_ENSEMBLE_NON_BEST_EXPLORATION_RATIO | 0.0 | ENSEMBLE 5% 探索 |
| KUN_CAPABILITY_CACHE_TTL_SEC | 30 | CapabilityCardCache TTL |

### 安全设计 (默认 OFF)

V2.3 新功能默认全部 **OFF** (除了 PC hook, 默认 ON 但 None 时不影响). 用户必须 explicit 启用. 防误开烧钱 / 防意外行为变化.

### Dogfood

```bash
./scripts/dogfood_v23.sh
```

跑一次 V2.3 闭环 demo. 看 protocol 涌现 + Pheromone 强化 + PC error 趋势.

### 兼容性

- V2.3 跟 V2.2 共存. KUN-Lab (V2.2 §26) 仍可用.
- 所有 V2.3 新功能默认 OFF, V2.2 行为完全保留.
- alembic single head: 0016_pheromone (向前兼容 0015 + 0014_lab_*).

### 未做 (留 V2.4)

- Predictive Coding 自动训练 cron (启窗口里跑)
- Darwin Gödel 自动 strategy generation (LLM 生 strategy)
- AntiGaming 自学新套路
- Verification 模板自动生成
- 协议自动 promote (基于 dogfood win_rate)
- 多窗口 + 自动选窗口

详见 `docs/v2/KUN-V2.4-spec.md`.

## 测试

测试数: 1302 (v2.2.0) → 1453 (v2.3.0) — **+151**.

```
uv run --extra dev ruff check .       # ✅
uv run --extra dev ruff format --check # ✅
uv run --extra dev mypy kun           # ✅ (195 source files, 0 error)
uv run --extra dev pytest tests/unit  # ✅ 1453 passed
```

## Migration

```bash
uv run alembic upgrade head  # → 0016_pheromone
```

## 致谢

V2.3 由 Claude (心脏 wire 38-50) + codex (BATCH13 周边: HTTP/CLI/lite_jury/AI Scientist v2/5% 探索/install_runtime) 协作完成. Claude 收尾真上线 (C71+C72+C73+CLI qi).

---

**真 dogfood + tag 步骤**:
1. 跑 `./scripts/dogfood_v23.sh` (需真 LLM API key)
2. 看 logs/events: protocol.applied / gaming.detected / pheromone reinforce
3. 看 Grafana dashboard 真有数据
4. 没 blocker → tag v2.3.0
