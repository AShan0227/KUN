# 5 张 dogfood v8 candidate seeds — strategy replay 报告 (stub)

> **状态**: stub — Replay 阶段未跑, 待真实历史数据校准. V7 §12.3 强制三类证据之一.

按 V7 §12.3, 每张 capability_candidate 进 Replay 阶段必须有 strategy_replay_report (旧策略 vs 新策略指标对比)。

由于 5 张 candidate 都是 **方法论级别** (不是 runtime code patch), 它们的"strategy"实际是 prompt 引导 / methodology selection 偏置, 不是直接代码改动。Replay 验证方式相应调整。

## Candidate 1: cache_aware_llm_wakeup_scheduler

| 指标 | Baseline (无 candidate) | Replay 期望 (含 candidate) | 验收阈值 |
|---|---|---|---|
| 平均 wakeup interval 偏离 cache TTL 比例 | 待测 | < baseline | 不劣于 baseline |
| Prompt prefix cache hit rate | 待测 | ≥ baseline | 不劣于 |
| LLM call 月成本 (含 wakeup) | 待测 | ≤ baseline | 不劣于 |

**Replay 数据集**: KUN 历史 idle_batch / ScheduleWakeup 事件 (近 1 个月).

## Candidate 2: prompt_user_decision_at_irreversible_branch

| 指标 | Baseline | Replay 期望 | 验收阈值 |
|---|---|---|---|
| 不可逆操作误执行率 | 待测 | < baseline | 显著下降 |
| 用户决策 ticket 触发数 | 待测 | ≥ baseline | 应增加 (但不应过量) |
| 任务完成率 | 待测 | ≥ baseline - 5% | 不能因增加 ticket 大幅下降 |

**Replay 数据集**: KUN 历史 decision_point_classifier 命中事件 (近 1 个月).

## Candidate 3: runtime_todo_tracker_state_machine

| 指标 | Baseline | Replay 期望 | 验收阈值 |
|---|---|---|---|
| PlanTree depth (静态拆解) vs runtime todo (动态状态机) 覆盖度 | 待测 | runtime todo 覆盖度 > PlanTree | 显著高 |
| 任务中途 plan_change 适应度 | 待测 | runtime todo 比 PlanTree 适应快 | 时间差 < 30s |

**Replay 数据集**: KUN 历史长任务 task_plan_version + plan_change_proposal (近 1 个月).

## Candidate 4: silence_detector_for_long_task_monitoring

| 指标 | Baseline | Replay 期望 | 验收阈值 |
|---|---|---|---|
| 长任务静默/卡死检出率 | 待测 | > baseline | 显著提高 |
| 误报率 | 待测 | < 10% | 阈值固定 |
| 平均检出延迟 | 待测 | < 5 min | 阈值固定 |

**Replay 数据集**: KUN 历史长任务 elapsed_seconds + tool_call 事件 (近 1 个月).

## Candidate 5: worker_agent_spawner_prompt_isolation

| 指标 | Baseline | Replay 期望 | 验收阈值 |
|---|---|---|---|
| 并行 sub-agent 触发率 | 待测 | > baseline | 显著提高 |
| sub-agent prompt 互污染率 | 待测 | < 5% | 阈值固定 |
| 主线任务延迟 | 待测 | < baseline + 10% | 不大幅恶化 |

**Replay 数据集**: KUN 历史 ExecutorLoop 并行 tool_call 事件 (近 1 个月).

---

## 整体 Replay 验收门禁 (5 张 candidate 整体)

**全部 5 张通过 Replay 阶段需满足**:
- 5 张各自 ≥ 3 个指标"不劣于 baseline"
- 0 张指标显著退化 (差 > 5%)
- 5 张 yaml.safe_load 全过
- 5 张引用的 KUN 模块 grep verify 真实存在

**任一不过**: 那张 candidate 退回 known_limit, 不进 Holdout 阶段。

---

## 下一步 (待执行, 不在本 stub 范围)

1. 部署 metrics 收集, 跑 1 周 baseline 数据
2. 跑历史数据回放 + candidate metrics
3. 写真实 Replay 报告替换本 stub
4. Replay 过 → 进 Holdout (V7 §12.2 / §15)
5. ... 直到 user explicit approval → Production

本 stub 是 V7 Phase 0.1 立项前置动作产物, 不是真实 Replay 结果. **不允许凭本 stub 把 candidate 推进到 Holdout**。
