"""Modern SaaS authentication and role-based access control (RBAC)."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import secrets

import jwt
from fastapi import HTTPException, Request


@dataclass(frozen=True)
class Principal:
    subject: str
    tenant_id: str
    roles: frozenset[str]
    user_id: str | None = None
    permissions: frozenset[str] = frozenset()


def auth_mode() -> str:
    if os.getenv("AUTH_MODE", "").strip().lower() != "jwt":
        raise RuntimeError("AUTH_MODE must be 'jwt'")
    return "jwt"


def jwt_signing_key() -> str:
    secret = os.getenv("JWT_SIGNING_KEY", "").strip()
    if len(secret.encode()) < 32 or secret.lower().startswith("replace_"):
        raise RuntimeError("JWT_SIGNING_KEY must contain at least 32 random bytes")
    return secret


def jwt_issuer() -> str:
    return os.getenv("JWT_ISSUER", "tenderabl").strip() or "tenderabl"


def jwt_audience() -> str:
    return os.getenv("JWT_AUDIENCE", "tenderabl-api").strip() or "tenderabl-api"


def jwt_access_ttl_seconds() -> int:
    return max(60, int(os.getenv("JWT_ACCESS_TTL_SECONDS", "900")))


def jwt_refresh_ttl_seconds() -> int:
    return max(3600, int(os.getenv("JWT_REFRESH_TTL_SECONDS", "604800")))


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


def _create_token(
    user_id: str,
    tenant_id: str,
    subject: str,
    role: str,
    token_version: int,
    token_type: str,
    *,
    ttl_seconds: int,
    token_id: str | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": subject,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "roles": [role],
            "token_type": token_type,
            "token_version": token_version,
            "iss": jwt_issuer(),
            "aud": jwt_audience(),
            "iat": now,
            "exp": now + timedelta(seconds=max(60, ttl_seconds)),
            "jti": token_id or secrets.token_urlsafe(16),
        },
        jwt_signing_key(),
        algorithm="HS256",
    )


def create_access_token(user_id: str, tenant_id: str, subject: str, role: str, token_version: int, *, ttl_seconds: int) -> str:
    return _create_token(user_id, tenant_id, subject, role, token_version, "access", ttl_seconds=ttl_seconds)


def create_refresh_token(user_id: str, tenant_id: str, subject: str, role: str, token_version: int) -> tuple[str, str]:
    token_id = secrets.token_urlsafe(16)
    return (
        _create_token(
        user_id,
        tenant_id,
        subject,
        role,
        token_version,
        "refresh",
        ttl_seconds=jwt_refresh_ttl_seconds(),
        token_id=token_id,
        ),
        token_id,
    )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return header.split(" ", 1)[1].strip()


def decode_jwt(token: str, expected_type: str) -> dict:
    try:
        claims = jwt.decode(
            token,
            jwt_signing_key(),
            algorithms=["HS256"],
            audience=jwt_audience(),
            issuer=jwt_issuer(),
            options={"require": ["sub", "user_id", "tenant_id", "token_type", "token_version", "iat", "exp", "jti"]},
        )
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid bearer token") from exc
    if claims["token_type"] != expected_type:
        raise HTTPException(status_code=401, detail="incorrect JWT token type")
    return claims


def jwt_principal_from_token(token: str, expected_type: str = "access") -> Principal:
    claims = decode_jwt(token, expected_type)
    from sqlalchemy import select

    from procurement.domain.models import Permission, Role, RolePermission, User
    from procurement.platform.database import make_session_factory, session_scope

    user_id = str(claims["user_id"])
    tenant_id = str(claims["tenant_id"])
    with session_scope(make_session_factory()) as session:
        try:
            token_version = int(claims["token_version"])
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=401, detail="invalid bearer token") from exc
        user = session.get(User, user_id)
        if (
            not user
            or not user.is_active
            or user.tenant_id != tenant_id
            or user.subject != str(claims["sub"])
            or user.token_version != token_version
        ):
            raise HTTPException(status_code=401, detail="user is inactive or no longer available")
        role_name = user.role or "reviewer"
        permissions: set[str] = set()
        if user.role_id:
            role = session.get(Role, user.role_id)
            if role:
                role_name = role.name
            permissions.update(session.scalars(
                select(Permission.slug)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == user.role_id)
            ).all())
        if role_name == "admin":
            permissions.update(session.scalars(select(Permission.slug)).all())
        return Principal(
            subject=user.subject,
            tenant_id=user.tenant_id,
            roles=frozenset({role_name}),
            user_id=user.id,
            permissions=frozenset(permissions),
        )


def _jwt_principal(request: Request) -> Principal:
    return jwt_principal_from_token(_bearer_token(request))


def authenticate(request: Request, _api_token: str | None = None) -> Principal:
    auth_mode()
    principal = _jwt_principal(request)

    state = getattr(request, "state", None)
    if state is not None:
        state.principal = principal
    return principal


def require_permission(request: Request, *allowed_permissions: str) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise HTTPException(status_code=401, detail="authentication required")
    if "admin" in principal.roles:
        return principal
    if allowed_permissions and not principal.permissions.intersection(allowed_permissions):
        raise HTTPException(status_code=403, detail="insufficient permissions")
    return principal


def require_role(request: Request, *allowed: str) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not isinstance(principal, Principal):
        raise HTTPException(status_code=401, detail="authentication required")
    if "admin" in principal.roles:
        return principal
    if allowed and not principal.roles.intersection(allowed):
        raise HTTPException(status_code=403, detail="insufficient role")
    return principal
