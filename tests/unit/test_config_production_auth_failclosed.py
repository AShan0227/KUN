"""Production refuses to start with auth disabled (audit F007a).

With auth off the API trusts an unverified X-Tenant-Id header, so any caller can
claim any tenant. Production must fail closed — _production_safety now treats
KUN_AUTH_ENABLED=false as an insecure-config violation.
"""

from __future__ import annotations

import pytest
from kun.core.config import InsecureProductionConfigError, Settings

# Non-dev values so the *other* production-safety checks pass and we isolate auth.
_PROD_SAFE = {
    "pg_admin_dsn": "postgresql+asyncpg://real:real@db.internal/kun",
    "s3_access_key": "real-access-key",
    "s3_secret_key": "real-secret-key",
    "default_tenant_id": None,
}
_STRONG_SECRET = "x" * 48


@pytest.mark.unit
def test_production_with_auth_off_fails_closed() -> None:
    with pytest.raises(InsecureProductionConfigError, match="KUN_AUTH_ENABLED"):
        Settings(env="production", auth_enabled=False, **_PROD_SAFE)


@pytest.mark.unit
def test_production_with_auth_on_is_allowed() -> None:
    s = Settings(
        env="production",
        auth_enabled=True,
        auth_jwt_secret=_STRONG_SECRET,
        **_PROD_SAFE,
    )
    assert s.env == "production"
    assert s.auth_enabled is True


@pytest.mark.unit
def test_dev_with_auth_off_is_fine() -> None:
    # The fail-closed rule only applies to production; dev defaults still work.
    s = Settings(env="dev", auth_enabled=False)
    assert s.auth_enabled is False
