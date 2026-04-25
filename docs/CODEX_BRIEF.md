# Codex 任务书 — 第 2 批

> 给在 `/Users/petrarain/KUN-codex` worktree 工作的 Codex agent。
> **第 1 批已完成并合入 main**（RLS / 幂等 / pending action 闭环 / DB 约束）。
> 这是第 2 批：6 个**独立模块**，不和主线代码冲突，可以并行做。

---

## 你是谁、在哪干活

- 身份：审计 / 修复 / 独立模块开发 agent
- 工作目录：**`/Users/petrarain/KUN-codex`**（git worktree，**不要碰** `/Users/petrarain/KUN`）
- 分支：`feat/codex-batch2`（本批）
- Claude（主线开发）在 `/Users/petrarain/KUN` 的 `main` 分支做主路径开发

完整协作规则见 [`docs/COLLABORATION.md`](./COLLABORATION.md)。

---

## 第一次 setup

```bash
cd /Users/petrarain/KUN-codex
git fetch && git rebase origin/main          # 同步 Claude 的最新进度
cp ../KUN/.env .env                           # 复用配置
./scripts/bootstrap.sh                        # 幂等
git switch -c feat/codex-batch2               # 新分支
```

---

## 任务总览

6 个**独立**模块。每个独立 commit + 独立 PR。完成后用户合并。

| # | 任务 | 估时 | 路径（新建） |
|---|---|---|---|
| **T1** | **中央重要度打分器** | 4-6h | `kun/context/importance.py` |
| **T2** | **翻译适配器层** | 4-6h | `kun/interface/adapters/` |
| **T3** | **多判官投票** | 3-4h | `kun/engineering/multi_judge.py` |
| **T4** | **LLM 故障转移** | 2-3h | `kun/interface/llm/failover.py` |
| **T5** | **红队测试机制** | 4-6h | `kun/security/red_team/` |
| **T6** | **外部 agent benchmark** | 4-6h | `kun/api/nuo/benchmark_panel.py` + `kun/engineering/agent_benchmark.py` |

合计 **20-30 小时**。建议按编号顺序做（依赖度递增）。

---

## T1 — 中央重要度打分器

**对应方案 §3.2**。Context 子系统的中枢——所有其他模块（检索权重、衰减速度、层级归属）都用它的输出。

**接口**：

```python
# kun/context/importance.py
from dataclasses import dataclass

@dataclass
class ImportanceScore:
    overall: float       # 0..1
    semantic: float      # 与 query 的语义相关
    frequency: float     # 访问频率（带饱和）
    recency: float       # 时间近期性
    rationale: str

class ImportanceScorer:
    def score(
        self,
        *,
        asset: LayeredAsset,
        query: str | None = None,
        now: datetime | None = None,
    ) -> ImportanceScore: ...
```

**实现要点**：

- 三因素加权求和：`overall = 0.5*semantic + 0.3*frequency + 0.2*recency`（系数初始；后续学习）
- 语义相关度：用 Qdrant embedding 算 cosine（query 也 embed 一遍）；query=None 时返 1
- 频率：`min(1, log(1 + access_count) / log(1 + 100))`（饱和到 100 次）
- 近期性：`exp(-elapsed_days / half_life)`，half_life 看 asset_kind（永久档 ∞ / 长期 11.25 / 短期 5）
- 启发式 + 自愈（方案 §3.2）：初始用规则，访问即强化，60% 即时 → 长期自愈到 85-90%
- 异常时调便宜模型复审：「持续被检索但低分」或「长期不访问占位」时 emit `importance.review_needed` 事件

**测试**：

- `tests/unit/test_importance_scorer.py`：每因素独立验证 + 综合分边界 + 衰减模型
- 集成：往 store 塞 5 个 asset，access 不同次数，assert score 排序符合预期

**禁忌**：不动 `kun/context/storage.py`（Claude 在重写）；不接任何 LLM 调用（这是纯工程组件）。

---

## T2 — 翻译适配器层

**对应方案 §4.2**。出口网关：把 KUN 内部结构化信息翻译成"对方需要的格式"（自然语言 / A2A / REST / Markdown）。

**接口**：

```python
# kun/interface/adapters/__init__.py
class OutputAdapter(Protocol):
    name: str  # "human" / "a2a" / "rest" / "markdown" / "email"
    
    async def translate(
        self,
        *,
        payload: dict,         # KUN 内部 JSON
        recipient_kind: str,    # "user" / "agent" / "company" / "doc_system"
        context: dict | None = None,
    ) -> str: ...

# 内置实现
- HumanAdapter        — 自然语言（中英根据 audience）
- A2AAdapter          — Agent-to-Agent JSON-RPC
- RESTAdapter         — REST 请求 body 模板
- MarkdownAdapter     — Markdown 报表
- EmailAdapter        — HTML 邮件模板
```

**实现要点**：

- 每 adapter 独立文件 `kun/interface/adapters/{name}.py`
- 注册表 `_REGISTRY: dict[str, OutputAdapter]`，类似 `kun.skills.dispatcher`
- HumanAdapter 调便宜模型（从 router 拿 cheap tier）做自然语言生成；不直接 import provider
- 测试用 stub adapter 验证调度逻辑

**测试**：`tests/unit/test_output_adapters.py`

**禁忌**：不动 orchestrator；不动 router；adapters 是叶子组件。

---

## T3 — 多判官投票

**对应方案 §8.1**。3-5 个 LLM 同时跑评估，多数票定。

**接口**：

```python
# kun/engineering/multi_judge.py
@dataclass
class JudgeBallot:
    judge_id: str            # 哪个 model 投的
    pass_: bool
    score: float             # 0..1
    reason: str
    cost_usd_actual: float
    latency_ms: float

@dataclass
class JuryVerdict:
    pass_: bool              # 多数票
    avg_score: float
    spread: float            # 标准差，反映分歧
    ballots: list[JudgeBallot]
    rationale: str

async def jury_evaluate(
    *,
    artifact: str,
    rubric: str,
    judge_models: list[str],   # ["claude-haiku", "gpt-5-mini", "minimax-m2.7"]
    router: LLMRouter,
) -> JuryVerdict: ...
```

**实现要点**：

- 并发跑 N 个 judge（`asyncio.gather`）
- 投票顺序随机化（给 prompt 加随机 seed 避免位置偏见）
- 与人类评审对齐目标：Spearman 0.80+（这个是评估目标，写在 docstring 里，留一个 `tests/integration/test_human_alignment.py` 占位）
- 失败 judge 不阻塞投票（少于 3 票才返 inconclusive）

**测试**：mock router 返 stub response，验证多数票计算 / 失败容错 / spread 计算。

---

## T4 — LLM 故障转移

**对应方案 §10.5 故障转移**。多供应商自动切换的硬门槛——某 provider 连续失败 N 次，自动切到备选。

**接口**：

```python
# kun/interface/llm/failover.py
@dataclass
class FailoverPolicy:
    failure_threshold: int = 3       # 连续失败次数
    cooldown_sec: int = 300          # 退避后多久重试
    primary: str                      # provider name
    backup: list[str]                 # 备选顺序

class FailoverGuard:
    def record_failure(self, provider_name: str) -> bool: ...  # 返回是否需要切换
    def record_success(self, provider_name: str) -> None: ...
    def current_active(self) -> str: ...
```

**集成点**：`LLMRouter.invoke` 失败路径调 `record_failure`；超阈值后下一次 invoke 跳过该 provider。

**测试**：用 fake providers 验证阈值触发 + 冷却 + 多备选切换。

---

## T5 — 红队测试机制

**对应方案 §12.5**。每月 + 每重要变更前自动跑越狱 / 长文本轰炸 / 假冒 A2A / 数据投毒。

**结构**：

```
kun/security/red_team/
├── __init__.py
├── runner.py              # async run_red_team_suite() -> RedTeamReport
├── scenarios/
│   ├── jailbreak.py       # 50+ 越狱 prompt
│   ├── prompt_injection.py
│   ├── long_context.py    # 大文本轰炸
│   ├── a2a_spoofing.py    # 伪造 X-Tenant-Id / 伪造 Authorization
│   └── data_poisoning.py
└── fixtures/
    └── jailbreak_corpus.yaml  # 测试用例数据库
```

**报告**：

```python
@dataclass
class RedTeamReport:
    suite_id: str
    started_at: datetime
    finished_at: datetime
    total_scenarios: int
    pass_count: int
    fail_count: int
    findings: list[RedTeamFinding]  # 哪个场景挂了 + 影响面 + 建议
```

**集成点**：CLI 命令 `kun security red-team`；后续接到 idle_batch 周期跑。

**测试**：mock 系统响应；验证 scenario 框架可加新条目。

---

## T6 — 外部 agent benchmark

**对应用户反馈 #11**：傩需要"判断其他 agent 的评分能力，综合打分"。

**结构**：

```
kun/engineering/agent_benchmark.py    # 跑 benchmark 的引擎
kun/api/nuo/benchmark_panel.py        # NUO API 端点
```

**接口**：

```python
# kun/engineering/agent_benchmark.py
@dataclass
class BenchmarkTask:
    task_id: str
    task_type: str
    prompt: str
    expected_kind: str       # "code_compile" / "exact_match" / "rubric_score"
    expected: Any

@dataclass
class AgentBenchmarkResult:
    agent_ref: str           # "external_agent:openai-codex" / "internal:rt-coder-01"
    task_id: str
    success: bool
    score: float
    cost_usd: float
    duration_sec: float

async def run_benchmark(
    *,
    agent_invoke: Callable[[str], Awaitable[str]],
    tasks: list[BenchmarkTask],
) -> list[AgentBenchmarkResult]: ...
```

**端点**：

```
GET  /nuo/benchmark/agents       — 列已注册的 agent + 最新得分
POST /nuo/benchmark/run          — 启动一轮 benchmark
GET  /nuo/benchmark/results/{id} — 看具体结果
```

**测试**：mock agent_invoke；跑 5 个示例任务；assert 评分 / 成功率计算。

---

## 协作铁律（重申）

1. 不在 `main` 上写代码 — 全部走你的 `feat/codex-batch2` 分支 + PR
2. 一个任务一个 commit（小 commit 勤 push）
3. 不动 Claude 主线在改的文件（如 `orchestrator.py` / `router.py` / `agent_loop.py` / `intent.py` — 这些 Claude 在重写）
4. 测试 + 文档跟代码一起走（每个新模块要有对应 unit test）
5. 不 commit `.env` / 任何 key

## 完成定义

每个任务完成 = ✅ 代码 + ✅ unit test + ✅ ruff/format/mypy clean + ✅ PR 描述说清楚集成点

主线（Claude）会在 PR 合入后做集成（接到 orchestrator / router / NUO 等）。

---

## 你做哪个先？

建议顺序：T1 → T3 → T4 → T2 → T6 → T5（依赖度从低到高）。
也可以并行做 T1 / T3 / T4（互相独立）。

启动后跟用户在主聊天里同步进度。Claude 会把你的 PR 合并到 main。
