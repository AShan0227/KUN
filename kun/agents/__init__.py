"""7 个 agent 角色 (ADR-020 实例层 L3).

主线 (串行 5 阶段): Director → Executor → Tester → Gate
监督线 (并行旁路):  Supervisor + Strategist + External Supervisor

每个角色独立子模块，最低契约为 Protocol。具体实现按 L1.2 (拆 orchestrator) +
L1.7 (Director GoalAnchor) 等子任务逐步迁过来。
"""
