"""CodeCapability — read, write, execute, debug, and review code."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from kun.skills.code_capability.debugger import CodeDebugger
from kun.skills.code_capability.executor import CodeExecutor
from kun.skills.code_capability.reader import CodeReader
from kun.skills.code_capability.reviewer import CodeReviewer
from kun.skills.code_capability.workflow import CodeChangeWorkflow
from kun.skills.code_capability.writer import CodeWriter


class CodeCapability:
    """Facade for the code capability layer."""

    _instance: ClassVar[CodeCapability | None] = None

    def __init__(self, *, workspace_root: str | Path = ".") -> None:
        self.reader = CodeReader(root=workspace_root)
        self.executor = CodeExecutor(workspace_root=workspace_root)
        self.writer = CodeWriter(workspace_root=workspace_root, executor=self.executor)
        self.debugger = CodeDebugger()
        self.reviewer = CodeReviewer(workspace_root=workspace_root)
        self.workflow = CodeChangeWorkflow(
            workspace_root=workspace_root,
            writer=self.writer,
            executor=self.executor,
            reviewer=self.reviewer,
            debugger=self.debugger,
        )

    @classmethod
    def get(cls) -> CodeCapability:
        """Return the process-local singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton state; mostly for tests."""
        cls._instance = None


__all__ = [
    "CodeCapability",
    "CodeChangeWorkflow",
    "CodeDebugger",
    "CodeExecutor",
    "CodeReader",
    "CodeReviewer",
    "CodeWriter",
]
