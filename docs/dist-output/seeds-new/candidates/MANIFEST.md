# dogfood v8 蒸馏 candidate seeds — 等待 lifecycle 验收

## 背景

2026-05-28 dogfood v8 真长任务跑通后, 蒸馏出 5 张方法论 yaml seeds。

**违规事件**: 这 5 张被直接合并到 `seeds/methodologies/` (commit `ad15bdc`), 当作 production 默认能力。**这违反**:

- **V7 §8 双账本约束**: "用户任务可以产生学习信号, 但这些信号只能作为候选证据进入治理链路, 不能直接修改 KUN 默认能力"
- **V7 §12 RSI 严格验收**: 任何 RSI candidate 必须走 9 阶段 lifecycle (其中 5 阶段严格验收: Replay → Holdout → Shadow → Canary → Production), 不允许跳过
- **V7 §15 capability lifecycle**: "KUN Runtime 默认只能消费 production 阶段能力"
- **V7 §16 生产闭环协议**: 7 层激活证据, 5 张 yaml 当前仅 Layer 1 (代码存在), 不到 Layer 4 (真实消费 with receipt), 严重缺验收

## 撤回动作 (V7 Phase 0.1)

5 张 yaml 已从 `seeds/methodologies/` 移除 (V7 commit 后做的), 现在归档在本目录 `candidates/`, 等待按 V7 §12.2 走完整 lifecycle 才能 promote 回 `seeds/methodologies/`。

| seed topic | 当前 lifecycle stage | 下一步 |
|---|---|---|
| cache_aware_llm_wakeup_scheduler | Candidate | 待 Replay (用历史 LLM 调度数据重放) |
| prompt_user_decision_at_irreversible_branch | Candidate | 待 Replay (用历史 decision_point 数据重放) |
| runtime_todo_tracker_state_machine | Candidate | 待 Replay (用历史 task plan 数据重放) |
| silence_detector_for_long_task_monitoring | Candidate | 待 Replay (用历史 long task event 数据重放) |
| worker_agent_spawner_prompt_isolation | Candidate | 待 Replay (用历史 parallel sub-task 数据重放) |

## 进入 production 的路径

每张 candidate 必须走完整 lifecycle, 缺一不可:

```
现在 → Candidate (本目录)
       ↓
       Replay: 用历史任务数据重放, 验证 candidate 不劣于现 baseline
       ↓
       Holdout: 隔离一组 held-out 任务测候选, 不影响 production
       ↓
       Shadow: 与 production 并行跑, 输出对比, 不真切换
       ↓
       Canary: 小比例 (5-15%) 切到候选, 监控指标
       ↓
       user explicit approval (CollaborationTicket)
       ↓
       Production: 全量切换, 重新写入 seeds/methodologies/
       ↓
       Monitor: 持续观察指标, 回归则 rollback
```

详见 `strategy_replay_report.md` (启 strategy replay 三类产物之一)
和 `process_audit.md` (三类产物之二, 说明原链路哪里浅 / 哪里错)。

## 历史 lesson

这次违规暴露的工程教训:
1. **dogfood / 用户任务产物没有强 lifecycle gate enforce**, 容易"开发完成"误当"生产链路生效" (Claude Code 给用户的真实复盘)
2. KUN V7 §16 生产闭环协议 + §23.1.0 产品魂级硬规则**专门为防此类违规设计**
3. 本次撤回 + 走 lifecycle 是 V7 §19.3 "dogfood v8 反例 case study" 的修复路径
