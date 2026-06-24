"""L6.AuthScaffold — config auth validator 单测."""

from __future__ import annotations

import pytest
from kun.core.config import Settings


def test_auth_disabled_by_default() -> None:
    s = Settings()
    assert s.auth_enabled is False
    assert s.auth_jwt_secret is None


def test_auth_enabled_requires_secret() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(auth_enabled=True, auth_jwt_secret=None)


def test_auth_enabled_rejects_short_secret() -> None:
    with pytest.raises(ValueError, match="32 characters"):
        Settings(auth_enabled=True, auth_jwt_secret="short_secret_lol")


def test_auth_enabled_accepts_strong_secret() -> None:
    s = Settings(
        auth_enabled=True,
        auth_jwt_secret="X" * 48,
    )
    assert s.auth_enabled is True
    assert s.auth_jwt_secret == "X" * 48


def test_auth_token_ttl_default() -> None:
    s = Settings()
    assert s.auth_token_ttl_seconds == 3600


def test_auth_token_ttl_min_enforced() -> None:
    with pytest.raises(ValueError):
        Settings(auth_token_ttl_seconds=10)  # below 60s floor
