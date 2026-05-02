from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from kun.security.auth import (
    AuthTokenError,
    sign_auth_token,
    verify_bearer_token,
    verify_bearer_token_any,
)


@pytest.mark.unit
def test_signed_auth_token_roundtrip() -> None:
    secret = "x" * 40
    token = sign_auth_token(
        {
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "scopes": ["world:approve", "world:dispatch"],
            "audience": "novice",
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        secret,
    )

    claims = verify_bearer_token(f"Bearer {token}", secret)

    assert claims.tenant_id == "tenant-a"
    assert claims.user_id == "user-a"
    assert claims.scopes == ("world:approve", "world:dispatch")
    assert claims.audience == "novice"


@pytest.mark.unit
def test_signed_auth_token_rejects_bad_signature() -> None:
    token = sign_auth_token({"tenant_id": "tenant-a"}, "x" * 40)

    with pytest.raises(AuthTokenError, match="signature"):
        verify_bearer_token(f"Bearer {token}", "y" * 40)


@pytest.mark.unit
def test_signed_auth_token_rejects_expired() -> None:
    token = sign_auth_token(
        {
            "tenant_id": "tenant-a",
            "exp": int((datetime.now(UTC) - timedelta(minutes=1)).timestamp()),
        },
        "x" * 40,
    )

    with pytest.raises(AuthTokenError, match="expired"):
        verify_bearer_token(f"Bearer {token}", "x" * 40)


@pytest.mark.unit
def test_verify_bearer_token_any_accepts_rotation_secret() -> None:
    old_secret = "old-" + "x" * 40
    new_secret = "new-" + "y" * 40
    token = sign_auth_token({"tenant_id": "tenant-rotate"}, old_secret)

    claims = verify_bearer_token_any(f"Bearer {token}", [new_secret, old_secret])

    assert claims.tenant_id == "tenant-rotate"
