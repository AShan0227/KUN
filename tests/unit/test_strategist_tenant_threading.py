"""Strategist threads the request tenant into quota / exploration penalty (audit F146).

Both were hardcoded to tenant_id="default", collapsing multi-tenant rate-limiting
and failure-penalty into one global bucket. They now use request["tenant_id"].
"""

from __future__ import annotations

import pytest
from kun.agents.strategist.service import StrategistService


class _CaptureQuota:
    def __init__(self) -> None:
        self.seen_tenants: list[str] = []

    async def check_and_record_experiment(self, *, tenant_id: str, experiment_id: str):
        self.seen_tenants.append(tenant_id)

        class _R:
            allowed = True
            reason = ""

        return _R()


class _CapturePenalty:
    def __init__(self) -> None:
        self.seen_tenants: list[str] = []

    async def filter_candidates(self, *, tenant_id: str, candidates):
        self.seen_tenants.append(tenant_id)
        return candidates


def _request(tenant_id: str | None) -> dict:
    req = {
        "request_id": "ss-1",
        "anomaly_kind": "llm_fallback_spike",
        "target_module": "llm.router",
        "evidence": [
            {
                "type": "fallback_count",
                "count": 5,
                "primary_provider": "anthropic",
                "primary_model": "claude-opus-4-7",
                "fallback_provider": "openai",
            }
        ],
    }
    if tenant_id is not None:
        req["tenant_id"] = tenant_id
    return req


@pytest.mark.unit
@pytest.mark.asyncio
async def test_real_tenant_reaches_quota_and_penalty() -> None:
    quota, penalty = _CaptureQuota(), _CapturePenalty()
    svc = StrategistService(resource_quota=quota, exploration_penalty=penalty)
    candidates = await svc.propose_candidates(_request("u-test"))
    assert candidates  # produced some candidates
    assert penalty.seen_tenants == ["u-test"]
    assert quota.seen_tenants and all(t == "u-test" for t in quota.seen_tenants)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_tenant_falls_back_to_default() -> None:
    quota, penalty = _CaptureQuota(), _CapturePenalty()
    svc = StrategistService(resource_quota=quota, exploration_penalty=penalty)
    await svc.propose_candidates(_request(None))
    assert penalty.seen_tenants == ["default"]
    assert all(t == "default" for t in quota.seen_tenants)
