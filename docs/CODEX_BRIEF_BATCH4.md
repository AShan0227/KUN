# Codex BATCH4 任务书 (V2.1 wire 之后的独立模块)

> 模型: **GPT-5.5** (codex MCP, gpt-5.5 API id)
> Worktree: `/Users/petrarain/KUN-codex`
> 分支: `feat/codex-batch4`
> 主分支基线: `feat/v2.1-foundation` (Claude 刚推, ~5500 行新代码 + 445 测试全绿)
>
> **角色边界**: codex 做这 10 个**独立模块**, 不动 Claude 在做的 wire 主线 (StrategyMatcher / FastPath / Panorama / Blackboard / TokenMeter / KillSwitch / AttentionAnchor / Precipitation 接 orchestrator).

## 协作铁律 (再申一次)

1. **必须在 worktree 工作**: `cd /Users/petrarain/KUN-codex && git status` 确认分支 `feat/codex-batch4`. 如果你发现自己在 `/Users/petrarain/KUN` 或 `/Users/petrarain/Documents/New project/KUN`, **立刻切回 worktree**.
2. **每个任务一个 PR**: 不要把 10 个任务一锅推. 每完成一个 → `git push origin feat/codex-batch4-<TASK_ID>` → `gh pr create`.
3. **必须跑测试**: `uv run pytest tests/unit -q` 全过才推. ruff/mypy 全过.
4. **从 main rebase**: 开工前 `git fetch origin && git rebase origin/main`.
5. **不动 Claude 主线模块**: 不改 `kun/core/strategy_matcher.py` / `kun/core/task_panorama.py` / `kun/api/blackboard.py` / `kun/engineering/fast_path.py` 等 V2.1 已有抽象 (除非 PR 描述明确说"扩展, 非冲突修改").

## 设置

```bash
cd /Users/petrarain/KUN-codex
git fetch origin
git checkout -b feat/codex-batch4 origin/feat/v2.1-foundation
cp ../KUN/.env .env  # 拿凭证
./scripts/bootstrap.sh
uv run pytest tests/unit -q  # 应 445 全过
```

如果 bootstrap 后 `uv run pytest` 不是 445, **STOP**, 报告问题. 不要继续.

---

## 10 个任务 (按依赖度排, 可并行)

### C1. T48 工具输出哈希 + diff 自检 (~6-8h)

**目标**: 防 agent 写假 pytest 输出 / 改 reward 文件 / 用 git log 抄答案. 解致命差评 #2 (Replit 4000 假用户事件).

**位置**: `kun/security/output_verifier.py`

**接口**:
```python
class OutputVerifier:
    def hash_artifact(self, path: str) -> str:
        """对 agent 产出的文件算 SHA256, 进 audit log."""

    def verify_diff(self, before: str, after: str, expected_changes: list[str]) -> bool:
        """对比 agent 声称的改动 vs 真实 diff. 不一致 → False."""

    def check_pytest_output(self, output_text: str, test_file_path: str) -> bool:
        """对比 pytest 输出 vs 真跑一次的输出. 不一致 → False (agent 编造)."""

    def detect_git_log_answer_leak(self, agent_output: str, git_log_text: str) -> bool:
        """检测 agent 是否抄了 git log 历史答案."""
```

**单测**: ≥10 个, 含 happy path + agent 编造 negative case.

**验收**: `uv run pytest tests/unit/test_output_verifier.py` 全过.

### C2. T50 计费透明承诺 — NUO 页面 + API (~4-6h)

**目标**: 解致命差评 #4 (Cursor 偷涨价 / Manus 私扣).

**位置**:
- `kun/api/billing_transparency.py` (后端 API)
- `frontend/app/billing/page.tsx` (前端页面)

**API 5 endpoint**:
```
GET /api/billing/promise           返 ADR-022 承诺正文
GET /api/billing/audit-log         user 自己每笔扣款审计
POST /api/billing/refund-request   用户一键退款 (按比例)
GET /api/billing/upcoming-changes  30 天预告窗口 (空数组也合法)
GET /api/billing/dashboard         汇总 used_today / used_month / saved_by_kun
```

**前端**: 单页面展示 5 部分:
- 30 天预告承诺横幅
- 余额永不蒸发声明
- 自助退款按钮
- 寒暄不计费规则展示
- 全量 audit log 表格

**单测 + Playwright e2e**: 至少 8 个 API 测试 + 前端渲染检查.

### C3. T53 任务"成功"必有可验证产物 (~6-8h)

**目标**: 解致命差评 #2 (假完成).

**位置**: `kun/datamodel/verification_spec.py` + `kun/engineering/verification_runner.py`

**数据模型**:
```python
class VerificationSpec(BaseModel):
    kind: Literal["exact_output", "test_pass", "lint_pass", "url_check",
                  "human_approval", "hash_match", "schema_validate"]
    spec: dict  # 各 kind 特定配置
    required: bool = True
    timeout_sec: int = 60
```

**Runner**:
```python
class VerificationRunner:
    async def verify(self, spec: VerificationSpec, artifact_ref: str) -> VerificationResult:
        """跑验证. 返 (passed, evidence_url, error_msg)."""
```

**集成点**: orchestrator 在标 `done` 之前必须调 `verification_runner.verify()`. 失败 → 不能 done, 走重试 / 升档.

**注意**: 不要直接改 orchestrator (Claude 在 wire). 只提供 runner, 加注释 `# TODO: orchestrator wire in M3.2`.

**单测**: 7 类 kind 各 ≥1 个测试.

### C4. T54 dev/prod 物理隔离 (~8-10h)

**目标**: 解致命差评 #5 (Replit 删生产库).

**位置**:
- `kun/core/env_isolation.py` (核心)
- `docker-compose.dev.yml` 加 prod-isolated 标签

**机制**:
```python
class EnvIsolation:
    def get_db_url(self, env: Literal["dev", "staging", "prod"]) -> str:
        """各 env 独立 DB connection (不同 user / 不同 schema / 不同实例)."""

    def get_object_store_bucket(self, env: ...) -> str:
        """各 env 独立 MinIO bucket."""

    def can_cross_env(self, from_env: str, to_env: str, user_id: str) -> bool:
        """跨 env 操作 (dev → prod) → 默认拒, 走双人审批."""
```

**集成**: orchestrator sandbox 分配时强制 `env_isolation.get_*` (不直接 wire, 留接口).

**单测**: ≥8 个, 含跨 env 访问拒绝场景.

### C5. T19 + T20 周月报 + 批处理报告推送 (~10-14h)

**目标**: V1 §11.1 三层透明化报告生产端 (NotificationLayer kind 枚举已有).

**位置**: `kun/engineering/reports.py`

**3 类报告生成器**:
```python
class WeeklyReportGenerator:
    async def generate(self, user_id: str, week_start: datetime) -> WeeklyReport:
        """汇总:消费 / 任务数 / 节省 / 涌现学到 / 系统改进."""

class MonthlyReportGenerator:
    async def generate(self, user_id: str, month: date) -> MonthlyReport:
        """月度同 + 环比."""

class IdleBatchReportGenerator:
    async def generate(self, batch_id: str) -> IdleBatchReport:
        """idle-batch 跑完 1h25min 推用户."""
```

**推送**: 走现有 NotificationLayer (`notification_kind=weekly_digest / batch_report`).

**调度**: 周报每周一 9am(用户时区) / 月报每月 1 号 / batch 报告 idle-batch 完成时.

**单测**: ≥6 个 + 1 个集成测试 (mock 数据生成完整 weekly).

### C6. T22 守望管 LLM 路由 (~6-8h)

**目标**: 守望规则 (RuleEngine) 接 capability_router, 实现"capability_card 数据驱动模型选".

**位置**: `kun/watchtower/llm_route_governance.py`

**机制**:
```python
class LLMRouteGovernor:
    def __init__(self, rule_engine, capability_router):
        ...

    async def consult_for_model_select(
        self,
        task_meta: dict,
        candidate_models: list[str],
    ) -> str:
        """守望咨询:在候选中按 capability_card 历史成功率重排."""

    async def trigger_route_change(
        self,
        task_type: str,
        from_model: str,
        to_model: str,
        reason: str,
    ) -> None:
        """规则触发"换默认模型". 走 §8.3 渐进部署 (影子→canary→stable)."""
```

**集成**: router 调用 `governor.consult_for_model_select()` 获建议 (Claude 主线 wire 时接).

**单测**: ≥6 个.

### C7. T28 idle_batch 7 step 完整实装 (~20-25h, 最大块)

**目标**: V1 §6.4 / V2.1 §6.4 7 个 step 全部真做 (现状: 6 个 placeholder).

**位置**: `kun/engineering/idle_batch.py` 扩展 (现有的不要破坏, 加 step)

**7 step 完整化**:
1. **任务回放** (V1 § 现状有占位): 用真实历史任务跑新旧版对比, 输出胜率
2. **多样本一致性测试**: 温度 / 改写 / 模型三重扰动
3. **方法论蒸馏**: 接 KnowledgePrecipitation NarrativeDistillStep
4. **知识冲突解决**: 资产池矛盾记忆仲裁 (走 multi_judge)
5. **AB 决策汇总**: experiments 状态机轮询 → 胜出推 shadow
6. **健康报告生成**: 接 C5 WeeklyReportGenerator
7. **路由规律涌现发现**: 聚类 + 关联规则挖掘

**单测**: 每 step 1 个 + 1 个集成 (跑完整 7 step 一遍).

### C8. T57 注意力预算 + 多 agent 摘要 (~4-6h)

**目标**: 解 "6 或 7 个 agent 同时跑用户大脑碎了" (HN scuff3d).

**位置**: `kun/engineering/attention_budget.py`

**机制**:
```python
class AttentionBudgetGuard:
    def __init__(self, max_active_sessions_default: int = 3):
        ...

    def can_start_session(self, user_id: str) -> bool:
        """检查是否能起新 session (不超 max_active)."""

    def queue_excess(self, user_id: str, task_meta: dict) -> str:
        """超过上限自动入队, 返 queue_id."""

    def summarize_agent_status(self, agents: list[AgentSnapshot]) -> str:
        """每个 agent 强制 ≤5 行 status digest, 不让用户被并发输出淹没."""
```

**单测**: ≥6 个.

### C9. T58 用户可配置中断频率 (~3-4h)

**目标**: 解 "Cursor 每完成一项弹窗" (V2EX 反向痛点).

**位置**: 加进 `kun/datamodel/soul_file.py` SoulFile (interruption_frequency 字段).

**3 档**:
```python
interruption_frequency: Literal["full_auto", "ask_every_n", "manual_review"] = "ask_every_n"
ask_every_n_steps: int = 5  # ask_every_n 时, 每 N 步问一次
```

**集成**: orchestrator 用 SoulFile 中此字段决定通知频率 (Claude 主线 wire 时接).

**单测**: ≥4 个 (3 档行为 + 默认值).

### C10. T25 早期错误左移 4 件 (~8-10h)

**目标**: V1 §8.5 5 件中已有 1 件 (cost_runaway), 补剩 4 件.

**位置**: `kun/engineering/early_error_detection.py`

**4 件**:
1. **死循环检测**: 同 step 名连续出现 N 次 / DAG 节点重复访问 N 次
2. **范围漂移检测**: 当前 step output 与原 intent_one_sentence 语义相似度 < 阈值
3. **一致性掉分检测**: 多步连续 multi_judge 一致性下滑趋势
4. **趋势监测**: cost / latency / quality 任一指标连续 N 步下降

**接 守望规则**: 触发后写 events.early_error.*, 守望规则消费 → 升档 / 暂停 / 升级到人.

**单测**: ≥8 个 (每件 ≥2 个).

---

## 推送策略

每完成一个任务:
```bash
cd /Users/petrarain/KUN-codex
git add <files>
git commit -m "feat(BATCH4-CN): C<N> 任务名"
git push origin feat/codex-batch4-c<N>
gh pr create --base main --title "BATCH4 C<N>: ..." --body "..."
```

10 个 PR 一起开, Claude 一个个审.

## 优先级 (你自己挑)

如果时间紧, 先做 C3 (verification) + C8 (attention budget) + C10 (early error) — 这三件**对致命差评对策最关键**. 然后 C1 / C5 / C6 / C7 / C2 / C4 / C9.

---

**问题反馈渠道**: 在 PR description 里 @Claude. 我会 review 时回.

**完成标志**: 10 个任务 = 10 个 PR, 每个独立可 review/revert.

---

*BATCH4 brief / 2026-04-26 / Claude → codex GPT-5.5 / 估 75-100h*
