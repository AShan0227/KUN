"""Skill selector — pick best skill(s) for a task (§2.1 原子 / 组合 / 元技能).

Walking skeleton:
  - Match by TaskSpec.required_skills (if present)
  - Fall back to simple substring match on task_type vs skill name
  - Return top-k skills

Later:
  - Vector similarity (Qdrant) over skill descriptions
  - Capability-card driven scoring
  - Two-tower recall for large skill libs
"""

from __future__ import annotations

from kun.core.logging import get_logger
from kun.datamodel.task import TaskRef
from kun.skills.loader import SkillRecord, SkillRegistry, get_registry

log = get_logger("kun.skills.selector")


class SkillSelector:
    """Map a TaskRef → ordered list of SkillRecord candidates."""

    def __init__(self, registry: SkillRegistry | None = None) -> None:
        self._reg = registry or get_registry()

    def select(self, task_ref: TaskRef, *, top_k: int = 3) -> list[SkillRecord]:
        # 1. Explicit required_skills from TaskSpec
        if task_ref.spec and task_ref.spec.required_skills:
            out: list[SkillRecord] = []
            for sid in task_ref.spec.required_skills:
                rec = self._reg.get(sid)
                if rec is not None:
                    out.append(rec)
            if out:
                return out[:top_k]

        # 2. Heuristic substring match on task_type parts
        task_type = task_ref.meta.task_type
        parts = set(task_type.split("."))
        scored: list[tuple[int, SkillRecord]] = []
        for rec in self._reg:
            name_parts = set(rec.skill_id.replace("_", "-").split("-"))
            overlap = len(parts & name_parts)
            if overlap > 0:
                scored.append((overlap, rec))
        scored.sort(key=lambda t: (-t[0], t[1].skill_id))
        return [rec for _, rec in scored[:top_k]]

    def summary(self, skills: list[SkillRecord]) -> str:
        """Produce a compact 'available skills' summary for the LLM prompt.

        Per §3.6 三级渐进披露 — L1 only: name + description.
        """
        if not skills:
            return ""
        lines = [f"- {s.skill_id}: {s.manifest.description}" for s in skills]
        return f"可用技能 (top {len(lines)}):\n" + "\n".join(lines)


_selector: SkillSelector | None = None


def get_selector() -> SkillSelector:
    global _selector
    if _selector is None:
        _selector = SkillSelector()
    return _selector


def reset_selector() -> None:
    global _selector
    _selector = None
