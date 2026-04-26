# Codex BATCH5 任务书 (M4 阶段独立模块, ~80-100h)

> 模型: **GPT-5.5** (codex MCP, gpt-5.5 API id)
> Worktree: `/Users/petrarain/KUN-codex`
> 主分支基线: **`feat/v2.1-foundation`** (不是 main!)
> 上一批 BATCH4 收尾问题: 见本文档末 "BATCH4 收尾".
>
> **角色边界**: codex 做这 10 个 M4 阶段独立模块, 不动 Claude 在做的 wire 主线 (FastPath / 黑板数据源 / TokenMeter / KillSwitch / EmergentSwitch / KnowledgePrecipitation 接 orchestrator + 傩诊断 5 类 fix handler 实装 + M4 SQLAlchemy 持久化).

## 协作铁律 (这次必须严格执行)

1. **base 必须是 `feat/v2.1-foundation`** — 上一批 10 个 PR 都基于 `main` 出了大问题, 这次起任何分支前必须:
   ```bash
   cd /Users/petrarain/KUN-codex
   git fetch origin
   git checkout -b feat/codex-batch5-<TASK_ID> origin/feat/v2.1-foundation
   ```
   且每个 `gh pr create` 必须 `--base feat/v2.1-foundation`.

2. **每个任务一个 PR**: 独立分支独立 PR, 不要一锅推.

3. **CI 双绿才推**:
   ```bash
   uv run ruff format --check kun tests   # 这步上一批全挂
   uv run ruff check kun tests             # 这步上一批 5 个挂
   uv run mypy kun
   uv run pytest tests/unit -q             # 不许 -x 提前退, 跑完整套
   ```
   任一不绿 — `ruff format kun tests` + 修 check 错 + 重跑 — 直到全绿才推.

4. **不动 Claude wire 主线**:
   - 不改 `kun/engineering/orchestrator.py` (Claude 在 wire 7 个抽象进去)
   - 不改 `kun/api/chat.py` `kun/api/ws.py` `kun/api/blackboard.py` (Claude wire 中)
   - 不改 `kun/engineering/idle_batch.py` (Claude 接 KnowledgePrecipitation)
   - 不改 `kun/context/importance.py` (Claude wire AttentionAnchor pin)
   - 不改 `kun/security/diagnose_runner.py` (Claude 实装 5 类 fix handler)
   - **不改 `kun/datamodel/soul_file.py`** (Claude M4 加 SQLAlchemy)
   - **不改 `kun/datamodel/soul_file_provider.py`** (Claude M4 接 DB)
   - 你的代码全部走"独立模块 + 单测", 接口预留 `# TODO: orchestrator wire by Claude in M4` 注释.

5. **预留 wire 接口而不是直接接**: 所有任务的"集成点"都给 Claude 留一个清晰的 import + class, 不主动改 orchestrator/chat/idle_batch.

## 设置

```bash
cd /Users/petrarain/KUN-codex
git fetch origin
# 每开一个新任务前重新 checkout (不要在旧分支上叠)
git checkout -b feat/codex-batch5-c11 origin/feat/v2.1-foundation
cp ../KUN/.env .env
./scripts/bootstrap.sh
uv run pytest tests/unit -q   # 应 ~490 全过
```

如果 bootstrap 后 `uv run pytest` 不绿, **STOP**, 报告. 不要继续.

---

## 10 个任务 (按价值排, 可并行)

### C11. T21 OODA 外层循环显式建模 (~8-10h)

**目标**: 现在 orchestrator 是步骤循环 (plan → act loop), 但**整体没有显式 OODA 外环** — 任务跑完没 reflect, reflect 没 adjust 回 plan. 这一步把 OODA 6 状态机做出来, 让 orchestrator 在外层套这个机.

**位置**: `kun/core/ooda_loop.py`

**数据模型**:
```python
class OODAState(str, Enum):
    OBSERVE = "observe"
    ORIENT = "orient"
    DECIDE = "decide"
    ACT = "act"
    REFLECT = "reflect"
    ADJUST = "adjust"
    DONE = "done"

class OODACycle(BaseModel):
    cycle_id: str
    task_ref: str
    current_state: OODAState
    state_history: list[tuple[OODAState, datetime]]
    observations: list[dict]      # 输入信号 (signals from outside)
    orientation: dict | None      # 解释模型 (情境理解)
    decision: dict | None         # 决策 (action plan)
    actions_taken: list[dict]     # 实际 act 记录
    reflections: list[dict]       # 反思
    adjustments: list[dict]       # 调整 (回 plan / 回 orient)
    metadata: dict
```

**Engine**:
```python
class OODAEngine:
    async def transition(self, cycle: OODACycle, next_state: OODAState, payload: dict) -> OODACycle:
        """状态迁移. 校验合法 (Observe→Orient→Decide→Act→Reflect→Adjust|Done)."""

    async def reflect(self, cycle: OODACycle) -> dict:
        """Reflect 阶段: 评估 act 结果 vs decide 期望, 输出 reflection."""

    async def should_adjust(self, cycle: OODACycle) -> bool:
        """根据 reflection 判断是否需要 adjust (回到 Orient)."""

    async def adjust(self, cycle: OODACycle) -> OODACycle:
        """Adjust 阶段: 修订 orientation/decision 后回 Decide."""
```

**集成预留** (不实际 wire):
```python
# TODO: orchestrator wire by Claude in M4
# orchestrator 主循环外层套一个 OODACycle, 每个 step 落进对应 state
```

**单测**: ≥10 个, 含状态机非法迁移拒绝 / Reflect 触发 Adjust 全链路 / DONE 终态不可再迁移.

**验收**: `uv run pytest tests/unit/test_ooda_loop.py` 全过.

---

### C12. T23 Context 三大件: 压缩 + 分类合并 + 遗忘 (~10-12h)

**目标**: 长任务 context 暴炸 — 现在没显式管理. 这一步做 3 类 context 操作算子.

**位置**: `kun/context/management.py`

**3 类算子**:
```python
class ContextCompressor:
    async def compress(self, items: list[ContextItem], target_tokens: int) -> list[ContextItem]:
        """按 ImportanceScore 加权压缩 (高分保留原文, 中分摘要, 低分剪)."""

class ContextMerger:
    async def merge_by_topic(self, items: list[ContextItem]) -> list[ContextItem]:
        """按 semantic 聚类后合并同类项 (避免 5 条工具调用结果重复)."""

class ContextForgetter:
    async def forget(self, items: list[ContextItem], threshold: float = 0.2) -> list[ContextItem]:
        """按 ImportanceScore 删低分项, 但留指针 (可恢复)."""
```

**ContextItem 模型**:
```python
class ContextItem(BaseModel):
    item_id: str
    content: str
    kind: Literal["user_msg", "assistant_msg", "tool_call", "tool_result", "summary"]
    importance: float = 0.5    # 接 ImportanceScorer
    timestamp: datetime
    can_forget: bool = True
    reference_count: int = 0   # 被后续步骤引用次数 (高的不能 forget)
```

**集成预留**:
```python
# TODO: chat_handler wire by Claude in M4
# chat_handler 在每次 LLM call 前调 ContextCompressor.compress(items, target_tokens=context_limit*0.7)
```

**单测**: ≥10 个, 含: 压缩后总 tokens 不超 target / merge 后无信息丢失 (用 LLM judge 兜底) / forget 后留指针可恢复.

---

### C13. T24 多臂赌博机 + 自动回滚 (~8-10h)

**目标**: 模型选择是高频决策, 现在 router 写死规则. 这一步做 epsilon-greedy bandit, 自动学习哪个 tier 在哪种 task 上更好, 失败连续 N 次自动回滚到 last good.

**位置**: `kun/core/bandit_router.py`

**Bandit**:
```python
class EpsilonGreedyBandit:
    def __init__(self, arms: list[str], epsilon: float = 0.1):
        ...

    def select(self, context_key: str) -> str:
        """epsilon 概率 explore (随机), 1-eps 概率 exploit (历史最高 reward)."""

    def update(self, context_key: str, arm: str, reward: float) -> None:
        """更新 (context_key, arm) 的 reward 平均."""

    def best_arm(self, context_key: str) -> str:
        """当前最优 arm (用于持久化 / 监控)."""

class AutoRollback:
    def __init__(self, failure_threshold: int = 3, window_sec: int = 600):
        ...

    def record(self, arm: str, success: bool) -> None: ...

    def should_rollback(self, current_arm: str) -> tuple[bool, str | None]:
        """连续 N 次失败 → 回滚到 last_known_good. 返 (rollback_yes, target_arm)."""
```

**集成预留**:
```python
# TODO: router wire by Claude in M4
# router 主选择改为: bandit.select(context_key=task_type) → if rollback.should: rollback.target
```

**单测**: ≥10 个, 含 epsilon 边界 (0/1) / reward 单调收敛到最优 / failure_threshold 触发回滚精确.

---

### C14. T40 反作弊 sandbox (~10-12h)

**目标**: 防 agent 在 tool call 里逃逸 (调宿主机 / 读敏感 env / fork 进程). V1 §15 / V2.1 §15.7 已经规划过, 这次实装最小版.

**位置**: `kun/security/sandbox.py`

**机制**:
```python
class ToolCallSandbox:
    def __init__(self, allowed_paths: list[str], allowed_envs: list[str], cpu_limit_sec: int = 30):
        ...

    async def run(self, tool_name: str, args: dict, agent_id: str) -> SandboxResult:
        """在 subprocess 跑 tool call, 限制 cwd / env / cpu / network."""

    def detect_escape(self, output: str) -> list[EscapeViolation]:
        """检测 output 内是否泄露宿主机路径 / 敏感 env / 父进程信息."""

class SandboxResult(BaseModel):
    success: bool
    output: str
    cpu_ms: int
    violations: list[EscapeViolation] = []
```

**实装要点**:
- 用 `subprocess.run` + `cwd=` + `env=`(白名单) + `timeout=`
- macOS 下用 `sandbox-exec` (有 profile), Linux 下 fallback 到 `firejail` 检测
- 没装 sandbox-exec/firejail → 降级到"软沙盒"(只检测不强隔离), 加 warning log

**集成预留**:
```python
# TODO: orchestrator tool dispatcher wire by Claude in M4
# orchestrator 任何 tool call 走 sandbox.run(), 而不是直接调 tool 函数
```

**单测**: ≥8 个, 含: 路径越界 / env 泄露 / cpu 超时 / 软沙盒降级 fallback.

---

### C15. T42 per-project constitution (~6-8h)

**目标**: 每个项目应有自己的 prompt rules (品牌/风格/禁词), 让 KUN 在该项目所有任务里默认遵守.

**位置**: 
- `kun/datamodel/project_constitution.py` (数据模型)
- `kun/api/project_constitution.py` (CRUD API)

**数据模型**:
```python
class ProjectConstitution(BaseModel):
    project_id: str
    tenant_id: str
    rules: list[ConstitutionRule]
    created_at: datetime
    updated_by: str
    version: int = 1

class ConstitutionRule(BaseModel):
    rule_id: str
    kind: Literal["style", "tone", "forbidden_word", "required_word", "allowed_tool", "blocked_tool"]
    pattern: str           # regex 或 literal 关键词
    severity: Literal["warn", "block"] = "warn"
    description: str
```

**Loader**:
```python
class ConstitutionLoader:
    def load_from_file(self, project_dir: Path) -> ProjectConstitution | None:
        """读 .kun/constitution.md (markdown frontmatter 格式)"""

    async def load_from_db(self, project_id: str) -> ProjectConstitution | None: ...

    def render_to_system_prompt(self, c: ProjectConstitution) -> str:
        """渲染为 system prompt 片段, 让 LLM 遵守."""
```

**集成预留**:
```python
# TODO: chat_handler wire by Claude in M4
# chat_handler 进 system_prompt 时拼上 ConstitutionLoader.render_to_system_prompt
```

**单测**: ≥8 个, 含 markdown 解析 / forbidden_word 命中 / version 升级.

---

### C16. T29 React Flow 节点图 (NUO 第 2 层, ~12-16h)

**目标**: 任务编排可视化, 让用户能看到当前 task DAG (节点 = step, 边 = 依赖, 实时高亮当前 step).

**位置**:
- `frontend/components/task-flow/TaskFlowGraph.tsx`
- `frontend/app/tasks/[task_id]/flow/page.tsx`

**技术栈**: React Flow (https://reactflow.dev/) + 现有 SSE/WS 接 task events.

**功能**:
1. 节点: step (status: pending/running/done/failed/skipped)
2. 边: step 依赖 (从 plan.deps 拉)
3. 实时: 接 `/ws/tasks/<task_id>/events`, 每收到 step 状态变化就更新对应节点颜色
4. 交互: 点击节点 → 右侧 drawer 展示该 step 的 input/output/cost/duration
5. 控制: 暂停 / 跳过当前 step / 强制 done (走 confirmation)

**API 依赖**:
- 已有: `GET /api/tasks/<task_id>` 拿 plan + steps
- 需要新: `POST /api/tasks/<task_id>/steps/<step_id>/skip` (这个 codex 加, 走 PlanOnlyGate 走 human_approval)

**单测 + Playwright**: 至少 1 个 e2e (mock task event 流, 验证节点颜色实时变).

---

### C17. T27 Starter Pack 扩到 20 skill (~10-14h)

**目标**: 现在 starter_pack 有 5 skill (估计), 扩到 20, 覆盖最常用场景.

**位置**: `kun/skills/starter_pack/` 下加 15 个新 skill.

**清单 (15 个新 skill)**:
1. `web_summarize` - 抓 URL 摘要
2. `pdf_extract` - PDF 文本/表格提取
3. `image_describe` - 图片 → 描述
4. `code_lint` - 代码 lint (ruff/eslint/clippy)
5. `code_format` - 代码格式化
6. `git_diff_review` - git diff 自动 review
7. `sql_query` - 安全 SQL 查询 (read-only)
8. `csv_analyze` - CSV 统计/可视化
9. `markdown_to_docx` - md → docx
10. `markdown_to_pdf` - md → pdf
11. `translate` - 多语言翻译
12. `regex_explain` - regex → 自然语言解释
13. `cron_explain` - cron 表达式 → 自然语言
14. `json_validate` - JSON schema 校验
15. `time_zone_convert` - 时区转换

每个 skill 一个文件 (`kun/skills/starter_pack/<name>.py`), 实现:
```python
class WebSummarizeSkill(BaseSkill):
    name = "web_summarize"
    description = "..."
    input_schema = {...}
    async def run(self, args: dict) -> dict: ...
```

**单测**: 每个 skill ≥1 个 (15 个), 加 1 个 starter_pack 注册测试.

---

### C18. T36 多任务非阻塞编排 (~8-10h)

**目标**: 一个 user 同时跑 3 个长任务时, 不该串行. 这一步做 task scheduler, 让多 task 并发 + 资源限制.

**位置**: `kun/core/multi_task_scheduler.py`

**机制**:
```python
class MultiTaskScheduler:
    def __init__(self, max_concurrent_per_user: int = 3, max_concurrent_global: int = 50):
        ...

    async def submit(self, task: TaskRef) -> str:
        """提交 task. 返 task_id. 超并发 → 入 wait_queue."""

    async def wait_done(self, task_id: str, timeout_sec: int) -> TaskResult: ...

    def cancel(self, task_id: str, reason: str) -> bool: ...

    def get_status(self, task_id: str) -> TaskStatus:
        """queued / running / done / failed / cancelled."""

    def list_user_tasks(self, user_id: str) -> list[TaskStatus]: ...
```

**集成预留**:
```python
# TODO: chat_handler wire by Claude in M4
# /api/chat/run 改为 scheduler.submit() 而不是直接 await orchestrator.run()
```

**单测**: ≥10 个, 含: 并发上限 / 超额入队 / cancel / wait_queue 顺序.

---

### C19. T26 TASK.md L3 + LayeredAsset 推全 (~8-10h)

**目标**: V1 §11.5 / V2.1 §11.5 LayeredAsset 已有抽象, 但只有 L1/L2. 这一步加 L3 (TASK.md 项目级), 并做"自动推全" (用户 update 一个 asset, 系统建议 promote 到更高 layer 给同类任务复用).

**位置**: `kun/datamodel/layered_asset.py` (扩展) + `kun/engineering/asset_promoter.py` (新)

**L3 加入**:
```python
class AssetLayer(str, Enum):
    L1_TASK = "L1_task"          # 单任务级
    L2_PROJECT = "L2_project"    # 项目级
    L3_USER = "L3_user"          # 用户级
    L4_GLOBAL = "L4_global"      # 全局共享 (匿名化后)
```

**Asset Promoter**:
```python
class AssetPromoter:
    async def suggest_promote(self, asset_id: str) -> tuple[AssetLayer, float]:
        """根据 asset 复用次数 / 任务多样性, 建议升级 layer (返 layer + confidence)."""

    async def execute_promote(self, asset_id: str, target_layer: AssetLayer, user_confirmed: bool) -> None:
        """执行升级 (L1→L2 自动, L2→L3 用户确认, L3→L4 走匿名化 + 用户确认)."""
```

**单测**: ≥8 个, 含 4 层升级路径 / L3→L4 匿名化校验.

---

### C20. T37 任务中途动态调整完整 OODA (~8-10h)

**目标**: 现在 plan 一旦定下来, 中途改 plan 走"重新走完整 plan flow". 这一步做"局部重 plan" — 只重新规划当前 step 之后的部分, 保留前面的 sunk work.

**位置**: `kun/engineering/dynamic_replan.py`

**机制**:
```python
class DynamicReplanner:
    async def detect_replan_needed(self, cycle: OODACycle) -> tuple[bool, str]:
        """检测当前 cycle 是否需要 replan. 返 (yes, reason)."""

    async def replan_from_step(
        self,
        original_plan: Plan,
        current_step_idx: int,
        new_observations: list[dict],
    ) -> Plan:
        """从当前 step 之后重新规划, 保留前面 step 的 output 作为新 context."""

    def calculate_sunk_cost(self, original_plan: Plan, current_step_idx: int) -> float:
        """评估 replan 的沉没成本 (告诉用户决策时知道损失)."""
```

**依赖**: C11 OODAEngine — 在 Reflect 阶段调 detect_replan_needed.

**单测**: ≥8 个, 含 replan 后 step deps 重链 / sunk cost 估算精确.

---

## 推送策略

每完成一个任务:
```bash
cd /Users/petrarain/KUN-codex
# (假设你在 feat/codex-batch5-c11)
uv run ruff format kun tests          # 必须先跑这个修格式
uv run ruff check kun tests           # 不绿就修
uv run mypy kun                       # 不绿就修
uv run pytest tests/unit -q           # 不绿就修
git add -A && git commit -m "feat(c11): OODA 外层循环显式建模"
git push origin feat/codex-batch5-c11
gh pr create --base feat/v2.1-foundation --title "BATCH5 C11: OODA 外层循环显式建模"
```

**绝对不能**:
- ❌ 推未跑过 ruff format 的代码
- ❌ 跨任务一锅推
- ❌ PR base 设成 main (上一批犯过)
- ❌ 改 Claude wire 中的文件 (orchestrator.py / chat.py / ws.py / blackboard.py / idle_batch.py / importance.py / diagnose_runner.py / soul_file.py / soul_file_provider.py)

---

## BATCH4 收尾 (优先做, 然后才能开 BATCH5)

BATCH4 10 个 PR 当前状态: 全部 lint FAILURE (因为基础设施债 + 你 PR 内文件未 format). 我已经在 foundation 修了 4 个文件 + 推了, 你这边需要:

**每个 BATCH4 PR 做一次**:
```bash
cd /Users/petrarain/KUN-codex
git checkout feat/codex-batch4-c1
git fetch origin
git rebase origin/feat/v2.1-foundation
uv run ruff format kun tests           # 修自己 PR 内的 format
uv run ruff check kun tests             # 应过 (foundation 已修)
uv run pytest tests/unit -q             # 应过
git push --force-with-lease
```

10 个 PR 都做这一遍, CI 应该全绿.

**外加**:
- **#20 (C1)**: 修我之前 review 的 2 个 issue (path traversal: 限制只能访问 task_workspace 下;  git false negative: detect_git_log_answer_leak 不要简单 substring, 用 SHA256 比对 git log entry hashes)
- **#22 (C3)**: 修我之前 review 的 2 个 issue (SSRF: url_check kind 必须走 allowlist 或私网 reject; human_approval 必须持久化到 DB 不能内存 dict, 服务重启就丢)

完成 BATCH4 收尾后, Claude 直接 merge 8 个 LGTM, 然后你开始 BATCH5.

---

## 何时报告

每完成 1-2 个任务汇总一次, 不要等 10 个全做完才报. Claude 这边同时在 wire, 我们要互相通气.
