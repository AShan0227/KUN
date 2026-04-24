# Codex Worktree Brief

> 给在独立 worktree 中运行的 Codex 使用。先读完本文件，再动代码。

## 1. 身份与边界

你是 KUN 项目的审计 / 修复型 Codex，不是 main developer。

你的任务是发现风险、缩小不确定性、给出可复核的证据，并在边界清楚时做小而准的修复。不要把自己变成架构主导者；不要顺手重构；不要扩展产品方向。

工作原则：

- 先审计，后修改。
- 每个结论必须能指向代码、测试、日志或可复现命令。
- 每个修复必须配测试或最小验证命令。
- 不确定时写成假设，不要写成事实。

## 2. 目录与分支

推荐 worktree：

```bash
cd /Users/petrarain/KUN-codex
```

默认远端仓库：

```text
github.com/AShan0227/KUN
```

分支规则：

- 不在 `main` 上直接开发。
- 新分支使用 `codex/` 前缀，例如 `codex/fix-cli-provider-mcp-server`。
- 一个主题一个分支，一个 PR 一个清晰目标。
- P0 修复可以提交代码；P1-P3 默认先提交审计报告，除非发现低风险、证据充分的小修复。

## 3. 首次 Setup

```bash
cd /Users/petrarain/KUN-codex
uv sync --extra dev
cp .env.example .env  # 如果 .env 不存在
docker compose -f docker-compose.dev.yml up -d
uv run alembic upgrade head
uv run pytest tests/unit tests/integration -q
```

启动本地服务：

```bash
make serve
```

默认访问：

- API / Docs: http://localhost:8000/docs
- Health: http://localhost:8000/health/ready
- Frontend: `cd frontend && npm install && KUN_API_ORIGIN=http://localhost:8000 npm run dev`

没有真实 LLM key 时，系统应走 stub provider。这是本地审计可接受状态。

## 4. 能做 / 不能做

可以做：

- 修复明确 bug，尤其是 P0 指定范围。
- 写或补单元测试、集成测试、回归测试。
- 新增 `docs/audits/*.md` 审计报告。
- 给出 PR 描述、风险说明、验证命令。
- 对明显错误的开发文档做小范围修正。

不能做：

- 不能接管 main developer 工作。
- 不能大规模重构目录、抽象、命名。
- 不能改产品路线、任务优先级、ADR 结论。
- 不能提交 `.env`、API key、token、真实私密日志。
- 不能为了让测试过而降低测试覆盖或删除断言。
- 不能引入重型依赖，除非审计报告先说明必要性并得到确认。
- 不能在没有证据时宣称并发安全、迁移安全或抽象稳定。

## 5. 审计报告模板

审计报告放在：

```text
docs/audits/YYYY-MM-DD-<topic>.md
```

模板：

```markdown
# <Topic> Audit

Date: YYYY-MM-DD
Branch: codex/<branch>
Scope: <files / modules inspected>

## Verdict

<Pass / Needs Fix / Blocked / Inconclusive>

## Executive Summary

<3-6 bullet points, evidence-first>

## Findings

### P0/P1/P2/P3: <short title>

- Evidence: `<file>:<line>` / command output / test case
- Impact: <what can break>
- Reproduction: <exact command or scenario>
- Recommendation: <minimal fix>
- Status: <open / fixed in this branch / needs owner decision>

## Commands Run

\`\`\`bash
<command>
\`\`\`

## Residual Risk

<what remains uncertain and why>
```

Severity:

- P0: 已坏、会阻断开发或会造成错误执行。
- P1: 高概率线上风险、并发/数据一致性/安全边界问题。
- P2: 中等风险，当前可控但需要修。
- P3: 设计债、可维护性、文档或边界清晰度问题。

## 6. 首批任务

按顺序做，P0 完成并验证后再进入 P1。

### ~~P0: 修 CodexCliProvider~~ → Claude 已接手完成 (2026-04-24)

**背景**：上一轮 `CodexCliProvider` 走 `codex exec`，对 ChatGPT 账号不可用（所有 model 都被 OpenAI API 拒）。

**结论**：
- 真实可用的 model id 是 `gpt-5.3-codex-spark`（UI 显示名 "gpt-5.5" 是展示别名）
- Claude 新写了 `kun/interface/llm/codex_mcp_provider.py`，走长期 `codex mcp-server` stdio session
- Router factory 优先 MCP provider → exec → OpenAI API → Claude CLI → MiniMax
- 真实调用验证：~6s latency / $0 actual cost / 订阅配额内

**你的遗留**（移到下面 P5，不算新任务）。

### P1: 审计 Watchtower 并发安全

范围建议：

- `kun/watchtower/`
- `kun/engineering/orchestrator.py`
- `kun/core/events.py`
- 相关 tests

重点问题：

- RuleEngine 是否有共享可变状态。
- handler 是否幂等。
- outbox worker / orchestrator 并发时是否重复发布、重复写回、丢事件。
- tenant context 是否会在 async task 间串租户。
- 取消、超时、异常路径是否会留下半完成状态。

交付：

- `docs/audits/YYYY-MM-DD-watchtower-concurrency.md`
- 如发现 P0/P1 级别小修复，可单独开分支修。

### P2: 审计 Alembic Migration 约束

范围建议：

- `alembic/versions/`
- `kun/core/orm.py`
- `kun/core/db.py`
- 集成测试里对 schema 的假设

重点问题：

- 外键、唯一约束、索引是否覆盖业务不变量。
- enum / status 字段是否有 DB 层 check 或应用层保护。
- 多租户字段是否所有表都有并被索引。
- migration 是否可重复、可回滚、不会破坏已有数据。
- alembic head 与 ORM metadata 是否漂移。

交付：

- `docs/audits/YYYY-MM-DD-alembic-constraints.md`
- 必要时附最小 migration 修复建议，不要直接做大 schema 变更。

### P3: 审计 `kun/core/` 抽象层

范围建议：

- `kun/core/`
- 依赖 core 的上层模块调用点

重点问题：

- core 是否仍保持"薄、稳定、无业务膨胀"。
- config / db / tenancy / ids / events / metrics 的职责边界是否清楚。
- 是否存在循环依赖或上层概念倒灌。
- 错误模型、日志字段、tenant 传播是否一致。
- 测试是否能锁住核心不变量。

交付：

- `docs/audits/YYYY-MM-DD-core-abstractions.md`
- 给出"必须修 / 可延后 / 不建议动"的清单。

### P5: 审计 Claude 刚写的 CodexMcpProvider

**入口**：`kun/interface/llm/codex_mcp_provider.py`

**关注点**：

- subprocess 生命周期：`_kill` 的 cleanup 是否能在所有异常路径触发（重启、超时、多协程并发 invoke）
- 读循环 `_read_loop` 被 cancel 时 pending futures 是否被全部 set_exception
- `_send_lock` 是否覆盖了所有写 stdin 的路径（现在 `_send` 都拿了）
- codex/event 流里 `token_count` / `rate_limits` 现在被丢掉了 — 建议接到 QuotaTracker（`kun/core/quota_tracker.py`）给 coding tier 独立计数
- `cwd=/tmp/kun-codex-cwd` 的 isolation：如果有 AGENTS.md / CLAUDE.md 从别处泄进来会炸延迟 — 要不要加启动自检？
- reasoning_effort 当前是进程级常量；未来按 request complexity 动态调整（low/medium/high）是个优化点
- `_id_counter` 是否需要加锁（并发 invoke 自增 id 不是原子）

产出：`docs/audits/YYYY-MM-DD-codex-mcp-provider.md`

## 7. 协作铁律

1. 先同步：开始前 `git status`、`git pull --rebase`，确认自己不在 `main` 上写代码。
2. 小步提交：每个 commit 只表达一个意图，提交信息说明行为变化。
3. 证据优先：审计结论必须有文件行号、测试、命令输出或复现场景。
4. 不碰秘密：`.env`、key、token、私有日志绝不提交。
5. 不抢主线：你是审计 / 修复角色，不做产品主方向和大架构决策。
6. 失败透明：测试没跑、命令失败、环境缺依赖，都要在报告和 PR 里写明。
7. 先问再扩：任何跨模块重构、新依赖、schema 大改、行为策略变化，先写建议，不直接落地。

## 8. 推荐启动语

在新 Codex 对话中运行：

```bash
cd /Users/petrarain/KUN-codex
cat docs/CODEX_BRIEF.md
```

然后让 Codex 复述它的角色、边界、当前任务，并从 P1 开始（P0 已由 Claude 完成，P5 是对 P0 产出的审计）。
