"""Auth verification for the admin API and MCP endpoint.

Three mechanisms are supported (different endpoints use different subsets):

1. **Entra ID JWT bearer tokens** — verified against the tenant's JWKS.
2. **Static API key** — for headless callers (e.g., a Foundry agent calling
   ``/mcp``). Provided via the ``MCP_API_KEY`` env var.
3. **Container Apps Easy Auth header** (``X-MS-CLIENT-PRINCIPAL``) — for the
   browser admin UI, sign-in is handled by the platform and roles are passed
   through as claims.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Depends, Header, HTTPException, status
from jose import jwt
from jose.exceptions import JWTError

log = logging.getLogger(__name__)

JWKS_CACHE_TTL = 3600.0


@dataclass(slots=True)
class AuthSettings:
    tenant_id: str | None = None
    audience: str | None = None
    issuer: str | None = None
    admin_app_role: str = "Admin"
    api_key: str | None = None
    allow_anonymous: bool = False

    @classmethod
    def from_env(cls) -> AuthSettings:
        tenant = os.getenv("ENTRA_TENANT_ID")
        audience = os.getenv("ENTRA_AUDIENCE")
        issuer = os.getenv("ENTRA_ISSUER") or (
            f"https://login.microsoftonline.com/{tenant}/v2.0" if tenant else None
        )
        return cls(
            tenant_id=tenant,
            audience=audience,
            issuer=issuer,
            admin_app_role=os.getenv("ADMIN_APP_ROLE", "Admin"),
            api_key=os.getenv("MCP_API_KEY"),
            allow_anonymous=os.getenv("ALLOW_ANONYMOUS", "").lower() in ("1", "true", "yes"),
        )


@dataclass(slots=True)
class Principal:
    kind: str  # "user" | "apikey"
    subject: str
    roles: tuple[str, ...] = ()
    claims: dict[str, Any] | None = None

    def has_role(self, role: str) -> bool:
        return role in self.roles


class _JWKSCache:
    def __init__(self) -> None:
        self._keys: dict[str, Any] | None = None
        self._fetched_at: float = 0.0

    async def get(self, tenant_id: str) -> dict[str, Any]:
        now = time.time()
        if self._keys is not None and (now - self._fetched_at) < JWKS_CACHE_TTL:
            return self._keys
        url = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            self._keys = resp.json()
            self._fetched_at = now
        return self._keys


_jwks_cache = _JWKSCache()


async def _verify_jwt(token: str, settings: AuthSettings) -> Principal:
    if not (settings.tenant_id and settings.audience and settings.issuer):
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Entra auth not configured")
    try:
        jwks = await _jwks_cache.get(settings.tenant_id)
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        key = next((k for k in jwks.get("keys", []) if k.get("kid") == kid), None)
        if key is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "unknown token signing key")
        claims = jwt.decode(
            token,
            key,
            algorithms=[unverified.get("alg", "RS256")],
            audience=settings.audience,
            issuer=settings.issuer,
        )
    except JWTError as exc:
        log.info("JWT validation failed: %s", exc)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid token") from exc

    roles = tuple(claims.get("roles", []) or [])
    subject = claims.get("oid") or claims.get("sub") or "unknown"
    return Principal(kind="user", subject=subject, roles=roles, claims=claims)


def _check_api_key(presented: str, settings: AuthSettings) -> Principal | None:
    if not settings.api_key:
        return None
    if hmac.compare_digest(presented, settings.api_key):
        return Principal(kind="apikey", subject="api-key", roles=("McpCaller",))
    return None


def get_settings() -> AuthSettings:
    return AuthSettings.from_env()


async def require_principal(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    settings: AuthSettings = Depends(get_settings),
) -> Principal:
    if settings.allow_anonymous:
        return Principal(kind="user", subject="anonymous", roles=("Admin", "McpCaller"))

    if x_api_key:
        principal = _check_api_key(x_api_key, settings)
        if principal is not None:
            return principal
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        return await _verify_jwt(token, settings)

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing credentials")


async def require_admin(principal: Principal = Depends(require_principal)) -> Principal:
    settings = AuthSettings.from_env()
    if principal.has_role(settings.admin_app_role) or principal.has_role("Admin"):
        return principal
    raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")


# ---------------------------------------------------------------------------
# Container Apps Easy Auth principal
# ---------------------------------------------------------------------------

_ROLE_CLAIM_TYPES = {
    "roles",
    "http://schemas.microsoft.com/ws/2008/06/identity/claims/role",
    "http://schemas.microsoft.com/identity/claims/role",
}
_OID_CLAIM_TYPES = {
    "oid",
    "http://schemas.microsoft.com/identity/claims/objectidentifier",
    "sub",
    "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/nameidentifier",
}


def _decode_easy_auth_principal(header_value: str) -> Principal | None:
    try:
        raw = base64.b64decode(header_value)
        data: dict[str, Any] = json.loads(raw)
    except (binascii.Error, ValueError, json.JSONDecodeError):
        log.info("Invalid X-MS-CLIENT-PRINCIPAL header")
        return None

    claims_list = data.get("claims") or []
    roles: list[str] = []
    subject = ""
    for claim in claims_list:
        ctype = claim.get("typ") or claim.get("type")
        cval = claim.get("val") or claim.get("value")
        if ctype is None or cval is None:
            continue
        if ctype in _ROLE_CLAIM_TYPES:
            roles.append(str(cval))
        elif not subject and ctype in _OID_CLAIM_TYPES:
            subject = str(cval)

    if not subject:
        subject = data.get("userId") or data.get("userPrincipalName") or "unknown"

    return Principal(
        kind="user",
        subject=subject,
        roles=tuple(roles),
        claims={"easyAuth": data},
    )


async def require_easy_auth_admin(
    x_ms_client_principal: str | None = Header(default=None, alias="X-MS-CLIENT-PRINCIPAL"),
    settings: AuthSettings = Depends(get_settings),
) -> Principal:
    """Dependency for endpoints behind Container Apps Easy Auth.

    Validates the platform-injected principal header and enforces the Admin
    role. ``ALLOW_ANONYMOUS=1`` short-circuits for local dev.
    """

    if settings.allow_anonymous:
        return Principal(kind="user", subject="anonymous", roles=("Admin",))

    if not x_ms_client_principal:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign-in required")

    principal = _decode_easy_auth_principal(x_ms_client_principal)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid sign-in")

    if principal.has_role(settings.admin_app_role) or principal.has_role("Admin"):
        return principal
    raise HTTPException(status.HTTP_403_FORBIDDEN, "admin role required")
