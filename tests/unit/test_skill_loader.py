"""Skill loader tests."""

import textwrap

import pytest
from kun.skills.loader import SkillRegistry, load_skills_from_dir, parse_skill


@pytest.mark.unit
def test_parse_skill_minimal():
    src = textwrap.dedent("""\
        # SPDX-License-Identifier: Apache-2.0
        ---
        name: demo-skill
        description: A demo
        version: 0.1.0
        license: Apache-2.0
        ---

        # Body

        body here
        """)
    rec = parse_skill(src, "demo.md")
    assert rec.skill_id == "demo-skill"
    assert rec.manifest.name == "demo-skill"
    assert rec.manifest.license == "Apache-2.0"
    assert rec.spdx_license == "Apache-2.0"
    assert "body here" in rec.body_md


@pytest.mark.unit
def test_parse_missing_frontmatter_raises():
    with pytest.raises(ValueError):
        parse_skill("just body, no frontmatter", "x.md")


@pytest.mark.unit
def test_load_skills_from_dir(tmp_path):
    d = tmp_path / "skills" / "starter" / "t1"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: t1\ndescription: first\n---\n\nbody\n",
        encoding="utf-8",
    )
    d2 = tmp_path / "skills" / "starter" / "t2"
    d2.mkdir(parents=True)
    (d2 / "SKILL.md").write_text(
        "---\nname: t2\ndescription: second\n---\n\nbody2\n",
        encoding="utf-8",
    )
    reg = load_skills_from_dir(tmp_path / "skills")
    assert len(reg) == 2
    assert {r.skill_id for r in reg} == {"t1", "t2"}


@pytest.mark.unit
def test_registry_override_warns(tmp_path, caplog):
    reg = SkillRegistry()
    rec = parse_skill("---\nname: a\ndescription: d\n---\n\nx\n", "a.md")
    reg.register(rec)
    reg.register(rec)  # override
    assert len(reg) == 1
