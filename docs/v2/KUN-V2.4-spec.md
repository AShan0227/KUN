# KUN V2.4 spec — 真上线后磨合 (草稿, 等真用户数据)

**起源**: V2.3 心脏 + 真消费 + dogfood 全做完后, 基于真用户数据反推 V2.4. 这是草稿, 等真 dogfood 数据来后再迭代.

**版本关系**:
- V2.1 = M3.3 + M4 walking skeleton (基础设施)
- V2.2 = KUN-Lab + Hermes + Verification (实验体 + 大脑)
- V2.3 = 启 (Qi) + 协议涌现 + Predictive Coding + Pheromone (差异化 IP)
- V2.4 = **真用户磨合 + 协议库丰富化 + 自动化更深**

---

## 0. 起点状态 (V2.3 完成时)

| 子系统 | V2.3 状态 | V2.4 改进方向 |
|---|---|---|
| 启 (Qi) 窗口 | 时间窗口 + 日预算 + force_active | 多 user 多窗口 + 自动选窗口 |
| ProtocolRegistry | 4 lifecycle + HTTP/CLI + orchestrator 消费 | 协议库丰富化 (50+ 协议) + 协议自动 promote |
| Predictive Coding | hook + train pipeline + opt-in | 自动训练 cron (启窗口) + 模型 versioning |
| Pheromone | reinforce + decay + skill_selector boost | GraphTraversal 也用 + multi-tenant 隔离 |
| Darwin Gödel | 4 stop + strategy_evolver | 自动 strategy generation (LLM 生 strategy) |
| AntiGamingDetector | 7 套路 + orchestrator post-step | 自动新套路学习 (LLM 找新套路) |
| Verification 模板 | 4 task_type 默认 + merge | 自动生成 (基于 dogfood 真数据) |
| 用户 feedback API | POST /feedback + emit | UI 浮窗 + 真融合到决策 |
| metrics + Grafana | 9 metric + dashboard | + alerting + SLO |

---

## 1. V2.4 主线: 真用户磨合 + 协议库丰富

### 1.1 dogfood 长期化

V2.3 dogfood_v23.sh 是一次性脚本. V2.4 应:
- 每天 cron 跑 30 个 task (跨 4 task_type)
- 自动 collect: protocol 涌现 + Pheromone 强化 + PC error 趋势
- 周报: 哪些 task_type 协议沉淀好了, 哪些还没

### 1.2 协议库丰富化

V2.3 spec 写了 4 个 task_type (writing/coding/decision/research). V2.4 目标:
- 50+ 个 protocol 涌现 (跨 ~20 个 task_type)
- 用户能 search / browse: `kun protocol browse --task-type writing.creative.*`
- 协议 recommendation: 给定 task → top-3 候选 protocol (含 confidence)

### 1.3 协议自动 promote

V2.3 是手动 promote (kun protocol promote). V2.4 加 cron:
- 自动 promote experimental → shadow (跑 7 天 + 跑次 ≥ 100 + win_rate ≥ 0.6)
- 自动 promote shadow → canary (跑 7 天 + win_rate ≥ 0.7)
- 自动 promote canary → stable (跑 14 天 + win_rate ≥ 0.75)
- KUN_PROTOCOL_AUTO_PROMOTE_ENABLED=0 (default) → 用户 explicit 启用

---

## 2. V2.4 子主题

### 2.1 启 (Qi) 多窗口 + 自动选窗口

V2.3 单窗口 (默认 1-5 AM). V2.4:
- SoulFile 配置 N 个窗口 (e.g. 1-5 AM 全天 + 12-13 午休 + 18-19 晚饭)
- Qi 自动选**最低成本窗口** (基于 LLM provider 价格波动 / 自己 SLA)

### 2.2 Predictive Coding 自动训练 cron

V2.3 train pipeline 完, 但要手动调 PredictionTrainer.train(). V2.4:
- 启窗口内自动 cron: 每 6h 跑一次 train → 输出 prediction_model.json
- 鲲下次启动 load (KUN_PC_MODEL_PATH)
- model versioning: prediction_model_v0.1.json / v0.2.json (基于 train 时间)

### 2.3 Darwin Gödel 自动 strategy generation

V2.3 strategy_evolver 是手写 4 个 preset (top_low_temp / strong_mid / chain_of_thought / etc.). V2.4:
- LLM 生新 strategy (e.g. "用 chain-of-thought + temperature 0.8 + 给 LLM 看 3 个 example")
- 启窗口跑探索 → 拿真数据 → strategy_evolver 学新 strategy

### 2.4 AntiGaming 自学新套路

V2.3 是 7 个手写 pattern. V2.4:
- 用户标注 (👎 + reason="LLM 在偷懒") → 自动学 → 加新 pattern
- 启窗口跑反作弊探索 (故意诱导 LLM 偷懒) → 自动学新套路

### 2.5 Verification 模板自动生成

V2.3 是 4 个 task_type 手写模板. V2.4:
- 基于 dogfood 真数据自动总结哪种 verification kind 对哪种 task 有效
- LLM 生新 verification spec (e.g. "writing.creative 加 'avoid_cliche' check")

---

## 3. V2.4 跟 V2.3 兼容性

- V2.3 心脏 (Wire 38-50) 完全保留, V2.4 是上层加自动化
- 默认 OFF: KUN_PROTOCOL_AUTO_PROMOTE / KUN_PC_AUTO_TRAIN / KUN_DARWIN_AUTO_STRATEGY 都默认 0
- 用户 opt-in 才启用. V2.3 安全保留.

---

## 4. V2.4 排期 (草稿, 等真数据)

按 ROI:
- **第 1 月**: dogfood 长期化 + 协议库丰富化 (1.1 + 1.2)
- **第 2 月**: 协议自动 promote (1.3) + Predictive Coding 自动训练 (2.2)
- **第 3 月**: Darwin 自动 strategy (2.3) + AntiGaming 自学 (2.4)
- **第 4 月**: 多窗口 (2.1) + Verification 自动 (2.5) → tag v2.4.0

---

## 5. V2.4 设计原则 (跟 V2.3 一脉相承)

1. **U2 用户原话优先**: 不抢用户决策, 协议 auto-promote 默认 OFF.
2. **鲲 100% 稳定 + 启 100% 探索**: V2.4 加的自动化都在启窗口里.
3. **协议是 KUN IP**: V2.4 重点是协议库丰富化, 不是新功能.
4. **真数据反推**: V2.4 spec 草稿 → dogfood 数据 → 真 V2.4 spec.

---

**修订日期**: 2026-04-28
**实装日期**: 等 V2.3 真用户跑 30 天后
**注**: 这是基于 V2.3 现状的草稿. 等 dogfood 真数据来后, 主题/排期会调整.
