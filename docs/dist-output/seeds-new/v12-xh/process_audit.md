# V7 Phase X.H 蒸的 2 张 seed — process audit (V7 §12.3 evidence 之一)

> 用户拷问 X.M 起因: X.H.META 自己 cp 2 张 yaml 进 `seeds/methodologies/`
> 没走 X.D-1 的正式审议流程 (`process_audit.md` + `strategy_replay_report.md`),
> 这是 RSI 写侧自身的半孤儿. X.M 补齐.
>
> 本 audit 不评估 seed 内容好坏 (那是 strategy_replay_report 的事), 而是回答:
> **为什么 KUN 之前没有这条方法论, 这反映了 KUN 哪里的工程缺口**。

---

## 2 张 v12-xh seed 反映的工程缺口

### 缺口 1 — 工程团队 (LLM + 人) 把"接 import"当成"接生产"

**candidate 涉及**: `production_path_wiring_audit_before_claim.yaml`

**原链路问题**:
- X.B.MF-1 蒸出的方法论说"`grep import` 验证非测试代码 import 了 X"
  作为"接生产"前的强制 grep. 但 import 在 Python 里**也可以只是
  type hint / 注释引用**, 不等于真生产 caller 实例化它.
- 三次 release (X.E + X.G + DIST-D) 用这条方法论审, 都"过", 但生产入口
  `kun/engineering/orchestrator.py:1312` 实际从不传新参数. **方法论自
  己的颗粒度错位**, 把 import 当生产 wired.

**audit 结论**: 这是 KUN **蒸馏方法论时的"自指错位"** — RSI 写侧蒸出
来的方法论, 自己就是低质量的方法论 (颗粒度错). 之前的 X.B.MF-1 audit
方法论只检查 import, 没检查"是否在 production-entries 列出的真生产入
口里被实例化". X.H 自检发现后补的这条 seed, 是把"颗粒度"显式拉到正确
层级 — 必须 grep production entry 里的真实例化, 不止 import.

**MERGE 推荐**: ✅ 通过, 直接合 `seeds/methodologies/`. 修正颗粒度的方
法论本身就是 RSI 闭环的修正信号.

---

### 缺口 2 — opt-in 默认 OFF + 无消费者强制 = 静默孤儿循环

**candidate 涉及**: `opt_in_feature_must_be_consumed_at_production_entry.yaml`

**原链路问题**:
- KUN 当前对 cost-sensitive 特性默认 env OFF (e.g.
  `KUN_V7_TRIFECTA_ENABLED=true` 默认 false 时全不 fire). 这是合理的产品
  默认.
- 但**没有自动化告警** "feature X 已经在 class 里 60 天, 但任何生产入
  口都不传 → 大概率孤儿". 没强制 a-b-c-d-e 5 步 (加 bundle 字段 / 加
  EXPECTED_BUNDLE_KEYS / 加 env 开关 / 加 enabled_flags / 加 CI 守卫).
- X.E + X.G + DIST-D 三次都是: ctor 加参数 → unit test 显式传 → dogfood
  脚本显式传 → 生产从不传. 测试 green 给假信号"已 wire". 这是
  R3 + R4 双重失败模式.

**audit 结论**: 这是 KUN **缺自动化"opt-in 消费者审计"**. seed 内容把
"必须 5 步走 bundle" 显式写成强约束, 加上 X.H.PROD-ENTRY-WIRE 已经实
装的 CI 测试 (`tests/integration/test_production_entry_runtime_bundle.py`)
作为自动化护栏, 合起来防住静默孤儿模式重复.

**MERGE 推荐**: ✅ 通过, 直接合 `seeds/methodologies/`.

---

## 整体 audit 结论

2 张 v12-xh seed 是**X.H 自检过程产物**, 不是凭空想象. 自检的源是 5 根
因 (R1-R5) + 8 机制盘点 + X.H/X.I 真改 commit + grep 真生产入口的真实
反例 (orchestrator.py:1312 真漏 3 个特性).

| seed | 反映 KUN 哪种 gap | 证据强度 |
|---|---|---|
| `production_path_wiring_audit_before_claim.yaml` | RSI 写侧自指错位 — 之前蒸的"接生产"方法论颗粒度错位 | X.B.MF-1 方法论 + 3 次 release 真实失败案例 (X.E/G/DIST-D) |
| `opt_in_feature_must_be_consumed_at_production_entry.yaml` | opt-in 无消费者强制 = 静默孤儿循环 | X.H AST audit test + bundle 5 步 + enabled_flags |

2 张全部通过 process audit:

| seed | 决议 | 原因 |
|---|---|---|
| `production_path_wiring_audit_before_claim.yaml` | **MERGE** | 修正颗粒度方法论本身是 RSI 修正信号 |
| `opt_in_feature_must_be_consumed_at_production_entry.yaml` | **MERGE** | 防 R3 + R4 静默孤儿; X.H AST CI 已落地强制执行 |

---

## V7 工程意义

audit 暴露了 KUN RSI 自身的**"自指错位"** 问题: 蒸馏方法论自己也会有缺
陷, 而且这种缺陷在 RSI 闭环里会**循环放大** (蒸出来 → 后续工作用 →
重复犯同样的错). X.H 是 KUN 第一次在 RSI 写侧承认自身蒸出的方法论错
位, 并蒸出修正方法论. X.M 补的 ProcessAudit 让这次修正走完整 V7 §12.3
RSI 验收流程, 不绕过.

**自指错位**这条经验本身也值得未来再蒸 (X.O 候选): 任何一次"我们重复
犯了同一个错"的复盘, 必须问一遍"是不是我们蒸的方法论本身就有缺陷".
