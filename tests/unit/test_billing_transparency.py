"""计费透明 API 测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from kun.api.billing_transparency import (
    BillingAuditEntry,
    UpcomingBillingChange,
    record_billing_audit,
    reset_billing_transparency_state,
    router,
    set_upcoming_billing_changes,
)


@pytest.fixture
def client() -> TestClient:
    reset_billing_transparency_state()
    app = FastAPI()
    app.include_router(router, prefix="/api/billing")
    return TestClient(app)


def _headers(user_id: str = "u-1", tenant_id: str = "t-1") -> dict[str, str]:
    return {"X-User-Id": user_id, "X-Tenant-Id": tenant_id}


def _charge(
    *,
    entry_id: str = "bill-1",
    tenant_id: str = "t-1",
    user_id: str = "u-1",
    amount_usd: float = 1.25,
    saved_usd: float = 0.5,
    occurred_at: datetime | None = None,
) -> BillingAuditEntry:
    return BillingAuditEntry(
        entry_id=entry_id,
        tenant_id=tenant_id,
        user_id=user_id,
        occurred_at=occurred_at or datetime.now(UTC),
        kind="charge",
        amount_usd=amount_usd,
        saved_usd=saved_usd,
        reason="task execution",
        task_id="task-1",
    )


def test_promise_contains_core_commitments(client: TestClient) -> None:
    resp = client.get("/api/billing/promise")

    assert resp.status_code == 200
    body = resp.json()
    text = " ".join(body["commitments"])
    assert body["version"] == "ADR-022"
    assert body["notice_window_days"] == 30
    assert "30 天" in text
    assert "余额" in text
    assert "audit log" in text
    assert "寒暄" in text
    assert "退款" in text


def test_audit_log_returns_only_current_user_entries(client: TestClient) -> None:
    record_billing_audit(_charge(entry_id="mine"))
    record_billing_audit(_charge(entry_id="other-user", user_id="u-2"))
    record_billing_audit(_charge(entry_id="other-tenant", tenant_id="t-2"))

    resp = client.get("/api/billing/audit-log", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["entry_count"] == 1
    assert body["entries"][0]["entry_id"] == "mine"


def test_dashboard_sums_today_month_and_saved(client: TestClient) -> None:
    record_billing_audit(_charge(entry_id="today", amount_usd=2.0, saved_usd=0.7))
    record_billing_audit(
        _charge(
            entry_id="old",
            amount_usd=3.0,
            saved_usd=0.2,
            occurred_at=datetime.now(UTC) - timedelta(days=10),
        ),
    )

    resp = client.get("/api/billing/dashboard", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["used_today"] == 2.0
    assert body["used_month"] == 5.0
    assert body["saved_by_kun"] == 0.9
    assert body["refundable_balance"] == 5.0


def test_dashboard_empty_state_is_zero(client: TestClient) -> None:
    resp = client.get("/api/billing/dashboard", headers=_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["used_today"] == 0
    assert body["used_month"] == 0
    assert body["saved_by_kun"] == 0
    assert body["audit_entry_count"] == 0


def test_refund_request_records_negative_audit_entry(client: TestClient) -> None:
    record_billing_audit(_charge(amount_usd=4.0))

    resp = client.post(
        "/api/billing/refund-request",
        json={"amount_usd": 2.5, "reason": "结果不满意"},
        headers=_headers(),
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "received"
    assert body["amount_usd"] == 2.5

    audit = client.get("/api/billing/audit-log", headers=_headers()).json()
    assert audit["entry_count"] == 2
    assert audit["entries"][1]["kind"] == "refund"
    assert audit["entries"][1]["amount_usd"] == -2.5


def test_refund_request_rejects_amount_above_refundable_balance(client: TestClient) -> None:
    record_billing_audit(_charge(amount_usd=1.0))

    resp = client.post(
        "/api/billing/refund-request",
        json={"amount_usd": 2.0, "reason": "too much"},
        headers=_headers(),
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["refundable_balance"] == 1.0


def test_upcoming_changes_default_empty(client: TestClient) -> None:
    resp = client.get("/api/billing/upcoming-changes")

    assert resp.status_code == 200
    body = resp.json()
    assert body["notice_window_days"] == 30
    assert body["changes"] == []


def test_upcoming_changes_only_returns_next_30_days(client: TestClient) -> None:
    now = datetime.now(UTC)
    set_upcoming_billing_changes(
        [
            UpcomingBillingChange(
                change_id="soon",
                title="套餐价格调整",
                effective_at=now + timedelta(days=10),
                impact_summary="所有用户提前 30 天可见",
            ),
            UpcomingBillingChange(
                change_id="late",
                title="太远的变化",
                effective_at=now + timedelta(days=45),
                impact_summary="暂不展示",
            ),
        ],
    )

    resp = client.get("/api/billing/upcoming-changes")

    assert resp.status_code == 200
    body = resp.json()
    assert body["change_count"] == 1
    assert body["changes"][0]["change_id"] == "soon"


def test_frontend_billing_page_contains_required_sections() -> None:
    page = Path("frontend/src/app/billing/page.tsx").read_text(encoding="utf-8")

    assert "30 天预告" in page
    assert "余额永不蒸发" in page
    assert "自助退款" in page
    assert "寒暄不计费" in page
    assert "Audit log" in page
