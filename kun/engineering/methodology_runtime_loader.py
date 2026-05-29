"""Runtime methodology loader / selector — V7 §12 RSI 闭环最后缺的那段.

Background (V7.PHASE-X.G.RSI-CLOSED-LOOP):

Before this module, ``seeds/methodologies/*.yaml`` were written by
``methodology_distill`` + ProcessAudit pipeline (X.D-1), but **never
loaded back into runtime LLM context**. That meant the RSI closed loop
was open at the most critical join:

    real task → spot gap → distill candidate → walk 9 stages
        → promote to seeds/methodologies/ → ⛔ (nothing reads it)

V7 §1.1 "每跑一次都让自己略变更聪明" requires the promoted methodology
to actually influence the next task. This module is the read-side: load
yaml seeds, score them against the current task, inject the top-K into
the system prompt.

Design (intentionally minimal — kept LLM-free; LLM-driven selection
is a future X.H upgrade):

  1. ``MethodologyEntry`` frozen dataclass — one yaml file's relevant fields
  2. ``load_methodologies(root_dir)`` — scan + parse + filter by status
  3. ``MethodologyRuntimeSelector.select_for(task_context)`` — score by
     keyword overlap (engineering-first ADR-018), return top-K
  4. ``render_for_system_prompt(entries)`` — emit a system-segment string
     for orchestrator injection

Tests in `tests/unit/test_methodology_runtime_loader.py` cover empty
dir / status filter / keyword scoring / render shape.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kun.core.logging import get_logger

log = get_logger("kun.engineering.methodology_runtime_loader")


# Stop words filtered out of keyword extraction (mostly English).
_STOP_WORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "is",
        "are",
        "be",
        "was",
        "were",
        "with",
        "by",
        "as",
        "from",
        "this",
        "that",
        "it",
        "its",
        "you",
        "we",
        "i",
        "he",
        "she",
        "they",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "not",
        "if",
        "then",
        "else",
    }
)


@dataclass(frozen=True)
class MethodologyEntry:
    """One yaml seed parsed into runtime shape.

    Frozen — V7 §13.6 agent IO contract pattern. Score is computed
    per-task at selection time, kept here for trace/log only.
    """

    file_path: str
    topic: str
    title: str
    description: str
    triggers: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    anti_patterns: list[str] = field(default_factory=list)
    applicability: list[str] = field(default_factory=list)
    confidence: str = "medium"
    lifecycle_stage: str = "production"  # seeds/methodologies/ is implied prod
    score: float = 0.0  # set by selector

    @property
    def keywords(self) -> set[str]:
        """All searchable text tokens for keyword-overlap matching."""
        sources = [
            self.topic,
            self.title,
            self.description,
            *self.triggers,
            *self.actions,
            *self.applicability,
        ]
        return _tokenize(" ".join(sources))


def _tokenize(text: str) -> set[str]:
    """Extract lowercase word-level tokens; keep ASCII + CJK characters.

    Engineering-first: simple whitespace + punctuation split + lowercase.
    No NLP libs. Stop-words filtered out (English; CJK terms are short
    so word-level filtering is moot)."""
    cleaned = re.sub(r"[^\w一-鿿\s]", " ", text.lower())
    tokens = {tok for tok in cleaned.split() if tok and tok not in _STOP_WORDS}
    # Drop pure-numeric tokens (e.g. "2026") — they add noise to overlap.
    return {t for t in tokens if not t.isdigit()}


def load_methodologies(
    root_dir: str | Path,
    *,
    status_whitelist: Iterable[str] = ("production",),
) -> list[MethodologyEntry]:
    """Scan a directory for *.yaml seeds, return parsed entries.

    Args:
      root_dir: directory containing yaml files (typically
        ``seeds/methodologies/``).
      status_whitelist: only entries whose ``lifecycle_stage`` is in this
        set are returned. Default = ``{"production"}`` so non-prod seeds
        (replay/holdout/etc) don't leak into runtime context.

    Returns:
      List of MethodologyEntry; empty list if the dir doesn't exist or
      contains no yaml. Malformed yaml files are logged + skipped, never
      raised — keeps the orchestrator path robust if a yaml gets corrupted.
    """
    root = Path(root_dir)
    if not root.exists() or not root.is_dir():
        log.info(
            "methodology_loader.skipped_missing_dir",
            root_dir=str(root),
        )
        return []

    whitelist = set(status_whitelist)
    entries: list[MethodologyEntry] = []

    for path in sorted(root.glob("*.yaml")):
        try:
            raw = path.read_text(encoding="utf-8")
            data = yaml.safe_load(raw) or {}
        except Exception as e:
            log.warning(
                "methodology_loader.parse_failed",
                file=str(path),
                error=f"{type(e).__name__}: {e}",
            )
            continue
        if not isinstance(data, dict):
            log.warning(
                "methodology_loader.unexpected_yaml_shape",
                file=str(path),
                shape=type(data).__name__,
            )
            continue

        # lifecycle_stage defaults to 'production' for files living in
        # seeds/methodologies/ (the merge gate has run). yaml may override.
        stage = str(data.get("lifecycle_stage", "production"))
        if stage not in whitelist:
            continue

        entry = MethodologyEntry(
            file_path=str(path),
            topic=str(data.get("topic", "")),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            triggers=_listify(data.get("trigger")),
            actions=_listify(data.get("action")),
            anti_patterns=_listify(data.get("anti_pattern")),
            applicability=_listify(data.get("applicability")),
            confidence=str(data.get("confidence", "medium")),
            lifecycle_stage=stage,
        )
        entries.append(entry)

    log.info(
        "methodology_loader.loaded",
        n_entries=len(entries),
        root_dir=str(root),
        whitelist=sorted(whitelist),
    )
    return entries


def _listify(raw: Any) -> list[str]:
    """yaml may give a list of dicts ({condition: '...'}) or a list of
    strings or a single string. Normalize all to a flat list[str]."""
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw.strip()] if raw.strip() else []
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if isinstance(item, str):
                if item.strip():
                    out.append(item.strip())
            elif isinstance(item, dict):
                # Common shape: {condition: '...'} or {do: '...'}
                for v in item.values():
                    if isinstance(v, str) and v.strip():
                        out.append(v.strip())
        return out
    return []


# ============================================================
# Selector — score + pick top-K relevant entries for one task
# ============================================================


@dataclass(frozen=True)
class TaskContext:
    """Minimal context for runtime selection.

    Pulled from the task being run — task_type from intent triage,
    goal_keywords from anchor goal statement + success criteria.
    """

    task_type: str = ""
    goal_keywords: list[str] = field(default_factory=list)
    goal_statement: str = ""
    extra_keywords: list[str] = field(default_factory=list)

    @property
    def query_tokens(self) -> set[str]:
        sources = [
            self.task_type,
            self.goal_statement,
            *self.goal_keywords,
            *self.extra_keywords,
        ]
        return _tokenize(" ".join(sources))


class MethodologyRuntimeSelector:
    """Score-and-rank methodologies against a TaskContext.

    Scoring algorithm (intentionally simple, engineering-first):
      - score = |query_tokens ∩ entry.keywords| / |query_tokens|
      - tie-break by len(applicability) (more applicability = more general)
      - cap to top_k

    Score range [0.0, 1.0]; 0 means no overlap (entry dropped). Selection
    is deterministic given the same inputs, so unit tests can assert
    exact orderings.
    """

    def __init__(
        self,
        entries: Sequence[MethodologyEntry],
        *,
        min_score: float = 0.05,
    ) -> None:
        self._entries = list(entries)
        self._min_score = min_score

    @property
    def n_entries(self) -> int:
        return len(self._entries)

    def select_for(
        self,
        context: TaskContext,
        *,
        top_k: int = 3,
    ) -> list[MethodologyEntry]:
        """Return up to `top_k` entries ranked by relevance to context."""
        query = context.query_tokens
        if not query or not self._entries:
            return []

        scored: list[MethodologyEntry] = []
        for entry in self._entries:
            kw = entry.keywords
            if not kw:
                continue
            overlap = len(query & kw)
            if overlap == 0:
                continue
            score = overlap / max(len(query), 1)
            if score < self._min_score:
                continue
            scored.append(
                MethodologyEntry(
                    file_path=entry.file_path,
                    topic=entry.topic,
                    title=entry.title,
                    description=entry.description,
                    triggers=entry.triggers,
                    actions=entry.actions,
                    anti_patterns=entry.anti_patterns,
                    applicability=entry.applicability,
                    confidence=entry.confidence,
                    lifecycle_stage=entry.lifecycle_stage,
                    score=score,
                )
            )

        scored.sort(
            key=lambda e: (e.score, len(e.applicability), e.title),
            reverse=True,
        )
        return scored[:top_k]


# ============================================================
# System-prompt rendering (for orchestrator injection)
# ============================================================


def render_for_system_prompt(
    entries: Sequence[MethodologyEntry],
    *,
    header: str = "已蒸馏方法论 (V7 §12 RSI 进化产物, 已 promote 进 production):",
) -> str:
    """Render selected entries as one system-prompt segment string.

    Output is short + actionable — each entry contributes one block with
    title + 1-3 actions. The string is meant to be appended to the existing
    system prompt (after goal anchor pin), not to replace it.
    """
    if not entries:
        return ""

    blocks: list[str] = [header, ""]
    for i, e in enumerate(entries, start=1):
        blocks.append(f"  {i}. **{e.title}** (相关度 {e.score:.2f})")
        if e.description:
            # 1 line of description, trim long ones
            desc = e.description.strip().splitlines()[0][:240]
            blocks.append(f"     - 要点: {desc}")
        actions_to_show = e.actions[:3]
        for a in actions_to_show:
            blocks.append(f"     - 行动: {a[:240]}")
    return "\n".join(blocks)


__all__ = [
    "MethodologyEntry",
    "MethodologyRuntimeSelector",
    "TaskContext",
    "load_methodologies",
    "render_for_system_prompt",
]
