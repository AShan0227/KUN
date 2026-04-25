"""Model playbook loader — read the hand-written model guide.

Pair with the runtime ``capability_cards`` (auto-learned per-model stats)
to drive informed routing. The playbook is the *prior*; capability cards
are the *evidence*. Both feed :class:`LLMRouter` decisions and the NUO
"模型画像" panel.

Reload on a schedule (``idle_batch``) or on demand. Falls back to an
empty playbook if the YAML is missing — KUN still runs with hardcoded
tier defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from kun.core.logging import get_logger

log = get_logger("kun.llm.playbook")

Audience = Literal["novice", "developer", "expert"]


@dataclass(frozen=True)
class ModelEntry:
    """One model's hand-written guide stanza."""

    model_id: str
    family: str
    display_name: str
    tier_default: str
    context_tokens: int
    strengths: tuple[str, ...]
    weaknesses: tuple[str, ...]
    notes: str
    pricing_usd_per_mtok: dict[str, float]
    subscription_quota: bool
    audience_default: Audience = "developer"

    def matches(self, *, strength: str | None = None) -> bool:
        """Quick predicate — does this model claim a given strength?"""
        if strength is None:
            return True
        return strength in self.strengths


@dataclass
class Playbook:
    """All entries from playbook.yaml."""

    version: int = 1
    updated_at: str = ""
    entries: tuple[ModelEntry, ...] = field(default_factory=tuple)

    def by_id(self, model_id: str) -> ModelEntry | None:
        for entry in self.entries:
            if entry.model_id == model_id:
                return entry
        return None

    def by_family(self, family: str) -> tuple[ModelEntry, ...]:
        return tuple(e for e in self.entries if e.family == family)

    def by_tier(self, tier: str) -> tuple[ModelEntry, ...]:
        return tuple(e for e in self.entries if e.tier_default == tier)

    def candidates_for(
        self,
        *,
        tier: str | None = None,
        strength: str | None = None,
        family: str | None = None,
    ) -> tuple[ModelEntry, ...]:
        """Filter candidates the router might consider."""
        out = self.entries
        if tier is not None:
            out = tuple(e for e in out if e.tier_default == tier)
        if strength is not None:
            out = tuple(e for e in out if strength in e.strengths)
        if family is not None:
            out = tuple(e for e in out if e.family == family)
        return out


def _coerce_entry(raw: dict[str, Any]) -> ModelEntry | None:
    try:
        return ModelEntry(
            model_id=str(raw["model_id"]),
            family=str(raw["family"]),
            display_name=str(raw.get("display_name") or raw["model_id"]),
            tier_default=str(raw.get("tier_default", "top")),
            context_tokens=int(raw.get("context_tokens") or 0),
            strengths=tuple(str(s) for s in raw.get("strengths") or ()),
            weaknesses=tuple(str(s) for s in raw.get("weaknesses") or ()),
            notes=str(raw.get("notes") or "").strip(),
            pricing_usd_per_mtok={
                k: float(v) for k, v in (raw.get("pricing_usd_per_mtok") or {}).items()
            },
            subscription_quota=bool(raw.get("subscription_quota", False)),
            audience_default=str(raw.get("audience_default") or "developer"),  # type: ignore[arg-type]
        )
    except (KeyError, TypeError, ValueError) as e:
        log.warning("playbook.entry_invalid", error=str(e), raw=raw)
        return None


def load_playbook(path: str | Path | None = None) -> Playbook:
    """Read playbook.yaml. Missing or malformed yields an empty playbook."""
    target = Path(path) if path else Path(__file__).parent / "playbook.yaml"
    if not target.exists():
        log.info("playbook.absent", path=str(target))
        return Playbook()

    try:
        with target.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        log.warning("playbook.yaml_invalid", path=str(target), error=str(e))
        return Playbook()

    raw_entries = data.get("models") or []
    parsed = tuple(filter(None, (_coerce_entry(r) for r in raw_entries)))

    return Playbook(
        version=int(data.get("version") or 1),
        updated_at=str(data.get("updated_at") or ""),
        entries=parsed,
    )


# ----- Module-level singleton (cheap to load, immutable in normal use) -----

_playbook: Playbook | None = None


def get_playbook() -> Playbook:
    """Cached playbook. Call :func:`reload_playbook` to refresh."""
    global _playbook
    if _playbook is None:
        _playbook = load_playbook()
    return _playbook


def reload_playbook(path: str | Path | None = None) -> Playbook:
    """Force-reload from disk. Used by CLI / idle-batch on schedule."""
    global _playbook
    _playbook = load_playbook(path)
    log.info("playbook.reloaded", count=len(_playbook.entries))
    return _playbook


__all__ = [
    "Audience",
    "ModelEntry",
    "Playbook",
    "get_playbook",
    "load_playbook",
    "reload_playbook",
]
