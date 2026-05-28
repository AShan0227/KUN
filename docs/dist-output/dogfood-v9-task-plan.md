# Dogfood v9 — V7 Phase X.B 真 e2e 验证任务计划

**目的**: 用真实长任务跑通 V7 Phase X.B 落地的全套接 runtime 改造,
**真在生产链路上**验证 multi-LLM ensemble × Mission Director 周期 review ×
lifecycle gate × auditor hat 四件套.

**写于**: 2026-05-28 (commit 786c38c 之后, V7 Phase X.B 软件层 6/6 落完之后)

---

## 前提 (operational, 不是 software)

跑这个任务前需要这些 *运维* 准备就绪 (软件已就绪):

| 准备 | 状态 |
|---|---|
| Postgres 起来, alembic upgrade head 到 0017 (含本 session 的 3 张新表) | ⏳ 用户准备 |
| `KUN_PG_DSN` / `KUN_PG_ADMIN_DSN` 配好 | ⏳ 用户准备 |
| gpt-5.5 CLI provider 配置 (用户说已配) | ✅ 用户已说就绪 |
| Anthropic provider 配置 (haiku 或 opus, 走 OAuth subscription 都行) | ⏳ 用户准备 |
| `python -m uvicorn kun.api.main:app` 起得来 | ⏳ 用户准备 |

KUN 软件层 (本 session 落地的 Phase X.B 6 块) **已 100%**, 跑得起来 = 全套真触发.

## V7 Phase X.B 真 e2e 验证矩阵

| Phase X.B 块 | 验证点 | 怎么看见 |
|---|---|---|
| X.B.MD | mission_alignment_reviews 表真有数据 | `psql -c "select count(*) from mission_alignment_reviews"` ≥ 1 |
| X.B.LC | lifecycle_transitions 表真有数据 | `psql ... lifecycle_transitions` ≥ 1 |
| X.B.AR | auditor_reports 表真有数据 | `psql ... auditor_reports` ≥ 1 |
| X.B.UI | Cockpit API 真返 DB 数据 | `curl /cockpit/capabilities?tenant_id=u-dogfood-v9` 返非空 |
| X.B.MDR | Mission Director runner 真跑 tick | log 里有 `mission_director.runner.tick_end` |
| X.B.ENS | Ensemble invoker 真跨 family 并行 | ensemble_calls 表有 ≥ 1 行, n_providers_total = 2 |

## 任务设计 (gpt-5.5 CLI + Anthropic, 总时长 30-60min)

**任务名**: "Dogfood v9 — V7 Phase X.B 真 runtime 验证"

```text
你是 KUN, 现在用真 LLM 走一次跨 family ensemble + Mission Director 周期监督 +
auditor hat 收尾的完整任务. 任务目的: 验证 V7 Phase X.B 落地后, multi-LLM
ensemble 真在 ExecutorLoop 跑, Mission Director 真在 tick 里产 review, lifecycle
真在 gate 时落 transition.

工具:
  - self-reflect: 读 / 写 docs/dist-output/ (同 dogfood v8)
  - grep-verify: 改前 grep 验证 (Path-3, dogfood-P4 加的 skill)
  - shell-exec: 跑 alembic / pytest 等

Phase A · 状态对账 (5 min):
  - self-reflect read docs/dev_logs/LT-progress.md 末段 "V7 Phase X.B 现状小结"
  - grep-verify `make_ensemble_llm_invoker` in kun/integration/ — 确认存在
  - grep-verify `EnsembleCallRow` in kun/core/orm.py — 确认存在
  - write `dogfood-v9-phase-a-statecheck.md` 摘录确认结果

Phase B · 真跑 ensemble (10 min):
  - 让 KUN 自己起 ensemble invoker (gpt-5.5 + anthropic), 跑 1 个简短任务
    e.g. "用一句话解释 KUN 的产品魂", 把响应写到
    docs/dist-output/dogfood-v9-ensemble-response.md
  - 跑完用 grep-verify 查 ensemble_calls 表是否有新 row (用 shell-exec 跑 psql)
  - write `dogfood-v9-phase-b-ensemble-verified.md` 含 call_id / divergence_score
    实际值

Phase C · Mission Director runner 验证 (10 min):
  - 用 shell-exec 在本任务 mid-process 触发 1 次 Mission Director tick
    (psql 写 tasks 行 + 等 runner 周期 / 直接 await runner.tick())
  - grep mission_alignment_reviews 表确认 row
  - write `dogfood-v9-phase-c-mission-director-verified.md`

Phase D · Lifecycle + Auditor 收尾 (10 min):
  - 用 service.transition 制造 1 个 candidate → replay transition (走三证据)
  - 模拟一次 auditor 周期审计, 产 AuditorReport, risk_level=P2 allow_release=True
  - 用 cockpit API GET /capabilities + /supervisor/auditor-reports 真查到
  - write `dogfood-v9-phase-d-lifecycle-auditor-verified.md`

Phase E · Retrospective (5 min):
  - write `LT-retrospective-v9.md` 记录:
    * 6 块软件 X.B 真 runtime 验证哪些 PASS / 哪些有 issue
    * 跨 family ensemble 实际 divergence_score 分布
    * 总成本 USD (按 ensemble_calls.total_cost_usd 求和)
  - write ≥ 3 张新 seeds/methodologies/*.yaml 候选 to docs/dist-output/seeds-new/v9/
    主题候选:
    * "ensemble divergence triage protocol" (高 divergence 怎么 fall back)
    * "Mission Director runner cadence tuning" (tick interval vs cost)
    * "auditor hat output as Canary→Production gate" (V7 §16.6 真实流程)

成功标准 (全部满足才算 done):
  - 5 个 phase 全跑完
  - 3 张 X.B DB 表都真有新 row (mission_alignment_reviews / lifecycle_transitions /
    auditor_reports / ensemble_calls 4 张, 因为 ensemble 也有)
  - cockpit API 真返 dogfood-v9 期间产生的数据 (非空 list)
  - dogfood-v9 期间 KUN 自己不改 service / 模块代码 (只产 docs/dist-output/)
  - retrospective + ≥ 3 候选 yaml seed 产出

约束:
  - 现有 dev_logs / seeds / kun / tests 全部保持 SHA 不变
  - 不改 V7 doc, 不改 X.B 已落地代码
  - dist-output/ 之外不写入

预估: 真跑 30-60 min, ensemble cost ~0.5-2 USD (取决于 gpt-5.5 + Anthropic pricing).
```

## 跑法

```bash
# Terminal 1: 起 KUN (确保 PG 起来, alembic upgrade head 跑过)
.venv/bin/uvicorn kun.api.main:app --host 0.0.0.0 --port 8000

# Terminal 2: 走 dogfood (沿用 scripts/dogfood_distill.py 的 WS pattern,
# 把 DOGFOOD_TASK 字符串替换成上面那段)
# - 或者直接用现有 cockpit_cli.py 后续看结果
```

## 期望产物

跑完后 `docs/dist-output/` 应该有 (含 v9 字样的):

- `dogfood-v9-phase-a-statecheck.md`
- `dogfood-v9-phase-b-ensemble-verified.md`
- `dogfood-v9-phase-c-mission-director-verified.md`
- `dogfood-v9-phase-d-lifecycle-auditor-verified.md`
- `LT-retrospective-v9.md`
- `seeds-new/v9/*.yaml` (≥ 3)

跑完后用户监督把 ≥ 3 个候选 yaml 走 capability_candidate → replay → ... lifecycle
正式入 `seeds/methodologies/`. **绝对不可以直接合并** —— 走 V7 §15 流程
(本 session 已经修过这个反模式: Phase 0.1 撤回 dogfood v8 的 5 yaml 违规合并).

---

## Smoke harness (本 session 已产)

不打 PG + 不打真 LLM, 跑得起来:

```
.venv/bin/python scripts/v7_xb_smoke.py
```

预期输出: 6/6 segments PASS. 是 dogfood v9 跑之前的 "代码层连通性已就绪"
预检.

Smoke 是软件可交付的最后一块 —— 真 dogfood v9 跑通是 operations 验证,
依赖用户的 PG / API key / uvicorn 就绪.
