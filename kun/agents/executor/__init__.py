"""Executor agent — 主线主体 (ADR-020)."""

from kun.agents.executor.automation_runner import (
    ActionEventEmitter,
    AutomationRunner,
    action_result_to_artifact,
)
from kun.agents.executor.base import Executor

__all__ = [
    "ActionEventEmitter",
    "AutomationRunner",
    "Executor",
    "action_result_to_artifact",
]
