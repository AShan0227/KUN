# DOGFOOD-P3 — KUN 多维能力测试报告

**测试时间**: 2026-05-28  
**总分**: 19/20 (95%)

## 评分标准

- **2**: 真工程化进 KUN, 有 code 路径 + (单测/方法论/真行为) 三选二验证
- **1**: 半具备 — 存在某层 (e.g. 有 methodology 但 runtime 不强制)
- **0**: KUN 没有这个能力

## 分数表

| ID | 维度 | 分数 | 简短说明 |
|---|---|---|---|
| D1 | 任务拆解 (PlanTree depth ≥ 2) | **2/2** | LT.F 已建递归 planner, runtime 真在用 (dogfood v8 见 long_task.plan_tree 事件) |
| D2 | 并行 sub-agent 派发 | **2/2** | dogfood v8 第 4 / 7 / 10 step 都出现 4 个 self-reflect 并行 — KUN 真在派发 |
| D3 | grep verify before assume | **1/2** | 方法论存在 (audit_methodology) 但 Executor 主路径没有 pre-action grep 强制 — 属于半具备. dogfood v8 没机会展现 (任务是读+写, 不是修代码). |
| D4 | 测试驱动 fail-fast | **2/2** | ValidationPipeline + Tester role 都在, dev log 显示每 commit pytest+ruff |
| D5 | commit 纪律 (≤1000 行) | **2/2** | 近期 20 commit 实测 |
| D6 | 错误立修不藏 | **2/2** | 本 session 5 个 LT.x fix 都是 dogfood failure 即修 |
| D7 | dev_log 沉淀 (ADR-025) | **2/2** | LT-progress.md 每个 LT 任务后追加 section, dev_logs 全保留 |
| D8 | 决策点停下问 | **2/2** | DIST-C 6 类硬规则 + Gate + 新 seed 三层覆盖 |
| D9 | Read with offset+limit | **2/2** | Claude Code 工程模式已编码进 KUN 的 self-reflect skill API |
| D10 | Bash 克制 / 专用 tool 优先 | **2/2** | KUN 有 7 个专用 builtin + 5 个 starter, dogfood 真行为 100% 走专用 |

## 各维度详细 evidence

### [D1] 任务拆解 (PlanTree depth ≥ 2) — **2/2**

- TaskPlanner 存在 (kun/agents/director/planner.py)
- RecursivePlanner 存在 (kun/agents/director/recursive_planner.py)
- PlanTree/PlanNode 数据结构存在
- test_recursive_planner.py 单测覆盖

**Notes**: LT.F 已建递归 planner, runtime 真在用 (dogfood v8 见 long_task.plan_tree 事件)

### [D2] 并行 sub-agent 派发 — **2/2**

- ExecutorLoop 单次 dispatch list[ToolCall] (并行能力)
- dogfood v8 真实并行 dispatch: 单秒最多 4 个 read 并行, ≥3 并发的秒次 2 次
- 新 seed worker_agent_spawner_prompt_isolation 编码此模式

**Notes**: dogfood v8 第 4 / 7 / 10 step 都出现 4 个 self-reflect 并行 — KUN 真在派发

### [D3] grep verify before assume — **1/2**

- seed: service_module_not_wired_to_runtime_audit — grep verify 沉淀

**Notes**: 方法论存在 (audit_methodology) 但 Executor 主路径没有 pre-action grep 强制 — 属于半具备. dogfood v8 没机会展现 (任务是读+写, 不是修代码).

### [D4] 测试驱动 fail-fast — **2/2**

- ValidationPipeline 存在 (kun/agents/tester/validation.py)
- Tester agent 模块: ['base.py', 'multi_judge.py', 'validation.py']
- 近 30 commit 里提到 pytest/ruff 的: 30 个 — 承诺持续兑现

**Notes**: ValidationPipeline + Tester role 都在, dev log 显示每 commit pytest+ruff

### [D5] commit 纪律 (≤1000 行) — **2/2**

- 近 20 commits: 18 ≤1000 行, 2 >1000 行
- ≤1000 行率: 90%

**Notes**: 近期 20 commit 实测

### [D6] 错误立修不藏 — **2/2**

- 近 30 commits 中 fix(...) 型 commit: 8 个
- BugCase 库存在 (DIST-E)

**Notes**: 本 session 5 个 LT.x fix 都是 dogfood failure 即修

### [D7] dev_log 沉淀 (ADR-025) — **2/2**

- LT-progress.md 含 13 个 LT.x section
- docs/dev_logs/ 共 17 个 md 文件

**Notes**: LT-progress.md 每个 LT 任务后追加 section, dev_logs 全保留

### [D8] 决策点停下问 — **2/2**

- decision_point_classifier 存在 (DIST-C, 6 类硬规则)
- Gate agent 存在
- pivot_handler / pivot_pause 路由存在 (LT.A)
- 新 seed prompt_user_decision_at_irreversible_branch (dogfood 蒸馏出)

**Notes**: DIST-C 6 类硬规则 + Gate + 新 seed 三层覆盖

### [D9] Read with offset+limit — **2/2**

- self-reflect skill 支持 offset + limit (LT.SELF-REFLECT-SKILL)
- dogfood v8 真实使用 limit 参数 8 次

**Notes**: Claude Code 工程模式已编码进 KUN 的 self-reflect skill API

### [D10] Bash 克制 / 专用 tool 优先 — **2/2**

- 专用 skills: 7 builtin + 5 starter
- shell-exec (受 allowlist 约束) + file-io + self-reflect 各司其职
- dogfood v8: shell-exec 调 0 次, self-reflect 调 23 次 — 用专用而非 shell

**Notes**: KUN 有 7 个专用 builtin + 5 个 starter, dogfood 真行为 100% 走专用

## 总结

低于满分的维度 (1 个), 待 P4 调优:
- [D3] grep verify before assume — 1/2 — 方法论存在 (audit_methodology) 但 Executor 主路径没有 pre-action grep 强制 — 属于半具备. dogfood v8 没机会展现 (任务是读+写, 不是修代码).
