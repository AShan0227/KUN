"""Application settings loaded from environment (pydantic-settings)."""

from __future__ import annotations

from functools import cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Defaults we tolerate in dev but must NEVER reach production.
_DEV_DEFAULT_PG_ADMIN_DSN = "postgresql+asyncpg://kun:kun@localhost:55432/kun"
_DEV_DEFAULT_S3_ACCESS_KEY = "minio"
_DEV_DEFAULT_S3_SECRET_KEY = "minio123"


class InsecureProductionConfigError(RuntimeError):
    """Raised at startup when production config still uses dev defaults."""


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

    @field_validator("default_tenant_id", mode="before")
    @classmethod
    def _blank_default_tenant_to_none(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    # Postgres
    pg_dsn: str = "postgresql+asyncpg://kun_app:kun_app@localhost:55432/kun"
    pg_admin_dsn: str = _DEV_DEFAULT_PG_ADMIN_DSN
    pg_pool_size: int = 10

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Qdrant
    qdrant_url: str = "http://localhost:16333"
    qdrant_api_key: str | None = None

    # NATS
    nats_url: str = "nats://localhost:4222"

    # S3 / MinIO
    s3_endpoint: str = "http://localhost:19000"
    s3_access_key: str = _DEV_DEFAULT_S3_ACCESS_KEY
    s3_secret_key: str = _DEV_DEFAULT_S3_SECRET_KEY
    s3_bucket: str = "kun-artifacts"
    s3_region: str = "us-east-1"

    # LLM
    ofox_proxy_url: str = "https://api.ofox.ai"
    ofox_api_key: str | None = None

    # External Supervisor (ADR-023) — 本地推理引擎 (ollama / llama.cpp / vLLM)
    external_supervisor_enabled: bool = Field(default=False)
    external_supervisor_model_id: str = Field(default="qwen2.5:32b")
    external_supervisor_base_url: str = Field(default="http://localhost:11434/v1")
    external_supervisor_api_key: str = Field(default="ollama")
    external_supervisor_timeout_sec: float = Field(default=120.0)
    external_supervisor_max_concurrent: int = Field(default=2, ge=1)

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
    api_cors_origins: str = "http://localhost:3000"

    # Auth (ADR-019 中期 posture). Default OFF — fall back to default_tenant_id.
    # Flag flip = production switch. See kun/api/auth/ for the runtime.
    auth_enabled: bool = Field(default=False)
    auth_jwt_secret: str | None = Field(default=None)
    auth_token_ttl_seconds: int = Field(default=3600, ge=60)

    @field_validator("api_cors_origins")
    @classmethod
    def _validate_cors_origins(cls, v: str) -> str:
        """Reject wildcard with credentials enabled (CSRF foothold)."""
        origins = [o.strip() for o in v.split(",") if o.strip()]
        if "*" in origins:
            raise ValueError(
                "api_cors_origins must not contain '*' — combined with "
                "allow_credentials=True this would enable CSRF. List exact origins."
            )
        return v

    @model_validator(mode="after")
    def _auth_consistency(self) -> Settings:
        """Auth enabled requires a strong JWT secret."""
        if self.auth_enabled:
            if not self.auth_jwt_secret:
                raise ValueError(
                    "KUN_AUTH_ENABLED=true but KUN_AUTH_JWT_SECRET is unset"
                )
            if len(self.auth_jwt_secret) < 32:
                raise ValueError(
                    "KUN_AUTH_JWT_SECRET must be at least 32 characters "
                    "(use `python -c 'import secrets; print(secrets.token_urlsafe(48))'`)"
                )
        return self

    @model_validator(mode="after")
    def _production_safety(self) -> Settings:
        """In production refuse to start with dev defaults still in place."""
        if self.env != "production":
            return self
        violations: list[str] = []
        if self.pg_admin_dsn == _DEV_DEFAULT_PG_ADMIN_DSN:
            violations.append("KUN_PG_ADMIN_DSN is the dev default 'kun:kun@...'")
        if self.s3_access_key == _DEV_DEFAULT_S3_ACCESS_KEY:
            violations.append("KUN_S3_ACCESS_KEY is the dev default 'minio'")
        if self.s3_secret_key == _DEV_DEFAULT_S3_SECRET_KEY:
            violations.append("KUN_S3_SECRET_KEY is the dev default 'minio123'")
        if self.default_tenant_id is not None:
            violations.append(
                f"KUN_DEFAULT_TENANT_ID={self.default_tenant_id!r} — must be unset "
                "in production so missing X-Tenant-Id fails closed"
            )
        if violations:
            joined = "\n  - ".join(violations)
            raise InsecureProductionConfigError(
                f"KUN_ENV=production but insecure defaults remain:\n  - {joined}"
            )
        return self


@cache
def settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
