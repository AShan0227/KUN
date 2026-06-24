"""Shared lexical tokenizer for context relevance/importance scoring.

Single source of truth for term extraction. Previously this exact function was
copy-pasted into both ``kun/context/importance.py`` and ``kun/context/packer.py``
(audit F098 — duplicated lexical scoring); both now import it from here.
"""

from __future__ import annotations

import re


def terms(text: str) -> set[str]:
    """Lowercased token set: word-ish runs of length >= 2."""
    return {part.lower() for part in re.findall(r"[\w.-]+", text) if len(part) >= 2}
