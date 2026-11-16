"""Create or update the administrator account directly in the database."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import secrets
import tempfile
import uuid

from sqlalchemy import select

from procurement.domain.models import Role, Tenant, User, UserSession
from procurement.platform.auth import hash_local_password
from procurement.platform.database import make_session_factory
from procurement.platform.env import load_local_env
from procurement.platform.operations import audit


_USERNAME = re.compile(r"^[A-Za-z0-9._@-]{3,80}$")


def _atomic_write(path: Path, content: str) -> None:
    handle, temporary = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_session_secret(env_path: Path) -> str:
    if not env_path.is_file():
        raise ValueError(".env was not found; run Setup.bat first")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    configured: dict[str, str] = {}
    for line in lines:
        k, sep, v = line.partition("=")
        if sep:
            configured[k.strip()] = v.strip()

    secret = configured.get("SESSION_SECRET_KEY", "")
    session_secret_changed = not secret or "replace_" in secret.lower()
    if session_secret_changed:
        secret = secrets.token_hex(32)
        configured["SESSION_SECRET_KEY"] = secret

    # Remove legacy .env credentials if present. This must happen even when
    # the existing session secret does not need to be rotated.
    obsolete_keys = (
        "PROCUREMENT_LOCAL_PASSWORD_HASH",
        "ALLOW_DIRECT_API_TOKEN_AUTH",
        "PROCUREMENT_API_TOKEN",
    )
    removed_legacy_key = any(key in configured for key in obsolete_keys)
    for obsolete in obsolete_keys:
        configured.pop(obsolete, None)

    if session_secret_changed or removed_legacy_key:
        output: list[str] = []
        seen = set()
        for line in lines:
            k, sep, _ = line.partition("=")
            clean_k = k.strip()
            if sep and clean_k in configured:
                output.append(f"{clean_k}={configured[clean_k]}")
                seen.add(clean_k)
            elif not sep or clean_k not in obsolete_keys:
                output.append(line)
        for k, v in configured.items():
            if k not in seen:
                output.append(f"{k}={v}")

        _atomic_write(env_path, "\n".join(output) + "\n")

    return secret


def set_admin_credentials(env_path: Path, username: str, password: str) -> None:
    username = username.strip()
    if not _USERNAME.fullmatch(username):
        raise ValueError("username must be 3-80 characters using letters, numbers, ., _, @, or -")
    if len(password) < 8:
        raise ValueError("password must contain at least 8 characters")
    if password.casefold() == username.casefold():
        raise ValueError("password must not match the username")

    ensure_session_secret(env_path)
    load_local_env(env_path)

    factory = make_session_factory()
    with factory() as session:
        # Find or create a default tenant
        tenant = session.scalar(select(Tenant).order_by(Tenant.created_at).limit(1))
        if tenant is None:
            tenant = Tenant(id=str(uuid.uuid4()), name=f"Workspace: {username}")
            session.add(tenant)
            session.flush()

        tenant_id = tenant.id

        # Find or create admin role
        admin_role = session.scalar(select(Role).where(Role.name == "admin"))
        if admin_role is None:
            admin_role = Role(
                id=str(uuid.uuid4()),
                tenant_id=None,
                name="admin",
                description="System administrator",
                is_system=True,
            )
            session.add(admin_role)
            session.flush()

        # Find existing admin or user
        subject = f"local:{username}"
        user = session.scalar(
            select(User).where(
                User.tenant_id == tenant_id,
                (User.email == username) | (User.subject == subject),
            )
        )
        if user is None:
            user = User(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                subject=subject,
                email=username,
                display_name=username,
                role="admin",
                role_id=admin_role.id,
                password_hash=hash_local_password(password),
                is_active=True,
            )
            session.add(user)
            session.flush()
        else:
            user.password_hash = hash_local_password(password)
            user.role = "admin"
            user.role_id = admin_role.id
            user.is_active = True

        # Invalidate any existing sessions for this user
        existing_sessions = session.scalars(
            select(UserSession).where(UserSession.user_id == user.id)
        ).all()
        for sess in existing_sessions:
            session.delete(sess)

        audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=user.id,
            event_type="local.credentials.updated",
            entity_type="user",
            entity_id=user.id,
            reason="administrator credential rotation",
            before=None,
            after={"username": username},
        )
        session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or replace the local workbench admin login.")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    username = input("Admin username or email: ").strip()
    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("ERROR: passwords do not match.")
    try:
        set_admin_credentials(Path(args.env), username, password)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print(f"Administrator account updated for '{username}'. Active sessions have been invalidated.")


if __name__ == "__main__":
    main()
