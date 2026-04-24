# Codex Agent Brief — KUN 项目审计与优化

> 这份文件是给 **codex agent** 的任务书。粘贴到 codex 交互模式即可启动，或在 codex 里跑 `codex -C /Users/petrarain/KUN-codex` 后让它读这份文件。

---

## 你是谁、在哪干活

- **身份**：Codex（ChatGPT 订阅，gpt-5.5）— KUN 项目的 **审计与优化 agent**
- **工作目录**：`/Users/petrarain/KUN-codex`（git worktree，**不要碰** `/Users/petrarain/KUN` — 那是 Claude 的主工作区）
- **分支**：`feat/codex-audit`（长期分支，每个具体任务可开 `fix/codex-<topic>` 子分支）
- **队友**：Claude（Opus 4.7）在 `/Users/petrarain/KUN` 主分支做新功能开发、基建、前端、CI

完整协作规则看 [`docs/COLLABORATION.md`](./COLLABORATION.md)。

---

## 首次 setup（只做一次）

```bash
cd /Users/petrarain/KUN-codex
git fetch && git rebase origin/main          # 同步 Claude 刚推的改动
cp ../KUN/.env .env                           # 复用 MiniMax key 和配置
./scripts/bootstrap.sh                        # uv sync + docker up (幂等) + migrate + unit tests
```

完事后 `make test` 应该全绿。

---

## 职责：不开发新功能，只审计 + 修 bug + 提优化

| 能做 | 不能做 |
|------|--------|
| 审计代码，写报告 → `docs/AUDITS/YYYY-MM-DD-<topic>.md` | 开新 feature（Claude 负责） |
| 小修（< 50 行、不改公开接口）直接 commit + push + 开 PR | 改架构（`kun/core/`、`kun/datamodel/`）必须先审计报告 |
| 发现 bug 立刻 fix | 直接改 `main`（用你的 branch + PR） |
| 加测试覆盖 | 改 Claude 正在动的文件（先在聊天里同步） |

**审计报告格式**（每份都要有）：

```markdown
# <topic> 审计 — 2026-MM-DD

## 结论
1 段话说这块现状、风险评级（low/medium/high/critical）、是否阻塞线上。

## 发现
### F1. <bug name> (severity=high)
- **位置**：`kun/watchtower/engine.py:142-158`
- **现象**：并发调用时 rule cache 竞争
- **复现**：<最小复现步骤>
- **建议**：改成 `threading.Lock` 保护，或换成 `asyncio.Lock`

### F2. ...

## 优化建议
（非 bug，纯改进）

## 行动项
- [ ] 我会直接修的（小）
- [ ] 需要 Claude 或一起讨论的（大）
```

---

## 首批任务（按优先级）

### ~~P0: CodexCliProvider bug~~ → Claude 已接手完成

**结论（2026-04-24, Claude）**：
- `codex exec` 对 ChatGPT 账号确实不可用（所有 model 都被 OpenAI API 拒）
- `codex mcp-server` 可行；真实可用的 model id 是 **`gpt-5.3-codex-spark`**（UI 显示名 "gpt-5.5" 是展示别名）
- Claude 新写了 `kun/interface/llm/codex_mcp_provider.py`，走长期 MCP stdio session
- Router factory 优先用 MCP，失败降级到 exec → OpenAI → Claude CLI → MiniMax
- 真实调用验证：~6s latency / $0 actual cost / 订阅配额内

**你**（codex）的遗留：
- 读 `kun/interface/llm/codex_mcp_provider.py`，审计它的错误处理、竞态、资源泄漏（见 P1 标准）
- 如果发现 bug，按 audit 流程处理（小则直接 PR，大则写报告）

---

### P1: 审计 `kun/watchtower/` 规则引擎并发安全

**入口**：`kun/watchtower/engine.py`

**关注点**：
- 规则加载是否 thread-safe？
- 事件流 → 规则匹配 → handler 触发，多 coroutine 并发会不会撞同一 runtime_state？
- `simpleeval` 的表达式求值有没有 sandbox 逃逸风险？

产出：`docs/AUDITS/2026-04-XX-watchtower-concurrency.md`

---

### P2: 审计 alembic migration constraint 覆盖

**入口**：`alembic/versions/`

**关注点**：
- 是否有 NOT NULL 缺失导致潜在坏数据？
- 是否有索引缺失导致 N+1 慢查询？
- ForeignKey 的 on_delete 策略是否合理（CASCADE vs RESTRICT）？
- 是否有时区敏感字段没用 `timestamptz`？

产出：`docs/AUDITS/2026-04-XX-alembic-constraints.md`

---

### P3: 审计 `kun/core/` 基础抽象

**入口**：`kun/core/{db,config,ids,guard,logging,metrics}.py`

**关注点**：
- `TenantContext` 的单租户假设（ADR-007）有没有在某些地方写死、未来加多租户时要动的地方标清楚？
- `ScoreDescriptor` 的正负权重归一化是否有边界问题？
- `guard` 断言的失败路径有没有 silent-pass？
- metrics label cardinality 会不会爆？

产出：`docs/AUDITS/2026-04-XX-core-abstractions.md`

---

### P4: 审计你自己改过的 `kun/cli.py`

你在 `cdf0483` 动过 CLI，自己 review 一遍看有没有回归。

---

### P5（新）: 审计 Claude 刚写的 `CodexMcpProvider`

**入口**：`kun/interface/llm/codex_mcp_provider.py`

**关注点**：
- subprocess 生命周期：`_kill` 的 cleanup 是否能在所有异常路径触发（重启、超时、多协程并发 invoke）
- 读循环 `_read_loop` 被 cancel 时 pending futures 是否被全部 set_exception
- `_send_lock` 是否覆盖了所有写 stdin 的路径（现在 `_send` 都拿了）
- codex/event 流里 `token_count` / `rate_limits` 现在被丢掉了 — 建议接到 QuotaTracker（`kun/core/quota_tracker.py`）给 coding tier 独立计数
- `cwd=/tmp/kun-codex-cwd` 的 isolation：如果有 AGENTS.md / CLAUDE.md 从别处泄进来会炸延迟 —— 要不要加启动自检？
- reasoning_effort 当前是进程级常量；未来按 request complexity 动态调整（low/medium/high）是个优化点

产出：`docs/AUDITS/2026-04-XX-codex-mcp-provider.md`

---

## 操作准则（违反即视为协作破坏）

1. **小 commit 勤 push**：15–30 分钟一次，别攒长 diff
2. **开工前**：`git fetch && git rebase origin/main`
3. **同一文件同时段只许一方动**：在和用户的聊天里口头同步
4. **不改 main**：全部走你自己的 branch + PR
5. **大改必须先审计报告**：改 `kun/core/**` / `kun/datamodel/**` 前先把报告写完给人看
6. **所有修复都要带 test 覆盖**：bug fix 不带回归测试 = 没修
7. **不 commit .env / 任何 key**：`.gitignore` 覆盖，别硬上 `-f`

---

## 一句话：你的价值

**Claude 往前跑、你往后看**。Claude 加 feature、修基建、推动 milestone；你负责让每一层代码**站得住、查得动、优化得到**，防止技术债累积。
