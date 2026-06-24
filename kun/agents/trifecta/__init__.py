"""V7 §12.4 RSI 三线并行 trifecta — past / present / future coordinator.

Three lines that run in parallel during a long task:

  - **过去线 (past)**: 启 (Qi) post-hoc retrospect — look at finished steps,
    diagnose root causes (bug_root_cause_cases lookup), feed findings back.

  - **现在线 (present)**: 外部监督者 (External Supervisor) watchdog tick —
    look at active steps, critique drift/loop/cost spikes in real time.

  - **未来线 (future)**: 启 (Qi) Explorer Pool — fan out N candidate solutions
    in parallel, keep best by metric (V7 §9.6 3 模式 each).

V7 §12.4 explicitly says these three should run **协调**, not strictly serial.
This module is the coordinator.

Cost multiplier (V7 §12.4.4 estimate):
  - all-off:          1x baseline
  - past only:        1.1x  (cheap — diagnostic lookup is local)
  - present only:     1.3x  (1 LLM watchdog call per N steps)
  - future only:      3-5x  (N candidates explored in parallel)
  - full trifecta:    5-6x

Production wiring: opt-in via env vars, default OFF.
"""

from kun.agents.trifecta.coordinator import (
    TrifectaCoordinator,
    TrifectaLineResult,
    TrifectaRunReport,
    TrifectaState,
)

__all__ = [
    "TrifectaCoordinator",
    "TrifectaLineResult",
    "TrifectaRunReport",
    "TrifectaState",
]
