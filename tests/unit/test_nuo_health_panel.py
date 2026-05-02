from __future__ import annotations

import pytest
from fastapi import HTTPException
from kun.api.nuo import health_panel
from kun.core.tenancy import TenantContext, tenant_scope
from kun.engineering.nuo_system_health import (
    GovernanceApplyBlockedReason,
    GovernanceRecommendationApplyResult,
)


@pytest.mark.asyncio
async def test_governance_apply_api_uses_current_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def fake_apply_governance_recommendation(**kwargs) -> GovernanceRecommendationApplyResult:
        calls.append(kwargs)
        return GovernanceRecommendationApplyResult(
            status="dry_run",
            applied=False,
            dry_run=True,
            blocked=False,
            recommendation_id=str(kwargs["recommendation_id"]),
            risk_level="low",
            message="Dry-run completed for context maintenance; no state was changed.",
        )

    monkeypatch.setattr(
        health_panel,
        "apply_governance_recommendation",
        fake_apply_governance_recommendation,
    )

    with tenant_scope(TenantContext(tenant_id="tenant-a", user_id="owner-a")):
        result = await health_panel.apply_governance_recommendation_once(
            recommendation_id="govern:context_slimming_candidates",
            dry_run=True,
            max_assets=25,
        )

    assert result.status == "dry_run"
    assert result.recommendation_id == "govern:context_slimming_candidates"
    assert calls == [
        {
            "tenant_id": "tenant-a",
            "recommendation_id": "govern:context_slimming_candidates",
            "dry_run": True,
            "max_assets": 25,
        }
    ]


@pytest.mark.asyncio
async def test_governance_apply_api_returns_404_for_missing_recommendation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_apply_governance_recommendation(**kwargs) -> GovernanceRecommendationApplyResult:
        return GovernanceRecommendationApplyResult(
            status="blocked",
            applied=False,
            dry_run=False,
            blocked=True,
            recommendation_id=str(kwargs["recommendation_id"]),
            risk_level="unknown",
            message="Governance recommendation 'govern:missing' is not in the current queue.",
            blocked_reason="recommendation_not_found",
            blocked_reasons=[
                GovernanceApplyBlockedReason(
                    code="recommendation_not_found",
                    detail=(
                        "Collect a fresh NUO health report and apply an existing recommendation_id."
                    ),
                )
            ],
        )

    monkeypatch.setattr(
        health_panel,
        "apply_governance_recommendation",
        fake_apply_governance_recommendation,
    )

    with (
        tenant_scope(TenantContext(tenant_id="tenant-a", user_id="owner-a")),
        pytest.raises(HTTPException) as exc,
    ):
        await health_panel.apply_governance_recommendation_once(
            recommendation_id="govern:missing",
            dry_run=False,
            max_assets=25,
        )

    assert exc.value.status_code == 404
    assert exc.value.detail["status"] == "blocked"
    assert exc.value.detail["recommendation_id"] == "govern:missing"
    assert exc.value.detail["risk_level"] == "unknown"
    assert exc.value.detail["blocked_reason"] == "recommendation_not_found"
