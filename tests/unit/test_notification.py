"""Notification defaults tests."""

from datetime import datetime

import pytest
from kun.datamodel.notification import Notification


@pytest.mark.unit
def test_defaults():
    n = Notification(tenant_id="u-sylvan", kind="cost_tick")
    assert n.channel == "side"
    assert n.severity == "info"
    assert isinstance(n.render_hint, dict)
    assert n.delivered_at is None


@pytest.mark.unit
def test_mark_delivered_sets_timestamp():
    n = Notification(tenant_id="u-sylvan", kind="alert", severity="warn")
    n.mark_delivered()
    assert isinstance(n.delivered_at, datetime)
