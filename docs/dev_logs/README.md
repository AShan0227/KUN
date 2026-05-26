# Dev Logs · 鲲工程过程捕获

> 本目录是 KUN 工程能力冷启动的原料库（ADR-025）。
> 每个有意义的工作单元（commit batch / L 里程碑 / 失败事故）必须写日志。
> KUN 的 idle-batch `methodology_distill` step（L3 实装）从这里蒸馏 methodology cards。

---

## 目录结构

```
docs/dev_logs/
├── README.md                          # 本文件 — 模板 + 工作流
├── phase-0-documentation.md           # Phase 0 完整回顾
├── L1-progress.md                     # L1 阶段微日志（追加式）
├── L1-retrospective.md                # L1 阶段完整回顾（里程碑完成时写）
├── L2-progress.md
├── L2-retrospective.md
├── ...
└── incident-YYYYMMDD-<topic>.md       # 失败事故文件（按需）
```

```
seeds/methodologies/                   # KUN 启动时加载的冷启动 methodology cards
├── *.yaml                             # 提取自 dev_logs 的 Methodology Card Candidates
└── (随 L 里程碑递增)
```

---

## 三种日志类型

### 1. 微日志（progress.md）— 每个子任务 / commit batch 后

追加到 `L<N>-progress.md`，3-5 句话：

```markdown
## L1.1 · 目录重组 (commit a1b2c3d)

完成什么 / 怎么做的 / 关键决策 / 遗留问题（如有）。

3-5 句话，不堆。
```

**强制时机**：每个 commit 后立即追加。不允许 commit 后跨 turn 不写。

### 2. 完整回顾（retrospective.md）— 每个 L 里程碑结束后

新建 `L<N>-retrospective.md`，按 9 段模板：

```markdown
# Dev Log: L<N> · <Phase Name>

**Date**: YYYY-MM-DD
**Phase / Level**: L<N>
**Duration**: ~X 小时 / Y 个 /loop 迭代
**Commits**: <hash 列表>

## Goal
本里程碑实际目标。

## Approach
具体走法。

## Key Decisions
- Decision A — Why
- Decision B — Why

## Constraints Applied
- RCDH 走到哪级（如有）
- Anti-drift: GoalAnchor / Plan Review 状态
- Forward / Backward 用了哪种
- 单 commit / 单轮 文件改动约束遵守情况

## Patterns Used
工程模式列表。

## What Failed
- Issue X — 根因 — 恢复方式

## What Worked
- Pattern Y — 复用条件

## Heuristics Extracted
- 启发式 1
- 启发式 2

## Methodology Card Candidates

\`\`\`yaml
- topic: <area>
  trigger: <when>
  action: <what>
  rationale: <why>
\`\`\`
```

**强制时机**：L 里程碑标 completed 之前必须写完，否则不算完成。

### 3. 事故日志（incident.md）— 任何失败 / 被审计发现的问题

文件名：`incident-YYYYMMDD-<short-topic>.md`

```markdown
# Incident: <topic>

**Date**: YYYY-MM-DD
**Severity**: low / medium / high / critical
**Discovered by**: 自审 / 外审 / 测试 / 用户

## What Happened
具体现象 + 何时发现 + 何时修复。

## Root Cause (走 RCDH)
- L0 设计层检查: ...
- L1 激活层检查: ...
- L2 模块开发层检查: ...
- L3 代码层检查: ...
- **Root cause level**: L?

## Fix
具体修复动作 + commit 引用。

## Preventive Measure
- 监督线规则更新（如有）
- methodology card 更新（如有）
- 测试补强（如有）

## Methodology Card Candidates

\`\`\`yaml
- topic: <area>
  trigger: <symptom>
  action: <fix pattern>
  rationale: <why this works>
\`\`\`
```

---

## 工作流

```
完成子任务 → commit
   ↓
追加 L<N>-progress.md (3-5 句话)
   ↓
更新 TodoWrite

完成 L 里程碑 → 跑测试 + 验收
   ↓
写完整 L<N>-retrospective.md (9 段)
   ↓
从 Methodology Card Candidates 段抽取 → 写成 seeds/methodologies/*.yaml
   ↓
commit retrospective + new seeds
   ↓
里程碑标 completed

发现失败 / 事故
   ↓
立即写 incident-YYYYMMDD-<topic>.md
   ↓
走 RCDH 定位根因
   ↓
修复 + 监督线规则更新（如必要）
   ↓
commit incident + fix
```

---

## Methodology Card 命名规范

`seeds/methodologies/<topic>.yaml` 中的 `topic` 字段：

- 用 snake_case
- 描述"做什么事的方法论"，不是"在哪里"
- 例子：
  - ✅ `large_design_refactor`
  - ✅ `incremental_doc_migration`
  - ✅ `RCDH_diagnosis_when_test_passes_but_feature_broken`
  - ❌ `kun_v3`（太宽泛）
  - ❌ `commit_message_style`（太局部，应归 `git_workflow`）

---

## 与 ADR-025 的关系

本 README 是 ADR-025 的**执行手册**。ADR-025 定 WHAT + WHY，本 README 定 HOW。

KUN 的 `methodology_distill` step（L3 实装）扫的是 `docs/dev_logs/*.md` + `seeds/methodologies/*.yaml`。

---

*建立日期：2026-05-26（Phase 0 同步落地）*
