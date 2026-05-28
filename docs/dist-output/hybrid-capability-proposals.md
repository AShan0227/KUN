# Hybrid Capability Proposals

## Phase E 目标

把新 seeds 与 KUN 已有硬能力（RSI、Anti-drift、Adapter Router、RecursivePlanner、七角色并行、External Supervisor Pool、Gate）融合，形成 KUN 专属增强，而不是简单复制 Claude Code。

## Proposal 1：Anti-drift Todo Tracker

- 组合：`runtime_todo_tracker_state_machine` × GoalAnchor × PlanReviewHeartbeat × RecursivePlanner。
- 机制：RecursivePlanner 生成 plan tree 后，RuntimeTodoTracker 为 leaf node 建 runtime todo；PlanReviewHeartbeat 每 N 步检查当前 in_progress 是否仍服务于 GoalAnchor。
- KUN 专属增值：Claude Code 的 TodoWrite 偏人工可观测；KUN 可把 todo 状态接入 anti-drift supervisor，让漂移检测有结构化输入。
- 实验：长任务中插入 scope expansion，观察 todo tracker 是否暂停并请求 review。
- ROI：Tier 1，高收益，中低改动。

## Proposal 2：Parallel Worker + 七角色治理

- 组合：`worker_agent_spawner_prompt_isolation` × 七角色 × Supervisor Pool × ResourceQuota × ExplorationPenalty。
- 机制：Director 根据依赖图 spawn worker；Executor worker 做局部实现；Tester worker 做验证；Supervisor Pool 审查冲突；Quota 控制并发成本；Penalty 防止失败 signature 反复探索。
- KUN 专属增值：Claude Code 的 subagent 主要靠主 agent discipline；KUN 可用现有治理层对 worker 并发做预算、去重、审计和回滚。
- 实验：3 个独立 adapter 任务并行，验证写入范围隔离与 completion event。
- ROI：Tier 2，高收益，中等改动。

## Proposal 3：User Decision Gate

- 组合：`prompt_user_decision_at_irreversible_branch` × LongTaskInputRouter × Gate × checkpoint/resume。
- 机制：当 Router 或 Supervisor 判定出现产品分叉，Gate 不直接 reject，而是生成 user_decision_required；用户选择写入 event log 后从 checkpoint 恢复。
- KUN 专属增值：把 Claude Code 的 AskUserQuestion 升级为可审计、可恢复、可被 Gate 触发的治理节点。
- 实验：多租户/单租户分叉任务，验证用户选择进入后续 context 且不会重复询问。
- ROI：Tier 1，中高收益，低到中改动。

## Proposal 4：Silence-aware External Supervisor

- 组合：`silence_detector_for_long_task_monitoring` × External Supervisor Pool × PlanReviewHeartbeat × status enum。
- 机制：long_task 事件流若超过阈值静默，Supervisor 不直接失败任务，而是生成 `suspected_stuck` review；External Supervisor 判断是正常等待、工具卡死、LLM 漂移还是 checkpoint 故障。
- KUN 专属增值：从 Claude Code 的 grep Monitor 升级为事件流治理和多维审计。
- 实验：构造正常长工具调用、工具卡死、executor 漂移三类事件流，要求 verdict 区分。
- ROI：Tier 1，高收益，低改动。

## Proposal 5：Cache-aware RSI Scheduler

- 组合：`cache_aware_llm_wakeup_scheduler` × idle_batch × LLMRouter × ResourceQuota。
- 机制：RSI 实验和 methodology distill 按 cache window 分组，LLMRouter 记录 prompt prefix hash 与 cache hit/miss，ResourceQuota 将 cache miss 作为成本信号。
- KUN 专属增值：Claude Code 只在单 agent 任务节奏中使用 cache 意识；KUN 可把它纳入全局 RSI 成本治理。
- 实验：对同一 distill workload 比较固定 300s、cache 内短唤醒、长批处理三种策略成本。
- ROI：Tier 2，中收益，中改动。

## 推荐落地顺序

1. Silence-aware External Supervisor：最小改动，直接补长任务卡死盲区。
2. Anti-drift Todo Tracker：把长任务可观测性接入 KUN anti-drift 核心。
3. User Decision Gate：降低产品方向擅自决策风险。
4. Parallel Worker + 七角色治理：吞吐收益大，但需要更严写入隔离。
5. Cache-aware RSI Scheduler：成本优化型，适合在前几项稳定后做。
