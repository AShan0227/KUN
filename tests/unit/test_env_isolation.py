"""EnvIsolation 单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from kun.core.env_isolation import EnvIsolation, EnvIsolationError


def test_dev_db_uses_explicit_config() -> None:
    iso = EnvIsolation(db_urls={"dev": "postgres://dev", "staging": None, "prod": None})

    assert iso.get_db_url("dev") == "postgres://dev"


def test_prod_db_missing_raises() -> None:
    iso = EnvIsolation(db_urls={"dev": "postgres://dev", "staging": None, "prod": None})

    with pytest.raises(EnvIsolationError):
        iso.get_db_url("prod")


def test_prod_db_explicit_config_is_returned() -> None:
    iso = EnvIsolation(
        db_urls={
            "dev": "postgres://dev",
            "staging": "postgres://staging",
            "prod": "postgres://prod",
        },
    )

    assert iso.get_db_url("prod") == "postgres://prod"


def test_object_store_bucket_is_env_specific() -> None:
    iso = EnvIsolation(
        object_store_buckets={
            "dev": "kun-dev-artifacts",
            "staging": "kun-staging-artifacts",
            "prod": "kun-prod-artifacts",
        },
    )

    assert iso.get_object_store_bucket("dev") == "kun-dev-artifacts"
    assert iso.get_object_store_bucket("prod") == "kun-prod-artifacts"


def test_missing_prod_bucket_raises() -> None:
    iso = EnvIsolation(object_store_buckets={"dev": "kun-dev", "staging": None, "prod": None})

    with pytest.raises(EnvIsolationError):
        iso.get_object_store_bucket("prod")


def test_same_env_operation_is_allowed_without_approval() -> None:
    iso = EnvIsolation()

    assert iso.can_cross_env("dev", "dev", "u-1") is True


def test_cross_env_operation_is_rejected_by_default() -> None:
    iso = EnvIsolation()

    assert iso.can_cross_env("dev", "prod", "u-1") is False


def test_cross_env_operation_can_be_approved() -> None:
    def checker(user_id: str, from_env: str, to_env: str) -> bool:
        return user_id == "admin" and from_env == "staging" and to_env == "prod"

    iso = EnvIsolation(approval_checker=checker)

    assert iso.can_cross_env("staging", "prod", "admin") is True
    assert iso.can_cross_env("dev", "prod", "admin") is False


def test_validate_isolation_detects_shared_db() -> None:
    iso = EnvIsolation(
        db_urls={
            "dev": "postgres://shared",
            "staging": "postgres://shared",
            "prod": "postgres://prod",
        },
    )

    issues = iso.validate_isolation()

    assert any("db resource reused" in issue for issue in issues)


def test_validate_isolation_detects_shared_bucket() -> None:
    iso = EnvIsolation(
        object_store_buckets={
            "dev": "same-bucket",
            "staging": "stage-bucket",
            "prod": "same-bucket",
        },
    )

    issues = iso.validate_isolation()

    assert any("bucket resource reused" in issue for issue in issues)


def test_validate_isolation_passes_for_distinct_resources() -> None:
    iso = EnvIsolation(
        db_urls={
            "dev": "postgres://dev",
            "staging": "postgres://staging",
            "prod": "postgres://prod",
        },
        object_store_buckets={
            "dev": "dev-bucket",
            "staging": "staging-bucket",
            "prod": "prod-bucket",
        },
    )

    assert iso.validate_isolation() == []


def test_compose_marks_stateful_services_as_prod_isolated() -> None:
    compose = Path("docker-compose.dev.yml").read_text(encoding="utf-8")

    assert "com.kun.environment" in compose
    assert "com.kun.prod-isolated" in compose
