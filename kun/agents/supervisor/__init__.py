"""Supervisor agent — 监督线常驻 (ADR-020).

实现:
  - base.py    · Supervisor Protocol (角色契约)
  - service.py · SupervisorService 工程化阈值检测 (L2.1)
"""

from kun.agents.supervisor.base import Supervisor
from kun.agents.supervisor.service import SupervisorAnomalyState, SupervisorService

__all__ = ["Supervisor", "SupervisorAnomalyState", "SupervisorService"]
