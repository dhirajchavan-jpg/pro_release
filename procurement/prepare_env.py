"""Generate local installation secrets; never invent external identity or AI settings."""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import uuid
from pathlib import Path

from procurement.platform.auth import hash_local_password


_GENERATED = {"PROCUREMENT_API_TOKEN", "PROCUREMENT_LOCAL_TENANT_ID"}


def _value(key: str) -> str:
    return str(uuid.uuid4()) if key == "PROCUREMENT_LOCAL_TENANT_ID" else secrets.token_urlsafe(32)


def _first_free_port(preferred: int) -> int:
    for port in range(preferred, min(65536, preferred + 100)):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    raise RuntimeError("no free local PostgreSQL port was found")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", default=".env")
    parser.add_argument("--template", default=".env.example")
    args = parser.parse_args()
    target, template = Path(args.env), Path(args.template)
    created = not target.exists()
    if created:
        target.write_text(template.read_text(encoding="utf-8"), encoding="utf-8")
    lines = target.read_text(encoding="utf-8").splitlines()
    configured = {
        key: value.strip()
        for line in lines
        for key, separator, value in [line.partition("=")]
        if separator
    }
    package_local_pending = configured.get("PACKAGE_LOCAL_ENV_PENDING", "") == "1"
    bundled_postgres = (target.parent / "postgresql" / "bin" / "postgres.exe").is_file()
    postgres_values: dict[str, str] = {}
    if bundled_postgres:
        def retained(key: str, default: str) -> str:
            value = configured.get(key, "")
            return value if value and "replace_" not in value.lower() else default

        postgres_password = retained("LOCAL_POSTGRES_PASSWORD", secrets.token_urlsafe(24))
        # Launcher.Common imports the packaged .env into the setup process. On
        # first setup that value is a preference, not an operator override, so
        # it must still be checked for availability.
        explicit_postgres_port = (
            "" if package_local_pending else os.getenv("LOCAL_POSTGRES_PORT", "").strip()
        )
        postgres_port = explicit_postgres_port or retained("LOCAL_POSTGRES_PORT", "55432")
        if (created or package_local_pending) and not explicit_postgres_port:
            postgres_port = str(_first_free_port(int(postgres_port)))
        postgres_user = retained("LOCAL_POSTGRES_USER", "procurement_app")
        postgres_database = retained("LOCAL_POSTGRES_DB", "procurement")
        postgres_values = {
            "LOCAL_POSTGRES_ENABLED": "1",
            "LOCAL_POSTGRES_PORT": postgres_port,
            "LOCAL_POSTGRES_USER": postgres_user,
            "LOCAL_POSTGRES_PASSWORD": postgres_password,
            "LOCAL_POSTGRES_DB": postgres_database,
            "DATABASE_URL": (
                f"postgresql+psycopg://{postgres_user}:{postgres_password}"
                f"@127.0.0.1:{postgres_port}/{postgres_database}"
            ),
        }
    local_values = dict(postgres_values)
    if package_local_pending:
        local_values["PACKAGE_LOCAL_ENV_PENDING"] = "0"
    python_runtime = os.getenv("PROCUREMENT_SETUP_PYTHON_RUNTIME_DIR", "").strip()
    if python_runtime:
        local_values["PYTHON_RUNTIME_DIR"] = python_runtime
    bundled_converter = target.parent / "libreoffice" / "program" / "soffice.com"
    configured_converter = configured.get("OFFICE_CONVERTER_BIN", "").strip()
    if bundled_converter.is_file() and (
        not configured_converter
        or "replace_" in configured_converter.lower()
        or not Path(configured_converter).is_file()
    ):
        local_values["OFFICE_CONVERTER_BIN"] = str(bundled_converter.resolve())
    seen: set[str] = set()
    output: list[str] = []
    generated: list[str] = []
    initial_password: str | None = None
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key in local_values:
            seen.add(key)
            if (
                ((created or package_local_pending) and key in local_values)
                or not value.strip()
                or "replace_" in value.lower()
            ):
                output.append(f"{key}={local_values[key]}")
                generated.append(key)
            elif key == "OFFICE_CONVERTER_BIN" and not Path(value.strip()).is_file():
                output.append(f"{key}={local_values[key]}")
                generated.append(key)
            else:
                output.append(line)
            continue
        if separator and key == "PROCUREMENT_LOCAL_PASSWORD_HASH":
            seen.add(key)
            if not value.strip() or "replace_" in value.lower():
                initial_password = secrets.token_urlsafe(18)
                output.append(f"{key}={hash_local_password(initial_password)}")
                generated.append(key)
            else:
                output.append(line)
            continue
        if not separator or key not in _GENERATED:
            output.append(line)
            continue
        seen.add(key)
        if not value.strip() or "replace_" in value.lower():
            output.append(f"{key}={_value(key)}")
            generated.append(key)
        else:
            output.append(line)
    for key in sorted(_GENERATED - seen):
        output.append(f"{key}={_value(key)}")
        generated.append(key)
    for key in sorted(local_values.keys() - seen):
        output.append(f"{key}={local_values[key]}")
        generated.append(key)
    if "PROCUREMENT_LOCAL_PASSWORD_HASH" not in seen:
        initial_password = secrets.token_urlsafe(18)
        output.append(f"PROCUREMENT_LOCAL_PASSWORD_HASH={hash_local_password(initial_password)}")
        generated.append("PROCUREMENT_LOCAL_PASSWORD_HASH")
    target.write_text("\n".join(output) + "\n", encoding="utf-8")
    print("Generated: " + (", ".join(generated) if generated else "nothing; existing local secrets retained"))
    if initial_password:
        print(f"Initial local login: admin / {initial_password}")
    print("Still set manually: AI_PROVIDER/AI_API_KEY/AI_BASE_URL/models and AUTH_JWKS_URL/AUTH_ISSUER/AUTH_AUDIENCE.")


if __name__ == "__main__":
    main()
