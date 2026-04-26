"""CodeCapability — read and execute code as a reusable capability layer.

C28 only wires the first two pieces:
- CodeReader: find/read nearby code.
- CodeExecutor: run snippets, tests, and lint with a soft sandbox.

Writer/debugger/reviewer are intentionally left as placeholders for BATCH8.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from kun.skills.code_capability.executor import CodeExecutor
from kun.skills.code_capability.reader import CodeReader


class CodeCapability:
    """Facade for the code capability layer."""

    _instance: ClassVar[CodeCapability | None] = None

    def __init__(self, *, workspace_root: str | Path = ".") -> None:
        self.reader = CodeReader(root=workspace_root)
        self.executor = CodeExecutor(workspace_root=workspace_root)
        self.writer = None
        self.debugger = None
        self.reviewer = None

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


__all__ = ["CodeCapability", "CodeExecutor", "CodeReader"]
