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
    embedding_provider: Literal["local", "openai", "voyage", "qdrant_fastembed"] = "local"
    embedding_model: str | None = None
    embedding_timeout_sec: float = 6.0

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

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    api_cors_origins: str = "http://localhost:3000"


@cache
def settings() -> Settings:
    """Cached settings singleton."""
    return Settings()
