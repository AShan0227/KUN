# 多 Agent 协同开发约定

> 同一台本机，Claude 和 codex 两个 Agent 并行开发 KUN。本文件是两边都必须遵守的铁律。

---

## 1. 角色分工

| Agent | 职责 | 工作目录 | 分支命名 |
|-------|------|----------|----------|
| **Claude** | 新功能开发、基建、CI、前端、对话主线 | `/Users/petrarain/KUN` | `feat/claude-<topic>` |
| **codex** | 底层逻辑审计、漏洞排查、可优化点挖掘；写审计报告，不直接大改 | `/Users/petrarain/KUN-codex` | `feat/codex-audit`（长期） → 需要落地改动时开 `fix/codex-<topic>` |

### codex 审计产出

- 每次审计结论写到 `docs/AUDITS/YYYY-MM-DD-<topic>.md`（Markdown 报告）
- 报告包含：发现、影响面、建议方案、引用的代码位置（`file:line`）
- **不直接重构**现有代码。小幅修复（< 50 行、不改接口）可以直接提 PR；大改动先出报告 → Claude 或 codex 开独立 PR 落地

---

## 2. 仓库与 worktree 布局

```
/Users/petrarain/KUN          main / feat/claude-*   ← Claude 用
/Users/petrarain/KUN-codex    feat/codex-audit       ← codex 用
                              (两者共享同一个 .git 目录)
```

操作一次性命令（Claude 已在 main 里执行过）：

```bash
git worktree add ../KUN-codex -b feat/codex-audit main
```

删除 worktree：`git worktree remove ../KUN-codex`

---

## 3. 共享 vs 独立

| 资源 | 共享 / 独立 | 说明 |
|------|-------------|------|
| `.git/` | 共享 | 同一个历史、同一套远程 |
| Docker 容器（postgres / redis / qdrant / nats / minio / 观测栈） | **共享** | colima 里只有一套；两边实时看到对方的数据 |
| `.venv/` | 独立 | 每个 worktree 单独 `uv sync --extra dev` |
| `.env` | 独立 | 各自 cp .env.example 后填 key（别 commit） |
| `frontend/node_modules/` | 独立 | 每个 worktree 单独 `npm install` |
| 分支、未 commit 的改动 | 独立 | worktree 天然隔离 |

**codex 首次在 `/Users/petrarain/KUN-codex` 的 setup**：

```bash
cd /Users/petrarain/KUN-codex
cp ../KUN/.env .env          # 直接复用 Claude 这边的 key
./scripts/bootstrap.sh       # uv sync --extra dev + docker up (幂等) + migrate + test
```

---

## 4. 三条铁律

1. **小 commit、勤 push**：15–30 分钟一次，缩小冲突窗口
2. **开工前 rebase**：`git fetch && git rebase origin/main`
3. **同一文件同一时段只许一方动**：在聊天里口头同步（"我要改 `kun/core/db.py`，十分钟"）

---

## 5. Alembic migration 约定

- 任意时刻只有 **一方** 能加新 migration（`alembic revision`）
- 另一方开工前 `git pull --rebase origin main && make migrate` 再干活
- 两方同时撞 migration → 回滚后的一方把自己的 revision rebase 在对方后面（改 `down_revision`）

---

## 6. 数据库数据打架怎么办

- 共享 postgres 不是 bug，是 feature——两边能联调同一份数据
- 某方需要独立数据：开新库 `kun_codex`，或跑 testcontainers
- 清库重来：`make down-volumes` 前 **必须** 在聊天里同步对方

---

## 7. LLM key

- `.env` 里的 `MINIMAX_API_KEY` 双方共用（同一 key，不同 worktree 各自复制）
- OpenAI coding tier 走本机 `codex` CLI（见 `kun/interface/llm/codex_cli_provider.py`），不占 API key 额度
- Anthropic 调用走用户订阅（Claude Code 本身），不从 KUN 项目里调 Anthropic API

---

## 8. Review / 合入

- Claude 的功能 PR：push 后 `gh pr create`，codex 可以 review
- codex 的审计 PR：一般走 `fix/codex-<topic>`，push 后 `gh pr create`，Claude review
- 合入 main 前互相看过，CI 绿
- 合入后对方 `git pull --rebase origin main` 同步

---

## 9. 快速自检

```bash
# 在哪个 worktree / 分支
git worktree list
git branch --show-current

# 远端同步
git fetch && git status -sb

# docker 容器状态（共享）
docker compose -f docker-compose.dev.yml ps
```
