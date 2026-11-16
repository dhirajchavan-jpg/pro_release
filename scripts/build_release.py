"""Build a clean, relocatable Windows release from an accepted installation."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil
import tempfile
import zipfile


INCLUDED_DIRECTORIES = {
    "alembic", "config", "docs", "frontend", "installers", "postgresql",
    "procurement", "scripts", "SECURITY", "wheelhouse",
}
INCLUDED_FILES = {
    ".env", ".env.example", "ai_provider.py", "alembic.ini", "app.py", "base_rfp.py",
    "Cost.bat", "Create_Credentials.bat", "Deploy.bat", "Disable_Autostart.bat",
    "Enable_Autostart.bat", "INSTALL_WINDOWS.md", "Logs.bat", "pyproject.toml",
    "python-3.12.10-amd64.exe", "README.md", "requirements-security.txt",
    "requirements.txt", "Restart_All.bat", "rfp_atoms.py", "Self_Check.bat", "Setup.bat",
    "Start_All.bat", "Stop_All.bat", "Verify_Package.bat",
}
TRANSIENT_NAMES = {".pytest_cache", "__pycache__"}
TRANSIENT_SUFFIXES = {".pyc", ".pyo"}
MACHINE_LOCAL_ENV = {
    "DATABASE_URL": "postgresql+psycopg://procurement_app:replace_me@127.0.0.1:55432/procurement",
    "LOCAL_POSTGRES_PASSWORD": "replace_with_generated_local_postgres_password",
    "LOCAL_POSTGRES_PORT": "55432",
    "OFFICE_CONVERTER_BIN": "",
    "PACKAGE_LOCAL_ENV_PENDING": "1",
    "PROCUREMENT_LOCAL_TENANT_ID": "compatibility",
    "PYTHON_RUNTIME_DIR": "",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ignore_transient(_directory: str, names: list[str]) -> set[str]:
    return {
        name for name in names
        if name in TRANSIENT_NAMES or Path(name).suffix.casefold() in TRANSIENT_SUFFIXES
    }


def _rewrite_machine_local_env(path: Path) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        key, separator, _value = line.partition("=")
        if separator and key in MACHINE_LOCAL_ENV:
            output.append(f"{key}={MACHINE_LOCAL_ENV[key]}")
            seen.add(key)
        else:
            output.append(line)
    output.extend(f"{key}={value}" for key, value in MACHINE_LOCAL_ENV.items() if key not in seen)
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _manifest(root: Path) -> None:
    target = root / "PACKAGE_MANIFEST.sha256"
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file() and item != target):
        digest = _sha256(path)
        rows.append(f"{digest}  {path.relative_to(root).as_posix()}")
    target.write_text("\n".join(rows) + "\n", encoding="utf-8")


def build(source: Path, output: Path, archive: Path, test_files: list[Path]) -> None:
    source, output, archive = source.resolve(), output.resolve(), archive.resolve()
    if output == source or source in output.parents:
        raise ValueError("output directory must be outside the source installation")
    if output.exists() or archive.exists():
        raise ValueError("output directory and archive must not already exist")
    if not (source / ".env").is_file():
        raise ValueError("source .env is missing")

    output.mkdir(parents=True)
    for name in sorted(INCLUDED_DIRECTORIES):
        path = source / name
        if path.is_dir():
            shutil.copytree(path, output / name, ignore=_ignore_transient, copy_function=shutil.copy2)
    for name in sorted(INCLUDED_FILES):
        path = source / name
        if path.is_file():
            shutil.copy2(path, output / name)
    react_dist = source / "frontend_pro" / "dist"
    if react_dist.is_dir():
        shutil.copytree(react_dist, output / "frontend_pro" / "dist", copy_function=shutil.copy2)
    _rewrite_machine_local_env(output / ".env")
    for folder in ("cache", "jobs", "logs", "objects", "postgres"):
        (output / "runtime" / folder).mkdir(parents=True, exist_ok=True)
    test_root = output / "Test Files"
    test_root.mkdir()
    for path in test_files:
        if not path.is_file():
            raise ValueError(f"test file is missing: {path}")
        shutil.copy2(path, test_root / path.name)
    _manifest(output)

    archive.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(delete=False, dir=archive.parent, suffix=".zip") as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
            for path in sorted(item for item in output.rglob("*") if item.is_file()):
                bundle.write(path, path.relative_to(output).as_posix())
        temporary_path.replace(archive)
    finally:
        temporary_path.unlink(missing_ok=True)
    digest = _sha256(archive)
    archive.with_suffix(archive.suffix + ".sha256").write_text(
        f"{digest}  {archive.name}\n", encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--test-file", action="append", type=Path, default=[])
    args = parser.parse_args()
    try:
        build(args.source, args.output, args.archive, args.test_file)
    except ValueError as exc:
        raise SystemExit(f"ERROR: {exc}") from exc
    print(f"Release directory: {args.output.resolve()}")
    print(f"Release archive: {args.archive.resolve()}")


if __name__ == "__main__":
    main()
