"""Safely replace the single local browser credential in a packaged installation."""

from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import re
import secrets
import tempfile

from procurement.domain.models import LocalSession
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


def update_credentials(env_path: Path, username: str, password: str) -> None:
    username = username.strip()
    if not _USERNAME.fullmatch(username):
        raise ValueError("username must be 3-80 characters using letters, numbers, ., _, @, or -")
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    if password.casefold() == username.casefold():
        raise ValueError("password must not match the username")
    if not env_path.is_file():
        raise ValueError(".env was not found; run Setup.bat first")

    replacements = {
        "PROCUREMENT_LOCAL_USERNAME": username,
        "PROCUREMENT_LOCAL_PASSWORD_HASH": hash_local_password(password),
        "PROCUREMENT_API_TOKEN": secrets.token_urlsafe(32),
    }
    lines = env_path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        key, separator, _value = line.partition("=")
        if separator and key in replacements:
            output.append(f"{key}={replacements[key]}")
            seen.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in replacements.items() if key not in seen)

    _atomic_write(env_path, "\n".join(output) + "\n")


def _record_change(env_path: Path, username: str) -> None:
    load_local_env(env_path)
    tenant_id = os.getenv("PROCUREMENT_LOCAL_TENANT_ID", "").strip()
    if not tenant_id:
        raise RuntimeError("PROCUREMENT_LOCAL_TENANT_ID is not configured")
    with make_session_factory()() as session:
        active = session.get(LocalSession, tenant_id)
        if active:
            session.delete(active)
        audit(
            session,
            tenant_id=tenant_id,
            actor_user_id=None,
            event_type="local.credentials.updated",
            entity_type="tenant",
            entity_id=tenant_id,
            reason="local administrator credential rotation",
            before=None,
            after={"username": username},
        )
        session.commit()


def change_credentials(env_path: Path, username: str, password: str) -> None:
    original = env_path.read_text(encoding="utf-8")
    update_credentials(env_path, username, password)
    try:
        _record_change(env_path, username.strip())
    except Exception:
        _atomic_write(env_path, original)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or replace the local workbench login.")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()
    username = input("New username: ").strip()
    password = getpass.getpass("New password: ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("ERROR: passwords do not match.")
    try:
        change_credentials(Path(args.env), username, password)
    except (RuntimeError, ValueError) as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print(f"Local login updated for {username}. Existing browser sessions are invalidated after restart.")


if __name__ == "__main__":
    main()
