"""Handoff packet tests."""

from datetime import UTC, datetime

import pytest
from kun.datamodel.capability import EntityRef
from kun.datamodel.handoff import (
    BudgetRemaining,
    CapabilitySnapshot,
    HandoffL1,
    HandoffL2,
    HandoffPacket,
)


def _mk_l1() -> HandoffL1:
    return HandoffL1(
        from_entity=EntityRef(entity_type="role_template", entity_id="rt-coder"),
        to_entity=EntityRef(entity_type="role_template", entity_id="rt-reviewer"),
        task_ref="tk-01HABC",
        timestamp=datetime.now(UTC),
        intent_one_sentence="review my code",
        deliverable_required="pass/fail + comments",
        budget_remaining=BudgetRemaining(usd=0.1),
    )


@pytest.mark.unit
def test_compact_emits_l1_and_l2_only():
    l1 = _mk_l1()
    l2 = HandoffL2(
        upstream_assumptions=["postgres up"],
        upstream_confidence=0.8,
        capability_card_snapshot=CapabilitySnapshot(
            task_type="coding.python.fastapi",
            historical_success_rate=0.9,
            sample_size_effective=50,
        ),
    )
    packet = HandoffPacket(l1=l1, l2=l2)
    compact = packet.compact()
    assert "l1" in compact and "l2" in compact
    assert "l3" not in compact and "l4" not in compact


@pytest.mark.unit
def test_compact_without_l2():
    packet = HandoffPacket(l1=_mk_l1())
    compact = packet.compact()
    assert set(compact.keys()) == {"l1"}
