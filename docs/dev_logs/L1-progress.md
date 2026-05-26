# L1 · 进度微日志（追加式）

> 完整回顾在 `L1-retrospective.md`（L1 全 10 项完成时写）。
> 本文件每完成一个子任务追加 3-5 句。

---

## L1.1 · 目录重组 kun/agents/<role>/ × 7 + kun/governance/ × 5

**完成**：2026-05-26 / commit pending

**做了什么**：
- 建 `kun/agents/<role>/` × 7（director / executor / tester / gate / supervisor / strategist / external_supervisor），每个目录有 `__init__.py` re-export + `base.py` 写 Protocol + 角色契约 docstring + 实施路径指引
- 建 `kun/governance/` 5 个模块骨架：`rcdh.py`（DiagnosticRecord + run_diagnostic）/ `diagnosis_scope.py`（narrow_scope + MAX_SCOPE_MODULES=5 强制约束）/ `evidence_ledger.py`（EvidenceEntry + append/get_trace）/ `promotion_queue.py`（RuntimeCapability + 晋级状态机 + 超时规则）/ `rsi_loop.py`（10 步编排 + is_self_referential 自指检查）

**关键决策**：
- 用 `typing.Protocol` 而非 `abc.ABC` — agents 多实现可能并存（旧 control_plane 暂留 + 新 kun/agents 迁过去），结构化类型比继承更灵活
- governance 模块**不只是 docstring stub**：每个有 pydantic schema（DiagnosticRecord / EvidenceEntry / RuntimeCapability）+ async 函数签名 + 内部规则（如 `MAX_SCOPE_MODULES = 5` / `SELF_REFERENTIAL_TARGETS` set）→ 让 L1.3 alembic 阶段直接照 schema 建表
- 每个 base.py / governance 模块 docstring 明确指 ADR-020/021/022/023/024 + 实施路径标签（L1.2 / L1.3 / L2 等），让后续子任务有清晰目标

**遗留**：
- governance 模块所有 async 函数体都是 `TODO L2/L1.3: 实装`，骨架可 import 但不可调用
- 旧 `kun/control_plane/` `kun/brain/` `kun/engineering/orchestrator.py` 暂未删（L1.2 拆 orchestrator 时迁移功能）
- 验证：760/760 unit tests pass + ruff clean + 全部 import OK + self-referential 检查工作正确

**为下一步**：L1.2 拆 orchestrator.py 时，brain/intent + brain/planner 主要迁到 agents/director；orchestrator 主体迁 agents/executor；validation.py 迁 agents/tester；control_plane/runtime（Gate 部分）+ capability_writeback 迁 agents/gate；control_plane/nuo + runtime_observation + idle_batch 迁 agents/supervisor；capability_evolution 迁 agents/strategist。

---

## L1.2 · 拆 orchestrator.py 到 Director / Executor / Tester / Gate

**完成**：2026-05-26 / commits 5129abe, 97f4062, 86963cc, 6cd7f7b, 80c5c34

**做了什么**：拆成 5 个独立 commit，每个迁一个模块，全程 760/760 测试常绿：
- **Commit A** (`5129abe`)：`kun/brain/intent.py` → `kun/agents/director/intent.py` (4 file edits)
- **Commit B** (`97f4062`)：`kun/brain/planner.py` → `kun/agents/director/planner.py` (4 file edits)
- **Commit C** (`86963cc`)：`kun/brain/router.py` → `kun/agents/director/role_router.py` (改名避免与 LLMRouter 撞, 4 file edits)
- **Commit D** (`6cd7f7b`)：`kun/engineering/validation.py` + `multi_judge.py` → `kun/agents/tester/` (6 file edits)
- **Commit E** (`80c5c34`)：`kun/engineering/capability_writeback.py` → `kun/agents/gate/` (8 file edits)

**关键决策**：
- **5 commit 而非 1 mega**：每个迁移逻辑独立 + 不混淆 + git log 易导航 + rollback 颗粒度合理。每 commit ≤ 1000 行、5-8 文件改动。
- **不留旧位置 shim**：直接 update 所有 importer。原 `kun/brain/__init__.py` 改成 re-export 兼容层让 `from kun.brain import IntentInterpreter` 仍可工作，但 `kun.brain.intent` 直接路径已失效。这比"留 shim 慢慢删"更干净。
- **router.py 改名 role_router.py**：原文件名 `router` 与 `kun/interface/llm/router.py` 在 grep / 导航时混淆。新名 `role_router` 强调它的功能（选 role template）。类名 `TaskRouter` 不变。
- **同时迁 multi_judge.py 跟 validation.py**：multi_judge 是 ValidationPipeline 的底层依赖，cross-import 关系，分开迁会有中间状态破裂。
- **logger / tracer name 同步更新**：mv 后 `get_logger("kun.brain.intent")` 仍然能跑（只是字符串），但语义上误导 grep / debug。一并改成新路径。

**踩坑 + 启发式**：
- **Edit 前必 Read**：git mv 之后文件物理位置变了，Edit 工具的"已读"标记没跟上。每次 mv 之后第一次 Edit 必须先 Read 新路径。规则：**git mv 等于触发 Read 失效**。
- **monkeypatch 字符串易漏**：`monkeypatch.setattr("kun.engineering.foo.bar", ...)` 的字符串里包含完整模块路径，grep `from kun.engineering.foo` 不会命中字符串字面量。改 import 时**额外 grep 字符串字面量**（不只 import 语句）。
- **logger / tracer name 同样易漏**：和 monkeypatch 同理，是模块路径字符串。`sed -i` bulk 替换最快。

**遗留**：
- `kun/brain/` 下只剩 `__init__.py` re-export shim。待全仓 grep `from kun.brain` 命中 0 次后整目录删（放 L1.10 验收时一并）。
- `kun/engineering/` 还有 `orchestrator.py` (1500+ 行主线协调器, 保留) + 9 个其他模块（按需逐步迁）。
- **验证**：5 commit 全部跑 760/760 unit tests pass，无 regression。

---

## L1.3 · alembic 0011 数据脊柱 7 张表

**完成**：2026-05-27 / commit pending

**做了什么**：
- 写 `alembic/versions/0011_rsi_data_spine.py` 建 7 张表：`runtime_capabilities` / `runtime_experiments` / `strategy_search_requests` / `diagnostic_records` / `goal_anchors` / `plan_reviews` / `evidence_ledger`
- 在 `kun/core/orm.py` 加 7 个 ORM 类（`RuntimeCapabilityRow` 等），sqlalchemy 2.0 declarative + Mapped 风格
- 所有表带 ADR-007 RLS：`ENABLE/FORCE ROW LEVEL SECURITY` + `tenant_isolation` policy
- DB 层 check constraint 强制业务约束：`scope_modules ≤ 5`（RCDH 圈定）/ `goal_statement ≤ 200 字符`（GoalAnchor 强制简短）/ `promotion_state` 8 态枚举 / `priority` low/medium/high / `target_level` 0-3（RCDH 层）/ `sampling_rate` 0-1（canary）
- 关键索引（部分索引 = WHERE 条件优化）：
  - `runtime_capabilities`: `WHERE enabled=TRUE` 的目标模块查找
  - `runtime_experiments`: `WHERE status IN ('pending','running')` 的活跃实验
  - `strategy_search_requests`: `WHERE status='open'` 的 dedup_key 查找（工程层做 1h 去重）
  - `evidence_ledger`: `WHERE diagnostic_id IS NOT NULL` 的诊断回溯

**关键决策**：
- **schema 约束放 DB 层而非纯 application 层**：`scope_modules ≤ 5` 用 `jsonb_array_length(scope_modules) <= 5` 强制 — pydantic `max_length=5` + DB constraint 双保险，**不允许任何 agent 绕过**
- **RLS 一并加，不留"以后再加"**：ADR-007 红线，所有 tenant_id 主键的表都 enable RLS。0007 阶段做了 grants + 0006 enable，新表不需要再 grant（ALTER DEFAULT PRIVILEGES 已生效）
- **`metadata` 列改用 ORM attribute `capability_metadata`**：SQLAlchemy 的 `Base` 已经有 `metadata` 属性（DB meta），列名 `metadata` 会冲突。用 `Mapped[dict] = mapped_column("metadata", ...)` 把 Python 属性名与 DB 列名错开
- **part index 而非 full index**：`WHERE enabled=TRUE` 类的部分索引比全索引小 10x+，对 hot path 查询足够

**验证**：
- `alembic upgrade head` 成功，`\dt` 确认 7 张表存在
- **Round-trip 测试**：`alembic downgrade 0010` → 7 表全删 → `alembic upgrade head` → 7 表重建。fully reversible
- 760/760 unit tests pass
- ruff check + format 全过

**为下一步**：6 张表 + evidence_ledger 已就位，但都是空表。L1.4 (ConcurrencySafety 真合并) / L1.5 (删 KnowledgePrecipitation) 之后，L1.7 阶段 Director 开始写 `goal_anchors`，L2 阶段 Supervisor / Strategist / Gate 开始写其余 6 张表。数据脊柱真正活起来在 L2。

---

## L1.4 + L1.5 · ConcurrencySafety 重命名 + KnowledgePrecipitation 注释清理

**完成**：2026-05-27 / commit pending

### L1.5 · KnowledgePrecipitation 抽象删除

**纯注释清理**（抽象已 0 调用方）：
- `kun/agents/gate/capability_writeback.py` docstring 改：删 "ADR-018 §16.4 KnowledgePrecipitation 的一个 step" 引用，改成 "ADR-024 RSI 闭环 step 4" + 注明 KP 抽象 2026-05-26 删
- `kun/engineering/orchestrator.py:1006` 注释改：删 "(ADR-018 §16.4 KnowledgePrecipitation)" 引用，改成 "(ADR-024 RSI step 4 — 闭环'执行→能力卡→路由')"

只剩 1 处 "deletion marker" 在 capability_writeback.py docstring（解释什么时候删的、为什么删）。**这是保留历史轨迹的有意行为**，不是遗留 ref。

### L1.4 · `control_plane/concurrency.py` 重命名为 `work_item_governance.py`

**核心认知**：审计的 M3 "两份 concurrency 重复" 是**误判**。两份文件实际是**同概念域、不同抽象层**：

| 文件 | 职责 | 主要导出 |
|---|---|---|
| `kun/engineering/concurrency.py` (575行) | 任务级并发原语 | IdempotencyKey / ResourceGuard / Lease / scan_pre_conflicts |
| `kun/control_plane/concurrency.py` (926行) | work-item 级治理 | WorkerPoolConfig / ResourceLockLease / SandboxIsolationSpec / MergeGovernanceReport / 4 种 lock store |

两者**不重复，但命名冲突**（grep / 导航易混）。**正确做法 = 重命名澄清职责，不强行合并**。

操作：
- `git mv kun/control_plane/concurrency.py kun/control_plane/work_item_governance.py`
- 7 个 importer (control_plane/feature_activation_audit / activation / cockpit / __init__ / kun_runtime_runner / daemon + tests/unit/test_control_plane_kun_runtime_runner_v6.py) 用 `sed -i` 一并改 `kun.control_plane.concurrency` → `kun.control_plane.work_item_governance`
- 新文件顶部加 docstring 解释重命名原因 + 两者区别

**关键决策**：
- **审计可能误判，要交叉验证**：M3 把命名冲突当 "重复" 写，没看实际 export。我先 `grep ^class ^def` 对比再下结论。**启发式：审计报告的"合并 / 删除"建议必须用源码级 grep 验证**
- **重命名 > 合并**：ADR-018 §16.5 ConcurrencySafety 因此从"待合并"降级为"半合并"（同概念域，分层共存）。这与 ADR-018 v3 修订一致（≥ 3 调用方才真合并）
- **批量重命名用 sed**：7 个文件 + 单一 grep pattern 用 `sed -i.bak ... && rm .bak`，比 7 次 Edit 快 + 不出错

**验证**：760/760 unit tests pass, ruff clean。

**为下一步**：L1.4 + L1.5 都是清理性收尾。下一步 L1.6 接 `capability_router` 进 `LLMRouter.decide()` — 这是 L1 中"建了但没接"的最大案例，接进去**立刻产生第一条真闭环**（任务结果 → capability card → 下次路由调整）。

---

## L1.6 · 接 capability_router 进 LLMRouter — 第一条真闭环

**完成**：2026-05-27 / commit pending

**做了什么**：
- 在 `LLMRouter.invoke()` 加新方法 `_apply_capability_adjustment(decision, request, purpose)`：拿到 `decide()` 输出后，**异步**查 capability_card 历史 → 强信号时微调 tier
- 在 `decide()` 输出之后、provider lookup 之前调用，使 capability data 真正影响路由决策
- 加 helper `_upgrade_tier(tier)`：cheap → strong → top → top
- 加 2 个单测验证闭环：`test_capability_adjustment_upgrades_low_reliability_tier`（强信号触发升级）+ `test_capability_adjustment_keeps_tier_when_signal_weak`（cold start 不动）

**调整算法（保守，只动强信号）**：
- `sample_size >= 10 + score < 0.4` → 升级一档（cheap → strong → top）
- `sample_size >= 20 + score > 0.85` → 标 "high confidence"（不动 tier，只记日志）
- 否则不调整

**保护边界**：
- `KUN_CAPABILITY_ROUTER_ENABLED=0` env var 一键关闭
- 只对 `top/strong/cheap` 三档调整，`coding/fallback` 是显式选项不动
- `risk_level=critical` 已被 `decide()` pin 到 top，不被反向调整
- capability_router 查询失败 / cold start (sample < 10) → 不调整，保留 `decide()` 默认

**这是 L1 中"建了没接"的最大案例修复**：
- `capability_router.py` 写了 1 年+，只在 `_select_by_capability` 单纯 tier 内候选选择被调用
- 没有真正被 `decide()` 用来调整 tier
- 现在 capability data 影响 tier 决策，**形成"任务执行 → capability_writeback 写卡 → 下次 decide() 读卡 → 调整 tier"完整闭环**

**关键决策**：
- **不改 `decide()` 改 `invoke()`**：`decide()` 是同步纯函数（design contract），引入 async DB 查询会破坏契约。`invoke()` 已是 async，加调整层无侵入
- **保守阈值**：sample_size >= 10 + score < 0.4 才动，避免少量样本误升级。这跟 capability_router 内部的 cold-start damping (sample/30 weight) 一致
- **`_apply_capability_adjustment` 是私有方法**：将来扩展 anti-drift 调整 / RSI 实验 override 等，都接入这个调整层

**踩坑**：
- 第一次写测试时用 `("default", model, task_type)` 作 cache key — 实际 `_tenant_id_for_capability_routing()` 在 dev 模式返回 `"u-sylvan"` (fallback to `default_tenant_id()` setting)。fix: cache key 用 `"u-sylvan"`
- 启发式：**测试 capability_router 内部 cache 时，cache key 第一位必须是 `_tenant_id_for_capability_routing()` 实际返回值**（dev 用 "u-sylvan"，env 设了 KUN_TENANT_ID 用 env，否则 "default"）

**验证**：762/762 unit tests pass（新加 2 个），ruff clean。

**意义**：L1 阶段第一条**真闭环**激活。任务跑完写能力卡（已工作）→ 下次同类任务到 router 时，强证据下自动升级 tier。鲲已经开始"学习"——虽然现在只是 router 这一处，是 RSI 闭环的微缩版。




