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
