"""Skill loader — 解析 SKILL.md (Anthropic Agent Skills 规范兼容) → 内存 registry.

SKILL.md 格式 (frontmatter + markdown body):

    # SPDX-License-Identifier: Apache-2.0
    ---
    name: writing-markdown
    description: ...
    version: 0.1.0
    license: Apache-2.0
    curated_by: KUN
    input_schema: {...}
    ---

    # writing-markdown

    Body markdown.

三级渐进披露:
  L1 = frontmatter 的 name + description + tags (始终在 context)
  L2 = 完整 frontmatter (input_schema / constraints) — 按需加载
  L3 = body markdown + 附带脚本 — 只有选中该 skill 才加载
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field

from kun.core.logging import get_logger

log = get_logger("kun.skills.loader")

FRONT_RE = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.DOTALL)


class SkillManifest(BaseModel):
    """SKILL.md frontmatter → pydantic model."""

    model_config = ConfigDict(extra="allow")

    name: str
    description: str
    version: str = "0.1.0"
    license: str = "Proprietary"
    curated_by: str | None = None
    source: str | None = None
    maturity: str = "cold_start"
    input_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_commands: list[str] = Field(default_factory=list)
    denied_patterns: list[str] = Field(default_factory=list)
    denied_domains: list[str] = Field(default_factory=list)
    # 主动用工具 layer 3: 每个 skill 自带的"看到这种 prompt 就触发我"声明.
    # 元素跟 rules/proactive/triggers.yaml 的 trigger 同形:
    #   - pattern: 正则
    #   - extract: {kind, param_name, min_len, max_len, extra_params}
    # 例子: [{pattern: '\\.csv\\b', extract: {kind: match_group_0, param_name: path}}]
    auto_trigger_when: list[dict[str, Any]] = Field(default_factory=list)


class SkillRecord(BaseModel):
    """In-memory skill entry."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str  # == manifest.name (unique within a pack)
    manifest: SkillManifest
    body_md: str  # L3 body
    spdx_license: str | None  # pulled from top-of-file SPDX header
    source_path: str
    loaded_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SkillRegistry:
    """In-memory skills registry with name lookup."""

    def __init__(self) -> None:
        self._by_name: dict[str, SkillRecord] = {}

    def register(self, record: SkillRecord) -> None:
        if record.skill_id in self._by_name:
            log.warning("skill.override", name=record.skill_id)
        self._by_name[record.skill_id] = record
        log.debug("skill.registered", name=record.skill_id, source=record.source_path)

    def get(self, name: str) -> SkillRecord | None:
        return self._by_name.get(name)

    def names(self) -> list[str]:
        return sorted(self._by_name)

    def __len__(self) -> int:
        return len(self._by_name)

    def __iter__(self) -> Iterator[SkillRecord]:
        return iter(self._by_name.values())

    def match_auto_triggers(self, prompt: str) -> list[tuple[str, str, dict[str, Any]]]:
        """主动用工具 layer 3: 扫所有 skill 的 auto_trigger_when, 返回命中.

        Returns: [(skill_id, pattern, extract_cfg), ...] — 每个 skill 至多 1 条.
        正则坏掉 / 字段缺失 → 跳过, 不抛.
        """
        out: list[tuple[str, str, dict[str, Any]]] = []
        for rec in self._by_name.values():
            for entry in rec.manifest.auto_trigger_when or []:
                pattern = entry.get("pattern")
                if not isinstance(pattern, str) or not pattern:
                    continue
                try:
                    if re.search(pattern, prompt, re.IGNORECASE | re.MULTILINE):
                        out.append((rec.skill_id, pattern, dict(entry.get("extract") or {})))
                        break
                except re.error:
                    continue
        return out


def _extract_spdx(content: str) -> str | None:
    """Grab SPDX-License-Identifier from the first 10 lines if present."""
    for line in content.splitlines()[:10]:
        if "SPDX-License-Identifier:" in line:
            return line.split("SPDX-License-Identifier:", 1)[1].strip(" #-")
    return None


def parse_skill(content: str, source_path: str) -> SkillRecord:
    """Parse a SKILL.md file content into a SkillRecord."""
    spdx = _extract_spdx(content)

    # Strip leading SPDX comment lines so frontmatter regex finds ---
    lines = content.splitlines()
    while lines and (lines[0].strip().startswith("#") or lines[0].strip() == ""):
        # stop when we hit the frontmatter start
        if lines[0].strip() == "---":
            break
        lines.pop(0)

    body = "\n".join(lines)
    m = FRONT_RE.match(body)
    if m is None:
        raise ValueError(f"SKILL.md missing frontmatter: {source_path}")

    fm_text, body_md = m.groups()
    fm_data = cast(dict[str, Any], yaml.safe_load(fm_text) or {})
    manifest = SkillManifest.model_validate(fm_data)
    return SkillRecord(
        skill_id=manifest.name,
        manifest=manifest,
        body_md=body_md,
        spdx_license=spdx or manifest.license,
        source_path=source_path,
    )


def load_skills_from_dir(root: str | Path = "skills") -> SkillRegistry:
    """Scan `root` recursively for SKILL.md files and register each."""
    root = Path(root)
    registry = SkillRegistry()
    if not root.exists():
        log.info("skills.dir_missing", path=str(root))
        return registry

    for skill_file in sorted(root.rglob("SKILL.md")):
        try:
            content = skill_file.read_text(encoding="utf-8")
            record = parse_skill(content, str(skill_file))
            registry.register(record)
        except Exception as e:
            log.warning("skills.parse_failed", path=str(skill_file), error=str(e))

    log.info("skills.loaded", count=len(registry), path=str(root))
    return registry


# Module-level singleton
_registry: SkillRegistry | None = None


def get_registry() -> SkillRegistry:
    """Return the process-level skill registry, loading on first access."""
    global _registry
    if _registry is None:
        _registry = load_skills_from_dir()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None
