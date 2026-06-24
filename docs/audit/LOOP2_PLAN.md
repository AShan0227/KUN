# 鲲 审查收尾 · Loop-2 计划（Option A：安全子集真落地 + 其余出草案）

> 接续 Loop-1（172 条发现 → pending+fix=0，见 [FIX_TRACKER.md](FIX_TRACKER.md)/[FIX_LOG.md](FIX_LOG.md)）。
> Loop-1 把所有"无法离线安全改绿"的项归入 12 份 proposals 标 needs-design（95 项）。
> **Loop-2 目标**：把这 95 项里**能离线验证全绿的子集真落地**；其余（需人裁决 / 需真实基建验证）**产出 review-ready 草案**，标 `needs-review`，**不自动 land**。
> 分支 `鲲V1.1-dev`，每次提交单测全绿后 push。**用户已选 Option A（2026-06-24）**。

## 铁律（不可破，沿用 Loop-1）
绝不删/弱化测试；绝不推红的提交；绝不 force-push；危险改动后单测必须仍全绿；pytest 跑不起来就停。
涉及 LLM/Anthropic/定价/模型 必须先按 claude-api 技能取权威信息、不得凭记忆。
**Option A 边界**：只有"能在本机离线验证（unit 全绿 / mypy 增量 / coverage 实测）"的项才允许真 land。
架构重构 / 持久层 / RSI 主链接线 / 真沙箱 / 多副本 / Docker / branch-protection 等**离线无法证绿的，一律只出草案、不 land**。

## 每轮迭代 recipe
一次一项（同根因可合并）。读代码确认（文件:符号:行）→ 实现(TIER1) 或 写草案(TIER2) →
TIER1：`uv run pytest tests/unit -q > /tmp/ut.log 2>&1; echo $?`==0（本机偏慢，用 run_in_background + 轮询 git log 落地，按退出码判定）→ commit+push →
更新 findings.json（TIER1→done；TIER2→needs-review）/ FIX_LOG.md / 刷新 FIX_TRACKER.md；非 0 回滚标 blocked。

---

## TIER 1 — 真落地（离线可验证），按 安全×性价比 排序

| # | 项 | 做什么 | 验证方式 |
|---|---|---|---|
| 1 | **F035a(诚实子集)** | `builtin/__init__.py:32` shell-exec 描述"受 allowlist 约束"→改为实情(env `KUN_SHELL_EXEC_ALLOW/DENY` 策略、默认 denylist)；`loader.py:56-58` 删 3 个**零消费者**死字段(allowed_commands/denied_patterns/denied_domains，model `extra="allow"` 故 SKILL.md 仍可加载)+注释指向 command_policy.py | unit 全绿 + 新增回归测试：断言 3 字段不在 `SkillManifest.model_fields` + 描述不再谎称 per-skill allowlist。**wire-vs-strip 那份留 security-posture 的 needs-design** |
| 2 | **F119** | 从 `kun/core/config.py` 字段 + `grep -roE 'KUN_[A-Z_]+'`(含 V7 开关族 KUN_V7_*) 重新生成 `.env.example`，逐项带注释 + 安全默认；auth 段显著标"生产必开" | 新增测试：每个 `Settings` 字段在 .env.example 有对应 KUN_ 键；`Settings()` 仍正常加载 |
| 3 | **F098** | 让 `ContextPacker._score_asset`(packer.py:115) 复用 `ImportanceScorer`，删 packer 重复 `_terms`/ad-hoc 打分；或判定过早则删 ImportanceScorer + 其测试去死码 | unit 全绿 + 打分行为测试（复用路径或删除后无悬挂引用） |
| 4 | **F111** | 给 `tests/integration/` ~20 个缺 marker 文件加模块级 `pytestmark = pytest.mark.integration` + 守卫测试断言每个 integration 文件都带 marker | `pytest -m "not integration" --co -q` 只选 unit；unit 全绿 |
| 5 | **F053** | 把 0011/0012 的 ~20 条 DB CHECK 逐表镜像进对应 ORM Row `__table_args__`(条件与迁移完全一致) + 漂移守卫测试（ORM CHECK 集合==迁移 CHECK 集合，仿 F054） | unit 全绿（逐表加、红则回滚该表） |
| 6 | **F133 / G10** | 本机先 `uv run pytest tests/unit --cov=kun --cov-report=term`，确认 ≥80%，再给 `ci.yml` unit job 加 `--cov=kun --cov-fail-under=80` | 本机 coverage 实测通过（这是 land ci.yml 的前提）|
| 7 | **F134(部分)** | 新增 `.github/CODEOWNERS`；ci.yml 触发分支补上活跃开发分支（branch-protection 本体是 GitHub 设置项，仅文档化为 TIER2） | 文件存在 + yaml 合法 |
| 8 | **G09 批1** | typecheck-debt 批1：删 12 个 `unused-ignore` + 补 17 个 `type-arg` 泛型参数（纯机械、不改运行时） | `uv run mypy kun` 错误数下降且无新错；unit 全绿 |
| 9 | **F147** | `supervisor/service.py:160` 把 `async with self._lock` 缩到只护内存状态读改，`await` DB/通知移出锁；或按 tenant per-key 锁 | unit 全绿 + 并发行为测试（不持锁跨 await） |

### TIER 1b — 前端（pytest 不覆盖，低风险加性改动；能起 dev server 则用 preview 验证，否则 land 并注明需目检）
| 10 | **F153** | `next.config.mjs` rewrites 加 `{ source:"/cockpit/:path*", destination:`${apiOrigin}/cockpit/:path*` }`；dev CORS 放行 3001 | 起 preview 验 /cockpit 不再 404；否则 land + 注明需目检 |
| 11 | **F067** | 在 globals.css / Tailwind 层定义缺失的 `kun-*` 组件类（或换实存类） | preview 目检；否则 land + 注明需目检 |

---

## TIER 2 — 只出 review-ready 草案（needs-review，不 land）

写到 `docs/audit/proposals/drafts/`，标 `needs-review`；findings 对应项 status→`needs-review`，notes 指向草案。

| 草案 | 内容 | 为何不 land |
|---|---|---|
| **ADR-027 控制面去留** (F038/F036/F142) | 起草 ADR：control_plane 拆散到五层 vs 正式承认为一等公民；含迁移影响面 | 产品/架构裁决，只有人能定 |
| **ADR-028 V6/V7 追溯** (F038/F128/F129/F130) | 给 Mission Director/trifecta/Control Plane 复活/新增 agent 角色补追溯 ADR；README 文档清单对账 | 需人确认历史决策 |
| **持久层重设计草案** (F019/F010/F011/F014/F015/F093) | CAS/版本号 schema 草案 + 接口 sketch；artifact/ledger append-only + 上限 | 需真 PG + 多进程才能证绿 |
| **RSI 主链接线 PR 串草案** (rsi-mainline 1→9) | 把脊柱表补列(F091)、evidence_ledger 表(F065)、gate→capabilities enable(F041/F042/F089) 等拆成有序迁移+PR 草案 | 需真 DB/集成验证；且依赖 ADR-027 |
| **外部监督 enforce 草案** (F030/F031/F032/F115/F151) | fail-close 守卫 + critique hook + 动作白名单 + 抗注入设计 | 需独立进程/NATS 基建 |
| **真沙箱草案** (F109/F035a 隔离半) | 容器/seccomp/nsjail/只读 rootfs/无网 ns/cgroup 方案 | 基础设施级，离线无法证 |
| **并发多副本草案** (concurrency 全) | per-key 锁/CAS/租户隔离落 DB 的接线图 | 需真 PG/多副本 |
| **演示诚实化 + L6 草案** (demo-honesty/F099/F114) | PASS 判据基于真证据的改造 + L6 评测入生产度量 | 依赖 RSI 主链通 |
| **typecheck 批2-5 草案** (G09 余下) | arg-type/attr-defined 等语义类清理顺序（批1 已 land 后） | 语义类需逐个当 bug 排查 |

---

## 终止条件
TIER1（含 1b）全部 land 或确认不可安全 land、TIER2 草案全部产出后：**结束 loop（不再 ScheduleWakeup）**，
输出交付总结：本轮真 land 清单 / 产出的草案清单 / findings 终态（done/needs-review/needs-design/deferred）/ 建议人审顺序。
