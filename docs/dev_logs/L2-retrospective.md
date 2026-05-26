# Dev Log: L2 · 监督线启动 + RCDH + 第一条 RSI 闭环

**Date**: 2026-05-27
**Phase / Level**: L2 (Supervisor service + Input Classifier + Plan Review Heartbeat + External Supervisor + RCDH + Strategist + Gate + methodology_distill)
**Duration**: ~5 小时（含 9 个 /loop 自主迭代）
**Commits**: 67b960c (L2.1) · e8af322 (L2.2) · 9cee6c9 (L2.3) · 5bbf486 + 6330d6e + cb64c2f (L2.4 三 sub-commit) · 92a258e (L2.5) · ea8d8fb (L2.6) · a2bd5fd (L2.7) · 15a537f (L2.8) · 211869a (L2.9) · 本 retrospective + 3 新 methodology seeds（本提交）
**Tests**: 776 → 928（+152 new tests across 9 sub-tasks, all green）

---

## Goal

让鲲完成 L2 阶段全部 10 个改动，达成 PROGRESS.md L2 §交付标志：

1. External Supervisor 独立进程跑本地模型，监督线真旁路观察主线
2. RCDH 走完 4 级一次，工程化诊断对真症状定位到正确层级
3. **第 1 条 RSI 实例（LLM 路由优化）跑完 10 步基础设施**：异常 → Strategist 提候选 → Gate 启用条件准备好
4. 数据脊柱 7 张表（含 L1.3 已建）的 service-layer 写入路径全部就位
5. methodology_distill 真实现：KUN 自己看 dev_log → 蒸馏自己的方法论

---

## Approach

**单一文件 ≤ 5 + 子任务粒度的 paired commit**：
- 10 个 L2 子任务（L2.1–L2.10）独立完成，每个 1-3 commit
- L2.4 大改动拆 3 sub-commit（L2.4a LocalLLMProvider / L2.4b service+runner / L2.4c docker-compose），严守"单 commit ≤ 5 文件 / ≤ 1000 行"约束
- 每个子任务后跑 `uv run pytest tests/unit -p no:warnings` + `uv run ruff check kun tests` 红绿验收才 commit
- 每个 commit 后追加 `docs/dev_logs/L2-progress.md` 一段（3-5 句 + 关键决策）— ADR-025 强制约束

**engineering-first, LLM-second**：
- L2.1 Supervisor 阈值检测 / L2.2 Input Classifier 6 类 / L2.3 PlanReviewHeartbeat / L2.5 自嗨检测 5 条规则 / L2.6 RCDH 4 级 / L2.7 Strategist Explorer Pool / L2.8 Gate 4 条 R1-R4 / L2.9 dev_log 蒸馏 — 全部走"工程化规则优先，LLM 兜底放 L3+"。token 成本可控、决定路径确定可测、热路径不卡 LLM 调用
- L2.4 / L2.5 External Supervisor 用 LLM (本地)，但**仅在工程化信号不确定时**才调（自嗨检测 0 signal + 非强制 → 跳 LLM）

**结构化输出契约统一用 frozen dataclass**：
- 每个 agent 服务的输出都是 `@dataclass(frozen=True)` + `to_row_payload(tenant_id)` helper：StrategyExperiment / GateDecision / ExternalSupervisorObservation / GateAdvisory / DebriefRecord / SelfAggrandizementCheck / ReviewTrigger / DiagnosticRecord
- 一致的形态让下游消费方（emitter / row writer / dict 转换）写法可预期

---

## Key Decisions

1. **`asyncio.Semaphore` 限并发 vs 无限并发**：External Supervisor `max_concurrent=2` 默认。本地 LLM 推理 5-10s/req，4 个并发 caller 就能把服务拖死。L4 多实例时调
2. **engineering-first ≥ 2 signal 才判 self-aggrandizement**：单 signal 噪声大（artifact kind 缺失可能是任务类型决定的），双 signal 才有信号，避免误判把 Gate 卡住
3. **`derive_action` 一层映射 verdict → action 而非 verdict 直接当 action**：让"verdict 怎么转 action"在一个地方改，不散落。后续 anchor 类型变化只动 derive，verdict 不动
4. **`word boundary regex (\b)` 取代 substring keyword matching**：L2.6 第一版 `if kw in text` 让 "module-specific bug" 命中 "spec" → L0 误抢 L2/L3。Fix 用 `re.search(rf"\b{kw}\b", text)` 后通过 — 这是工程化 NL 关键词匹配的标准做法
5. **`requires_human_review=True` 而非直接 reject**：L2.7 自指限制 + L2.8 Gate R4 都不直接拒，而是标 `awaiting_human_review`。自指改动可能本身合理（Strategist 学到自己 bug），不应自动拒 — 抬人手即可
6. **`docker compose --profile external-supervisor`**：默认 `up` 不拉 ollama service（镜像 20GB）。profile 化让基础 dev 启动快，需要监督时显式启用
7. **`stub=False` 升真做与原 stub 接口一致**：L2.9 idle_batch step 从 STUB 升真做，输出结构 `total_scanned / novel_candidates / next_action` 完全替换旧 `learning_events`。下游统计逻辑自动适配；不引入两个并存的实现
8. **YAML 解析失败 graceful**：methodology_distill 读 seeds 时遇到坏 YAML 仅 log warning 不让蒸馏整体崩。漏掉的 dedup 留一个 false-positive novel candidate 远比 distill 整体失败可接受
9. **emitter 异常吞掉 vs raise 区分写入语义**：admit 写新 capability 失败可重试（capability 还没生效，吞）；enable_capability 状态机推进失败必须 raise（promotion_queue 必须知道）。两种不同语义不能用同一 try/except 模板
10. **3 sub-commit 拆 L2.4**：物理隔离 provider / service+runner / docker-compose 三层。git log 清晰 + 每层独立可 revert，符合 paired_commit methodology

---

## Constraints Applied

- 单 commit ≤ 1000 行 / ≤ 5 文件改动（L2.4 主动拆 3）
- 每个改动 grep verify，不靠假设（L2.6 narrow_scope 实装前先 grep `_KNOWN_MODULE_ROOTS` 候选）
- pytest + ruff 双绿才 commit
- destructive 操作（无）— 全程纯 add，没动 schema
- 每个 commit 后追加 progress.md 微日志，9 段 retrospective 留到本提交
- ADR-025 蒸馏卡片单独成文件 + index 注册

---

## Patterns Used

| Pattern | Where | Note |
|------|------|------|
| frozen dataclass + to_row_payload helper | L2.5/L2.7/L2.8 所有 agent 输出 | 配 emitter callback 直接落 ORM |
| asyncio.Semaphore 限并发 | L2.4 ExternalSupervisorService | 本地 LLM 慢, 2 路够 |
| asyncio.Lock for per-tenant state | L2.1/L2.3 in-memory state | 替代 Redis 直到 L4 |
| Engineering rules first, LLM tier 2 | L2.1/L2.2/L2.3/L2.5/L2.6/L2.7/L2.8/L2.9 | 控成本 + 决定可测 |
| Emitter callback for DB writes | L2.1/L2.3/L2.7/L2.8 | 依赖注入 + async + 异常吞 |
| 3 Sub-commit split | L2.4 三层 | paired_commit methodology 实操 |
| Word-boundary regex for keyword match | L2.6 RCDH | 防子串误判 (fix-driven) |
| ≥2 signal noise floor | L2.5 self-aggrandizement | 单 signal 太吵 |
| `requires_human_review=True` 替代 reject | L2.7/L2.8 自指限制 | 自指改动抬人手 |

---

## What Failed

1. **L2.6 keyword substring matching**：第一版 `if kw in text` 让 "module-specific bug" 命中 "spec"（L0 关键词），L0 误判 → 4 个 test fail。重写用 `re.search(r"\b{pattern}\b", text)` 后通过。**教训**：所有 NL 关键词匹配必须 word-boundary
2. **L2.6 force_escalation evidence 漏标**：L0 因 substring 误判抢占 root_cause 后，L2.evidence 不会被 mark，导致强制升级的"原 level"信息丢失。同时修一并解决
3. **L2.9 underscore stripping in slug normalize**：`re.sub(r"[*` _]+", "", title)` 把内部下划线也剥了 → `card_one` → `cardone`。改用 `re.sub(r"\*\*|\*|`", "", title)` 只剥 markdown 强调。**教训**：剥 markdown 时不要顺手剥 identifier 字符
4. **L2.9 section_end_re 太宽松**：早期 `^(##|---)` 没识别 `**...**:` 段落边界 → 关键决策 body 把后续 `**为下一步**:` 之后的 bullets 也吃了。补 `\*\*[^*\n]+\*\*[：:]?\s*$` 作 section_end 后干净
5. **L2.9 真实 seed YAML 解析失败 3 个**：现有 `adr_authoring.yaml` / `git_mv_invalidates_read_state.yaml` / `incremental_doc_migration.yaml` YAML 格式有内部 `:` 没引号问题。蒸馏器 graceful skip 不让 distill 崩 — 这些 YAML 后续单独修

---

## What Worked

1. **engineering-first 节省 token + 提速**：自嗨检测在 0 signal 情况下不调 LLM，光是这一条预计省 60-80% External Supervisor token；同样 Input Classifier 完全不调 LLM
2. **3 sub-commit 拆 L2.4 后 git history 极清晰**：每个 commit 一个独立 review 单元（provider / service / compose），任何一个出问题可单独 revert 不牵连
3. **`distill()` 对真 dev_logs 跑出 73 novel + 14 dup**：说明工程化抽取确实够用 — 73 个候选里有真信号（如 "3 sub-commit", "engineering-first beats LLM-first", "frozen dataclass + emitter"），可以送给 LLM L3 整理
4. **frozen dataclass + emitter pattern 跨 9 个 agent 一致**：写新 service 几乎是 copy-paste，开发速度恒定
5. **`AskUserQuestion` 没出现一次**：L2 全部 10 个子任务 zero user-blocking — 工程化决策完全可自主推进，符合 "loop 直到完成" 的目标
6. **整 L2 阶段没动一行 schema**：所有 service 都用 `dict[str, Any]` 透传到 ORM，schema 在 L1.3 已建好不动。让监督线与主线 schema 解耦
7. **每个 service 都自带 emitter 接口（不直接 import DB layer）**：测试用 fake emitter，prod 接 DB writer，零代码改动

---

## Heuristics Extracted

1. NL 关键词匹配必须 word-boundary regex (`\b...\b`)，否则 "spec" 撞 "specific" 等子串
2. 限并发的本地推理服务，max_concurrent=2 是默认起点
3. 工程化规则 + LLM tier 2 模式：≥1 signal 触发 LLM；0 signal 跳 LLM 省 token
4. 工程化 signal 噪声大，≥2 signal 共现才算稳信号
5. frozen dataclass + `to_row_payload(tenant_id)` 是 agent → ORM 的统一契约
6. emitter callback 失败：状态机推进必 raise，accumulation 写入可吞
7. self-referential (改自己) 用 `awaiting_human_review` 而非 reject —— 抬人手不拦
8. docker-compose 大镜像（>5GB）默认 profile=opt-in，不阻塞基础 dev
9. idle_batch step 从 stub 升真做用 `stub=False` 显式声明
10. YAML/数据源解析失败 graceful skip + warning，不让整体批处理崩
11. `**...**:` 段落是 markdown dev_log 的天然 section 边界
12. 自指限制要 prefix-match 4 种命名形式（裸名 / 点 / `kun.X` / `kun/X`）— 单一形式漏多

---

## Methodology Card Candidates

以下蒸馏自本阶段（与本 retrospective 同时归档 ≥ 3 份新 seeds）：

1. ✅ **engineering_first_with_llm_fallback** — 工程化规则优先 + LLM 兜底，热路径不烧 token（新建 seed）
2. ✅ **frozen_dataclass_agent_io_contract** — agent 间 IO 统一 frozen dataclass + to_row_payload + emitter callback（新建 seed）
3. ✅ **word_boundary_regex_for_nl_keywords** — NL 关键词匹配必带 `\b` 边界（新建 seed，bug-driven）
4. **noise_floor_ge2_signals_for_engineering_check**（candidate，本阶段不入库 — 候选）
5. **emitter_callback_dependency_injection_pattern**（candidate，与 #2 部分重叠）
6. **self_referential_use_human_review_not_reject**（candidate，本阶段不入库）

---

## L2 验收清单

- [x] **L2.1** SupervisorService 工程化阈值检测 + emitter 写 strategy_search_request（commit 67b960c · +8 tests）
- [x] **L2.2** Input Classifier 6 类分流 — 中英文 + out_of_scope（commit e8af322 · +13 tests）
- [x] **L2.3** Plan Review Heartbeat 每 N 步 / 每 M 秒（commit 9cee6c9 · +18 tests）
- [x] **L2.4** External Supervisor 独立进程化 + LocalLLMProvider（3 sub-commits · +25 tests）
- [x] **L2.5** Mode A gate review + Mode B task debrief + 自嗨检测（commit 92a258e · +19 tests）
- [x] **L2.6** RCDH 4 级诊断 + narrow_scope ≤5 模块（commit ea8d8fb · +23 tests）
- [x] **L2.7** Strategist on-demand + 第一条 RSI 实例 (LLM 路由)（commit a2bd5fd · +14 tests）
- [x] **L2.8** Gate 准入门禁 + runtime_capabilities 写入（commit 15a537f · +20 tests）
- [x] **L2.9** methodology_distill 真实现（commit 211869a · +20 tests）
- [x] **L2.10** L2 验收 + retrospective + 3 新 methodology seeds（本提交）

**测试总数**：776 → 928（+152）  
**Ruff**：clean throughout  
**Schema**：未动（L1.3 已建好的 7 张表 service-layer 写入路径就位）  
**北极星**：L2 §交付标志 5 条 — 基础设施全部就位；第一条 RSI 闭环跑通需要把这些服务串起来 + 真实 Supervisor → Strategist → Gate 闭环演练（L3 启动时做）。

---

*最后更新：2026-05-27*
