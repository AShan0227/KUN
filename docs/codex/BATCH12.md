# BATCH12 brief — V2.2 收尾 + 6 rebase + v2.2.0 tag 准备

派给 codex. 总量 ~10-15h (大部分是 rebase + 文档). 全部基于
`feat/v2.1-foundation` (现 head `aae592b`).

跟 BATCH11 关系: BATCH11 12 PR + BATCH9/10 收尾 6 PR 共 18 PR, 14 merged,
6 等 rebase. BATCH12 = **rebase + 收尾**, 不开新功能 PR.

---

## 第 1 部分 — 6 PR rebase (优先)

### #62 C41 TaskPanorama 接 GraphTraversal

**现状**: conflict 在 `kun/cli.py` + `kun/core/task_panorama.py`, 跟 #66 #71 改动冲突.

**操作**:
```bash
gh pr checkout 62
git fetch origin
git rebase origin/feat/v2.1-foundation
# 解决 cli.py / task_panorama.py 冲突 (保留你的 + 新代码)
git push --force-with-lease
```

### #68 C47 lab benchmark replay

**现状**: conflict 在 `kun/cli.py` + `kun/lab/__init__.py` + `kun/lab/benchmark.py`, 跟 #66 (lab/__init__.py 加 InMemoryLabRecipeStorage) + #71 (cli.py promises 子命令) 冲突.

**操作**: 同 #62 — rebase + push -f.

### #67 C46 Hermes prompt template versioning

**现状**: base 是 `codex/batch11-c45` (已 merge 进 main 但 branch 没了). 改 base.

**操作**:
```bash
gh pr edit 67 --base feat/v2.1-foundation
gh pr checkout 67
git fetch origin
git rebase origin/feat/v2.1-foundation
git push --force-with-lease
```

### #53 C29 ExperimentLog DB persistence

**现状**: base `codex/batch9-c36` 已 squash 进 main, branch 没了. 改 base + rebase.

**操作**:
```bash
gh pr edit 53 --base feat/v2.1-foundation
gh pr checkout 53
git fetch origin
git rebase origin/feat/v2.1-foundation
git push --force-with-lease
```

### #54 C30 lab inspect/explain/replay CLI (stacked on #53)

**现状**: base `codex/batch9-c29` (#53 的 branch). 等 #53 rebase 后再处理.

**操作** (#53 rebase 后):
```bash
gh pr edit 54 --base feat/v2.1-foundation
gh pr checkout 54
git fetch origin
git rebase origin/feat/v2.1-foundation
# 可能跟 #53 同时改的文件有冲突, 解决
git push --force-with-lease
```

### #55 C31 lab HTTP API (stacked on #53)

**现状**: 同 #54.

**操作**: 同 #54 模式.

---

## 第 2 部分 — V2.2 v2.2.0 release 准备

### C51 自动生成 v2.2.0 changelog (~2-3h)

复用 #71 PROMISES auto-generator 框架, 加 release notes 渲染:

**任务**:
1. `kun/engineering/promises_autogen.py` 加 `render_release_notes(start_rev, end_rev) → str`
   - 抽 git log 范围内所有 Wire/C/BATCH 编号 + commit subject
   - 按 V2.2 章节 (§19-§28) 分组
   - 输出 markdown release notes
2. CLI: `kun release notes --range v2.1.0..HEAD --output CHANGELOG-v2.2.md`
3. 测试 4-5 个 (mock git log + 验分组渲染)

### C52 v2.2.0 release checklist + tag 流程 docs (~1-2h)

**任务**:
1. `docs/ops/release-checklist.md` — 打 tag 前的 checklist:
   - 所有 V2.2 PR merged
   - 1272+ 测试全绿
   - dogfood 跑过 (script + CLI 两种)
   - changelog 生成
   - alembic head 跟 v2.1.0 比对 (有几个新 migration)
   - 所有 env 开关默认值确认
2. release tag commit 模板 (giving 用户 ready-to-paste)

### C53 V2.2 spec 同步标 [实装] (~1h)

**任务**: `docs/v2/KUN-V2.2-revisions.md` 每个章节末尾加 `[实装: ✅ Wire X + #Y]` 标签, 对应 `docs/v2/V2.2-implementation-audit.md`.

跟 audit 文档对照, 不再重写, 只是把 audit 的状态映射回 spec 文档. 让 V2.2 spec 文档自带"实装状态"信息.

---

## 第 3 部分 — V2.3 前置准备 (低优先, 等用户讨论后启动)

用户即将开始 V2.3 / dogfood 讨论. 这些是讨论后可能用到的脚手架, 不强必需:

### C54 V2.2 metrics baseline 收集脚本 (~2-3h)

跑 1 周 dogfood 收集 baseline metrics (lab cost / classifier 决策分布 / Hermes rethink 频率 / verification 失败率), 给 V2.3 决策提供数据.

**任务**:
1. `scripts/collect_metrics_baseline.sh` — 拉 Prometheus 数据 + 输出 csv
2. `scripts/analyze_metrics_baseline.py` — pandas 分析 (P50/P95 + outlier 检测)

### C55 dogfood 数据集扩 (~3-4h)

#69 dogfood report 用了 5 类典型任务. 用户讨论 V2.3 时可能要看更多场景:

**任务**: `scripts/dogfood_extended.sh` 跑 20+ 类任务, 输出 markdown 比较表.

---

## 排期建议

按 ROI:
- **第 1 周**: 6 PR rebase (优先, V2.2 收尾) + C51 changelog + C52 release docs
- **第 2 周**: C53 spec 同步标实装
- **第 3 周** (等用户 V2.3 讨论后): C54 + C55

**6 PR rebase 是阻塞 v2.2.0 tag 的, 优先级最高.**

---

## 重要约束

1. **不开新 PR** — BATCH12 主要是 rebase + 文档. 想到新功能写进 BATCH13 brief 我看.
2. **commit 前 4 step**: ruff format + ruff check + mypy + pytest. 跟之前一样.
3. **不动 Wire 19-37 接口** + 不动 BATCH9/10/11 已 merged 接口 (Wire 30 GraphTraversal / LabRecipeRegistry / Hermes ExecutionStep / ENSEMBLE 第 4 档 / etc.)
4. v2.2.0 tag 由用户决定, 你只准备 changelog + checklist.

---

谢 codex. V2.2 即将打 tag, 是大里程碑 ⚡
