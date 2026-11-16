"""Local-filesystem or S3-compatible object storage boundary."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from procurement.platform.env import env_int


class ObjectStorageError(RuntimeError):
    pass


class DiskCapacityError(ObjectStorageError):
    pass


def _backend() -> str:
    value = os.getenv("OBJECT_STORAGE_BACKEND", "filesystem").strip().lower()
    if value not in {"filesystem", "s3"}:
        raise ObjectStorageError("OBJECT_STORAGE_BACKEND must be filesystem or s3")
    return value


def _safe_key(key: str) -> str:
    path = PurePosixPath(key)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ObjectStorageError("unsafe object key")
    return str(path)


def _local_path(key: str) -> Path:
    root = Path(os.getenv("OBJECT_STORAGE_LOCAL_ROOT", "samples/runs/objects")).resolve()
    target = (root / _safe_key(key)).resolve()
    if target != root and root not in target.parents:
        raise ObjectStorageError("unsafe object key")
    return target


def _native_path(path: Path) -> str:
    value = str(path)
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value.lstrip("\\")
    return "\\\\?\\" + value


def ensure_free_disk(path: Path, *, additional_bytes: int = 0) -> None:
    location = path.resolve()
    while not location.exists() and location != location.parent:
        location = location.parent
    reserve = env_int("MIN_FREE_DISK_MB", 1024, minimum=1) * 1024 * 1024
    free = shutil.disk_usage(_native_path(location)).free
    required = reserve + max(0, int(additional_bytes))
    if free < required:
        raise DiskCapacityError(
            f"insufficient disk space: {free // (1024 * 1024)} MB free; "
            f"{required // (1024 * 1024)} MB required"
        )


def _s3_client():
    try:
        import boto3
    except ImportError as exc:
        raise ObjectStorageError("boto3 is required for OBJECT_STORAGE_BACKEND=s3") from exc
    access_key = os.getenv("OBJECT_STORAGE_ACCESS_KEY_ID", "").strip()
    secret_key = os.getenv("OBJECT_STORAGE_SECRET_ACCESS_KEY", "").strip()
    if not access_key or not secret_key or not os.getenv("OBJECT_STORAGE_BUCKET", "").strip():
        raise ObjectStorageError("S3 bucket and credentials must be configured")
    from botocore.config import Config

    addressing_style = os.getenv("OBJECT_STORAGE_ADDRESSING_STYLE", "auto").strip().lower()
    if addressing_style not in {"auto", "path", "virtual"}:
        raise ObjectStorageError("OBJECT_STORAGE_ADDRESSING_STYLE must be auto, path or virtual")
    ca_bundle = os.getenv("OBJECT_STORAGE_CA_BUNDLE", "").strip()
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("OBJECT_STORAGE_ENDPOINT_URL", "").strip() or None,
        region_name=os.getenv("OBJECT_STORAGE_REGION", "ap-south-1"),
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=os.getenv("OBJECT_STORAGE_SESSION_TOKEN", "").strip() or None,
        verify=ca_bundle or True,
        config=Config(s3={"addressing_style": addressing_style}),
    )


def put_file(source: Path, key: str, *, content_type: str) -> None:
    key = _safe_key(key)
    if _backend() == "filesystem":
        target = _local_path(key)
        Path(_native_path(target.parent)).mkdir(parents=True, exist_ok=True)
        source = source.resolve()
        ensure_free_disk(target.parent, additional_bytes=source.stat().st_size)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=_native_path(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            os.replace(_native_path(temporary), _native_path(target))
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise
        return
    _s3_client().upload_file(str(source), os.environ["OBJECT_STORAGE_BUCKET"], key, ExtraArgs={"ContentType": content_type, "ServerSideEncryption": "AES256"})


def download_file(key: str, destination: Path) -> None:
    key = _safe_key(key)
    Path(_native_path(destination.resolve().parent)).mkdir(parents=True, exist_ok=True)
    if _backend() == "filesystem":
        shutil.copyfile(_native_path(_local_path(key)), _native_path(destination.resolve()))
        return
    _s3_client().download_file(os.environ["OBJECT_STORAGE_BUCKET"], key, str(destination))


def delete_file(key: str) -> None:
    key = _safe_key(key)
    if _backend() == "filesystem":
        Path(_native_path(_local_path(key))).unlink(missing_ok=True)
        return
    _s3_client().delete_object(Bucket=os.environ["OBJECT_STORAGE_BUCKET"], Key=key)


def list_keys(prefix: str = "") -> set[str]:
    """List stored keys for operator consistency checks; never mutates storage."""
    prefix = prefix.strip("/")
    if prefix:
        _safe_key(prefix)
    if _backend() == "filesystem":
        root = Path(os.getenv("OBJECT_STORAGE_LOCAL_ROOT", "samples/runs/objects")).resolve()
        start = _local_path(prefix) if prefix else root
        if not start.exists():
            return set()
        return {
            path.relative_to(root).as_posix()
            for path in start.rglob("*")
            if path.is_file()
        }
    client = _s3_client()
    paginator = client.get_paginator("list_objects_v2")
    return {
        str(item["Key"])
        for page in paginator.paginate(
            Bucket=os.environ["OBJECT_STORAGE_BUCKET"],
            Prefix=f"{prefix}/" if prefix else "",
        )
        for item in page.get("Contents", [])
        if item.get("Key")
    }


def ready() -> bool:
    try:
        if _backend() == "filesystem":
            root = Path(os.getenv("OBJECT_STORAGE_LOCAL_ROOT", "samples/runs/objects"))
            root.mkdir(parents=True, exist_ok=True)
            return root.is_dir()
        _s3_client().head_bucket(Bucket=os.environ["OBJECT_STORAGE_BUCKET"])
        return True
    except Exception:
        return False
