"""Runtime gate-evaluation id is unique per output, not per work item (audit F080).

The old id depended only on work_item_id, so a retry reused it and overwrote the
prior gate evaluation. It now folds in the output content hash.
"""

from __future__ import annotations

import pytest
from kun.control_plane.kun_runtime_runner import _runtime_gate_id


@pytest.mark.unit
def test_distinct_output_gives_distinct_gate_id() -> None:
    # Same work item, different output content (e.g. a retry that changed the
    # answer) → distinct gate ids, so history is not overwritten.
    a = _runtime_gate_id("wi-1", "a" * 64)
    b = _runtime_gate_id("wi-1", "b" * 64)
    assert a != b
    assert "wi-1" in a and "wi-1" in b


@pytest.mark.unit
def test_identical_output_is_stable() -> None:
    # Byte-identical retry → same id (it is the same evaluation).
    h = "deadbeefcafe0000"
    assert _runtime_gate_id("wi-1", h) == _runtime_gate_id("wi-1", h)


@pytest.mark.unit
def test_distinct_work_items_distinct_ids() -> None:
    h = "deadbeefcafe0000"
    assert _runtime_gate_id("wi-1", h) != _runtime_gate_id("wi-2", h)


@pytest.mark.unit
def test_empty_hash_does_not_crash() -> None:
    gid = _runtime_gate_id("wi-1", "")
    assert gid.startswith("gate-kun-runtime-")
    assert "nohash" in gid
