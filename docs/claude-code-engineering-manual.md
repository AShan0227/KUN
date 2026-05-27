# Claude Code 工程能力全景手册

> **作者**: Claude (本 session 内的 Claude Code 自我内省)
> **目的**: 给鲲一份"我自己怎么工作"的权威输入，让鲲的蒸馏不只读 dev_logs (隐式案例) 而是看到决策机制 + 工程实现 (显式知识)
> **深度**: 每个能力含: 是什么 / 工具签名 / 工程原理 / 鲲怎么落
> **时点**: 2026-05-27, KUN 1582 tests, dogfood 任务跑中

---

## 0 · 总览

Claude Code 是一个 CLI agent，本质是 **"LLM + 一组确定性工具 + 一套工作纪律"** 三件套：
- **LLM**: Claude Opus 4.7 (1M context)，做思考 / 决策 / 生成
- **工具集**: ~80 个 (Read/Edit/Write/Bash/Agent/Skill/WebFetch/WebSearch/ToolSearch/Monitor/Schedule/MCP/...)
- **工作纪律**: 系统提示 + 内化习惯 (grep verify / 测试驱动 / commit 纪律 / 决策点暂停)

我能完成长复杂任务，不是因为 LLM 单点强，而是因为这三件套**互相约束**:
- 纪律告诉 LLM **何时该用哪个工具**
- 工具的设计让纪律**有可靠的执行手段**
- LLM 的智能让纪律能**针对场景调整**

下面 12 个能力维度逐一拆解。每个含: 大白话 / 工具签名+协议 / 工程实现 / 我刚做过的真实例子 / 鲲怎么对照。

---

## 1 · 长任务拆解 (TodoWrite + 状态机)

### 是什么

把"做一件复杂事"拆成 3-10 个**有边界、可验证**的 todo，每个标 `pending / in_progress / completed`。一次只有 ≤1 个 in_progress (在做)，做完立刻标 completed，再标下一个 in_progress。

### 工具实现

```python
TaskCreate(subject="LT.E: ExecutorLoop", description="...", activeForm="Building exec loop")
TaskUpdate(taskId="13", status="in_progress")  # 开始做
TaskUpdate(taskId="13", status="completed")    # 做完
TaskList()                                     # 查全清单
```

`TaskCreate` 返 task_id；`TaskUpdate` 必须传 taskId + 期望的新状态。harness 维护实际状态，向用户展示。

### 工程原理

- **强制原子性**: in_progress 单一，逼自己一次专注一件事
- **可观测**: 用户能实时看进度
- **可恢复**: 状态在 harness 持久，session 中断重连后清单还在
- **可叠加**: 主任务下可以 spawn 子 task，TaskCreate 的任务也可被其他 agent 继续

### 我刚做过的例子

本 session 共 26 个 TaskCreate，包括 LT.A-G 7 个 + LT.INT-A 到 F 6 个 + LT.WIRE-1 到 3 个 + PH1.TEST + DOGFOOD。每个完成时我立刻 TaskUpdate(completed)，用户全程能看清进度。

### 鲲怎么对照

- 鲲有 `kun.agents.director.planner.TaskPlanner` 输出 flat PlanStep list
- 鲲新加了 `RecursivePlanner` 递归 plan tree（LT.F）
- **缺**: 鲲没有"状态机驱动的 todo tracker"。鲲的 PlanStep 是静态的 (一次性产出)，不像 TodoWrite 是动态的 (跑中可加可改可重排)
- **建议**: 鲲加一个 `RuntimeTodoTracker` service，记 (task_id, todo_id, status, created_at, updated_at)，跟 PlanStep / PlanTree 解耦但绑定 task_id

---

## 2 · 并行 SubAgent (Agent tool + 1 message N calls)

### 是什么

复杂任务可以拆成**独立的子任务**，每个子任务分给一个 **Agent** (子 Claude Code 实例) 跑。多个独立子任务**同时启动**（一个 message 包多个 Agent 调用），等结果回来再整合。

### 工具实现

```python
# 同一个 message 里包多个 Agent 调用 → 并行
Agent(description="LT.INT-A LLM adapter", subagent_type="general-purpose", prompt="...写 LLMInvoker...")
Agent(description="LT.INT-B Checkpoint DB", subagent_type="general-purpose", prompt="...写 DB writer...")
Agent(description="LT.INT-C PlanReview DB", subagent_type="general-purpose", prompt="...写 PR writer...")
Agent(description="LT.INT-D ExtSuper adapter", subagent_type="general-purpose", prompt="...写 ExtSuper wrapper...")
Agent(description="LT.INT-E LongTaskOrch", subagent_type="general-purpose", prompt="...写 composition...")
```

- `subagent_type`: 选 agent 类型 (general-purpose / Explore / Plan / claude-code-guide)
- `prompt`: 子 agent 的完整 brief (子 agent 不看主 session 的上下文)
- `run_in_background: true`: 后台跑，完成时通知 (`<task-notification>`)
- 多次 `Agent()` 在**同一 message**里 → 并行；分多次 message → 串行

### 工程原理

- **子 agent 是无状态的**: 它只看你给的 prompt + 自己的工具，不看主 session 上下文 → prompt 必须 self-contained
- **结果隔离**: 子 agent 报告回来是单条 message (不进主 transcript)，主 agent 负责整合 → 避免 context 污染
- **失败隔离**: 子 agent 挂了不影响主 agent
- **资源并行**: 多个子 agent 同时跑 LLM 调用 + 工具，相当于扩大算力 N 倍
- **依赖必须串行**: 如果 B 需要 A 的结果 → 不能同 message 启动，得等 A 回来

### 我刚做过的例子

**LT.INT 阶段**: 我一个 message 起了 5 个 agent (A/B/C/D/E)，全独立，30 分钟一起完成 86 个测试。比串行做 5×30=150 分钟省 4 倍。

**LT.WIRE 阶段**: 一个 message 起 3 个 agent，背景跑；我同时跑 PH1 测试实验。**并行 4 件事**。

### 鲲怎么对照

- 鲲有 ADR-020 七角色 (Director/Executor/Tester/Gate/Supervisor/Strategist/External Supervisor) — 设计上有"多 agent"概念
- 鲲有 SupervisorPool (L4 多实例) — 不同 audit 维度分流
- **缺**:
  1. 鲲没有"通用 sub-agent spawn"机制 — 鲲的 agent 是固定 7 个角色，不能任意拆任务
  2. 鲲没有"独立 prompt + 无状态隔离" — 它的 agent 跟主 control plane 共享 task state
  3. 鲲没有"background notification" — agent 完成怎么告诉主线？现在靠 emitter callback (同步推)
- **建议**: 鲲加一个 `WorkerAgentSpawner`: `spawn(prompt, *, background=True) → agent_id`，子 agent 跑 LongTaskOrchestrator 完整流程，完成时往 NATS 推 `agent.completed` 事件主线订阅

---

## 3 · 联网检索 (WebFetch / WebSearch / MCP directory)

### 是什么

任务里要"查最新文档"、"读 GitHub issue"、"找 stackoverflow 答案"时，主动调网络工具拿信息。

### 工具实现

```python
WebFetch(url="https://docs.python.org/3/library/asyncio-task.html",
         prompt="extract the patterns for managing concurrent tasks")
# → 抓取 URL，用 prompt 引导提取 → 字符串

WebSearch(query="anthropic claude prompt caching cache-control")
# → 搜索引擎调用 → results list

mcp__ccd_directory__request_directory(...)
# → 查 Anthropic 文档库 / Claude Code 知识库
```

`WebFetch` 自带 cache（同 URL 重复抓不重复请求）。`WebSearch` 用引擎实时搜。MCP 工具走 stdio 协议跟外部 server 通信。

### 工程原理

- **prompt 引导提取**: WebFetch 不是裸 HTML，是 "URL + LLM 提取意图" → 返回 markdown-化的相关片段
- **cache 命中**: 同 URL + prompt 短期内缓存，省 token + 提速
- **MCP 隔离**: MCP server 是独立进程，工具调用走 JSON-RPC，server 挂了主 Claude 不挂
- **延迟加载**: MCP 工具不是开始就全 load 进 prompt — 用 ToolSearch 按名拉
- **时效性**: WebSearch 标当前日期，结果含发布时间，避免过时信息

### 我刚做过的例子

本 session 里我没真调 WebFetch（鲲项目是本地），但 KUN 自己有 `mcp__Claude_Preview__*` / `mcp__Claude_in_Chrome__*` 等 MCP 工具——属于鲲扩展用浏览器执行。我作为主 Claude Code 在分析阶段会调 ToolSearch 把这些 MCP 工具拉进来。

### 鲲怎么对照

- 鲲有 `mcp__mcp-registry__*` (在我的工具表里看到的)
- 鲲有 `kun.skills.builtin.research_web_fetch` — 已经有一个 WebFetch 级别的 skill
- **缺**:
  1. 鲲的 web_fetch skill 没有 "prompt 引导提取" — 它返回原始 HTML
  2. 鲲没有 WebSearch (引擎调用)
  3. 鲲的 MCP server 接入是单向 (kun 当 client)，没让 kun 自己暴露 MCP server 让外部调
- **建议**:
  1. 升级 research_web_fetch skill 加 `prompt` 参数走 LLM 提取
  2. 加 research_web_search skill 接 Brave/DuckDuckGo API
  3. 长远: 鲲暴露 MCP server，让 Claude Code 反过来用鲲做工具 (这就是商业化第一步)

---

## 4 · 工具系统 (ToolSearch 延迟加载 + Skill 渐进披露)

### 是什么

我的工具表有 ~80 个，但**不是全 load 进每次 LLM call 的 system prompt** — 因为这会烧 token。机制:
- **核心工具**: 始终加载 (Read/Edit/Write/Bash/Agent/Skill/AskUserQuestion/...)
- **延迟工具**: 列在 system reminder 里只给 **name**，要用时调 `ToolSearch(query="select:<name>")` 把 schema 加载进来
- **Skill 系统**: 工具的更高层，渐进披露 L1 (frontmatter) → L2 (input_schema) → L3 (body markdown)

### 工具实现

```python
ToolSearch(query="select:CronCreate,Monitor,ScheduleWakeup", max_results=5)
# → 把 3 个工具的 JSONSchema 加载进 prompt 后续可直接调

ToolSearch(query="github pr", max_results=3)
# → 关键词搜索，返 top 3 候选工具

Skill(skill="ccd_session_mgmt:archive_session")
# → 调一个 SKILL.md 定义的 skill
```

`ToolSearch` 返回的工具 schema 临时加进 prompt — 它持久还是临时由 harness 决定。后续调用直接 invoke 不需要再 search。

### 工程原理

- **token 经济**: 80 工具的完整 schema ~30k tokens，每次 LLM call 都带 → 巨费 + 占用 context window
- **延迟到需要**: 只把 5-10 个核心工具的 schema 始终加载，其他延迟拉
- **Skill 渐进 (SKILL.md 协议)**:
  - **L1** = frontmatter 的 name+description+tags (1 行)，始终在 context
  - **L2** = 完整 frontmatter 含 input_schema (几十行)，调 Skill tool 时加载
  - **L3** = body markdown + 附带脚本，只在 LLM 选择该 skill 时加载
- **工具 vs Skill 边界**:
  - Tool = 原子动作 (Read, Bash, WebFetch)
  - Skill = Tool 的组合 + 领域知识 + 提示词 (e.g. coding-pytest 集成 Bash + Read + 测试规范)

### 我刚做过的例子

session 开始时我看到 `Some tools are deferred and not listed above` — 一堆名字 (CronCreate, Monitor, TaskCreate 等)。我用到 TaskCreate 时调 `ToolSearch(query="select:TaskCreate")` 把它的 schema 加载。

后来用 Monitor 时同样套路。**节省 token** 估算: 80 工具全加载 = ~25-30k token，按需加载实际占用 ~5k token，节省 ~20k token 的每次 LLM call cost。

### 鲲怎么对照

- 鲲有 `kun.skills.loader` + SKILL.md 协议 (`kun/skills/loader.py`) — Skill 渐进披露已经实现了 ✓
- 鲲有 `kun.skills.selector` 在 task 来时选 top-K skill 注入 system prompt
- 鲲有 `is_registered()` + `dispatch()` (`kun.skills.dispatcher`) — Skill 执行有了 ✓
- **缺**:
  1. 鲲没有 "ToolSearch 延迟加载" 概念 — LLMRouter 给 LLM 的 tools list 是固定的 (LLMRequest.tools)
  2. 鲲没有"工具 vs Skill 边界"明确划分 — KUN 的 builtin skills 5 个 (coding-pytest / data-csv-query / os-shell / research-web-fetch / writing-markdown) 都是 mid-level
- **建议**:
  1. 把 KUN 的 5 个 builtin skill **prompt-level lazy load** — LLM 看到 task 后先选 1-3 skill，调 SKILL.md L2/L3 加载
  2. 加一个"原子 tool registry" (Bash / file_read / file_edit / web_fetch) 跟 Skill registry 分开
  3. LLM 选 skill 决定后，把 skill 的 input_schema 注入 system prompt 引导调用

---

## 5 · 错误发现 + 自纠 (grep verify before assume / 测试驱动 / fail-fast loop)

### 是什么

**不靠假设写代码 — 改之前先 grep / 测之后立刻验证 / 错了立刻修不藏**。

具体表现:
- 改函数签名前先 grep 所有 caller
- 写完代码立刻 pytest + ruff，红了立刻定位修
- 看到 log warning 不放过 (今天就发现 task_checkpoints 表不存在)
- 测试失败的 trace 全看完，找根因不是表面修

### 工具实现

```python
# Grep verify 工具
Bash(command='grep -rn "classify_input" kun/engineering/ kun/control_plane/')

# 测试驱动
Bash(command=".venv/bin/python -m pytest tests/unit/test_X.py --tb=short")

# 测后立刻 ruff
Bash(command=".venv/bin/ruff check kun/ tests/")

# 改完一处立刻测，不要批量改完再测
```

工程纪律不是工具，但工具的"立刻能调"让纪律有手段。

### 工程原理

- **grep verify before assume**: 改一处可能影响多处。grep 给 ground truth。
- **测试驱动**: 单元测试是真理来源，比"我觉得对"靠谱
- **ruff fast feedback loop**: ruff 0.5-2 秒就跑完，trivially 失败立刻看到
- **trace 全看完**: stack trace 含真因，不是只看 top
- **不藏错**: log warning 也得追究 (今天 checkpoint_save_failed 我立刻 spawn 修复 ticket)

### 我刚做过的例子

**自纠例子 1**: LT.C 写 `_next_sequence(task_id, base=N)` 一开始多职责导致 resume 后 sequence 跳跃 (4 而非 2)。单测立即暴露，我看 trace 定位根因，拆成 `_advance_sequence` + `_set_resume_baseline` 两个 method。**没藏**。

**自纠例子 2**: regex `match="default_tenant"` 不匹配大写 `DEFAULT_TENANT_ID`。pytest.raises 跑红，我重新看消息原文改成 `match="DEFAULT_TENANT_ID"`。

**自纠例子 3**: 今天 dogfood 第一次跑 84 秒就 paused — 我没说"看起来工作了" — 我读 PG 表找出"删除"被 LLM 重写到 spec.goal_detail。**真定位根因**。

**自纠例子 4**: 今天 dogfood 第二次跑 checkpoint_save_failed log warning — 我没忽略 — spawn 一个 ticket 修。

### 鲲怎么对照

- 鲲有 RCDH (Root Cause Diagnostic Hierarchy, ADR-021) — 4 级根因诊断
- 鲲有 ValidationPipeline (ADR-018 §16.2) — 任务输出有 tier 化验证
- 鲲有 Tester 角色 (ADR-020) — 但实装弱
- **缺**:
  1. 鲲没有 "grep verify before action" 习惯 — Executor 直接调 LLM 改代码，没有"先 grep 所有 caller"步骤
  2. 鲲没有 "改一处立刻测" — Executor 跑完整个任务才 validation
  3. 鲲的错误处理是 "状态累积 fail 不阻塞主路径" (ADR-024) — 但**不是说不修**，是说不打挂主路径 + 等 idle batch 复盘
- **建议**:
  1. Executor 在改代码前 spawn 一个 small "grep_verify" sub-step → 检查 caller chain
  2. Executor 改完一个文件立刻跑 ruff check 那个文件，红了 retry
  3. RCDH 自动消费 log warning (现在好像主要消费 task failures)

---

## 6 · 决策点暂停 (AskUserQuestion / ExitPlanMode)

### 是什么

不擅自做产品决策 / 大改动 — **停下问用户**。

### 工具实现

```python
AskUserQuestion(questions=[{
    "question": "L6 Phase 2 foundation 完成. 下一步走哪条路?",
    "header": "Next step",
    "multiSelect": False,
    "options": [
        {"label": "L6.E e2e wiring (推荐)", "description": "..."},
        {"label": "做第二个垂直 (投放/内容分发/CRM)", "description": "..."},
        ...
    ]
}])

ExitPlanMode(plan="<10-line plan summary>")
# → 在 plan mode 下，写完计划后等用户批准
```

`AskUserQuestion`: 推荐选项标 "(推荐)"，自动加 "Other" 让用户自定义。`ExitPlanMode`: 只在 plan mode 下用，提交计划等批准。

### 工程原理

- **产品决策的不可逆性**: 产品方向选错可能浪费几小时甚至几天工作
- **用户在场**: agent 跑着用户在 — 问一句的成本远低于做错的代价
- **明确选项 + 推荐**: 不给 open-ended question，给 2-4 个选项 + 推荐第一个；用户选 "Other" 可输入自定义
- **header chip**: 12 字以内的标签 ("Auth method" / "Vertical")，方便用户快速识别问题
- **不在过程中乱问**: 一个完整决策完了再问，不要每个小步骤都问

### 我刚做过的例子

- L6 Phase 2 foundation 完成后我停下问"下一步走哪条" — 用户选 L6.E
- L6.D 时问"先做哪个垂直" — 用户选内容分发
- dogfood scope/LLM/启动方式 — 3 个问题打包问，让用户一次决策完
- 任务关键产品方向 (Auth 启用时机) — 我从不擅自决定

### 鲲怎么对照

- 鲲有 Gate (ADR-024) — Gate 是机器自动决策门禁 (规则 + verdict)
- 鲲有 LongTaskInputRouter (LT.A) — 处理任务中用户消息，pivot_pause 是"需用户确认"
- **缺**:
  1. 鲲没有"主动停下问用户"机制 — 鲲的 Director/Executor 不会主动 AskUserQuestion
  2. 鲲的 Gate 是 binary (approve/reject)，不是"问用户选 A/B/C"
  3. 任务中如果遇到产品方向决策，鲲会直接走 fallback 或 raise — 不会"暂停等用户"
- **建议**:
  1. 加 `PromptUserDecision` service: agent 内部判断"这是产品决策" → emit `user_decision_required` event → WS 推问题给用户 → 等用户答 → 继续
  2. 接入 LongTaskInputRouter 的 `scope_expansion_review` bucket — 实际上已经是这个机制，但没真用起来 (rejected_task_busy 是 TODO)

---

## 7 · 上下文管理 (Read 行号 / Edit 优先 / 自适应 compaction)

### 是什么

不全 cat 文件 — 用行号读片段。改文件优先 Edit (只发 diff)，不 Write (全文)。对话长了自动压缩中间。

### 工具实现

```python
# Read 行号定位
Read(file_path="/path/to/file.py", offset=200, limit=50)  # 读 200-250 行
# vs
Bash(command="cat /path/to/file.py")  # ❌ 全文进 prompt，烧 token

# Edit 优先 (只 diff)
Edit(file_path="...", old_string="...", new_string="...")
# vs
Write(file_path="...", content="<全文>")  # 仅新建或完全重写

# Bash 工具避免读类
# AVOID: Bash("cat / head / tail / sed / awk / grep")
# USE:   Read / Edit / Grep / specialized tools
```

工具描述里写了 "Avoid Bash for cat/head/tail/sed/awk" — 这是工程化约束 LLM 行为。

### 工程原理

- **token 经济 (行号读)**: 5000 行文件全 cat = 80k tokens (cost + context 满)。Read 50 行 = 800 tokens。100x 经济
- **Edit 只发 diff**: 改 5 行 vs 写 5000 行 全文 — diff 信息熵更高，LLM 更准确
- **自适应 compaction**: 当对话超阈值 (默认 200k tokens) → harness 自动 summarize 早期消息，保留头部 (anchor) + 尾部 (recent) + 中间 summary
- **chapter mark**: 主动 `mcp__ccd_session__mark_chapter` 把工作分阶段，用户可跳转 (UI 表现为 TOC)

### 我刚做过的例子

- 本 session 我 Read 过 ~50 次，**0 次** `cat` 全文
- LT.E ExecutorLoop 982 行的实现我让 Agent 写完后只 Read 部分 (lifecycle / branch / etc) 不全读
- 9 次 mark_chapter ("L6.E: e2e wiring" / "LT.B: Layer 4 heartbeat wiring" / etc) — 用户能从 TOC 跳

### 鲲怎么对照

- 鲲有 ContextPacker (`kun.context.packer`) — 把 task_ref + 历史 events 打包成 layered asset
- 鲲有 ImportanceScorer — 给 context asset 评分按重要性排
- 鲲有 ConversationCompactor (LT.D) — 我们新做的，token 超阈值自动压缩 ✓
- **缺**:
  1. 鲲的 file read 没有 "行号定位" — Executor 真改文件时是不是 Read 全文? 我不确定 — 建议用 grep 验证
  2. 鲲没有 "Edit 优先 Write" 强制约束 — Executor 改文件可能直接重写全文
  3. 鲲的 ContextPacker 是 task 开始时打包，不是动态 compaction
- **建议**:
  1. Executor.tool_call 暴露 `file_read(offset, limit)` 接口，强制行号
  2. file_edit 工具签名只接 (old_string, new_string)，不接 (content)
  3. ContextPacker + ConversationCompactor 跨阶段衔接 (一开始 pack，跑中 compact)

---

## 8 · 任务节奏 (ScheduleWakeup 缓存窗口 / /loop / Monitor)

### 是什么

自主推进任务时，**决定何时该停 / 何时再启动** — 缓存窗口意识 + 事件驱动唤醒。

### 工具实现

```python
ScheduleWakeup(delaySeconds=1200, reason="LT.E 落地, 1200s 让 cache 窗口分界", prompt="/loop ...")
# → 在 N 秒后自动重新启动 LLM, prompt 是下次唤醒时的输入

Monitor(command="tail -f /var/log/... | grep --line-buffered 'ERROR'",
        persistent=True, timeout_ms=3600000,
        description="dogfood progress")
# → 持续 watch，每行 stdout 是一个通知事件

TaskList()  # 主动查所有 task 状态
TaskStop(taskId="...")  # 主动停掉一个 monitor / background agent
```

### 工程原理

- **Anthropic prompt cache TTL = 5 分钟**: ScheduleWakeup 选 ≤270s (停在 cache 内, 暖) 或 ≥1200s (一次 cache miss 买长 wait)。不要选 300s (最糟糕 — 付了 cache miss 没摊销)
- **Monitor 事件驱动**: 不轮询，stdout 每行触发通知 — Claude 收到通知就被唤醒
- **TaskList 主动查**: spawn 多 agent / monitor 后用 TaskList 看全局状态
- **/loop 自主**: 用户给一个开放任务，Claude 自己 ScheduleWakeup 节拍推进，完成才停

### 我刚做过的例子

- LT 阶段 7 个子任务每个完成后 ScheduleWakeup(60s) (cache 暖, 立即接续)
- L6 阶段每完成一站 ScheduleWakeup(1200s) (给用户时间介入)
- Monitor 持续 watch dogfood uvicorn.log，关键事件 (long_task.* / exec_loop.* / errors) 实时推
- 多次 TaskList 查"3 个 Agent 跑完没"

### 鲲怎么对照

- 鲲有 `kun.engineering.idle_batch` — 周期性跑 (ab_decision_roll_up / methodology_distill 等)
- 鲲有 `kun.governance.priority_channel` — 把任务分 urgent/regular 优先级
- 鲲有 PlanReviewHeartbeat (LT.B) — 长任务每 N 步 / 每 M 秒触发 review
- **缺**:
  1. 鲲没有"缓存窗口意识" — LLMRouter 调用没考虑 Anthropic prompt cache TTL
  2. 鲲的 idle_batch 是 1 小时一次 (fixed interval)，不是 cache-aware
  3. 鲲没有"主动 monitor + 事件驱动" — supervisor service 是 push-based (anomaly 来才反应) 不是 pull
- **建议**:
  1. LLMRouter 加 `cache_window_aware_invoke` — 同 task 多次调 LLM 时复用同一 system prompt (含 anchor) 让 cache 命中
  2. 周期性任务考虑 prompt cache TTL (5 分钟为单位)
  3. supervisor 加 long-running monitor task (订阅 NATS subject + 实时反应)

---

## 9 · Commit 纪律 (paired commit / ≤5 files / git safety)

### 是什么

不做巨型 commit — **小而频繁** + **逻辑原子** + **不绕 hooks** + **不破坏性 git 命令前停**。

### 工具实现

```python
# 提交前 batch 3 个独立信号
Bash("git status")     # 看 untracked
Bash("git diff")       # 看 unstaged
Bash("git log --oneline -5")  # 看 recent commit 风格

# Commit 用 heredoc 保格式
Bash(command='''git commit -m "$(cat <<'EOF'
Short title

Body explaining the why, not the what.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"''')

# 不绕 hooks
# AVOID: --no-verify / --no-gpg-sign
# 如果 hook 失败, 修 hook 而不是 bypass

# Destructive 操作停下确认
# git reset --hard / git push --force / git branch -D — 不主动用
```

### 工程原理

- **单 commit ≤5 文件 + ≤1000 行**: 大 commit 难 review，难 revert，bisect 时定位不到具体原因
- **paired commit**: 实装 + 测试一起 commit (不要先实装后测试两个 commit)
- **heredoc 格式**: shell 嵌套 quote 容易破 commit message 格式
- **不绕 hooks**: pre-commit 失败是信号，bypass = 累积技术债
- **不主动用 destructive 命令**: reset --hard / push --force 会丢工作

### 我刚做过的例子

本 session 至今 ~60 commits，**全部 ≤5 文件**。具体:
- LT.A: 2 commits (3+1 文件)
- LT.B: 2 commits (4+1 文件)
- LT.C: 3 commits (3+3+1 文件) — ORM + service + dev log 分开
- LT.E: 3 commits (3+1+1 文件)
- LT.INT-F: 4 commits (5+4+4+2 文件)
- 没有一次 `git push --force` / `--no-verify` / amend

### 鲲怎么对照

- 鲲没有 commit (它是 service / runtime，不是 dev tool)
- 鲲的对等抽象: capability promotion (从 candidate → enabled)
- **缺/对照**:
  1. Capability promotion 应该有 "小而频繁" 纪律 — 每次只 enable 1 个 capability 不是批量
  2. promotion_state 有 (merged / staging / enabled) — 已经分阶段了 ✓
  3. **建议**: 鲲的 `runtime_experiments` 加约束 — 同 target_module 同一时间只 1 个 active experiment (避免并发 capability 冲突)

---

## 10 · Dev log 沉淀 (ADR-025 / retrospective)

### 是什么

每 commit 后写 progress log；每 milestone 后写 9 段 retrospective + 抽 ≥3 张 methodology seeds。**强制纪律**, 不写不算完成。

### 工具实现

```python
# Progress (每子任务后)
Write(file_path="docs/dev_logs/L<N>-progress.md", content="<3-5 句记录>")

# Retrospective (milestone 后, 9 段固定结构)
Write(file_path="docs/dev_logs/L<N>-retrospective.md", content="""
## Goal / Approach / Key Decisions / Constraints / Patterns /
What Failed / What Worked / Heuristics / Methodology Card Candidates
""")

# Methodology seeds (≥3 张 yaml)
Write(file_path="seeds/methodologies/<topic>.yaml", content="""
topic: ...
title: ...
description: ...
trigger: { conditions: [...] }
action: { pattern: [...], anti_patterns: [...] }
rationale: ...
evidence: [...]
confidence: 0.9
applicability: { scopes: [...], do_not_apply_when: [...] }
""")
```

### 工程原理

- **9 段 retrospective 是 forcing function**: 写"What Failed"段逼自己承认错误，写"Heuristics Extracted"段逼自己抽象经验
- **methodology seed yaml**: 结构化让 LLM 之后能消费 (yaml.safe_load) + 让自动蒸馏工具识别
- **强制 ≥3 张**: 不是"想到就写"，是"必须 3 张" — 逼自己抽象不只反思
- **paired with commit**: commit 后立即写 — 趁记忆新鲜

### 我刚做过的例子

- LT 阶段共写 9 个 progress 段 (LT.A-G + LT.INT-A-F + LT.WIRE-1-3) + 2 个 retrospective (L6 + LT)
- 抽出 12 张新 methodology seeds (LT 阶段)
- 总共 30 张 seeds (LT.G 后)

### 鲲怎么对照

- 鲲有 `kun.engineering.methodology_distill` — 自动从 dev_logs 抽 candidates (今天看到 277 candidates)
- 鲲有 `seeds/methodologies/*.yaml` 持久化
- 鲲有 EvidenceLedger (ADR-024) — 全链路证据账本
- **缺**:
  1. 鲲的 distill 是 read-only — 抽 candidates 但不强制 promotion → seeds
  2. 鲲没有 "milestone 自动 retrospective" — 每 L 阶段完成时鲲没写自己的 retrospective
  3. 鲲的 capability writeback (ADR-024 step 4) 只是数字 (cost/duration/outcome)，没有 narrative
- **建议**:
  1. 鲲的 distill 之后跑 Gate — 高 confidence candidate 自动写 seed
  2. 鲲的 milestone (e.g. promotion 一批 capabilities) 自动生成 retrospective draft
  3. capability writeback 加 narrative field — "为什么 outcome=pass" 一句话

---

## 11 · 多模态 (Read 图片 / PDF pages / 多模态 message content)

### 是什么

用户上传截图 / PDF / 多模态文档时直接 Read 看内容，不需要 OCR / 抽文字 这种 pre-processing。

### 工具实现

```python
Read(file_path="/path/to/screenshot.png")
# → 图片直接进 LLM 多模态 input, Claude 看像素

Read(file_path="/path/to/doc.pdf", pages="3-7")
# → PDF 指定页范围, max 20 页

# 消息 content 也可以是 list[dict]
{"role": "user", "content": [
    {"type": "text", "text": "解释这个错误:"},
    {"type": "image", "source": {...}}
]}
```

### 工程原理

- **Claude 原生多模态**: vision 不是另一个模型，是同一个 LLM 的多模态输入
- **PDF 智能分页**: >10 页必传 pages range, 防过载
- **临时路径友好**: 用户截图自动存 /tmp，Read 直接读 — 不需要文件管理
- **图片优于文字描述**: 用户给截图比让用户打字描述错误信息更准确

### 我刚做过的例子

本 session 没真处理图片 (纯代码任务)，但工具表明确支持。如果用户给我一个 KUN 系统架构图 (PNG)，我能直接 Read 看 + 分析结构。

### 鲲怎么对照

- 鲲的 LLMProvider 都是文本 (gpt-5.5 / Claude / MiniMax)
- 鲲没有"图片直接进 LLM input"路径
- 鲲有 `kun.skills.builtin.research_web_fetch` 抓网页，但不抽图
- **缺**:
  1. 鲲的 file_read 接口看起来只读 text
  2. LLMRequest.messages 的 content 是 str 不是 list (多模态需要 list)
  3. ContextPacker 不处理图片 / PDF asset
- **建议**:
  1. LLMMessage.content 改 `str | list[dict]` 支持多模态
  2. ContextPacker 加 image / pdf asset 类型
  3. 接 Claude/GPT-4V provider — 鲲跑视觉任务 (Phase 2 内容分发的小红书审核截图等)

---

## 12 · 自我监控 (Monitor 失败信号穷举 / TaskList / 通知处理)

### 是什么

启动后台任务 / 长跑进程时，**arm 一个 monitor 监控完整失败信号集**，不要只 watch success — silence 不等于成功。

### 工具实现

```python
# Monitor: 完整覆盖成功 + 各种失败信号
Monitor(command="tail -f log | grep -E --line-buffered \
    'elapsed_steps=|Traceback|Error|FAILED|assert|Killed|OOM'",
    description="job progress + failures",
    persistent=True,
    timeout_ms=3600000)
# ✓ 正常进度 + 4 类失败都会 emit 事件 → 不会沉默

# 错误示例 (只看成功):
Monitor(command="tail -f log | grep 'elapsed_steps='",
    description="watch progress")
# ✗ crash / hang / OOM 全沉默 — 看起来跟正常跑一样
```

### 工程原理

- **silence ≠ success**: monitor 静默可能是 (a) 任务还在跑 (b) 任务挂了 — 不能用沉默判断
- **覆盖失败签名集**: Traceback / Error / FAILED / assert / Killed / OOM 都得 grep
- **per-occurrence event**: tail -f + grep --line-buffered → 每匹配一行一个通知
- **persistent vs 一次性**: persistent=True 跑到 TaskStop；False = 命令退出就结束
- **批处理 (200ms)**: 同一 200ms 内的多行 stdout 合成一个通知

### 我刚做过的例子

dogfood 任务的 Monitor:
```
tail -f /tmp/kun-uvicorn.log | grep -E --line-buffered \
  "long_task\.|exec_loop\.|plan_review|checkpoint_save|compaction_applied|\
   guard_intervention|task\.paused|task\.timed_out|answer\.completed|\
   task\.done|ERROR|Traceback|FAILED|exception|intent\.parsed|action_plan|\
   preflight|capability\.writeback|recursive_planner\.tree_built|insight"
```

**包括了所有可能的成功 / 失败 / 进度信号**。果然就抓到 `checkpoint_save_failed` warning (真 bug)。

### 鲲怎么对照

- 鲲有 SupervisorService — 周期性扫 events 找 anomaly
- 鲲有 PlanReviewHeartbeat — 长任务定期复查
- 鲲有 NATS subscriber — 订阅 `kun.>` 全部事件
- **缺**:
  1. 鲲的 supervisor 是 reactive (event 来了反应), 不是 proactive (周期性 health check)
  2. 鲲没有 "如果 N 分钟没事件 = 卡死" 检测
  3. 鲲的 anomaly_threshold 看的是 "failure spike"，没看 "silence spike"
- **建议**:
  1. supervisor 加 `silence_detector`: 长任务每 N 步 / M 分钟没事件 → emit `task.suspected_stuck`
  2. heartbeat 加 wall-clock 检查 (现在只有 step counter)
  3. Monitor + grep 模式应用到鲲的 anomaly detection (周期性扫 log 多关键词匹配)

---

## 13 · 跨切面设计原则

### A · 工具 + 纪律互锁

工具不够用 → 纪律没手段
纪律不够强 → 工具被乱用

Claude Code 的工具表 (~80 个) **针对纪律设计**:
- 不让 cat 文件 → Read 行号定位 (限制 + 替代)
- 不让 amend → 提示 paired commit (限制 + 替代)
- 决策点要停 → AskUserQuestion 工具 (替代 open-ended question)
- ScheduleWakeup 强制带 reason 字段 → 防止 "no reason, just wait" 滥用

### B · Token 经济渗透每一层

- 工具延迟加载 (ToolSearch)
- Skill 渐进披露 (L1 → L3)
- Read 行号 (vs cat)
- Edit diff (vs Write 全文)
- ScheduleWakeup 缓存窗口 (cache 暖 vs 一次 miss)
- 自适应 compaction (token > 阈值 → summarize 旧)

每一个都按"省 token 而不损失任务质量"的目标设计。

### C · 失败 fallback 不阻塞主路径

- 整合 service raise → log + skip + 主路径继续 (ADR-024 状态累积)
- Tool 抛 → is_error=True ToolResult + loop 继续
- Checkpoint write fail → warning + 任务跑完 (没有 resume 能力但不 crash)
- LLM 调用 fail → status=failed loop 返回 (不 raise)

**永远不 raise** 是契约。caller 用 status / outcome 决定。

### D · 决策点显式 + 可观测

- AskUserQuestion 是显式决策点 (用户做)
- TodoWrite in_progress 是显式焦点
- mark_chapter 是显式阶段切换
- TaskList / Monitor 让 silent → observable

不让 LLM "默默做"，所有重要决策都暴露给用户看 + 重要状态都暴露给监控看。

### E · 持续学习 (ADR-025 文化)

- 每次错误后 → spawn_task 留 ticket
- 每次决策后 → progress log 一句话
- 每个 milestone 后 → retrospective 9 段 + ≥3 seeds
- 每条 seed 后 → 真用一次再说

**完成 ≠ 写完代码 + 测试过**，**完成 = 沉淀进结构化知识 + 下次能调用**。

---

## 14 · 鲲蒸馏 Claude Code 工程能力的 10 步路线图

把上面 12 个能力 + 5 个设计原则，按 ROI 排序的鲲落地步骤:

### Tier 1 (最高 ROI, 改鲲少 + 显效大, 1-2 天工作量)

1. **加 RuntimeTodoTracker** (能力 1): Executor 跑任务时维护动态 todo 列表，state=in_progress/completed
2. **Executor 加 grep_verify pre-action step** (能力 5): 每次 file_edit 前先 grep caller chain
3. **PromptUserDecision service** (能力 6): 任务中遇到产品方向 → emit event → WS 推问题
4. **Monitor 风格 silence_detector** (能力 12): supervisor 加 "N 步 / M 分钟无事件 = stuck" 检测

### Tier 2 (中 ROI, 改鲲中 + 显效大, 3-5 天)

5. **WorkerAgentSpawner** (能力 2): 通用 sub-agent spawn 机制 (不只是 ADR-020 七角色)
6. **Token 经济渗透** (设计 B): LLMMessage list-content 支持 + 多模态 / 引导式 web fetch / Edit-only file 修改
7. **Cache-aware LLMRouter** (能力 8): 复用 system prompt 让 Anthropic cache 命中

### Tier 3 (大改造 + 长线收益, 1-2 周)

8. **暴露 KUN 为 MCP server** (能力 3 反向): 让 Claude Code 反过来用鲲做工具 (商业化第一步)
9. **多模态 ContextPacker** (能力 11): image/pdf asset + Claude/GPT-4V provider 接入
10. **持续学习闭环自动化** (设计 E): distill → Gate auto-promote → seeds → enable → 真跑一次 — 全自动

---

## 15 · 鲲蒸馏的成功标准 (给 dogfood Phase D 用)

每张新 seed 要满足:
- [ ] 对应上面 12 维某一项的具体动作 (不是空泛的"要更好")
- [ ] trigger 含具体条件 (e.g. "Executor file_edit 前 + 改的 function ≥ 2 caller")
- [ ] action 含工具签名 / 数据流 (不是"应该这么做")
- [ ] anti_pattern ≥ 1 (具体的反面教材)
- [ ] evidence 引用本手册或 dev_logs 实例
- [ ] confidence ≥ 0.7 (来自工程实证)
- [ ] applicability.do_not_apply_when 列了边界

每个 hybrid 提案 (Phase E) 要满足:
- [ ] Claude Code 能力 × 鲲已有硬能力 1:1 配对
- [ ] 鲲专属增值是什么 (不只是 "+ 鲲也能做")
- [ ] 改动量估算 (Tier 1/2/3)
- [ ] ROI 评估 (改鲲多少 + 显效多大)

---

**手册结束**. 这份是 Phase 0 输入，给 dogfood Phase A 用。

dogfood Phase A 不再只读 dev_logs — 改成 (dev_logs 案例) + (本手册系统理论) 双输入。
