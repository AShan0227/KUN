from __future__ import annotations

import os
from unittest.mock import patch

from kun.api.main import _cron_scheduler_enabled, _standalone_idle_batch_enabled


def test_standalone_idle_batch_defaults_off_when_cron_owns_it() -> None:
    with patch.dict(os.environ, {}, clear=True):
        assert _cron_scheduler_enabled() is True
        assert _standalone_idle_batch_enabled() is False


def test_standalone_idle_batch_defaults_on_when_cron_disabled() -> None:
    with patch.dict(os.environ, {"KUN_CRON_SCHEDULER_ENABLED": "0"}, clear=True):
        assert _cron_scheduler_enabled() is False
        assert _standalone_idle_batch_enabled() is True


def test_standalone_idle_batch_can_be_explicitly_enabled_with_cron() -> None:
    with patch.dict(
        os.environ,
        {"KUN_CRON_SCHEDULER_ENABLED": "1", "KUN_IDLE_BATCH_ENABLED": "1"},
        clear=True,
    ):
        assert _cron_scheduler_enabled() is True
        assert _standalone_idle_batch_enabled() is True
