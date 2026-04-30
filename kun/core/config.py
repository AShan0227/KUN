"""Application settings loaded from environment (pydantic-settings)."""

from __future__ import annotations

from functools import cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """KUN runtime settings loaded from .env / environment."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="KUN_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Deployment
    env: Literal["dev", "staging", "production"] = "dev"
    log_level: str = "INFO"
    default_tenant_id: str | None = "u-sylvan"
    auth_secret: str | None = None
    # Optional comma-separated secret list for zero-downtime rotation.  The
    # first secret is used by operators to mint new tokens; all listed secrets
    # are accepted for verification.
    auth_secrets: str | None = None
    self_signup_enabled: bool = False
    self_signup_invite_code: str | None = None

    @field_validator("default_tenant_id", mode="before")
    @classmethod
    def _blank_default_tenant_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    # Postgres
    pg_dsn: str = "postgresql+asyncpg://kun_app:kun_app@localhost:55432/kun"
    pg_admin_dsn: str = "postgresql+asyncpg://kun:kun@localhost:55432/kun"
    pg_pool_size: int = 10

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Qdrant
    qdrant_url: str = "http://127.0.0.1:16333"
    qdrant_api_key: str | None = None

    # NATS
    nats_url: str = "nats://localhost:4222"

    # S3 / MinIO
    s3_endpoint: str = "http://localhost:19000"
    s3_access_key: str = "minio"
    s3_secret_key: str = "minio123"
    s3_bucket: str = "kun-artifacts"
    s3_region: str = "us-east-1"

    # LLM
    ofox_proxy_url: str = "https://api.ofox.ai"
    ofox_api_key: str | None = None

    # Budgets (ADR-008)
    budget_daily_usd: float = Field(default=10.0)
    budget_monthly_usd: float = Field(default=200.0)
    # Soft warn threshold as fraction of daily budget. orchestrator emits a
    # warning event before hitting the hard cap.
    budget_warn_fraction: float = Field(default=0.8)

    # Task hard ceiling — orchestrator cancels and emits task.timed_out when
    # a single task runs longer than this. Per-task TaskProfile.max_duration_sec
    # overrides this. Set generously up front (30 min); idle-batch is meant to
    # learn realistic per-task-type defaults later.
    task_max_duration_sec: int = Field(default=1800)

    # MinIO / object storage offload threshold. Task result_json over this
    # size is stored in MinIO and a reference kept in DB instead.
    result_offload_threshold_bytes: int = Field(default=51200)  # 50 KiB

    # Proactive tool learning. When the same (tenant, skill, pattern) is
    # missed this many times, watchtower promotes it into the learned trigger
    # table and emits proactive.trigger_promoted.
    missed_tool_threshold: int = Field(default=10, ge=1)

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_cors_origins: str = "http://localhost:3000,http://localhost:3001,http://localhost:3002"

    def production_safety_issues(self) -> list[str]:
        """Return deployment blockers that make a production KUN unsafe."""

        issues: list[str] = []
        if self.env != "production":
            return issues
        if self.default_tenant_id:
            issues.append("KUN_DEFAULT_TENANT_ID must be blank in production")
        if not self.auth_secret_candidates():
            issues.append(
                "KUN_AUTH_SECRET or KUN_AUTH_SECRETS must contain at least one 32+ character secret"
            )
        if self.self_signup_enabled and not (self.self_signup_invite_code or "").strip():
            issues.append("KUN_SELF_SIGNUP_INVITE_CODE is required when self signup is enabled")
        if "kun:kun@" in self.pg_dsn:
            issues.append("KUN_PG_DSN must use the non-admin app role in production")
        if self.s3_access_key == "minio" or self.s3_secret_key == "minio123":
            issues.append("S3/MinIO default credentials must be changed in production")
        return issues

    def auth_secret_candidates(self) -> list[str]:
        """Return active auth secrets, newest/primary first."""

        secrets: list[str] = []
        for raw in (self.auth_secret, self.auth_secrets):
            if not raw:
                continue
            secrets.extend(item.strip() for item in raw.split(",") if item.strip())
        deduped: list[str] = []
        seen: set[str] = set()
        for secret in secrets:
            if len(secret) < 32 or secret in seen:
                continue
            deduped.append(secret)
            seen.add(secret)
        return deduped


@cache
def settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
