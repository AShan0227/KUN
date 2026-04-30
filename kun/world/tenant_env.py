"""Tenant-scoped environment helpers for WorldGateway.

This is not a full secret manager.  It is the current source-available bridge:
global handler enable flags stay global, while individual tenants can override
handler credentials and allowlists through scoped env names such as
`KUN_TENANT_TENANT_A_WORLD_SMTP_FROM`.
"""

from __future__ import annotations

import os
from collections.abc import Mapping


def tenant_env_key(tenant_id: str) -> str:
    """Return the safe tenant key used by env-scoped world credentials."""
    return "".join(ch if ch.isalnum() else "_" for ch in tenant_id.upper()).strip("_")


def tenant_env_name(tenant_id: str, env_name: str) -> str | None:
    tenant_key = tenant_env_key(tenant_id)
    if not tenant_key:
        return None
    suffix = env_name.removeprefix("KUN_")
    return f"KUN_TENANT_{tenant_key}_{suffix}"


def tenant_env(
    tenant_id: str,
    env_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    scoped_name = tenant_env_name(tenant_id, env_name)
    if not scoped_name:
        return None
    return _empty_to_none((env or os.environ).get(scoped_name))


def env_for_tenant(
    tenant_id: str,
    env_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    source = env or os.environ
    return tenant_env(tenant_id, env_name, env=source) or _empty_to_none(source.get(env_name))


def has_tenant_env(
    tenant_id: str,
    *env_names: str,
    env: Mapping[str, str] | None = None,
) -> bool:
    return any(tenant_env(tenant_id, name, env=env) is not None for name in env_names)


def has_any_tenant_env_prefix(
    tenant_id: str,
    env_prefix: str,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    scoped_name = tenant_env_name(tenant_id, env_prefix)
    if not scoped_name:
        return False
    return any(
        name.startswith(scoped_name) and _empty_to_none(value) is not None
        for name, value in (env or os.environ).items()
    )


def has_any_scoped_env(
    env_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return true when any tenant provides the scoped form of env_name."""
    suffix = "_" + env_name.removeprefix("KUN_")
    return any(
        name.startswith("KUN_TENANT_")
        and name.endswith(suffix)
        and _empty_to_none(value) is not None
        for name, value in (env or os.environ).items()
    )


def has_required_world_env(
    required_envs: tuple[str, ...],
    *,
    tenant_id: str = "",
    env: Mapping[str, str] | None = None,
) -> bool:
    return not missing_required_world_env(required_envs, tenant_id=tenant_id, env=env)


def missing_required_world_env(
    required_envs: tuple[str, ...],
    *,
    tenant_id: str = "",
    env: Mapping[str, str] | None = None,
) -> list[str]:
    source = env or os.environ
    if all(_empty_to_none(source.get(name)) for name in required_envs):
        return []
    if tenant_id:
        return [
            name
            for name in required_envs
            if not _empty_to_none(source.get(name)) and not tenant_env(tenant_id, name, env=source)
        ]

    tenant_keys: set[str] = set()
    for name in required_envs:
        tenant_keys.update(_tenant_keys_with_env(name, env=source))
    for key in tenant_keys:
        if all(_scoped_env_by_key(key, name, env=source) for name in required_envs):
            return []
    return [name for name in required_envs if not _empty_to_none(source.get(name))]


def env_int_for_tenant(tenant_id: str, env_name: str, *, default: int) -> int:
    value = env_for_tenant(tenant_id, env_name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{env_name} must be an integer") from exc


def env_bool_for_tenant(tenant_id: str, env_name: str, *, default: bool) -> bool:
    value = env_for_tenant(tenant_id, env_name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def csv_set_for_tenant(
    tenant_id: str,
    env_name: str,
    *,
    default: set[str],
) -> set[str]:
    value = tenant_env(tenant_id, env_name)
    if value is None:
        return set(default)
    return {item.strip().lower() for item in value.split(",") if item.strip()}


def _empty_to_none(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _tenant_keys_with_env(
    env_name: str,
    *,
    env: Mapping[str, str],
) -> set[str]:
    suffix = "_" + env_name.removeprefix("KUN_")
    keys: set[str] = set()
    for name, value in env.items():
        if not name.startswith("KUN_TENANT_") or not name.endswith(suffix):
            continue
        if _empty_to_none(value) is None:
            continue
        key = name[len("KUN_TENANT_") : -len(suffix)]
        if key:
            keys.add(key)
    return keys


def _scoped_env_by_key(
    tenant_key: str,
    env_name: str,
    *,
    env: Mapping[str, str],
) -> str | None:
    suffix = env_name.removeprefix("KUN_")
    return _empty_to_none(env.get(f"KUN_TENANT_{tenant_key}_{suffix}"))
