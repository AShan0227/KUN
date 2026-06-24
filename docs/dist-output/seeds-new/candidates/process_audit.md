# 5 张 dogfood v8 candidate seeds — process audit (说明原链路哪里浅 / 哪里错)

> V7 §12.3 强制启 strategy replay 三类产物之二.
>
> 这份 audit 不评估 candidate 内容好坏 (那是 strategy_replay_report 的事), 而是回答: **为什么 KUN 原本链路里没有这些 capability, 这是哪里的工程缺口**。

## 5 张 candidate 反映的原链路 4 个普遍缺口

### 缺口 A: 调度层不感知 LLM provider 内部缓存机制

**candidate 涉及**: cache_aware_llm_wakeup_scheduler

**原链路问题**:
- KUN 的 `idle_batch` / `ScheduleWakeup` 是统一时间间隔机制, 不知道 Anthropic prompt cache 有 5 分钟 TTL
- 调度 300s 轮询恰恰错过 5min cache window, cost 翻倍
- KUN 设计前提是 "LLM provider 是黑盒", 但实际 provider 内部有 cache / batch 优化空间

**audit 结论**: 这是 KUN **抽象层泄漏** —— 上层调度需要懂下层 provider 内部状态. V7 §11.3 加 provider cache awareness 到 LLM matrix 是治本.

### 缺口 B: 决策点判定从 KUN-internal 跨到 user-facing 的耦合

**candidate 涉及**: prompt_user_decision_at_irreversible_branch

**原链路问题**:
- KUN `decision_point_classifier` (DIST-C) 是 KUN 内部 6 类硬规则
- 但**真正"需要用户决策"的不是 KUN 内部判定**, 是 LLM agent 自己识别"我接下来这步不可逆 / 影响产品方向"
- 现有规则覆盖 destructive operation, 但不覆盖 "agent 自己怀疑这一步是否走对"
- 缺一个 **"agent 主动 raise 用户决策"** 的接口

**audit 结论**: KUN 把决策点判定**集中在 classifier 规则层**, 缺 agent 自己主动 raise 的路径. Claude Code 风格是 agent 自己判断 + 主动停下问. V7 §10.3.3 决策权三档 + §10.2.5 监督者递归不变量已部分补这个 gap.

### 缺口 C: 静态 plan tree vs 动态 todo 状态机的失配

**candidate 涉及**: runtime_todo_tracker_state_machine

**原链路问题**:
- KUN RecursivePlanner 产 **静态 PlanTree** (任务开始时定结构, 跑中不变)
- 但真实复杂任务**经常需要中途加任务 / 删任务 / 换顺序**
- KUN 现在的方案是 `PlanChangeProposal` 重写 TaskPlanVersion, 但**粒度太粗** (重写整个 plan), 不像 Claude Code TodoWrite 那样**一次只改 1 个 todo**

**audit 结论**: KUN 设计上假设了"plan 是稳定的", 缺 runtime 动态 todo 状态机。V7 §10.3 双线动态优化 + §12.4 RSI 三线 (现在线 + 未来线) 部分补 gap. 但 runtime todo state machine 仍需单独 capability 实装.

### 缺口 D: 长任务静默检测 — 无 anomaly = 不一定 healthy

**candidate 涉及**: silence_detector_for_long_task_monitoring + worker_agent_spawner_prompt_isolation

**原链路问题**:
- KUN Watchtower 规则引擎检测**显式 anomaly** (timeout / failure / cost spike)
- 但**任务"卡住没事件"** (LLM 沉思 / 死锁 / 等输入) **没 anomaly 触发**
- 缺 monitor 五维 watch: success / Traceback / FAILED / OOM / **silence**

**audit 结论**: KUN 设计假设了"事件流就能反映任务状态", 缺**主动探活 / 静默检测**。 V7 §10.2.3 External Supervisor 持续 watchdog 部分补 gap (持续 tick 看主任务), 但需要明确加 "silence == anomaly" 这条规则.

### 缺口 E (跨多个 candidate): 并行 sub-agent 派发协议不完整

**candidate 涉及**: worker_agent_spawner_prompt_isolation

**原链路问题**:
- KUN ExecutorLoop 单次 dispatch `list[ToolCall]` 已支持并行 (LT.E)
- 但**未支持"派发独立 prompt 上下文的子 agent"**, 只能派发独立 tool call (无完整 LLM 上下文)
- Claude Code Task tool 派发的是**独立 LLM agent**, 有自己的 prompt + system + history
- KUN 现在没这种能力, 复杂任务并行受限

**audit 结论**: KUN 并行能力**深度不够** — 工具级并行有, agent 级并行无. 跟 OpenClaw / Hermes ensemble 形态有差距. 需要新建 `WorkerAgentSpawner` 子模块.

---

## 整体 audit 结论

5 张 candidate 都不是 "KUN 自己想到了写出来", 是 **gpt-5.5 (在 dogfood v8 中) 蒸馏自 Claude Code 工程经验**:
- 50% 反映 KUN **抽象层泄漏 / 设计假设过强**
- 30% 反映 KUN **某个能力"开发了但没真用"** (V7 §16 生产闭环硬规则要解决的)
- 20% 反映 KUN **跟 OpenClaw / Hermes 形态的真实差距** (V7 §4 三方对标想吸收的)

**这 5 张 candidate 应该走 lifecycle 验收**, 通过的进 production, 不通过的归 known_limit. 不应该跳过 lifecycle 直接合并 (dogfood v8 已经犯过, V7 §19.3 反例 + Phase 0.1 撤回是修复)。

---

## V7 工程意义

audit 暴露了 KUN 设计的 5 个真实 gap (A-E). 这些 gap 应该写入:
- V7 §25 差异附录 (V6 当前实装度评估)
- 启 (Qi) `bug_root_cause_cases` 库 (DIST-E)
- 后续 Phase 实施时优先级排序依据

**这是过去线 (post-hoc retrospect) 的真实输出, 不是 ceremonial paperwork**。
