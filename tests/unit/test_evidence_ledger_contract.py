"""Contract tests for the evidence_ledger stub (audit F155).

evidence_ledger is imported by gate / mission_director / api (production paths)
yet had 0% test coverage. It is still a stub (append logs + returns an id;
get_trace returns []); real persistence is tracked in the RSI mainline proposal
(F065/F042). These tests pin the current contract so behavior — and the eventual
wiring — is guarded; update them when the real read/write lands.
"""

from __future__ import annotations

import pytest
from kun.governance.evidence_ledger import (
    EvidenceEntry,
    _new_id,
    append,
    get_trace,
)


@pytest.mark.unit
def test_new_id_is_prefixed() -> None:
    eid = _new_id()
    # new_id("evidence") uses the "ev_l" prefix (kun.core.ids._PREFIX).
    assert eid.startswith("ev_l-")
    assert _new_id() != eid  # unique per call


@pytest.mark.unit
def test_evidence_entry_requires_core_fields() -> None:
    entry = EvidenceEntry(entry_id="e1", task_id="t1", kind="artifact", payload={"hash": "abc"})
    assert entry.kind == "artifact"
    assert entry.created_at is not None  # default-stamped
    with pytest.raises(Exception):
        EvidenceEntry(entry_id="e1", task_id="t1")  # missing kind/payload


@pytest.mark.unit
@pytest.mark.asyncio
async def test_append_returns_evidence_id() -> None:
    eid = await append("task-1", kind="test_report", payload={"pass_rate": 1.0})
    assert isinstance(eid, str) and eid.startswith("ev_l-")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_trace_returns_list() -> None:
    # Stub returns [] today; the contract is "a list", so callers can iterate safely.
    trace = await get_trace("task-1")
    assert isinstance(trace, list)
