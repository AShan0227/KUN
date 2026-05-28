# RSI Mini 实验计划

## Phase D 目标

对新 seed 进行 Selector + External Supervisor + Gate 的最小实验验证。实验不直接改生产代码，只验证 seed 是否能被正确选择、是否能产出可执行建议、是否应进入 staging。

## 通用实验管线

1. **Selector**：给定 synthetic task / dev_log snippet，判断是否命中新 seed。
2. **External Supervisor**：审查 selector 理由、适用边界、误触发风险。
3. **Gate**：根据 evidence、risk、expected ROI 给出 `promote_to_staging / revise_seed / reject`。
4. **记录**：每个实验写入 outcome、false_positive、false_negative、recommended_next_action。

## 实验 1：runtime_todo_tracker_state_machine

- 输入任务：一个 12 步长任务，其中第 4 步用户改变优先级，第 7 步出现阻塞。
- Selector 期望：命中 `runtime_todo_tracker_state_machine`，理由是长任务超过 5 步且需要恢复/可观测。
- External Supervisor 检查：
  - 是否误把简单任务也要求 todo tracker。
  - 是否强制最多一个 in_progress。
  - 是否保留历史 todo 状态。
- Gate 标准：若 selector 命中准确且 action 可转为 service spec，则 staging。

## 实验 2：worker_agent_spawner_prompt_isolation

- 输入任务：实现 3 个互不依赖 adapter，每个 adapter 写入不同目录。
- Selector 期望：命中 worker spawner seed，不命中时视为 false negative。
- External Supervisor 检查：
  - prompt 是否自包含。
  - 写入范围是否不重叠。
  - 主线是否没有把阻塞关键路径交给 worker 后空等。
- Gate 标准：若并行收益明确且冲突风险低，promote_to_staging；若依赖图不清晰，revise_seed。

## 实验 3：prompt_user_decision_at_irreversible_branch

- 输入任务：用户要求“优化部署方案”，中途出现“改为多租户还是单租户”的产品分叉。
- Selector 期望：命中 user decision seed。
- External Supervisor 检查：
  - 是否确实是产品/范围决策，而非普通实现细节。
  - 选项是否互斥、包含推荐项与影响说明。
  - checkpoint/resume 是否纳入方案。
- Gate 标准：若能减少擅自决策风险且不过度打扰，promote_to_staging。

## 实验 4：silence_detector_for_long_task_monitoring

- 输入事件流：long_task.started 后 12 分钟无 checkpoint/heartbeat，但进程未退出。
- Selector 期望：命中 silence detector seed。
- External Supervisor 检查：
  - silence_threshold 是否合理。
  - 是否区分正常长工具调用与卡死。
  - 是否按 task_id 隔离 last_progress_at。
- Gate 标准：若能发现 stuck 且不会直接 kill 正常任务，promote_to_staging。

## 实验 5：cache_aware_llm_wakeup_scheduler

- 输入场景：同一 task 在 60s、300s、1200s 三种唤醒策略下多轮调用。
- Selector 期望：命中 cache-aware scheduler seed。
- External Supervisor 检查：
  - 是否依赖 provider cache TTL。
  - 是否保持 stable prompt prefix。
  - 是否记录 cache hit/miss。
- Gate 标准：若成本模型可观测且不会牺牲任务质量，staging；若 provider 不支持 caching，标 applicability 限制。

## 汇总指标

- selector_precision >= 0.8
- selector_recall >= 0.7
- external_supervisor false_positive_block >= 1 个样例
- gate 只允许低风险 seed 进入 staging
- 每个 staging seed 必须有 owner、rollback_on、sample_rate
