"""L5.2 — Promotion Queue 超时规则单测."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from kun.governance.promotion_queue import (
    DEFAULT_STALE_THRESHOLD_DAYS,
    PromotionTimeoutSweeper,
    TimeoutCheckResult,
    evaluate_capability_timeout,
)


def _cap(
    *,
    capability_id: str = "cp-1",
    promotion_state: str = "merged",
    last_state_change_at: datetime | None = None,
    promotion_deadline: datetime | None = None,
    tenant_id: str = "u-test",
    target_module: str = "llm.router",
) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "capability_id": capability_id,
        "tenant_id": tenant_id,
        "target_module": target_module,
        "promotion_state": promotion_state,
        "last_state_change_at": last_state_change_at or now,
        "promotion_deadline": promotion_deadline or (now + timedelta(days=14)),
        "change_summary": "test capability",
    }


# ---- evaluate_capability_timeout ----


def test_fresh_capability_not_expired_or_stale() -> None:
    result = evaluate_capability_timeout(_cap())
    assert result.expired is False
    assert result.stale is False
    assert result.recommended_action == "keep"


def test_capability_past_deadline_is_expired() -> None:
    cap = _cap(promotion_deadline=datetime.now(UTC) - timedelta(days=1))
    result = evaluate_capability_timeout(cap)
    assert result.expired is True
    assert result.recommended_action == "mark_expired"


def test_capability_stale_after_threshold_days() -> None:
    cap = _cap(
        last_state_change_at=datetime.now(UTC)
        - timedelta(days=DEFAULT_STALE_THRESHOLD_DAYS + 1)
    )
    result = evaluate_capability_timeout(cap)
    assert result.stale is True
    assert result.expired is False
    assert result.recommended_action == "re_evaluate"


def test_capability_ready_state_recommended_advance() -> None:
    cap = _cap(promotion_state="ready")
    result = evaluate_capability_timeout(cap)
    assert result.recommended_action == "advance"


def test_capability_ready_stale_still_advance_not_reaudit() -> None:
    """ready state 即使 stale 也是推 advance 不是 reaudit."""
    cap = _cap(
        promotion_state="ready",
        last_state_change_at=datetime.now(UTC) - timedelta(days=10),
    )
    result = evaluate_capability_timeout(cap)
    # ready + stale 但未过 deadline → 推 advance (等 Gate enable)
    assert result.recommended_action == "advance"


def test_iso_string_timestamps_accepted() -> None:
    """ISO 字符串时间戳也支持."""
    expired_iso = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    cap = _cap(promotion_deadline=expired_iso)
    result = evaluate_capability_timeout(cap)
    assert result.expired is True


def test_iso_string_without_tzinfo_treated_as_utc() -> None:
    ts = (datetime.now(UTC) - timedelta(days=1)).replace(tzinfo=None).isoformat()
    cap = _cap(promotion_deadline=ts)
    result = evaluate_capability_timeout(cap)
    assert result.expired is True


def test_days_in_state_calculated() -> None:
    cap = _cap(
        last_state_change_at=datetime.now(UTC) - timedelta(days=3)
    )
    result = evaluate_capability_timeout(cap)
    assert 2.9 <= result.days_in_state <= 3.1


# ---- PromotionTimeoutSweeper ----


@pytest.mark.asyncio
async def test_sweeper_empty_list_returns_zero_counts() -> None:
    async def reader() -> list[dict[str, Any]]:
        return []

    sweeper = PromotionTimeoutSweeper(capability_reader=reader)
    report = await sweeper.sweep()
    assert report["scanned"] == 0
    assert report["expired"] == 0
    assert report["stale"] == 0


@pytest.mark.asyncio
async def test_sweeper_marks_expired_and_emits_search_request() -> None:
    state_updates: list[tuple[str, dict[str, Any]]] = []
    search_requests: list[dict[str, Any]] = []

    async def reader() -> list[dict[str, Any]]:
        return [
            _cap(
                capability_id="cp-old",
                promotion_deadline=datetime.now(UTC) - timedelta(days=2),
            )
        ]

    async def state_writer(cap_id: str, payload: dict[str, Any]) -> None:
        state_updates.append((cap_id, payload))

    async def search_emitter(payload: dict[str, Any]) -> None:
        search_requests.append(payload)

    sweeper = PromotionTimeoutSweeper(
        capability_reader=reader,
        state_writer=state_writer,
        search_emitter=search_emitter,
    )
    report = await sweeper.sweep()
    assert report["expired"] == 1
    assert report["search_requests_emitted"] == 1
    assert len(state_updates) == 1
    assert state_updates[0][0] == "cp-old"
    assert state_updates[0][1]["promotion_state"] == "expired"
    req = search_requests[0]
    assert req["triggered_by"] == "promotion_timeout"
    assert req["anomaly_kind"] == "promotion_expired"
    assert req["priority"] == "high"


@pytest.mark.asyncio
async def test_sweeper_emits_reaudit_for_stale_but_not_expired() -> None:
    """stale 但未过 deadline → 只推 reaudit search_request, 不动 state."""
    state_updates: list[tuple[str, dict[str, Any]]] = []
    search_requests: list[dict[str, Any]] = []

    stale_cap = _cap(
        capability_id="cp-stale",
        last_state_change_at=datetime.now(UTC) - timedelta(days=10),
    )

    async def reader() -> list[dict[str, Any]]:
        return [stale_cap]

    async def state_writer(cap_id: str, payload: dict[str, Any]) -> None:
        state_updates.append((cap_id, payload))

    async def search_emitter(payload: dict[str, Any]) -> None:
        search_requests.append(payload)

    sweeper = PromotionTimeoutSweeper(
        capability_reader=reader,
        state_writer=state_writer,
        search_emitter=search_emitter,
    )
    report = await sweeper.sweep()
    assert report["stale"] == 1
    assert report["expired"] == 0
    assert report["search_requests_emitted"] == 1
    # state 不动
    assert state_updates == []
    # reaudit 推 anomaly_kind=promotion_stale, priority=medium
    req = search_requests[0]
    assert req["anomaly_kind"] == "promotion_stale"
    assert req["priority"] == "medium"


@pytest.mark.asyncio
async def test_sweeper_handles_mixed_capabilities() -> None:
    fresh = _cap(capability_id="cp-fresh")
    stale = _cap(
        capability_id="cp-stale",
        last_state_change_at=datetime.now(UTC) - timedelta(days=10),
    )
    expired = _cap(
        capability_id="cp-old",
        promotion_deadline=datetime.now(UTC) - timedelta(days=2),
    )

    async def reader() -> list[dict[str, Any]]:
        return [fresh, stale, expired]

    async def search_emitter(payload: dict[str, Any]) -> None:
        pass

    async def state_writer(cap_id: str, payload: dict[str, Any]) -> None:
        pass

    sweeper = PromotionTimeoutSweeper(
        capability_reader=reader,
        state_writer=state_writer,
        search_emitter=search_emitter,
    )
    report = await sweeper.sweep()
    assert report["scanned"] == 3
    assert report["expired"] == 1
    assert report["stale"] == 1


@pytest.mark.asyncio
async def test_sweeper_reader_exception_returns_error_report() -> None:
    async def bad_reader() -> list[dict[str, Any]]:
        raise RuntimeError("DB down")

    sweeper = PromotionTimeoutSweeper(capability_reader=bad_reader)
    report = await sweeper.sweep()
    assert report["scanned"] == 0
    assert "error" in report
    assert "DB down" in report["error"]


@pytest.mark.asyncio
async def test_sweeper_state_writer_exception_swallowed() -> None:
    async def reader() -> list[dict[str, Any]]:
        return [
            _cap(
                capability_id="cp-old",
                promotion_deadline=datetime.now(UTC) - timedelta(days=2),
            )
        ]

    async def bad_writer(cap_id: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("write failed")

    sweeper = PromotionTimeoutSweeper(
        capability_reader=reader, state_writer=bad_writer
    )
    # 不抛 — 报错 swallow + log
    report = await sweeper.sweep()
    assert report["expired"] == 1


@pytest.mark.asyncio
async def test_sweeper_search_emitter_exception_swallowed() -> None:
    async def reader() -> list[dict[str, Any]]:
        return [
            _cap(
                capability_id="cp-old",
                promotion_deadline=datetime.now(UTC) - timedelta(days=2),
            )
        ]

    async def bad_emitter(payload: dict[str, Any]) -> None:
        raise RuntimeError("emit failed")

    sweeper = PromotionTimeoutSweeper(
        capability_reader=reader, search_emitter=bad_emitter
    )
    report = await sweeper.sweep()
    # 仍报 expired (state writer 不挂; search_emitter 异常不破坏 sweep)
    assert report["expired"] == 1


def test_check_result_dataclass_fields() -> None:
    result = evaluate_capability_timeout(_cap())
    assert isinstance(result, TimeoutCheckResult)
    assert hasattr(result, "capability_id")
    assert hasattr(result, "expired")
    assert hasattr(result, "stale")
    assert hasattr(result, "recommended_action")
