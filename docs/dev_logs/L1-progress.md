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

