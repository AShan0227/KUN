"""C13 bandit router and rollback tests."""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest
from kun.core.bandit_router import AutoRollback, EpsilonGreedyBandit


@pytest.mark.unit
def test_bandit_rejects_empty_arms() -> None:
    with pytest.raises(ValueError, match="arms"):
        EpsilonGreedyBandit([])


@pytest.mark.unit
def test_bandit_rejects_duplicate_arms() -> None:
    with pytest.raises(ValueError, match="unique"):
        EpsilonGreedyBandit(["cheap", "cheap"])


@pytest.mark.unit
def test_epsilon_zero_exploits_best_arm() -> None:
    bandit = EpsilonGreedyBandit(["cheap", "top"], epsilon=0.0, rng=random.Random(1))
    bandit.update("coding", "top", 0.9)
    bandit.update("coding", "cheap", 0.2)

    assert bandit.select("coding") == "top"


@pytest.mark.unit
def test_epsilon_one_explores_with_rng() -> None:
    bandit = EpsilonGreedyBandit(["cheap", "top"], epsilon=1.0, rng=random.Random(0))
    selections = {bandit.select("coding") for _ in range(10)}

    assert selections == {"cheap", "top"}


@pytest.mark.unit
def test_reward_updates_average_and_clamps() -> None:
    bandit = EpsilonGreedyBandit(["cheap", "top"], epsilon=0.0)
    bandit.update("judge", "cheap", 0.5)
    bandit.update("judge", "cheap", 2.0)
    bandit.update("judge", "top", -1.0)

    stats = bandit.stats_for("judge")
    assert stats["cheap"].count == 2
    assert stats["cheap"].average_reward == 0.75
    assert stats["top"].average_reward == 0.0


@pytest.mark.unit
def test_best_arm_tie_keeps_arm_order() -> None:
    bandit = EpsilonGreedyBandit(["cheap", "strong", "top"], epsilon=0.0)

    assert bandit.best_arm("new-context") == "cheap"


@pytest.mark.unit
def test_bandit_unknown_arm_rejected() -> None:
    bandit = EpsilonGreedyBandit(["cheap"])

    with pytest.raises(ValueError, match="unknown arm"):
        bandit.update("judge", "top", 0.5)


@pytest.mark.unit
def test_bandit_context_key_required() -> None:
    bandit = EpsilonGreedyBandit(["cheap"])

    with pytest.raises(ValueError, match="context_key"):
        bandit.select("")


@pytest.mark.unit
def test_rollback_triggers_after_consecutive_failures() -> None:
    rollback = AutoRollback(failure_threshold=3, window_sec=600)
    rollback.record("stable", True)
    rollback.record("challenger", False)
    rollback.record("challenger", False)

    assert rollback.should_rollback("challenger") == (False, None)

    rollback.record("challenger", False)

    assert rollback.should_rollback("challenger") == (True, "stable")


@pytest.mark.unit
def test_rollback_does_not_target_current_last_good() -> None:
    rollback = AutoRollback(failure_threshold=1)
    rollback.record("top", True)
    rollback.record("top", False)

    assert rollback.should_rollback("top") == (False, None)


@pytest.mark.unit
def test_rollback_success_breaks_failure_streak() -> None:
    rollback = AutoRollback(failure_threshold=2)
    rollback.record("stable", True)
    rollback.record("challenger", False)
    rollback.record("challenger", True)
    rollback.record("challenger", False)

    assert rollback.last_known_good == "challenger"
    assert rollback.should_rollback("challenger") == (False, None)


@pytest.mark.unit
def test_rollback_window_prunes_old_failures() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)

    def clock() -> datetime:
        return now

    rollback = AutoRollback(failure_threshold=2, window_sec=10, clock=clock)
    rollback.record("stable", True)
    rollback.record("challenger", False)

    now += timedelta(seconds=11)
    rollback.record("challenger", False)

    assert rollback.should_rollback("challenger") == (False, None)
    assert len(rollback.recent_records()) == 1


@pytest.mark.unit
def test_rollback_requires_valid_settings() -> None:
    with pytest.raises(ValueError, match="failure_threshold"):
        AutoRollback(failure_threshold=0)
    with pytest.raises(ValueError, match="window_sec"):
        AutoRollback(window_sec=0)


@pytest.mark.unit
def test_rollback_rejects_empty_arm() -> None:
    rollback = AutoRollback()

    with pytest.raises(ValueError, match="arm"):
        rollback.record("", False)
    with pytest.raises(ValueError, match="current_arm"):
        rollback.should_rollback("")
