"""IDs: prefix + ULID round-trip."""

import pytest
from kun.core.ids import new_id, parse_kind


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["task", "handoff", "runtime", "skill", "score"])
def test_new_id_has_prefix(kind):
    ident = new_id(kind)
    assert (
        ident.split("-", 1)[0]
        == {
            "task": "tk",
            "handoff": "hp",
            "runtime": "rs",
            "skill": "sk",
            "score": "sc",
        }[kind]
    )
    assert parse_kind(ident) == kind


@pytest.mark.unit
def test_new_id_is_sortable():
    # ULID is lexicographically sortable by time
    a = new_id("task")
    b = new_id("task")
    assert a < b or a == b


@pytest.mark.unit
def test_parse_kind_unknown():
    assert parse_kind("xx-01HABCDE") is None
    assert parse_kind("no_dash") is None
