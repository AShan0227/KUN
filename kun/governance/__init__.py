"""治理层 L5 (ADR-020) — RSI 闭环 + RCDH + 数据脊柱.

包含:
  - rsi_loop.py        — RSI 10 步闭环编排 (ADR-024)
  - rcdh.py            — 强制诊断层级检查器 (ADR-021)
  - evidence_ledger.py — 任务证据账本 (ADR-024)
  - promotion_queue.py — capability 晋级队列 (ADR-024)
  - diagnosis_scope.py — 范围圈定工具 (ADR-021, ≤5 模块强制约束)

这些模块在 L1.2 (拆 orchestrator) + L1.3 (alembic 0011) 阶段获得真实现.
"""
