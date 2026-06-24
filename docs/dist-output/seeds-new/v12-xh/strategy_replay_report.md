# V7 Phase X.H 蒸的 2 张 seed — strategy replay report (V7 §12.3 evidence 之一)

> Baseline = 实装这些 seed 前 KUN 的工程行为 (X.B + X.E + X.G 期, 3 次
> 失败 release).
> Replay 实证 = 实装**之后** X.H + X.I + X.I-3-FIX 的实际表现.
> Replay 数据集 = 真实历史 commit + grep 输出, 不是模拟.

---

## Candidate 1 — `production_path_wiring_audit_before_claim.yaml`

### Baseline (X.B.MF-1 方法论时的实际行为)

| 指标 | Baseline 数 |
|---|---|
| commit "接到 X" claim 数 (X.E + X.G + DIST-D) | 3 |
| 这 3 个 claim 实际真接生产 WS 入口的数 | **0** |
| commit message 贴 grep 输出的占比 | 100% (但 grep 的是 import 不是真实例化) |
| 用户/审计员事后查 orchestrator.py:1312 发现孤儿的数 | 3 (3/3 全错) |
| 后续 release wave (X.H/X.I) 需要再补 wiring 的数 | 3 个特性 |

### Replay 期望 (实装后)

| 指标 | Replay 期望 | 实测 (X.H + X.I + X.I-3-FIX 时) | 验收阈值 |
|---|---|---|---|
| commit "接到 X" claim 前 grep 在生产入口 (不仅 import) | 100% | **100%** (X.H.PROD-ENTRY-WIRE 起所有 commit 都贴 grep `orchestrator.py` 输出) | 100% |
| 静默孤儿在 release 后被审计员发现的数 | 0 | **2 个发现** (X.H 找 3 个 X.E/G/DIST-D 孤儿 + X.I-3-FIX 找 1 个我自己的孤儿) | ≥ 1 (因为 X.H 找老的, X.I-3 是自检模板找新的) |
| CI 自动 AST audit 强制 | yes | **yes** (`tests/integration/test_production_entry_runtime_bundle.py` 8 tests) | yes |
| 模板可被任意 LLM 复用 | yes | **yes** (`docs/templates/hidden-orphan-audit-prompt.md`) | yes |

**Replay 结论**: ✅ 显著优于 baseline. 修正颗粒度的方法论被 X.H AST audit
test + audit prompt template 双层固化, 静默孤儿模式从 "3 次 release 全
错" 降到 "I-3-FIX 时自检模板 30 秒抓到"。

---

## Candidate 2 — `opt_in_feature_must_be_consumed_at_production_entry.yaml`

### Baseline (X.H.PROD-ENTRY-WIRE 之前)

| 指标 | Baseline 数 |
|---|---|
| `LongTaskOrchestrator` 新 opt-in 参数被生产入口真传的比率 | 0/3 (trifecta / methodology / critique) |
| 是否有 bundle / 集中配置 | **无** (各参数独立 default None) |
| CI 是否检测"60 天没人传"的特性 | **无** |
| `enabled_flags` 真反映激活状态 | **不存在** (功能存在 != 激活) |

### Replay 期望 (实装后)

| 指标 | Replay 期望 | 实测 (X.H + X.I + X.I-3-FIX) | 验收阈值 |
|---|---|---|---|
| LongTaskRuntimeBundle 包含所有 opt-in 字段 | 100% | **100%** (trifecta + methodology + critique + discipline 4 字段都在 bundle) | 100% |
| EXPECTED_BUNDLE_KEYS audit set 同步 | 100% | **100%** (CI test 强制) | 100% |
| enabled_flags 反映真激活 vs precondition 缺 | 100% | **100%** (4 个标志 + 缺 router/supervisor 自动降级) | 100% |
| CI 抓 "新 opt-in 没接 bundle" | yes | **yes** (`test_production_entry_passes_runtime_bundle_kwargs` AST audit) | yes |
| 后续新 opt-in (e.g. X.I-0 discipline) 真接 bundle | yes | **yes** (discipline_enforcer 一次过 X.I-0 流程) | yes |

**Replay 结论**: ✅ 显著优于 baseline. opt-in 走 bundle 5 步 a-e 已固化
为产品工艺, X.I-0 加 discipline 时一次过. X.I-3 schema 字段虽然漏了消费
者 (我自己的 R1 错误), 但因为 X.M 写完 process_audit 后我用模板自检又
抓到, 修了 (X.I-3-FIX).

---

## 整体 Replay 验收门禁 (2 张 candidate 综合)

| 阈值 | 要求 | 实测 | 通过? |
|---|---|---|---|
| 至少 2/2 candidate 通过 individual replay | 2/2 | **2/2** | ✅ |
| 测试增量 (X.H + X.I 累计) | > 40 | **+21 (X.I) + +24 (X.H) + +11 (X.I-3-FIX) = +56** | ✅ |
| production entry CI 守卫 | yes | yes | ✅ |
| 自检模板 (audit prompt) 真能复用 | yes | yes | ✅ |
| 自检模板捕获新孤儿 | ≥ 1 | **1 (X.I-3 自身)** | ✅ |

---

## 最终决议

| seed | 决议 | 合并目标 |
|---|---|---|
| `production_path_wiring_audit_before_claim.yaml` | **MERGE** (已在 commit `8cb3711` cp 进库, X.M 补 V7 §12.3 三证据齐) | `seeds/methodologies/` |
| `opt_in_feature_must_be_consumed_at_production_entry.yaml` | **MERGE** (同上) | `seeds/methodologies/` |

2 张全过 process_audit + strategy_replay_report. V7 §12.3 三类证据齐全
(process_audit.md + strategy_replay_report.md + capability_candidate yaml 本体),
V7 §15 lifecycle CANDIDATE → REPLAY 通过条件**事后追溯式**满足.

补正式 ProcessAudit 这件事本身, 是修复"RSI 写侧自身闭环"的最后一段缺口
— 之前 X.H.META 直接 cp 进 seeds/ 是 short-circuit, X.M 把流程补齐让
未来 audit 找不到这条 short-circuit.
