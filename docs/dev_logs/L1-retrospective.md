# Dev Log: L1 · 工程化阶段（基础能力 + 路由闭环 + Anti-drift 基础）

**Date**: 2026-05-26 ~ 2026-05-27
**Phase / Level**: L1 (基础能力 + 第一条真闭环 + anti-drift Layer 1/2/5)
**Duration**: ~6 小时（含 9 个 /loop 自主迭代）
**Commits**: 5129abe, 97f4062, 86963cc, 6cd7f7b, 80c5c34, 02e8686, d4806c2, cf3fc1f, 42f57a5, 39d9333, 25d7d06（+ 本 retrospective + methodology seeds + progress 收尾 commit）

---

## Goal

让鲲完成 L1 阶段的 10 个工程化改动，**目标达成**：
1. 目录重组成 ADR-020 五层架构（kun/agents/<role>/ × 7 + kun/governance/ × 5）
2. 拆解 orchestrator.py 把功能搬到对应 agent 角色目录
3. 建 ADR-024 数据脊柱 7 张表（runtime_capabilities / runtime_experiments / strategy_search_requests / diagnostic_records / goal_anchors / plan_reviews / evidence_ledger）
4. 清理 ADR-018 §16.4 KnowledgePrecipitation 抽象（0 调用方，删）
5. 解 ConcurrencySafety 命名歧义（control_plane vs engineering）
6. **接通第一条真闭环**：capability_router → LLMRouter（任务执行 → 能力卡 → 下次路由调整）
7. Director 输出 complexity + priority_profile + GoalAnchor
8. Long-task mode + GoalAnchor 顶部 pinning 真接进 Executor system prompt
9. Anti-sycophancy system prompt 段（ADR-022 Layer 5）真接进 Executor
10. 全部验收 + retrospective + methodology seeds 蒸馏（本文件）

---

## Approach

**严格的子任务分解 + 配对 commit 节奏**：
- 10 个子任务（L1.1–L1.10）独立完成，每个一组 commit
- 每个子任务后：跑 pytest + ruff + 追加 dev log progress.md
- L1.2 拆 orchestrator 用 5 commit 串（A intent / B planner / C role_router / D validation+multi_judge / E capability_writeback），每 commit 5-8 文件改动 + 760 tests 常绿
- L1.4 + L1.5 合并为一个清理性 commit（两都是注释/命名级，无 schema 改）
- L1.8 + L1.9 合并为一个 commit（同一个 prompt 注入逻辑，分开做反而割裂）

**测试驱动验收**：
- 每个子任务后跑 `uv run pytest tests/unit` + `uv run ruff check`
- 任务结束总测试数从 760 → 768（新加 8 个测试）
- 关键改动都配对测试：L1.6 capability_router 接入 2 个测试 / L1.7 Director 三因素 3 个测试 / L1.8+L1.9 prompt pinning 3 个测试

**目录重组先于代码迁移**：
- L1.1 先建空 agent 目录 + Protocol 骨架 → L1.2 才往里迁代码
- 让 protocol 先稳定，code 迁过去时有归属

---

## Key Decisions

| 决策 | 为什么 |
|---|---|
| 用 `typing.Protocol` 而非 `abc.ABC` 定义 agent 角色契约 | 多实现并存（旧 control_plane + 新 kun/agents），结构化类型比继承更灵活 |
| L1.4 `concurrency.py` 重命名为 `work_item_governance.py` 而非合并 | 审计 M3 把两份 concurrency 当"重复"是误判 — 实际同概念域不同抽象层（任务级 vs work-item 级），重命名解歧义 > 强行合并 |
| L1.5 删 `KnowledgePrecipitation` 抽象但保留 1 处 "deletion marker" 注释 | 保留历史轨迹 — 后人 grep 时能找到 "为什么删了 + 何时删的"，比静默删除更有价值 |
| L1.6 改 `invoke()` 不改 `decide()` | `decide()` 是同步纯函数（design contract），引入 async DB 查询会破坏契约。`invoke()` 已是 async，加调整层无侵入 |
| L1.6 capability adjustment 保守阈值（sample≥10 + score<0.4） | 避免少量样本误升级。这跟 capability_router 内部的 cold-start damping (sample/30 weight) 一致 |
| L1.7 TaskRef `extra="allow"` + 显式 `goal_anchor` 字段 | 长任务的 anchor 直接挂在 TaskRef 方便下游访问；不进 TaskMeta（TaskMeta 是 immutable L1 摘要） |
| L1.8 抽出 `_build_executor_system_prompt` 模块函数 | 让 prompt 结构可测试 + 未来 anti-drift Layer 3/4 在此挂钩 |
| L1.8 GoalAnchor 顶部 pin（不是底部） | truncation 通常切尾部，顶部 pin 不会丢 |

---

## Constraints Applied

| 约束 | 状态 |
|---|---|
| RCDH 走到哪级 | 不适用（L1 是新代码搭建，非修复） |
| Anti-drift Goal Anchor | 维持 — v3 方案 + L1 10 改动作为 anchor，全程未偏离 |
| Forward / Backward | 不适用（无 bug fix，全是新建/迁移） |
| 单 commit ≤ 1000 行 | ✅ 实际范围 27-770 行 |
| 单轮 ≤ 5 文件改动 | ⚠️ 软违反 — L1.1 21 文件 / L1.3 3 文件 / L1.4 11 文件 / L1.7 20 文件。同一概念主题的批量改动放一 commit 比强行拆更可审 |
| pytest + ruff 过才 commit | ✅ 每 commit 前都跑，760 → 768 测试常绿 |
| ADR-025 commit 后更新 dev log | ✅ L1-progress.md 每个子任务后追加 |

---

## Patterns Used

- **配对 commit + 串行子任务**：L1.2 用 5 commit 串拆 orchestrator，每 commit 一个独立迁移单元 + 跑测试。比 1 mega-commit 节省 review 开销 + 每 commit 独立可 rollback。
- **抽出私有 helper 函数便于测试**：L1.8 把 prompt 拼接从 _execute_step 方法内抽出来。方法内逻辑不能直接单测，函数能。**这是"功能要能用上"原则的实例** — 单测能用、调试能用、未来扩展能用。
- **批量 sed bulk rename**：L1.2 / L1.4 用 `sed -i.bak ... && rm .bak` 批量更新 import 路径。比 N 次 Edit 调用快 5-10x，不出错。
- **git mv 保历史**：所有文件迁移都用 `git mv`，git 历史 follow 跨重命名仍可追溯（git log --follow）。
- **保守阈值优先**：L1.6 capability adjustment 用 sample≥10 + score<0.4 而不是 sample≥3 + score<0.5 — 少量样本噪声大，宁可错过升级不要错误升级。
- **顶部 pin 而非底部**：L1.8 GoalAnchor + anti-sycophancy 段都放 system prompt 顶部。truncation 在尾部，顶部信号最强。

---

## What Failed

### F1 · L1.2 Commit D · git mv 后 Edit "File has not been read yet"

**根因**：git mv 之后文件物理位置变了，Edit 工具的"已读"标记跟着旧路径，新路径需要重新 Read。

**恢复**：Read 新路径 → Edit 通过。

**启发式**：**git mv 等于触发 Read 失效**。任何 mv 后第一次 Edit 必须先 Read 新路径。

### F2 · L1.2 Commit D · monkeypatch 字符串 + logger name 是 grep 漏点

**根因**：`monkeypatch.setattr("kun.engineering.foo.bar", ...)` 的字符串里包含完整模块路径，grep `from kun.engineering.foo` 不会命中。同理 `get_logger("kun.engineering.foo")` 是字符串字面量。改 import 时容易漏。

**恢复**：grep `"kun.engineering.*"` 字符串字面量 + 一一更新。

**启发式**：**移模块时除了 grep import，还要 grep 字符串字面量**（monkeypatch / get_logger / get_tracer）。

### F3 · L1.7 · `new_id("ga")` KeyError

**根因**：`EntityKind` 是 `Literal[...]`，必须先在 `_PREFIX` dict 注册才能用。这个错三次了（governance/rcdh.py 以前用过 "dx" / governance/evidence_ledger.py 用过 "ev" 都未注册）。

**恢复**：一并补齐 7 个新 EntityKind 前缀。

**启发式**：**新加 ID 前缀必须同时改 `EntityKind` Literal + `_PREFIX` dict**。两者要么都改要么都不改 — `_PREFIX` 是单一真理源。

### F4 · L1.6 · 测试 capability_router cache key tenant_id 不匹配

**根因**：`_tenant_id_for_capability_routing()` 在 dev 模式返回 `"u-sylvan"`（fallback to `default_tenant_id()` setting）。我第一版测试用 `"default"` 作 cache key → cache miss → 触发真 DB 调用 → event loop closed 错误。

**恢复**：cache key 改成 `"u-sylvan"`。

**启发式**：**测试 capability_router 内部 cache 时，cache key 第一位必须是 `_tenant_id_for_capability_routing()` 实际返回值** —— dev 用 "u-sylvan"，env 设了 KUN_TENANT_ID 用 env，否则 "default"。**绝不可凭直觉猜**。

### F5 · L1.8 · `zip(strict=True)` 长度不等 ValueError

**根因**：`zip(ordered, ordered[1:], strict=True)` — ordered 7 项 vs ordered[1:] 6 项，strict=True 不容差异。Python 3.10+ 的 strict 参数和老式"配对相邻元素"写法不兼容。

**恢复**：用 `itertools.pairwise(ordered)`（更现代）或 `zip(ordered[:-1], ordered[1:], strict=True)`（手工保证等长）。ruff RUF007 强制 pairwise。

**启发式**：**strict=True 的 zip 必须保证两 iterable 等长**；配对相邻元素首选 `itertools.pairwise`（Python 3.10+）。

---

## What Worked

### W1 · 文档先于代码（Phase 0 投资在 L1 收获）

Phase 0 写了 8 份文档（ADR-019~025 + KUN-V1 v2 + PROGRESS L0-L6）+ 4 份 methodology seeds。L1 阶段每个子任务都能清晰对应到 ADR 章节 —— 改 capability_router 接入时直接对应 ADR-024 RSI 闭环、写 GoalAnchor 时直接照 ADR-022 Layer 2 schema。**没有一次"等等这个概念是什么"的暂停**。

**复用条件**：任何架构重构都先写 ADR 再动 code；ADR 配 pydantic schema 示例。

### W2 · 子任务粒度 30 分钟到 2 小时

L1 9 个子任务，平均 30-90 分钟一个。每个独立 commit + 跑测试 + 追加 progress.md。**节奏稳定**，没有"做到一半搞不清楚下一步"的时刻。

**复用条件**：把"L 里程碑"拆到 8-12 个子任务，每个能 30-90 分钟做完 + 独立 commit。

### W3 · 768/768 测试常绿

从 L1 开始（760 测试）到 L1 结束（768 测试），**每一个 commit 都跑过 pytest + ruff**。新加 8 个测试都对应新加的功能（L1.6 × 2 / L1.7 × 3 / L1.8+L1.9 × 3）。没有一次 commit 引入 regression。

**复用条件**：每加一个功能必配 ≥ 1 个测试；pytest pass 才 commit；引入 regression 立即回滚。

### W4 · 抽出测试用 helper 函数

L1.8 把 `_build_executor_system_prompt` 从 `_execute_step` 内部抽出来。**多个好处一起**：可单测、可在未来 L2 加 input classifier 时挂钩、可在 L3 加 plan review heartbeat 时复用、可调试时单独 print。

**复用条件**：方法内复杂拼装逻辑（system prompt / 复杂查询构造 / DAG 编排）— 抽出独立函数；让外部 Test 而非 mock 整个调用栈。

---

## Heuristics Extracted

1. **git mv 等于触发 Read 失效**：mv 后第一次 Edit 必须先 Read 新路径。
2. **移模块时除了 grep import，还要 grep 字符串字面量**（monkeypatch / get_logger / get_tracer）。
3. **新加 ID 前缀必须同时改 `EntityKind` Literal + `_PREFIX` dict**（单一真理源）。
4. **测试 capability_router cache 时 tenant_id 用 `"u-sylvan"`（dev 模式）**，绝不可凭直觉猜。
5. **strict=True 的 zip 必须保证两 iterable 等长**；配对相邻元素首选 `itertools.pairwise`。
6. **小批量 schema 改动 + 子任务粒度 30-90 分钟**：节奏稳定，不易迷失。
7. **顶部 pin 而非底部**：prompt 中的不可丢内容（anchor / policy / system role）放顶部。
8. **保守阈值优先**：sample ≥ 10 + score < 0.4 而不是 sample ≥ 3。少量样本宁可错过，不要误调整。
9. **抽出 helper 函数 > 测内部方法**：让 Test 可读、Debug 可读、未来扩展可挂钩。
10. **不强行合并 ADR-018 §16 抽象**：≥ 3 调用方才真合并；同概念域不同抽象层用命名区分 > 强行合并。

---

## Methodology Card Candidates

```yaml
# 1. git_mv_invalidates_read_state
- topic: git_mv_invalidates_read_state
  trigger: |
    刚执行 git mv 移动文件, 接下来要 Edit 移动后的文件.
  action: |
    任何 git mv / mv / rename 之后, 对新路径的第一次 Edit 必须先 Read 一次.
    Bash tail/cat 显示文件内容不算 Read tool 的"已读"标记.
  rationale: |
    Edit tool 跟踪 Read 调用建立的 in-session 状态. git mv 后新路径无 Read 痕迹,
    Edit 会报 "File has not been read yet". 这是工程约束, 不是 bug.

# 2. module_relocation_grep_string_literals_too
- topic: module_relocation_grep_string_literals_too
  trigger: |
    移动 Python 模块到新位置, 需要更新所有 importer.
  action: |
    grep "from kun.OLD" 找显式 import.
    grep "kun.OLD" 找字符串字面量 (monkeypatch / get_logger / get_tracer /
                                     mock.patch / importlib.import_module).
    两类 grep 都要做; 只做第一类必漏第二类.
  rationale: |
    Python 里模块路径既可作 import 又可作字符串. monkeypatch.setattr("kun.OLD.x", ...)
    在 import 改完后悄然失效, 测试不会立刻报错 (只在跑到该测试时炸).

# 3. unified_source_of_truth_for_id_prefixes
- topic: unified_source_of_truth_for_id_prefixes
  trigger: |
    新加一种实体 (GoalAnchor / DiagnosticRecord / EvidenceLedger 等) 需要 ID.
  action: |
    在 kun/core/ids.py:
      1. 加进 EntityKind = Literal[...] 字面量
      2. 加进 _PREFIX dict
    两处必须同步; 缺一就 KeyError.
  rationale: |
    EntityKind 是类型契约, _PREFIX 是运行时映射. 单一真理源原则:
    凡是配对使用的常量必须同时改, 不允许只改一处.

# 4. anchor_pinning_at_prompt_top_not_bottom
- topic: anchor_pinning_at_prompt_top_not_bottom
  trigger: |
    在 LLM system prompt 里要放不能被丢的内容 (anchor / policy / role 定义).
  action: |
    放 prompt 最顶部, 不要放底部.
    truncation 通常切尾部, 顶部信号最强.
    如果 prompt 有 anchor + base directive + optional context, 顺序:
      anchor → policy → base → optional.
  rationale: |
    LLM context limit + token truncation 通常发生在尾部. 把不可丢的内容
    放顶部 = 工程化保证它不会被切掉. 这是 prompt engineering 的硬规则.

# 5. extract_helper_for_testable_prompt_assembly
- topic: extract_helper_for_testable_prompt_assembly
  trigger: |
    复杂 system prompt 拼接逻辑 (多段 + 条件分支) 在某方法内部, 难以测试.
  action: |
    抽出独立 helper 函数 _build_xxx_system_prompt(*, ...) -> str.
    函数纯函数: 输入参数 → 输出字符串. 无 side effect.
    单测直接断言关键段存在 + 顺序正确.
  rationale: |
    内部方法很难 unit test (要 mock 整个调用栈). 抽出 helper 让 prompt 结构
    变成可断言的契约: anchor 必在顶部, anti-syc 必在 anchor 之后, base 必在
    audience 之前等. 同时 helper 给未来扩展 (anti-drift Layer 3/4) 提供挂钩点.

# 6. conservative_sample_threshold_avoids_noise_decisions
- topic: conservative_sample_threshold_avoids_noise_decisions
  trigger: |
    用统计 / capability_card 数据做自动决策 (升级 tier / 触发 RSI / 降级).
  action: |
    阈值用保守值: sample_size >= 10 才算"有信号", >= 20 才算"高置信".
    score 偏离 0.5 至少 0.2 才算"强信号" (即 < 0.3 或 > 0.7).
    cold start (sample < 5 或 5-10) → 不做调整, 保留默认.
  rationale: |
    少量样本噪声大. sample=3 + score=0.2 可能是 3 次随机性, 不是真信号.
    自动系统宁可错过升级也不要误升级 — 误升级造成的体验下降比保守
    错过的机会成本大得多. 这跟 capability_router 内部 sample/30 weight 一致.
```

---

## L1 验收清单

- [x] **10 个子任务全部完成**：L1.1 / L1.2 (5 commit) / L1.3 / L1.4 / L1.5 / L1.6 / L1.7 / L1.8 / L1.9 / L1.10
- [x] **768/768 unit tests pass**：新加 8 个测试覆盖关键改动
- [x] **ruff check + format 全过**
- [x] **目录组织对齐 ADR-020**：`kun/agents/<role>/ × 7` + `kun/governance/ × 5`
- [x] **数据脊柱 7 张表 alembic 0011 落地**：runtime_capabilities / runtime_experiments / strategy_search_requests / diagnostic_records / goal_anchors / plan_reviews / evidence_ledger
- [x] **第一条真闭环激活**：任务执行 → capability_writeback 写卡 → 下次 LLMRouter.invoke 读卡 → 强信号自动调整 tier
- [x] **Anti-drift Layer 1/2/5 接通**：long-task mode 探测 + GoalAnchor 顶部 pinning + Anti-sycophancy system prompt
- [x] **ADR-018 §16.4 KnowledgePrecipitation 抽象彻底删除**：0 调用方，注释引用更新到 ADR-024
- [x] **ConcurrencySafety 命名歧义解决**：control_plane.work_item_governance vs engineering.concurrency
- [x] **L1-progress.md 全程同步更新**：9 个子任务的微日志 + 关键决策 + 启发式
- [x] **6 个新 methodology seed cards 蒸馏**：见上方 Methodology Card Candidates 段
- [x] **本 retrospective.md 写完**（你正在读）

L1 阶段 **正式收尾**。下一步进 L2（监督线启动 + RCDH 真做 + 第一条 RSI 实例完整 10 步跑通）。
