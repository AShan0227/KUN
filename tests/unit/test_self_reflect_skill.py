"""LT.SELF-REFLECT-SKILL — unit tests for the self-reflect skill.

Covers:
  - Whitelist enforcement (read dirs / write dir)
  - Path traversal refusal (../)
  - Read with offset+limit (Claude Code Read pattern)
  - Read of nonexistent / wrong-type targets
  - Write to docs/dist-output/ creates dir lazily
  - Write outside docs/dist-output/ is refused
  - Delete op is rejected (only read/list/write supported)
  - Repo root detection (KUN_REPO_ROOT env var override)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from kun.skills.builtin.self_reflect import execute


@pytest.fixture(autouse=True)
def _pin_repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Each test gets a fresh fake repo under tmp_path with the expected layout.

    Layout:
        tmp_path/
          docs/dev_logs/foo.md      # readable
          seeds/methodologies/bar.yaml  # readable
          kun/agents/x.py           # readable
          tests/unit/y.py           # readable
          private/secret.txt        # NOT in whitelist
    """
    (tmp_path / "docs" / "dev_logs").mkdir(parents=True)
    (tmp_path / "docs" / "dev_logs" / "foo.md").write_text(
        "\n".join(f"line {i}" for i in range(1, 21)) + "\n"
    )
    (tmp_path / "seeds" / "methodologies").mkdir(parents=True)
    (tmp_path / "seeds" / "methodologies" / "bar.yaml").write_text("topic: test\n")
    (tmp_path / "kun" / "agents").mkdir(parents=True)
    (tmp_path / "kun" / "agents" / "x.py").write_text("def f(): pass\n")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "y.py").write_text("import pytest\n")
    (tmp_path / "private").mkdir()
    (tmp_path / "private" / "secret.txt").write_text("nope\n")
    monkeypatch.setenv("KUN_REPO_ROOT", str(tmp_path))
    return tmp_path


@pytest.mark.unit
def test_read_dev_logs_file_succeeds() -> None:
    r = asyncio.run(execute({"op": "read", "path": "docs/dev_logs/foo.md"}))
    assert r.ok, r.error
    assert r.output["line_count"] == 20
    assert "line 1" in r.output["content"]
    assert r.output["truncated"] is False


@pytest.mark.unit
def test_read_with_offset_limit_returns_window() -> None:
    """Claude Code's Read tool pattern — start at offset, take limit lines."""
    r = asyncio.run(
        execute({"op": "read", "path": "docs/dev_logs/foo.md", "offset": 5, "limit": 3})
    )
    assert r.ok, r.error
    content = r.output["content"]
    assert "line 5\n" in content
    assert "line 7\n" in content
    assert "line 8" not in content  # excluded — limit=3 ends at 5+3=8 exclusive
    assert r.output["truncated"] is True


@pytest.mark.unit
def test_read_outside_whitelist_is_refused() -> None:
    """The 'private/' directory is NOT in the read whitelist."""
    r = asyncio.run(execute({"op": "read", "path": "private/secret.txt"}))
    assert r.ok is False
    assert "not in read whitelist" in r.error


@pytest.mark.unit
def test_path_traversal_via_dotdot_is_refused() -> None:
    """`..` segments in the path must be rejected even within whitelist prefix."""
    r = asyncio.run(execute({"op": "read", "path": "docs/../private/secret.txt"}))
    assert r.ok is False
    assert "path traversal" in r.error


@pytest.mark.unit
def test_read_nonexistent_file_returns_clear_error() -> None:
    r = asyncio.run(execute({"op": "read", "path": "docs/dev_logs/nope.md"}))
    assert r.ok is False
    assert "not a file" in r.error


@pytest.mark.unit
def test_list_directory_returns_entries() -> None:
    r = asyncio.run(execute({"op": "list", "path": "docs/dev_logs"}))
    assert r.ok, r.error
    names = {e["name"] for e in r.output}
    assert "foo.md" in names


@pytest.mark.unit
def test_list_outside_whitelist_refused() -> None:
    r = asyncio.run(execute({"op": "list", "path": "private"}))
    assert r.ok is False
    assert "not in read whitelist" in r.error


@pytest.mark.unit
def test_write_to_dist_output_creates_dir_and_file(tmp_path: Path) -> None:
    """Write op creates docs/dist-output/ lazily and writes the file."""
    r = asyncio.run(
        execute(
            {
                "op": "write",
                "path": "hello.md",
                "content": "# hello\n\nworld\n",
            }
        )
    )
    assert r.ok, r.error
    out_file = tmp_path / "docs" / "dist-output" / "hello.md"
    assert out_file.exists()
    assert out_file.read_text() == "# hello\n\nworld\n"
    assert r.output["bytes_written"] > 0


@pytest.mark.unit
def test_write_accepts_full_path_prefix_too() -> None:
    """Caller can pass either 'foo.md' or 'docs/dist-output/foo.md' — both work."""
    r = asyncio.run(
        execute(
            {
                "op": "write",
                "path": "docs/dist-output/sub/deep.md",
                "content": "deep",
            }
        )
    )
    assert r.ok, r.error
    assert "dist-output" in r.output["full_path"]
    assert r.output["full_path"].endswith("sub/deep.md")


@pytest.mark.unit
def test_write_outside_dist_output_is_refused() -> None:
    """Trying to write to docs/dev_logs/ (or anywhere else) must fail."""
    r = asyncio.run(
        execute(
            {
                "op": "write",
                "path": "../seeds/methodologies/evil.yaml",
                "content": "topic: evil",
            }
        )
    )
    assert r.ok is False
    # Either path traversal or whitelist refusal — both are valid block points
    assert (
        "path traversal" in r.error
        or "writes only allowed" in r.error
    )


@pytest.mark.unit
def test_delete_op_is_rejected() -> None:
    """Skill explicitly does NOT support delete — preserves dev_logs invariant."""
    r = asyncio.run(execute({"op": "delete", "path": "docs/dev_logs/foo.md"}))
    assert r.ok is False
    assert "invalid op" in r.error
    assert "delete" not in r.error.split("allowed:")[1] if "allowed:" in r.error else True


@pytest.mark.unit
def test_unknown_op_returns_clear_error() -> None:
    r = asyncio.run(execute({"op": "exec", "path": "docs"}))
    assert r.ok is False
    assert "invalid op" in r.error


@pytest.mark.unit
def test_repo_root_env_override_works(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """KUN_REPO_ROOT env var is the canonical path resolution mechanism."""
    other_root = tmp_path / "alt"
    (other_root / "kun").mkdir(parents=True)
    (other_root / "kun" / "marker.py").write_text("# marker")
    monkeypatch.setenv("KUN_REPO_ROOT", str(other_root))
    r = asyncio.run(execute({"op": "read", "path": "kun/marker.py"}))
    assert r.ok, r.error
    assert "marker" in r.output["content"]


@pytest.mark.unit
def test_write_content_must_be_string() -> None:
    """Defensive: non-string content (e.g. a dict from a buggy LLM) is rejected."""
    r = asyncio.run(execute({"op": "write", "path": "x.md", "content": {"oops": 1}}))
    assert r.ok is False
    assert "must be a string" in r.error
