"""NUO-facing runtime secret and external-handler configuration audit.

This is not a secret manager.  It is the honest preflight/NUO view that tells
operators which credentials are missing, default, or too risky for production.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field

from kun.core.config import Settings, settings
from kun.world.handler_health import EXPECTED_REAL_WORLD_HANDLERS

AuditSeverity = Literal["ok", "warn", "blocker"]
AuditArea = Literal[
    "tenant",
    "auth",
    "database",
    "storage",
    "world_gateway",
    "llm",
]


class SecretAuditItem(BaseModel):
    """One NUO/operator-facing configuration finding."""

    model_config = ConfigDict(extra="forbid")

    item_id: str
    area: AuditArea
    severity: AuditSeverity
    title: str
    detail: str
    suggested_action: str = ""
    env_vars: list[str] = Field(default_factory=list)


class SecretAuditReport(BaseModel):
    """Aggregated runtime secret/configuration audit."""

    model_config = ConfigDict(extra="forbid")

    env: str
    status: Literal["pass", "warn", "block"]
    items: list[SecretAuditItem] = Field(default_factory=list)
    summary: dict[str, int] = Field(default_factory=dict)

    @property
    def blockers(self) -> list[SecretAuditItem]:
        return [item for item in self.items if item.severity == "blocker"]

    @property
    def warnings(self) -> list[SecretAuditItem]:
        return [item for item in self.items if item.severity == "warn"]


def audit_runtime_secrets(
    *,
    cfg: Settings | None = None,
    environ: Mapping[str, str] | None = None,
) -> SecretAuditReport:
    """Audit deploy-time secrets and external handler configuration.

    Values are never returned.  The report only names env vars and the class of
    problem, so it is safe to show in NUO or CI logs.
    """

    active = cfg or settings()
    env = dict(os.environ if environ is None else environ)
    items: list[SecretAuditItem] = []
    items.extend(_tenant_items(active))
    items.extend(_auth_items(active))
    items.extend(_database_items(active))
    items.extend(_storage_items(active))
    items.extend(_world_gateway_items(env))
    items.extend(_llm_items(env))

    summary: dict[str, int] = {"ok": 0, "warn": 0, "blocker": 0}
    for item in items:
        summary[item.severity] += 1
    if summary["blocker"]:
        status: Literal["pass", "warn", "block"] = "block"
    elif summary["warn"]:
        status = "warn"
    else:
        status = "pass"
    return SecretAuditReport(env=active.env, status=status, items=items, summary=summary)


def _tenant_items(cfg: Settings) -> list[SecretAuditItem]:
    if cfg.env == "production" and cfg.default_tenant_id:
        return [
            SecretAuditItem(
                item_id="tenant.default_tenant_enabled",
                area="tenant",
                severity="blocker",
                title="生产环境不能使用默认租户兜底",
                detail="KUN_DEFAULT_TENANT_ID 仍然有值；忘记鉴权的路径会被静默塞进默认租户。",
                suggested_action="生产环境把 KUN_DEFAULT_TENANT_ID 设为空；没有租户就直接拒绝请求。",
                env_vars=["KUN_DEFAULT_TENANT_ID"],
            )
        ]
    return [
        SecretAuditItem(
            item_id="tenant.default_tenant_policy",
            area="tenant",
            severity="ok" if cfg.env == "production" else "warn",
            title="租户兜底策略已检查",
            detail=(
                "生产环境未启用默认租户兜底。"
                if cfg.env == "production"
                else "当前是本地/预发环境，允许默认租户便于开发；不能代表生产安全。"
            ),
            suggested_action="" if cfg.env == "production" else "正式部署前关闭默认租户兜底。",
            env_vars=["KUN_DEFAULT_TENANT_ID"],
        )
    ]


def _auth_items(cfg: Settings) -> list[SecretAuditItem]:
    raw_values = [item for item in (cfg.auth_secret, cfg.auth_secrets) if item]
    candidates = cfg.auth_secret_candidates()
    items: list[SecretAuditItem] = []
    if not candidates:
        items.append(
            SecretAuditItem(
                item_id="auth.no_valid_secret",
                area="auth",
                severity="blocker" if cfg.env == "production" else "warn",
                title="没有可用认证密钥",
                detail="KUN_AUTH_SECRET / KUN_AUTH_SECRETS 没有 32 位以上可验签密钥。",
                suggested_action="配置至少一个 32+ 字符随机密钥；生产建议用 KUN_AUTH_SECRETS 做轮换。",
                env_vars=["KUN_AUTH_SECRET", "KUN_AUTH_SECRETS"],
            )
        )
        return items
    weak = [value for value in raw_values if _looks_like_weak_secret(value)]
    if weak:
        items.append(
            SecretAuditItem(
                item_id="auth.weak_secret_shape",
                area="auth",
                severity="blocker" if cfg.env == "production" else "warn",
                title="认证密钥像测试值",
                detail="发现重复字符、changeme、password、secret、test 等测试形态；不会输出具体值。",
                suggested_action="换成密码管理器生成的高熵随机值。",
                env_vars=["KUN_AUTH_SECRET", "KUN_AUTH_SECRETS"],
            )
        )
    elif cfg.env == "production" and len(candidates) == 1:
        items.append(
            SecretAuditItem(
                item_id="auth.rotation_single_secret",
                area="auth",
                severity="warn",
                title="认证密钥没有轮换余量",
                detail="生产环境只有一个可用密钥；一旦泄露或替换会更难无损轮换。",
                suggested_action="用 KUN_AUTH_SECRETS 配置 newest,previous 两个以上密钥。",
                env_vars=["KUN_AUTH_SECRETS"],
            )
        )
    else:
        items.append(
            SecretAuditItem(
                item_id="auth.secret_policy",
                area="auth",
                severity="ok",
                title="认证密钥基础检查通过",
                detail=f"发现 {len(candidates)} 个可用密钥候选；未输出密钥值。",
                env_vars=["KUN_AUTH_SECRET", "KUN_AUTH_SECRETS"],
            )
        )
    return items


def _database_items(cfg: Settings) -> list[SecretAuditItem]:
    items: list[SecretAuditItem] = []
    if cfg.pg_dsn == cfg.pg_admin_dsn:
        items.append(
            SecretAuditItem(
                item_id="database.app_equals_admin",
                area="database",
                severity="blocker" if cfg.env == "production" else "warn",
                title="应用连接和管理员连接相同",
                detail="KUN_PG_DSN 与 KUN_PG_ADMIN_DSN 完全相同；RLS/权限边界会失去意义。",
                suggested_action="应用使用 kun_app 等低权限账号；迁移/维护才使用 admin DSN。",
                env_vars=["KUN_PG_DSN", "KUN_PG_ADMIN_DSN"],
            )
        )
    if cfg.env == "production":
        if _dsn_contains_default_credential(cfg.pg_dsn):
            items.append(
                SecretAuditItem(
                    item_id="database.app_default_credential",
                    area="database",
                    severity="blocker",
                    title="应用数据库账号仍像默认密码",
                    detail="KUN_PG_DSN 看起来仍在使用 kun_app:kun_app / postgres:postgres 等默认凭据。",
                    suggested_action="给应用 DB 账号换强随机密码，并确认它没有 BYPASSRLS/superuser。",
                    env_vars=["KUN_PG_DSN"],
                )
            )
        if _dsn_contains_admin_default(cfg.pg_admin_dsn):
            items.append(
                SecretAuditItem(
                    item_id="database.admin_default_credential",
                    area="database",
                    severity="blocker",
                    title="管理员数据库账号仍像默认密码",
                    detail="KUN_PG_ADMIN_DSN 看起来仍在使用 kun:kun / postgres:postgres 等默认凭据。",
                    suggested_action="给 admin DB 账号换强随机密码，并限制只在迁移/维护链路使用。",
                    env_vars=["KUN_PG_ADMIN_DSN"],
                )
            )
    if not any(item.area == "database" for item in items):
        items.append(
            SecretAuditItem(
                item_id="database.role_split",
                area="database",
                severity="ok",
                title="数据库角色基础检查通过",
                detail="应用 DSN 和管理员 DSN 已区分，未发现明显默认凭据。",
                env_vars=["KUN_PG_DSN", "KUN_PG_ADMIN_DSN"],
            )
        )
    return items


def _storage_items(cfg: Settings) -> list[SecretAuditItem]:
    items: list[SecretAuditItem] = []
    if cfg.env == "production" and (
        cfg.s3_access_key == "minio" or cfg.s3_secret_key == "minio123"
    ):
        items.append(
            SecretAuditItem(
                item_id="storage.default_minio_secret",
                area="storage",
                severity="blocker",
                title="对象存储仍是默认账号密码",
                detail="S3/MinIO access key 或 secret key 仍是开发默认值。",
                suggested_action="换成生产专用 bucket 和强随机访问密钥。",
                env_vars=["KUN_S3_ACCESS_KEY", "KUN_S3_SECRET_KEY"],
            )
        )
    if cfg.env == "production" and _is_local_url(cfg.s3_endpoint):
        items.append(
            SecretAuditItem(
                item_id="storage.local_endpoint",
                area="storage",
                severity="warn",
                title="生产对象存储 endpoint 指向本机",
                detail="KUN_S3_ENDPOINT 是 localhost/127.0.0.1；除非这是同机私有部署，否则备份和产物会很脆。",
                suggested_action="正式 SaaS 部署建议使用托管对象存储或内网高可用 MinIO。",
                env_vars=["KUN_S3_ENDPOINT"],
            )
        )
    if not any(item.area == "storage" for item in items):
        items.append(
            SecretAuditItem(
                item_id="storage.secret_policy",
                area="storage",
                severity="ok",
                title="对象存储基础检查通过",
                detail="未发现开发默认对象存储凭据。",
                env_vars=["KUN_S3_ENDPOINT", "KUN_S3_ACCESS_KEY", "KUN_S3_SECRET_KEY"],
            )
        )
    return items


def _world_gateway_items(env: Mapping[str, str]) -> list[SecretAuditItem]:
    items: list[SecretAuditItem] = []
    for action_type, (enable_env, required_envs) in EXPECTED_REAL_WORLD_HANDLERS.items():
        enabled = _env_truthy(env.get(enable_env))
        missing = [name for name in required_envs if not env.get(name, "").strip()]
        if enabled and missing:
            items.append(
                SecretAuditItem(
                    item_id=f"world_gateway.{action_type}.missing_required_env",
                    area="world_gateway",
                    severity="blocker",
                    title=f"{action_type} 真实 handler 半启用",
                    detail=f"{enable_env}=true，但缺少 {', '.join(missing)}。",
                    suggested_action="补齐必需 env，或关闭该真实外部执行器。",
                    env_vars=[enable_env, *required_envs],
                )
            )
            continue
        if enabled:
            extra = _world_gateway_extra_risks(action_type, env)
            items.extend(extra)
            if not extra:
                items.append(
                    SecretAuditItem(
                        item_id=f"world_gateway.{action_type}.configured",
                        area="world_gateway",
                        severity="ok",
                        title=f"{action_type} 基础配置通过",
                        detail="真实外部 handler 已启用，必需 env 已提供；执行仍需权限和人工确认。",
                        env_vars=[enable_env, *required_envs],
                    )
                )
    if not any(item.area == "world_gateway" for item in items):
        items.append(
            SecretAuditItem(
                item_id="world_gateway.no_real_handlers_enabled",
                area="world_gateway",
                severity="ok",
                title="没有启用真实外部执行器",
                detail="当前 WorldGateway 主要是草稿、dry-run、计划和审计；真实外发能力未打开。",
                suggested_action="需要真实邮件/API/浏览器执行时，逐个启用 handler 并配置权限/补偿策略。",
            )
        )
    return items


def _world_gateway_extra_risks(
    action_type: str,
    env: Mapping[str, str],
) -> list[SecretAuditItem]:
    if action_type == "email.send":
        username = env.get("KUN_WORLD_SMTP_USERNAME", "").strip()
        password = env.get("KUN_WORLD_SMTP_PASSWORD", "").strip()
        if username and not password:
            return [
                SecretAuditItem(
                    item_id="world_gateway.email.send.missing_password",
                    area="world_gateway",
                    severity="blocker",
                    title="email.send 缺少 SMTP 密码",
                    detail="配置了 KUN_WORLD_SMTP_USERNAME，但没有 KUN_WORLD_SMTP_PASSWORD。",
                    suggested_action="补 SMTP 应用专用密码；不要复用个人主密码。",
                    env_vars=["KUN_WORLD_SMTP_USERNAME", "KUN_WORLD_SMTP_PASSWORD"],
                )
            ]
    if action_type == "enterprise_api.post":
        header = env.get("KUN_WORLD_API_AUTH_HEADER", "").strip()
        value = env.get("KUN_WORLD_API_AUTH_VALUE", "").strip()
        if bool(header) != bool(value):
            return [
                SecretAuditItem(
                    item_id="world_gateway.enterprise_api.partial_auth",
                    area="world_gateway",
                    severity="blocker",
                    title="enterprise_api.post 认证头配置不完整",
                    detail="KUN_WORLD_API_AUTH_HEADER / KUN_WORLD_API_AUTH_VALUE 必须成对出现。",
                    suggested_action="补齐认证头和值，或两者都不配置并确认 API 真的允许无认证。",
                    env_vars=["KUN_WORLD_API_AUTH_HEADER", "KUN_WORLD_API_AUTH_VALUE"],
                )
            ]
        if not header and not value:
            return [
                SecretAuditItem(
                    item_id="world_gateway.enterprise_api.no_auth",
                    area="world_gateway",
                    severity="warn",
                    title="enterprise_api.post 未配置认证",
                    detail="企业 API handler 已启用，但没有认证头；这通常只适合内网测试。",
                    suggested_action="生产建议配置专用 token，并把 host 白名单收窄。",
                    env_vars=["KUN_WORLD_API_AUTH_HEADER", "KUN_WORLD_API_AUTH_VALUE"],
                )
            ]
    return []


def _llm_items(env: Mapping[str, str]) -> list[SecretAuditItem]:
    primary = env.get("KUN_LLM_PRIMARY", "").strip().lower()
    if primary in {"openai", "anthropic", "minimax"}:
        env_name = {
            "openai": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "minimax": "MINIMAX_API_KEY",
        }[primary]
        if not env.get(env_name, "").strip():
            return [
                SecretAuditItem(
                    item_id=f"llm.{primary}.missing_api_key",
                    area="llm",
                    severity="blocker",
                    title=f"LLM 主链路 {primary} 缺少 API key",
                    detail=f"KUN_LLM_PRIMARY={primary}，但 {env_name} 没有配置。",
                    suggested_action="补齐对应 API key，或把 KUN_LLM_PRIMARY 切到可用 provider。",
                    env_vars=["KUN_LLM_PRIMARY", env_name],
                )
            ]
    return [
        SecretAuditItem(
            item_id="llm.provider_secret_policy",
            area="llm",
            severity="ok",
            title="LLM provider 配置基础检查通过",
            detail="未发现显式主 provider 缺少必需 API key。",
            env_vars=["KUN_LLM_PRIMARY"],
        )
    ]


def _looks_like_weak_secret(raw: str) -> bool:
    lowered = raw.lower()
    bad_words = ("changeme", "password", "secret", "test", "dev-only", "example")
    if any(word in lowered for word in bad_words):
        return True
    compact = raw.replace("-", "").replace("_", "")
    return bool(compact) and len(set(compact)) <= 2


def _dsn_contains_default_credential(dsn: str) -> bool:
    lowered = dsn.lower()
    return any(
        marker in lowered
        for marker in (
            "kun_app:kun_app@",
            "postgres:postgres@",
            "app:app@",
        )
    )


def _dsn_contains_admin_default(dsn: str) -> bool:
    lowered = dsn.lower()
    return any(
        marker in lowered
        for marker in (
            "kun:kun@",
            "postgres:postgres@",
            "admin:admin@",
        )
    )


def _is_local_url(url: str) -> bool:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"}


def _env_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


__all__ = [
    "AuditArea",
    "AuditSeverity",
    "SecretAuditItem",
    "SecretAuditReport",
    "audit_runtime_secrets",
]
