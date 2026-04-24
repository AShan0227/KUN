"""鲲 (KUN) — Agent OS / Agent 管家."""

from __future__ import annotations

# Auto-load .env from the project root (or nearest upward) at import time.
# This makes `uv run`, `python -m kun.cli`, tests, and imported contexts all
# see the same configuration. Safe no-op if .env is absent.
try:
    from dotenv import load_dotenv as _load_dotenv

    _load_dotenv(override=False)
except ImportError:
    pass


__version__ = "0.1.0"
