"""Tenant onboarding pack for the current HMAC auth slice.

This is not a full account system.  It gives an operator a safe starter token,
scopes, and smoke commands for a tenant while the proper SaaS account layer is
still pending.
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kun.security.auth import sign_auth_token

Audience = Literal["novice", "developer", "expert"]


class TenantOnboardingPack(BaseModel):
    """Operator-facing tenant bootstrap output."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    user_id: str | None = None
    audience: Audience = "developer"
    scopes: list[str] = Field(default_factory=list)
    expires_at: int
    bearer_token: str
    curl_examples: list[str] = Field(default_factory=list)
    missing_full_product: list[str] = Field(default_factory=list)


def create_tenant_onboarding_pack(
    *,
    tenant_id: str,
    secret: str,
    user_id: str | None = None,
    scopes: list[str] | None = None,
    audience: Audience = "developer",
    ttl_sec: int = 86400,
    api_origin: str = "http://localhost:8000",
) -> TenantOnboardingPack:
    """Create a signed-token starter pack for one tenant."""

    cleaned_tenant = tenant_id.strip()
    if not cleaned_tenant:
        raise ValueError("tenant_id is required")
    if len(secret) < 32:
        raise ValueError("secret must be at least 32 characters")
    cleaned_scopes = [scope.strip() for scope in (scopes or []) if scope.strip()]
    expires_at = int(time.time()) + ttl_sec
    token = sign_auth_token(
        {
            "tenant_id": cleaned_tenant,
            "user_id": user_id,
            "scopes": cleaned_scopes,
            "audience": audience,
            "exp": expires_at,
        },
        secret,
    )
    return TenantOnboardingPack(
        tenant_id=cleaned_tenant,
        user_id=user_id,
        audience=audience,
        scopes=cleaned_scopes,
        expires_at=expires_at,
        bearer_token=token,
        curl_examples=[
            _curl(api_origin, token, "/health/ready"),
            _curl(api_origin, token, "/nuo/health/summary"),
            _curl(api_origin, token, "/nuo/health/delivery-status"),
        ],
        missing_full_product=[
            "这不是完整账号体系，还没有注册/登录/组织成员/账单。",
            "这不是集中密钥管理，token 轮换仍由运维手动执行。",
            "这只是让 production API 不再信任裸 X-Tenant-Id 的安全启动包。",
        ],
    )


def _curl(api_origin: str, token: str, path: str) -> str:
    return f'curl -H "Authorization: Bearer {token}" {api_origin.rstrip("/")}{path}'


__all__ = ["TenantOnboardingPack", "create_tenant_onboarding_pack"]
