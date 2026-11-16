"""Modern SaaS authentication and role-based access control (RBAC)."""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
import os
import secrets
import time

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
    mode = os.getenv("AUTH_MODE", "session").strip().lower()
    if mode in {"api_token", "session"}:
        return "session"
    if mode == "jwt":
        return "jwt"
    raise RuntimeError("AUTH_MODE must be 'session' or 'jwt'")


def session_secret_key() -> str:
    secret = os.getenv("SESSION_SECRET_KEY", "").strip()
    if not secret:
        raise RuntimeError("SESSION_SECRET_KEY is required in .env")
    return secret


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


@lru_cache(maxsize=1)
def _auth_sessions():
    from procurement.platform.database import make_session_factory

    return make_session_factory()


def _session_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def sign_session_token(token: str) -> str:
    secret = session_secret_key()
    signature = hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()
    return f"{token}.{signature}"


def verify_session_token(cookie_value: str) -> str | None:
    try:
        token, signature = cookie_value.split(".", 1)
        expected = hmac.new(session_secret_key().encode(), token.encode(), hashlib.sha256).hexdigest()
        if secrets.compare_digest(signature, expected):
            return token
        return None
    except (ValueError, TypeError, RuntimeError):
        return None


def create_user_session(
    user_id: str,
    tenant_id: str,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
    ttl_seconds: int = 28800,
) -> str:
    from procurement.domain.models import UserSession
    from procurement.platform.database import session_scope

    raw_token = secrets.token_urlsafe(32)
    token_hash = _session_digest(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(300, ttl_seconds))

    with session_scope(_auth_sessions()) as session:
        user_session = UserSession(
            id=token_hash,
            user_id=user_id,
            tenant_id=tenant_id,
            user_agent=user_agent[:512] if user_agent else None,
            ip_address=ip_address[:64] if ip_address else None,
            expires_at=expires_at,
        )
        session.add(user_session)

    return sign_session_token(raw_token)


def authenticate_user_session(cookie_value: str) -> Principal | None:
    from sqlalchemy import select

    from procurement.domain.models import Permission, Role, RolePermission, User, UserSession
    from procurement.platform.database import session_scope

    raw_token = verify_session_token(cookie_value)
    if not raw_token:
        return None

    token_hash = _session_digest(raw_token)
    with session_scope(_auth_sessions()) as session:
        sess = session.get(UserSession, token_hash)
        if sess is None:
            return None

        expires_at = sess.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            session.delete(sess)
            return None

        user = session.get(User, sess.user_id)
        if user is None or not user.is_active:
            return None

        role_name = user.role or "reviewer"
        permissions: set[str] = set()

        if user.role_id:
            role = session.get(Role, user.role_id)
            if role:
                role_name = role.name
            role_perms = session.scalars(
                select(Permission.slug)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .where(RolePermission.role_id == user.role_id)
            ).all()
            permissions.update(role_perms)

        if role_name == "admin":
            all_perms = session.scalars(select(Permission.slug)).all()
            permissions.update(all_perms)

        return Principal(
            subject=user.subject,
            tenant_id=user.tenant_id,
            roles=frozenset({role_name}),
            user_id=user.id,
            permissions=frozenset(permissions),
        )


def revoke_user_session(cookie_value: str) -> bool:
    from procurement.domain.models import UserSession
    from procurement.platform.database import session_scope

    raw_token = verify_session_token(cookie_value)
    if not raw_token:
        return False

    token_hash = _session_digest(raw_token)
    with session_scope(_auth_sessions()) as session:
        sess = session.get(UserSession, token_hash)
        if sess is not None:
            session.delete(sess)
            return True
        return False


def create_api_key(
    tenant_id: str,
    name: str,
    scopes: list[str],
    *,
    ttl_days: int | None = None,
) -> tuple[str, str]:
    """Generate a new scoped API key. Returns (key_id, raw_key). Raw key is only shown once."""
    from procurement.domain.models import ApiKey
    from procurement.platform.database import session_scope

    raw_secret = secrets.token_urlsafe(32)
    full_key = f"proc_live_{raw_secret}"
    key_hash = _session_digest(full_key)
    prefix = full_key[:14]
    expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl_days)) if ttl_days else None

    with session_scope(_auth_sessions()) as session:
        key_record = ApiKey(
            tenant_id=tenant_id,
            name=name.strip(),
            prefix=prefix,
            key_hash=key_hash,
            scopes=scopes,
            expires_at=expires_at,
        )
        session.add(key_record)
        session.flush()
        key_id = key_record.id

    return key_id, full_key


def authenticate_api_key(raw_token: str) -> Principal | None:
    from sqlalchemy import select

    from procurement.domain.models import ApiKey
    from procurement.platform.database import session_scope

    if not raw_token.startswith("proc_live_"):
        return None

    key_hash = _session_digest(raw_token)
    with session_scope(_auth_sessions()) as session:
        key_record = session.scalar(select(ApiKey).where(ApiKey.key_hash == key_hash))
        if key_record is None:
            return None

        if key_record.expires_at is not None:
            expires_at = key_record.expires_at
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return None

        scopes = frozenset(str(scope) for scope in (key_record.scopes or []))
        return Principal(
            subject=f"apikey:{key_record.name}",
            tenant_id=key_record.tenant_id,
            roles=frozenset({"api_key"}),
            user_id=None,
            permissions=scopes,
        )


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return header.split(" ", 1)[1].strip()


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


def authenticate(request: Request, _api_token: str | None = None) -> Principal:
    if auth_mode() == "jwt":
        principal = _jwt_principal(request)
    else:
        # 1. Try browser session cookie
        cookie = getattr(request, "cookies", {}).get("procurement_session", "")
        if cookie:
            try:
                principal = authenticate_user_session(cookie)
                if principal is None:
                    raise HTTPException(status_code=401, detail="session was replaced or expired")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(status_code=503, detail="session store is unavailable") from exc
        else:
            # 2. Try a database-backed bearer session, then a scoped API key.
            token = request.headers.get("x-api-key", "")
            header = request.headers.get("authorization", "")
            bearer_token = header.lower().startswith("bearer ")
            if bearer_token:
                token = header.split(" ", 1)[1].strip()

            if not token:
                raise HTTPException(status_code=401, detail="missing authentication credentials")

            principal = authenticate_user_session(token) if bearer_token else None
            if principal is None:
                principal = authenticate_api_key(token)
            if principal is None:
                raise HTTPException(status_code=401, detail="missing or invalid bearer token or API key")

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
