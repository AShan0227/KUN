"""PROMISES.md auto-generator tests."""

from __future__ import annotations

from datetime import UTC, datetime

from kun.engineering.promises_autogen import (
    CommitPromise,
    append_promises_section,
    extract_refs,
    group_release_notes,
    infer_v22_section,
    parse_git_log_lines,
    render_promises_section,
    render_release_notes,
)


def test_extract_refs_from_commit_subject() -> None:
    refs = extract_refs("feat: C50 Wire 29A BATCH11 T23 V2.2 promises generator")
    assert refs == ["C50", "Wire29A", "BATCH11", "T23", "V2.2"]


def test_parse_git_log_lines_handles_tab_and_space_formats() -> None:
    commits = parse_git_log_lines(
        [
            "abc123\tfeat: C45 persist registry",
            "def456 fix: Wire 30 graph traversal",
        ]
    )
    assert commits[0].commit == "abc123"
    assert commits[0].refs == ["C45"]
    assert commits[1].commit == "def456"
    assert commits[1].refs == ["Wire30"]


def test_render_promises_section_outputs_reviewable_table() -> None:
    section = render_promises_section(
        [
            CommitPromise(
                commit="abc123",
                subject="feat: C50 auto promises",
                refs=["C50"],
            )
        ],
        title="Z.15 自动同步",
        generated_at=datetime(2026, 4, 27, tzinfo=UTC),
    )

    assert "## Z.15 自动同步" in section
    assert "`abc123`" in section
    assert "C50" in section
    assert "feat: C50 auto promises" in section


def test_append_promises_section_preserves_existing_content(tmp_path) -> None:
    target = tmp_path / "PROMISES.md"
    target.write_text("# KUN 历史承诺清单\n")

    append_promises_section(target, "## New\n\nbody\n")

    text = target.read_text()
    assert text.startswith("# KUN 历史承诺清单")
    assert "## New" in text


def test_infer_v22_section_from_c_task_ref() -> None:
    commit = CommitPromise(
        commit="abc123",
        subject="feat: C43 wire input translator into chat",
        refs=["C43"],
    )

    assert infer_v22_section(commit) == "§23"


def test_infer_v22_section_prefers_explicit_spec_section() -> None:
    commit = CommitPromise(
        commit="abc123",
        subject="docs: V2.2 §26 release notes for lab",
        refs=["V2.2"],
    )

    assert infer_v22_section(commit) == "§26"


def test_infer_v22_section_does_not_treat_codex_as_codecapability() -> None:
    commit = CommitPromise(
        commit="abc123",
        subject="docs(codex): BATCH12 release prep",
        refs=["BATCH12"],
    )

    assert infer_v22_section(commit) == "release"


def test_group_release_notes_by_section() -> None:
    groups = group_release_notes(
        [
            CommitPromise("a1", "feat: C38 graph API", ["C38"]),
            CommitPromise("b2", "feat: C32 ensemble mode", ["C32"]),
            CommitPromise("c3", "docs: C52 release checklist", ["C52"]),
        ]
    )

    assert [(group.section, len(group.commits)) for group in groups] == [
        ("§20", 1),
        ("§21", 1),
        ("release", 1),
    ]


def test_render_release_notes_outputs_grouped_markdown() -> None:
    notes = render_release_notes(
        [
            CommitPromise("a1", "feat: C38 graph API", ["C38"]),
            CommitPromise("b2", "feat: C32 ensemble mode", ["C32"]),
        ],
        version="v2.2.0",
        generated_at=datetime(2026, 4, 27, tzinfo=UTC),
    )

    assert "# KUN v2.2.0 Changelog" in notes
    assert "## §20 知识图谱 + 导航式记忆" in notes
    assert "`a1` [C38] feat: C38 graph API" in notes
    assert "## §21 ExecutionMode FAST/SMART/MAX/ENSEMBLE" in notes
    assert "`b2` [C32] feat: C32 ensemble mode" in notes
