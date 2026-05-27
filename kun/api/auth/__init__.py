"""Auth subsystem (L6.AuthScaffold / ADR-019 中期).

JWT (HS256) + tenant_id 提取 + Postgres RLS 绑定. 完整 production-ready
但默认 `KUN_AUTH_ENABLED=false` — flag flip 即可启用.

模块:
  - jwt_token.py    · HMAC-SHA256 JWT encode/decode (stdlib only, no PyJWT 依赖)
  - middleware.py   · FastAPI middleware: extract Bearer → decode → request.state.tenant_id
  - rls.py          · Postgres `SET LOCAL app.tenant_id` 绑定 helper

启用流程 (升级到 Phase 2 中期 ADR-019):
  1. 设 KUN_AUTH_ENABLED=true
  2. 设 KUN_AUTH_JWT_SECRET=<32+ char random secret>
  3. 改 alembic policy 让 RLS 用 app.tenant_id 而不是 default_tenant_id
  4. 改 KUN_DEFAULT_TENANT_ID=null (生产 must)
"""

from kun.api.auth.jwt_token import (
    JWTDecodeError,
    JWTPayload,
    decode_jwt,
    encode_jwt,
)
from kun.api.auth.middleware import (
    AuthError,
    AuthSettings,
    ResolvedIdentity,
    build_auth_settings,
    extract_bearer_token,
    resolve_tenant_id,
)
from kun.api.auth.rls import InvalidTenantIdError, bind_tenant_to_session

__all__ = [
    "AuthError",
    "AuthSettings",
    "InvalidTenantIdError",
    "JWTDecodeError",
    "JWTPayload",
    "ResolvedIdentity",
    "bind_tenant_to_session",
    "build_auth_settings",
    "decode_jwt",
    "encode_jwt",
    "extract_bearer_token",
    "resolve_tenant_id",
]
