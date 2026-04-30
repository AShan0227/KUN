"""Small file-backed secret store bridge.

This is intentionally modest: it is not a cloud KMS, not encrypted-at-rest by
itself, and not a rotation workflow.  It gives KUN one centralized runtime
lookup path for tenant-scoped handler credentials so WorldGateway does not have
to rely only on scattered environment variables.

Expected JSON shape:

{
  "global": {"KUN_WORLD_SMTP_HOST": "smtp.example.com"},
  "tenants": {
    "tenant-a": {"KUN_WORLD_SMTP_FROM": "kun@example.com"}
  }
}
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SECRET_STORE_FILE_ENV = "KUN_SECRET_STORE_FILE"


@dataclass(frozen=True)
class SecretStoreStatus:
    configured: bool
    readable: bool
    path: str | None = None
    tenant_count: int = 0
    global_key_count: int = 0
    error: str = ""


@dataclass(frozen=True)
class SecretStoreWriteResult:
    """Result of a local file-backed secret-store update.

    Values are intentionally not included so CLI/API output cannot leak them.
    """

    path: str
    scope: str
    name: str
    tenant_id: str = ""
    tenant_count: int = 0
    global_key_count: int = 0
    honest_limits: tuple[str, ...] = (
        "这是本地 JSON secret store 写入工具，不是云 KMS 或托管 Secret Manager。",
        "文件会设置为 0600，但加密、轮换、访问审计仍需后续生产级密钥系统。",
    )


def secret_for_tenant(
    tenant_id: str,
    name: str,
    *,
    env: Mapping[str, str] | None = None,
    allow_global: bool = False,
) -> str | None:
    """Return a secret value for a tenant without exposing it in reports."""

    data = _load(env=env)
    if data is None:
        return None
    tenants = _dict(data.get("tenants"))
    tenant_values = _dict(tenants.get(tenant_id))
    value = _empty_to_none(tenant_values.get(name))
    if value is not None:
        return value
    if allow_global:
        return _empty_to_none(_dict(data.get("global")).get(name))
    return None


def has_secret_store(env: Mapping[str, str] | None = None) -> bool:
    return _store_path(env=env) is not None


def secret_store_status(env: Mapping[str, str] | None = None) -> SecretStoreStatus:
    path = _store_path(env=env)
    if path is None:
        return SecretStoreStatus(configured=False, readable=False)
    try:
        data = _load_from_path(path)
    except Exception as exc:
        return SecretStoreStatus(
            configured=True,
            readable=False,
            path=str(path),
            error=str(exc),
        )
    tenants = _dict(data.get("tenants"))
    global_values = _dict(data.get("global"))
    return SecretStoreStatus(
        configured=True,
        readable=True,
        path=str(path),
        tenant_count=len(tenants),
        global_key_count=len(global_values),
    )


def secret_store_has_required(
    required_names: tuple[str, ...],
    *,
    tenant_id: str = "",
    env: Mapping[str, str] | None = None,
) -> bool:
    data = _load(env=env)
    if data is None:
        return False
    if tenant_id:
        return all(
            secret_for_tenant(tenant_id, name, env=env, allow_global=True) is not None
            for name in required_names
        )
    global_values = _dict(data.get("global"))
    if all(_empty_to_none(global_values.get(name)) for name in required_names):
        return True
    tenants = _dict(data.get("tenants"))
    return any(
        all(_empty_to_none(_dict(values).get(name)) for name in required_names)
        for values in tenants.values()
    )


def upsert_secret_store_value(
    *,
    path: Path,
    name: str,
    value: str,
    tenant_id: str = "",
) -> SecretStoreWriteResult:
    """Write/update one WorldGateway secret value in a local JSON store."""

    cleaned_name = name.strip()
    cleaned_value = value.strip()
    cleaned_tenant = tenant_id.strip()
    if not cleaned_name:
        raise ValueError("secret name is required")
    if not cleaned_name.startswith("KUN_WORLD_"):
        raise ValueError("only KUN_WORLD_* keys may be written through this helper")
    if not cleaned_value:
        raise ValueError("secret value is required")
    target = path.expanduser()
    data = _load_from_path(target) if target.exists() else {}
    global_values = _dict(data.get("global"))
    tenants = _dict(data.get("tenants"))
    if cleaned_tenant:
        tenant_values = _dict(tenants.get(cleaned_tenant))
        tenant_values[cleaned_name] = cleaned_value
        tenants[cleaned_tenant] = tenant_values
        scope = "tenant"
    else:
        global_values[cleaned_name] = cleaned_value
        scope = "global"
    payload = {"global": global_values, "tenants": tenants}
    _atomic_write_json(target, payload)
    return SecretStoreWriteResult(
        path=str(target),
        scope=scope,
        name=cleaned_name,
        tenant_id=cleaned_tenant,
        tenant_count=len(tenants),
        global_key_count=len(global_values),
    )


def has_any_tenant_secret_prefix(
    tenant_id: str,
    prefix: str,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    data = _load(env=env)
    if data is None:
        return False
    values = _dict(_dict(data.get("tenants")).get(tenant_id))
    return any(
        name.startswith(prefix) and _empty_to_none(value) is not None
        for name, value in values.items()
    )


def has_any_scoped_secret(
    env_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    data = _load(env=env)
    if data is None:
        return False
    tenants = _dict(data.get("tenants"))
    return any(
        _empty_to_none(_dict(values).get(env_name)) is not None for values in tenants.values()
    )


def _load(*, env: Mapping[str, str] | None) -> dict[str, Any] | None:
    path = _store_path(env=env)
    if path is None:
        return None
    try:
        return _load_from_path(path)
    except Exception:
        # Runtime lookup must not crash a user task because an optional external
        # secret-store bridge is misconfigured.  NUO / preflight still reports
        # the same problem as a blocker through secret_store_status().
        return None


def _store_path(*, env: Mapping[str, str] | None) -> Path | None:
    source = env or {}
    raw = _empty_to_none(source.get(SECRET_STORE_FILE_ENV))
    if raw is None:
        import os

        raw = _empty_to_none(os.environ.get(SECRET_STORE_FILE_ENV))
    if raw is None:
        return None
    return Path(raw).expanduser()


def _load_from_path(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("secret store root must be a JSON object")
    return payload


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(raw)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _empty_to_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


__all__ = [
    "SECRET_STORE_FILE_ENV",
    "SecretStoreStatus",
    "SecretStoreWriteResult",
    "has_any_scoped_secret",
    "has_any_tenant_secret_prefix",
    "has_secret_store",
    "secret_for_tenant",
    "secret_store_has_required",
    "secret_store_status",
    "upsert_secret_store_value",
]
