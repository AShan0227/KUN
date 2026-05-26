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


