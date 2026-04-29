"""Per-project constitution: project-level rules for prompt/runtime guards."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

RuleKind = Literal[
    "style",
    "tone",
    "forbidden_word",
    "required_word",
    "allowed_tool",
    "blocked_tool",
]
RuleSeverity = Literal["warn", "block"]

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<yaml>.*?)\n---\s*(?P<body>.*)\Z", re.S)


class ConstitutionRule(BaseModel):
    """One project rule."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    kind: RuleKind
    pattern: str
    severity: RuleSeverity = "warn"
    description: str = ""
    is_regex: bool = False

    @field_validator("rule_id", "pattern")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @model_validator(mode="after")
    def _valid_regex_when_marked(self) -> ConstitutionRule:
        if self.is_regex:
            re.compile(self.pattern)
        return self


class ProjectConstitution(BaseModel):
    """Rules attached to one project."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    tenant_id: str
    rules: list[ConstitutionRule] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_by: str
    version: int = Field(default=1, ge=1)

    @field_validator("project_id", "tenant_id", "updated_by")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class ConstitutionViolation(BaseModel):
    """A rule hit against generated output or requested tools."""

    rule_id: str
    kind: RuleKind
    severity: RuleSeverity
    description: str
    evidence: str


class InMemoryProjectConstitutionStore:
    """Simple store for tests and the standalone API router."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], ProjectConstitution] = {}

    async def put(self, constitution: ProjectConstitution) -> ProjectConstitution:
        key = (constitution.tenant_id, constitution.project_id)
        existing = self._items.get(key)
        version = existing.version + 1 if existing is not None else constitution.version
        stored = constitution.model_copy(
            update={"version": version, "updated_at": datetime.now(UTC)},
            deep=True,
        )
        self._items[key] = stored
        return stored.model_copy(deep=True)

    async def get(self, *, tenant_id: str, project_id: str) -> ProjectConstitution | None:
        item = self._items.get((tenant_id, project_id))
        return item.model_copy(deep=True) if item is not None else None

    async def delete(self, *, tenant_id: str, project_id: str) -> bool:
        return self._items.pop((tenant_id, project_id), None) is not None

    async def list(self, *, tenant_id: str) -> list[ProjectConstitution]:
        items = [item for (item_tenant, _), item in self._items.items() if item_tenant == tenant_id]
        return [
            item.model_copy(deep=True) for item in sorted(items, key=lambda item: item.project_id)
        ]


class ConstitutionLoader:
    """Load and render project constitution rules."""

    def __init__(self, store: InMemoryProjectConstitutionStore | None = None) -> None:
        self.store = store or get_constitution_store()

    def load_from_file(self, project_dir: Path) -> ProjectConstitution | None:
        """Read `.kun/constitution.md` with YAML frontmatter."""

        path = project_dir / ".kun" / "constitution.md"
        if not path.exists():
            return None
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER_RE.match(text)
        if match is None:
            raise ValueError(f"constitution missing frontmatter: {path}")
        data = yaml.safe_load(match.group("yaml")) or {}
        if not isinstance(data, dict):
            raise ValueError(f"constitution frontmatter must be a mapping: {path}")
        return ProjectConstitution.model_validate(data)

    async def load_from_db(self, project_id: str, *, tenant_id: str) -> ProjectConstitution | None:
        return await self.store.get(tenant_id=tenant_id, project_id=project_id)

    def render_to_system_prompt(self, constitution: ProjectConstitution) -> str:
        """Render a compact system prompt snippet."""

        if not constitution.rules:
            return ""
        lines = [
            f"Project constitution for {constitution.project_id} (v{constitution.version}):",
            "Follow these project-level rules unless a higher-priority safety rule conflicts.",
        ]
        for rule in constitution.rules:
            action = "BLOCK" if rule.severity == "block" else "WARN"
            desc = f" — {rule.description}" if rule.description else ""
            lines.append(f"- [{action}] {rule.kind}: {rule.pattern}{desc}")
        return "\n".join(lines)

    def evaluate_text(
        self,
        constitution: ProjectConstitution,
        text: str,
    ) -> list[ConstitutionViolation]:
        """Check output text against word/style rules."""

        violations: list[ConstitutionViolation] = []
        for rule in constitution.rules:
            if rule.kind == "forbidden_word" and _matches(rule, text):
                violations.append(_violation(rule, evidence=rule.pattern))
            if rule.kind == "required_word" and not _matches(rule, text):
                violations.append(_violation(rule, evidence=f"missing:{rule.pattern}"))
        return violations

    def evaluate_tools(
        self,
        constitution: ProjectConstitution,
        requested_tools: list[str],
    ) -> list[ConstitutionViolation]:
        """Check requested tools against allow/block rules."""

        allow_rules = [rule for rule in constitution.rules if rule.kind == "allowed_tool"]
        block_rules = [rule for rule in constitution.rules if rule.kind == "blocked_tool"]
        violations: list[ConstitutionViolation] = []

        for tool in requested_tools:
            for rule in block_rules:
                if _matches(rule, tool):
                    violations.append(_violation(rule, evidence=tool))
            if allow_rules and not any(_matches(rule, tool) for rule in allow_rules):
                violations.append(
                    ConstitutionViolation(
                        rule_id="allowed_tool.default",
                        kind="allowed_tool",
                        severity="block",
                        description="tool is outside the project allowlist",
                        evidence=tool,
                    )
                )
        return violations


_store: InMemoryProjectConstitutionStore | None = None


def get_constitution_store() -> InMemoryProjectConstitutionStore:
    global _store
    if _store is None:
        _store = InMemoryProjectConstitutionStore()
    return _store


def reset_constitution_store() -> None:
    global _store
    _store = InMemoryProjectConstitutionStore()


def _matches(rule: ConstitutionRule, text: str) -> bool:
    if rule.is_regex:
        return re.search(rule.pattern, text, flags=re.I) is not None
    return rule.pattern.lower() in text.lower()


def _violation(rule: ConstitutionRule, *, evidence: str) -> ConstitutionViolation:
    return ConstitutionViolation(
        rule_id=rule.rule_id,
        kind=rule.kind,
        severity=rule.severity,
        description=rule.description,
        evidence=evidence,
    )


__all__ = [
    "ConstitutionLoader",
    "ConstitutionRule",
    "ConstitutionViolation",
    "InMemoryProjectConstitutionStore",
    "ProjectConstitution",
    "get_constitution_store",
    "reset_constitution_store",
]
