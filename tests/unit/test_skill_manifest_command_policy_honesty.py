"""Audit F035a (honesty subset): no phantom per-skill allowlist.

The shell-exec sandbox is guarded by an *environment-level* command policy
(``KUN_SHELL_EXEC_ALLOW`` / ``KUN_SHELL_EXEC_DENY`` in
``kun/skills/command_policy.py``), NOT by a per-manifest allowlist. The dead
``allowed_commands`` / ``denied_patterns`` / ``denied_domains`` typed fields on
``SkillManifest`` had zero consumers, so declaring them falsely advertised an
enforced per-skill allowlist. They were removed; the builtin shell-exec
description was corrected. These tests lock both in so the misleading contract
cannot silently return.
"""

from __future__ import annotations

import pytest

from kun.skills.builtin import BUILTIN_MANIFESTS
from kun.skills.loader import SkillManifest

_DEAD_FIELDS = ("allowed_commands", "denied_patterns", "denied_domains")


@pytest.mark.unit
def test_skill_manifest_has_no_dead_command_policy_fields() -> None:
    # The unenforced per-manifest command-policy fields must not be typed fields.
    for name in _DEAD_FIELDS:
        assert name not in SkillManifest.model_fields, (
            f"{name} is back as a typed SkillManifest field — it advertises a "
            "per-skill command policy that is NOT enforced (see audit F035a). "
            "Either wire it through command_policy or keep it out."
        )


@pytest.mark.unit
def test_manifest_with_allowlist_frontmatter_still_loads_as_extra() -> None:
    # extra="allow": SKILL.md frontmatter using the old keys must still parse,
    # just as inert extra metadata (not a typed, enforced contract).
    m = SkillManifest(
        name="os-shell",
        description="x",
        allowed_commands=["ls", "cat"],
    )
    assert m.name == "os-shell"
    # It is NOT promoted to a typed attribute.
    assert "allowed_commands" not in type(m).model_fields


@pytest.mark.unit
def test_shell_exec_description_does_not_claim_per_skill_allowlist() -> None:
    desc = BUILTIN_MANIFESTS["shell-exec"]["description"]
    # Must not bare-claim "allowlist 约束" without the env-policy qualifier — that
    # was the misleading wording. The real guard is the env command policy.
    assert "allowlist 约束" not in desc, (
        "shell-exec still claims a bare 'allowlist 约束' — there is no enforced "
        "per-skill allowlist; the guard is env-level command_policy (F035a)."
    )
    assert "KUN_SHELL_EXEC" in desc, (
        "shell-exec description should name the real env command policy."
    )
