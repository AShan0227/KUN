"""dev / staging / prod 物理隔离守门器 (BATCH4 C4 / T54)."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Literal

from kun.core.config import settings

EnvironmentName = Literal["dev", "staging", "prod"]
ApprovalChecker = Callable[[str, EnvironmentName, EnvironmentName], bool]


class EnvIsolationError(RuntimeError):
    """环境隔离配置错误."""


class EnvIsolation:
    """统一管理各环境 DB / ObjectStore 资源和跨环境权限."""

    def __init__(
        self,
        *,
        db_urls: dict[EnvironmentName, str | None] | None = None,
        object_store_buckets: dict[EnvironmentName, str | None] | None = None,
        approval_checker: ApprovalChecker | None = None,
    ) -> None:
        self._settings = settings()
        self._db_urls = db_urls or self._load_db_urls()
        self._object_store_buckets = object_store_buckets or self._load_object_store_buckets()
        self._approval_checker = approval_checker

    def get_db_url(self, env: EnvironmentName) -> str:
        """各 env 独立 DB connection. staging/prod 必须显式配置."""

        return self._require_resource(self._db_urls.get(env), env, "db")

    def get_object_store_bucket(self, env: EnvironmentName) -> str:
        """各 env 独立对象存储 bucket. staging/prod 必须显式配置."""

        return self._require_resource(self._object_store_buckets.get(env), env, "bucket")

    def can_cross_env(self, from_env: EnvironmentName, to_env: EnvironmentName, user_id: str) -> bool:
        """跨 env 操作默认拒绝; 同环境允许; 双人审批可通过 checker 放行."""

        if from_env == to_env:
            return True
        if self._approval_checker is None:
            return False
        return self._approval_checker(user_id, from_env, to_env)

    def validate_isolation(self) -> list[str]:
        """扫描资源是否被多个环境复用. 返回问题列表."""

        issues: list[str] = []
        issues.extend(_duplicate_resource_issues("db", self._db_urls))
        issues.extend(_duplicate_resource_issues("bucket", self._object_store_buckets))
        return issues

    def _load_db_urls(self) -> dict[EnvironmentName, str | None]:
        return {
            "dev": _env("KUN_DEV_PG_DSN") or self._settings.pg_dsn,
            "staging": _env("KUN_STAGING_PG_DSN"),
            "prod": _env("KUN_PROD_PG_DSN"),
        }

    def _load_object_store_buckets(self) -> dict[EnvironmentName, str | None]:
        return {
            "dev": _env("KUN_DEV_S3_BUCKET") or self._settings.s3_bucket,
            "staging": _env("KUN_STAGING_S3_BUCKET"),
            "prod": _env("KUN_PROD_S3_BUCKET"),
        }

    @staticmethod
    def _require_resource(resource: str | None, env: EnvironmentName, kind: str) -> str:
        value = (resource or "").strip()
        if not value:
            raise EnvIsolationError(f"{env} {kind} resource is not configured")
        return value


def _env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _duplicate_resource_issues(
    kind: str,
    resources: dict[EnvironmentName, str | None],
) -> list[str]:
    seen: dict[str, EnvironmentName] = {}
    issues: list[str] = []
    for env, raw_value in resources.items():
        value = (raw_value or "").strip()
        if not value:
            continue
        existing = seen.get(value)
        if existing is not None:
            issues.append(f"{kind} resource reused by {existing} and {env}: {value}")
        else:
            seen[value] = env
    return issues
