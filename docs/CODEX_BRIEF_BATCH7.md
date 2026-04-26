# Codex BATCH7 任务书 (V2.2 §24 CodeCapability + 杂项)

> 模型: **GPT-5.5** (codex MCP)
> Worktree: `/Users/petrarain/KUN-codex-worktrees/<task>`
> 主分支基线: **`feat/v2.1-foundation`**
>
> **背景**: V2.2 §24 加 "代码能力层" (CodeCapability), Andrej Karpathy ai-skills 启发. 让 KUN 真能"用 code 解决问题" — 不只是调 skill, 还要 read 代码 / write+lint+test 闭环 / sandbox 执行 / 自动 debug / 静态审查.
>
> **跟前面的连贯**: BATCH5 / BATCH6 还有任务, 但用户拍板 BATCH7 优先 — 因为 CodeCapability 是 V2.2 完整度的关键缺口 (现在 KUN 不会真"用代码", 只会调单点 skill).

## 协作铁律 (BATCH4-6 已稳定, BATCH7 同)

1. **base = `feat/v2.1-foundation`** (老三件: 起分支前 fetch + checkout, PR `--base` 写对)
2. 每任务一 PR, 不一锅推
3. CI 双绿: ruff format + ruff check + mypy + pytest 全过才推
4. 不动 Claude 心脏 (orchestrator/value_gate/marginal_roi/anchor_expand/importance/packer/selector/multi_judge)
5. 每个 task 做完报一下, 不等全做完才报

## 设置

```bash
cd /Users/petrarain/KUN-codex-worktrees/c28
git fetch origin
git checkout -b feat/codex-batch7-c28 origin/feat/v2.1-foundation
cp ../KUN/.env .env
./scripts/bootstrap.sh
uv run pytest -q   # 应 ~860 全过
```

---

## 任务列表 (按优先级)

### C28. CodeCapability — CodeReader + CodeExecutor (~10-12h, V2.2 §24 起步)

**目标**: V2.2 §24 第一批 — CodeReader (读 codebase) + CodeExecutor (sandbox 跑). CodeDebugger + CodeReviewer + CodeWriter 后续 BATCH8.

**位置**: `kun/skills/code_capability/` (新目录)

**CodeReader** (`kun/skills/code_capability/reader.py`):
```python
class CodeReader:
    """读 codebase, 理解结构.
    
    关键能力:
    - find_anchor_file(query): grep + LLM 找最相关 file
    - get_dependencies(file_path): 沿 import 找邻接 file
    - get_callers(symbol): 反查 symbol 在哪些地方被用
    - explain(file_path, lines=None): LLM 解释这段代码
    """
    
    async def find_anchor_file(self, query: str, root: str = ".") -> str | None:
        """用 ripgrep + os.walk 找 query 命中的 file, 按相关性排序返 top 1."""
    
    async def get_dependencies(self, file_path: str) -> list[str]:
        """解析 import 语句, 返被 import 的 file 路径列表."""
    
    async def get_callers(self, symbol: str, root: str = ".") -> list[str]:
        """grep 找 symbol 出现的所有 file:line 位置."""
    
    async def explain(self, file_path: str, lines: tuple[int, int] | None = None) -> str:
        """调 LLM 解释代码 (用 starter_pack 的 reading skill)."""
    
    async def read_anchor_then_expand(self, query: str) -> AsyncIterator[str]:
        """V2.2 §19.3 集成: 先 anchor file, 沿 dependencies 邻接 expand."""
```

**CodeExecutor** (`kun/skills/code_capability/executor.py`):
```python
class CodeExecutor:
    """sandbox 跑 code, 看输出.
    
    安全要求:
    - 强制 timeout (默认 30s)
    - 强制 cwd (限定 task workspace)
    - 走 V2.1 sandbox (env 隔离 / ZeroTelemetry / KillSwitch 兼容)
    - 禁止 network access (除非用户授权)
    """
    
    async def execute_python(
        self, code: str, *, timeout_sec: int = 30, cwd: Path | None = None
    ) -> ExecutionResult:
        """跑 python 代码片段. 走 starter_pack coding-pytest 风格."""
    
    async def execute_test(
        self, test_path: str, *, timeout_sec: int = 60
    ) -> TestResult:
        """跑 pytest. 返 (passed, failed, skipped) + 错误细节."""
    
    async def execute_lint(
        self, target: Path, tool: str = "ruff"
    ) -> LintResult:
        """跑 ruff/black/mypy. 返 issues list."""
```

**Facade** (`kun/skills/code_capability/__init__.py`):
```python
class CodeCapability:
    """5 组件 facade. C28 只接 reader + executor, 其他 BATCH8."""
    
    def __init__(self) -> None:
        self.reader = CodeReader()
        self.executor = CodeExecutor()
        # self.writer / self.debugger / self.reviewer 留 BATCH8
    
    @classmethod
    def get(cls) -> "CodeCapability":
        """singleton getter (跟 starter_pack 注册集成)."""
```

**集成 (留 wire 给 Claude)**:
```python
# TODO: hermes ExecutionStep action_type 加 "code_read" / "code_execute"
#       (待 Claude 在 V2.2 §22 升级时一起做)
# TODO: ValueGate.value_estimator 看到 hermes action_type=code_execute → 算 sandbox 成本
```

**单测**: ≥12 个
- CodeReader: 5 个 (find_anchor / get_dependencies / get_callers / explain mock LLM / read_anchor_then_expand 集成)
- CodeExecutor: 5 个 (execute_python happy / timeout / lint pass+fail / test pass+fail / sandbox cwd 限制)
- CodeCapability facade: 2 个 (singleton / 5 组件位置预留)

**fixture**: `tests/fixtures/code_samples/`
- minimal_module.py (有 import + 函数 + 测试)
- broken_module.py (语法错误)
- failing_test.py

---

### C12. Context 三大件: 压缩 + 分类合并 + 遗忘 (~10-12h, V2.2 §16 wire)

**目标**: 长任务 context 暴炸. 加 3 类 context 操作算子: 压缩 / 合并 / 遗忘.

详见 `docs/CODEX_BRIEF_BATCH5.md` C12 (brief 没变).

---

### C13. 多臂赌博机 + 自动回滚 (~8-10h)

详见 BATCH5 brief C13.

---

### C14. 反作弊 sandbox (~10-12h)

详见 BATCH5 brief C14. **跟 C28 CodeExecutor 配合** — sandbox 要复用同一套机制.

---

## 推荐执行顺序

**你建议先**: BATCH6 收尾 (#37 rebase + merge), 然后 BATCH7 C28 (CodeCapability), 然后 BATCH5 C12-C14 (Context / Bandit / Sandbox 三件 — 跟 C28 协同价值最大).

**总剩工时估算**:
- BATCH6 收尾: #37 rebase + 合 (5 min)
- BATCH7 C28: 10-12h
- BATCH5 C12: 10-12h
- BATCH5 C13: 8-10h
- BATCH5 C14: 10-12h (跟 C28 复用 sandbox)
- BATCH5 C15-C20: 各 5-15h, 共 ~40-50h

**总 ~80-100h codex 工.**

Claude 这边等 codex 完成后做最后 wire (CodeCapability hermes action_type / ExecutionStep extend 等, ~5-10h).

---

## 关于 Karpathy ai-skills

V2.2 §24 是基于 Andrej Karpathy ai-skills 项目的启发. 关键洞察:
- LLM 写代码不难, 难的是"写完会自己跑、调试、修"
- 真闭环需要: read → write → execute → debug → review 5 件套
- KUN 现有 starter_pack (5 个 skill) 是"零件", CodeCapability 是"组装机"

C28 是第一步起步, 完整 5 件套估 25-35h (C28 + BATCH8 后续 3 件).
