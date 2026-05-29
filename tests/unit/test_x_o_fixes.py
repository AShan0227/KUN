"""V7 Phase X.O — tests for the 2 bugs self-audit caught + template upgrade.

Bug 1 fix proof: ``extract_changed_paths_from_messages`` extracts paths
from tool_calls so the production WS entry can feed a non-empty actual
list to the X.I-3-FIX checker.

Bug 2 fix proof: ``discipline_store`` cache + cockpit /discipline/recent
return real data instead of hardcoded [].

Template §4 Step 7 proof: the new section is present + describes
chain-reach methodology.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kun.api.discipline_store import (
    list_recent_discipline_reports,
    record_discipline_report,
    reset_for_tests,
)
from kun.engineering.extract_changed_paths import (
    extract_changed_paths_from_messages,
)

# ============================================================
# Bug 1 fix — extract_changed_paths_from_messages
# ============================================================


def test_extract_returns_empty_on_no_messages() -> None:
    assert extract_changed_paths_from_messages([]) == []


def test_extract_ignores_messages_without_tool_calls() -> None:
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert extract_changed_paths_from_messages(messages) == []


def test_extract_returns_paths_from_known_write_tools() -> None:
    """Tool calls whose name matches the known-writer set surface their
    'path' arg."""
    messages: list[dict[str, Any]] = [
        {
            "role": "assistant",
            "content": "writing files",
            "tool_calls": [
                {
                    "id": "t1",
                    "name": "self_reflect.write",
                    "arguments": {"path": "docs/dist-output/x.md", "content": "..."},
                },
                {
                    "id": "t2",
                    "name": "file_io.write",
                    "arguments": {"path": "/tmp/y.txt", "content": "..."},
                },
                {
                    "id": "t3",
                    "name": "search",
                    "arguments": {"q": "..."},
                },
            ],
        }
    ]
    paths = extract_changed_paths_from_messages(messages)
    assert "docs/dist-output/x.md" in paths
    assert any("y.txt" in p for p in paths)
    # 'search' is not a writer — must not appear
    assert not any("search" in p for p in paths)


def test_extract_handles_string_arguments() -> None:
    """Some providers serialise arguments as a JSON string — parse it."""
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {
                    "name": "self_reflect.write",
                    "arguments": '{"path": "docs/y.md"}',
                }
            ],
        }
    ]
    assert "docs/y.md" in extract_changed_paths_from_messages(messages)


def test_extract_deduplicates_paths() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"name": "Write", "arguments": {"path": "a.txt"}},
                {"name": "Edit", "arguments": {"path": "a.txt"}},
            ],
        }
    ]
    assert extract_changed_paths_from_messages(messages) == ["a.txt"]


def test_extract_drops_empty_paths_and_unsupported_tools() -> None:
    messages = [
        {
            "role": "assistant",
            "tool_calls": [
                {"name": "self_reflect.write", "arguments": {"path": ""}},
                {"name": "shell_exec", "arguments": {"cmd": "ls"}},
                {"name": "Write", "arguments": {}},
            ],
        }
    ]
    assert extract_changed_paths_from_messages(messages) == []


# ============================================================
# Bug 2 fix — discipline_store + cockpit reader
# ============================================================


@pytest.fixture(autouse=True)
def _reset_discipline_cache() -> Any:
    reset_for_tests()
    yield
    reset_for_tests()


def test_discipline_store_empty_by_default() -> None:
    assert list_recent_discipline_reports() == []


def test_discipline_store_records_and_lists_newest_first() -> None:
    record_discipline_report(
        task_id="t-1",
        overall_score=0.9,
        n_total=10,
        n_passed=9,
        failed_disciplines=["greppy"],
    )
    record_discipline_report(
        task_id="t-2",
        overall_score=0.7,
        n_total=10,
        n_passed=7,
        failed_disciplines=["greppy", "coa"],
    )
    listed = list_recent_discipline_reports()
    assert len(listed) == 2
    # Newest first
    assert listed[0]["task_id"] == "t-2"
    assert listed[1]["task_id"] == "t-1"
    assert listed[0]["failed_disciplines"] == ["greppy", "coa"]


def test_discipline_store_limit_clamp() -> None:
    for i in range(5):
        record_discipline_report(
            task_id=f"t-{i}",
            overall_score=1.0,
            n_total=1,
            n_passed=1,
            failed_disciplines=[],
        )
    assert len(list_recent_discipline_reports(limit=3)) == 3
    assert len(list_recent_discipline_reports(limit=999)) == 5


# ============================================================
# Cockpit endpoint returns real data via store
# ============================================================


def test_cockpit_discipline_endpoint_returns_store_data() -> None:
    """The /discipline/recent endpoint must read from the store, not
    hardcoded []. We import the route handler and call it directly."""
    from kun.api.cockpit import get_recent_discipline_checks

    record_discipline_report(
        task_id="t-cockpit",
        overall_score=0.5,
        n_total=2,
        n_passed=1,
        failed_disciplines=["x"],
    )

    import asyncio

    result = asyncio.run(get_recent_discipline_checks(limit=20))
    assert result["total"] == 1
    assert result["discipline_checks"][0]["task_id"] == "t-cockpit"
    assert "stub" not in result.get("data_source", "").lower()


# ============================================================
# Template §4 Step 7 chain-reach upgrade
# ============================================================


def _template_text() -> str:
    p = (
        Path(__file__).resolve().parent.parent.parent
        / "docs"
        / "templates"
        / "hidden-orphan-audit-prompt.md"
    )
    return p.read_text(encoding="utf-8")


def test_template_step7_section_present() -> None:
    src = _template_text()
    assert "Step 7 — Chain-reach" in src
    assert "X.O 升级" in src


def test_template_step7_explains_chain_reach_methodology() -> None:
    src = _template_text()
    # Step 7 must distinguish "Step 4 grep empty + Step 7 finds chain" =
    # NOT orphan, vs "Step 4 grep empty + Step 7 also empty" = orphan
    assert "chain-wired" in src or "chain wired" in src
    assert "假阳性" in src or "false positive" in src
    # And must mention §6 self-audit confession requirement
    assert "§6" in src
    assert "R1" in src
