"""PROMISES.md auto-generator tests."""

from __future__ import annotations

from datetime import UTC, datetime

from kun.engineering.promises_autogen import (
    CommitPromise,
    append_promises_section,
    extract_refs,
    parse_git_log_lines,
    render_promises_section,
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
