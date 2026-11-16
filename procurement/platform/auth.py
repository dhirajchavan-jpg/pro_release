"""Authentication boundary. Production JWT validation never falls back to an API token."""

from __future__ import annotations

import os
import secrets
import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import jwt
from fastapi import HTTPException, Request


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[str]


def auth_mode() -> str:
    mode = os.getenv("AUTH_MODE", "api_token").strip().lower()
    if mode not in {"api_token", "jwt"}:
        raise RuntimeError("AUTH_MODE must be api_token or jwt")
    return mode


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return header.split(" ", 1)[1].strip()


def mint_local_session(
    api_token: str,
    *,
    subject: str = "compatibility-api-token",
    tenant_id: str = "compatibility",
    roles: frozenset[str] = frozenset({"admin"}),
    ttl_seconds: int = 28800,
) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "exp": int(time.time()) + ttl_seconds,
            "nonce": secrets.token_urlsafe(18),
            "sub": subject,
            "tenant_id": tenant_id,
            "roles": sorted(roles),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    encoded = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    signature = hmac.new(api_token.encode(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def hash_local_password(password: str, *, iterations: int = 310_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(
        (
            "pbkdf2_sha256",
            str(iterations),
            base64.urlsafe_b64encode(salt).decode().rstrip("="),
            base64.urlsafe_b64encode(digest).decode().rstrip("="),
        )
    )


def verify_local_password(password: str, encoded_hash: str) -> bool:
    try:
        algorithm, raw_iterations, raw_salt, raw_digest = encoded_hash.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(raw_iterations)
        if iterations < 100_000 or iterations > 2_000_000:
            return False
        salt = base64.urlsafe_b64decode(raw_salt + "=" * (-len(raw_salt) % 4))
        expected = base64.urlsafe_b64decode(raw_digest + "=" * (-len(raw_digest) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
        return secrets.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _local_session_principal(value: str, api_token: str) -> Principal | None:
    try:
        encoded, signature = value.split(".", 1)
        expected = hmac.new(api_token.encode(), encoded.encode(), hashlib.sha256).hexdigest()
        if not secrets.compare_digest(signature, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode())
        subject = str(payload.get("sub") or "").strip()
        tenant_id = str(payload.get("tenant_id") or "").strip()
        raw_roles = payload.get("roles")
        roles = frozenset(str(role) for role in raw_roles if isinstance(role, str) and role) if isinstance(raw_roles, list) else frozenset()
        if (
            payload.get("v") != 1
            or int(payload.get("exp") or 0) < int(time.time())
            or not subject
            or len(subject) > 255
            or not tenant_id
            or len(tenant_id) > 36
            or not roles
        ):
            return None
        return Principal(subject=subject, tenant_id=tenant_id, roles=roles)
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _session_digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@lru_cache(maxsize=1)
def _auth_sessions():
    from procurement.platform.database import make_session_factory

    return make_session_factory()


def activate_local_session(value: str, principal: Principal, *, ttl_seconds: int) -> None:
    from sqlalchemy import select

    from procurement.domain.models import LocalSession, Tenant
    from procurement.platform.database import session_scope

    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(300, ttl_seconds))
    with session_scope(_auth_sessions()) as session:
        tenant_id = session.scalar(
            select(Tenant.id).where(Tenant.id == principal.tenant_id).with_for_update()
        )
        if not tenant_id:
            raise RuntimeError("local tenant is not provisioned")
        active = session.get(LocalSession, principal.tenant_id)
        if active is None:
            active = LocalSession(tenant_id=principal.tenant_id)
            session.add(active)
        active.token_hash = _session_digest(value)
        active.subject = principal.subject
        active.expires_at = expires_at
        active.created_at = datetime.now(timezone.utc)


def _active_local_session(value: str, principal: Principal) -> bool:
    from procurement.domain.models import LocalSession
    from procurement.platform.database import session_scope

    with session_scope(_auth_sessions()) as session:
        active = session.get(LocalSession, principal.tenant_id)
        if active is None or active.subject != principal.subject:
            return False
        expires_at = active.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at > datetime.now(timezone.utc) and secrets.compare_digest(
            active.token_hash,
            _session_digest(value),
        )


def revoke_local_session(value: str, api_token: str) -> bool:
    from procurement.domain.models import LocalSession
    from procurement.platform.database import session_scope

    principal = _local_session_principal(value, api_token)
    if principal is None:
        return False
    with session_scope(_auth_sessions()) as session:
        active = session.get(LocalSession, principal.tenant_id)
        if active is None or not secrets.compare_digest(active.token_hash, _session_digest(value)):
            return False
        session.delete(active)
        return True


@lru_cache(maxsize=1)
def _jwks_client(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=300)


def _jwt_principal(request: Request) -> Principal:
    jwks_url = os.getenv("AUTH_JWKS_URL", "").strip()
    issuer = os.getenv("AUTH_ISSUER", "").strip()
    audience = os.getenv("AUTH_AUDIENCE", "").strip()
    tenant_claim = os.getenv("AUTH_TENANT_CLAIM", "tenant_id").strip()
    roles_claim = os.getenv("AUTH_ROLES_CLAIM", "roles").strip()
    if not jwks_url or not issuer or not audience:
        raise HTTPException(status_code=503, detail="AUTH_JWKS_URL, AUTH_ISSUER and AUTH_AUDIENCE are required for JWT authentication")
    try:
        token = _bearer_token(request)
        key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
        claims = jwt.decode(token, key.key, algorithms=["RS256", "ES256"], audience=audience, issuer=issuer)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid bearer token") from exc
    subject = str(claims.get("sub") or "").strip()
    tenant_id = str(claims.get(tenant_claim) or "").strip()
    raw_roles = claims.get(roles_claim, [])
    roles = {str(role) for role in (raw_roles if isinstance(raw_roles, list) else [raw_roles]) if str(role)}
    if not subject or not tenant_id:
        raise HTTPException(status_code=403, detail="token lacks subject or tenant claim")
    return Principal(subject=subject, tenant_id=tenant_id, roles=frozenset(roles))


def authenticate(request: Request, api_token: str | None) -> Principal:
    if auth_mode() == "jwt":
        principal = _jwt_principal(request)
    else:
        if not api_token:
            raise HTTPException(status_code=503, detail="PROCUREMENT_API_TOKEN is required when AUTH_MODE=api_token")
        token = request.headers.get("x-api-key", "")
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            token = header.split(" ", 1)[1].strip()
        cookie = getattr(request, "cookies", {}).get("procurement_session", "")
        local_principal = _local_session_principal(
            cookie,
            api_token,
        )
        if local_principal is not None:
            try:
                if not _active_local_session(cookie, local_principal):
                    raise HTTPException(status_code=401, detail="session was replaced or expired")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=503, detail="session store is unavailable") from exc
            principal = local_principal
        else:
            direct_api_enabled = os.getenv("ALLOW_DIRECT_API_TOKEN_AUTH", "0").strip() == "1"
            if not direct_api_enabled or not secrets.compare_digest(token, api_token):
                raise HTTPException(status_code=401, detail="missing or invalid API token")
            principal = Principal(
                subject="compatibility-api-token",
                tenant_id="compatibility",
                roles=frozenset({"admin"}),
            )
    state = getattr(request, "state", None)
    if state is not None:
        state.principal = principal
    return principal


def require_role(request: Request, *allowed: str) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise HTTPException(status_code=401, detail="authentication required")
    if allowed and not principal.roles.intersection(allowed):
        raise HTTPException(status_code=403, detail="insufficient role")
    return principal
