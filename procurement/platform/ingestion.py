"""Shared validation for untrusted uploaded document containers."""

from __future__ import annotations

import os
import hashlib
import shutil
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath


SUPPORTED_DOCUMENT_SUFFIXES = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".txt",
        ".md",
        ".csv",
        ".json",
        ".xml",
        ".html",
        ".htm",
    }
)
SUPPORTED_UPLOAD_SUFFIXES = SUPPORTED_DOCUMENT_SUFFIXES | {".zip"}


class IngestionValidationError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def safe_filename(value: str) -> str:
    filename = Path(value.replace("\\", "/")).name
    stem = filename.split(".", 1)[0].upper()
    if (
        not filename
        or filename.endswith((" ", "."))
        or ":" in filename
        or any(ord(character) < 32 for character in filename)
        or stem in _WINDOWS_RESERVED_NAMES
    ):
        raise IngestionValidationError(400, "upload filename is invalid")
    return filename


def validate_zip_members(
    members: Iterable[zipfile.ZipInfo],
    *,
    allowed_suffixes: set[str] | frozenset[str] | None = None,
    maximum_files: int | None = None,
    maximum_total_bytes: int | None = None,
    maximum_entry_bytes: int | None = None,
    maximum_ratio: int | None = None,
) -> list[zipfile.ZipInfo]:
    maximum_files = maximum_files or max(1, int(os.getenv("MAX_EXTRACTED_FILES", "80")))
    maximum_total_bytes = maximum_total_bytes or (
        max(1, int(os.getenv("MAX_EXTRACTED_MB", "250"))) * 1024 * 1024
    )
    maximum_entry_bytes = maximum_entry_bytes or (
        max(1, int(os.getenv("MAX_ZIP_ENTRY_MB", "80"))) * 1024 * 1024
    )
    maximum_ratio = maximum_ratio or max(1, int(os.getenv("MAX_ZIP_COMPRESSION_RATIO", "200")))
    all_members = list(members)

    for member in all_members:
        member_path = PurePosixPath(member.filename.replace("\\", "/"))
        mode = member.external_attr >> 16
        if (
            member_path.is_absolute()
            or ".." in member_path.parts
            or mode & 0o170000 == 0o120000
        ):
            raise IngestionValidationError(400, "archive contains an unsafe zip path or link")
        for part in member_path.parts:
            safe_filename(part)
        if member.flag_bits & 0x1:
            raise IngestionValidationError(400, "encrypted archive members are not supported")

    documents = [member for member in all_members if not member.is_dir()]
    if not documents:
        raise IngestionValidationError(400, "archive contains no documents")
    if len(documents) > maximum_files:
        raise IngestionValidationError(413, f"archive contains more than {maximum_files} files")

    total_size = 0
    for member in documents:
        if member.file_size > maximum_entry_bytes:
            raise IngestionValidationError(413, "archive member exceeds configured maximum")
        if member.file_size / max(1, member.compress_size) > maximum_ratio:
            raise IngestionValidationError(413, "archive compression ratio is unsafe")
        if (
            allowed_suffixes is not None
            and Path(member.filename).suffix.lower() not in allowed_suffixes
        ):
            raise IngestionValidationError(415, "archive contains an unsupported document type")
        total_size += member.file_size
    if total_size > maximum_total_bytes:
        raise IngestionValidationError(413, "archive expands beyond configured maximum")
    return documents


def validate_zip_file(
    path: Path,
    *,
    allowed_suffixes: set[str] | frozenset[str] | None = None,
) -> list[zipfile.ZipInfo]:
    try:
        with zipfile.ZipFile(path) as archive:
            members = validate_zip_members(
                archive.infolist(), allowed_suffixes=allowed_suffixes
            )
            if archive.testzip() is not None:
                raise IngestionValidationError(415, "invalid ZIP file")
            return members
    except (zipfile.BadZipFile, EOFError, RuntimeError) as exc:
        raise IngestionValidationError(415, "invalid ZIP file") from exc


def unpack_upload(
    upload_path: Path,
    extract_dir: Path,
    *,
    allowed_suffixes: set[str] | frozenset[str] = SUPPORTED_DOCUMENT_SUFFIXES,
    maximum_files: int | None = None,
    maximum_total_bytes: int | None = None,
    maximum_entry_bytes: int | None = None,
    maximum_ratio: int | None = None,
) -> list[Path]:
    extract_dir.mkdir(parents=True, exist_ok=True)
    suffix = upload_path.suffix.lower()
    if suffix != ".zip":
        safe_filename(upload_path.name)
        if suffix not in allowed_suffixes:
            raise IngestionValidationError(415, "unsupported document type")
        target = extract_dir / upload_path.name
        shutil.copy2(upload_path, target)
        return [target]

    try:
        with zipfile.ZipFile(upload_path) as archive:
            members = validate_zip_members(
                archive.infolist(),
                allowed_suffixes=allowed_suffixes,
                maximum_files=maximum_files,
                maximum_total_bytes=maximum_total_bytes,
                maximum_entry_bytes=maximum_entry_bytes,
                maximum_ratio=maximum_ratio,
            )
            output: list[Path] = []
            seen_paths: set[str] = set()
            seen_content: set[str] = set()
            for index, member in enumerate(members, start=1):
                member_path = PurePosixPath(member.filename.replace("\\", "/"))
                relative_path = member_path.as_posix()
                copy_number = 0
                while relative_path.casefold() in seen_paths:
                    copy_number += 1
                    prefix = f"{index}" if copy_number == 1 else f"{index}-{copy_number}"
                    relative_path = str(
                        PurePosixPath(member_path.parent) / f"{prefix}-{member_path.name}"
                    )
                seen_paths.add(relative_path.casefold())
                target = extract_dir.joinpath(*PurePosixPath(relative_path).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as source, target.open("wb") as destination:
                    shutil.copyfileobj(source, destination)
                with target.open("rb") as extracted:
                    digest = hashlib.file_digest(extracted, "sha256").hexdigest()
                if digest in seen_content:
                    target.unlink()
                    continue
                seen_content.add(digest)
                output.append(target)
            return output
    except zipfile.BadZipFile as exc:
        raise IngestionValidationError(415, "invalid ZIP file") from exc


def validate_pdf_file(path: Path, *, maximum_pages: int | None = None) -> None:
    if path.read_bytes()[:1024].lstrip()[:5] != b"%PDF-":
        raise IngestionValidationError(415, "invalid PDF file")

    from pypdf import PdfReader

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise IngestionValidationError(415, "invalid PDF file") from exc
    if reader.is_encrypted:
        raise IngestionValidationError(400, "encrypted PDFs are not supported")
    maximum_pages = maximum_pages or max(1, int(os.getenv("MAX_PDF_PAGES", "500")))
    if len(reader.pages) > maximum_pages:
        raise IngestionValidationError(413, "PDF exceeds configured page limit")


def validate_document_container(
    path: Path,
    suffix: str,
    *,
    allowed_zip_suffixes: set[str] | frozenset[str] | None = None,
    maximum_pdf_pages: int | None = None,
) -> None:
    suffix = suffix.lower()
    if suffix == ".pdf":
        validate_pdf_file(path, maximum_pages=maximum_pdf_pages)
    elif suffix == ".zip":
        validate_zip_file(path, allowed_suffixes=allowed_zip_suffixes)
    elif suffix in {".docx", ".xlsx"}:
        try:
            validate_zip_file(path)
        except IngestionValidationError as exc:
            if exc.detail != "invalid ZIP file":
                raise
            raise IngestionValidationError(415, "invalid Office Open XML document") from exc
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
        except zipfile.BadZipFile as exc:
            raise IngestionValidationError(415, "invalid Office Open XML document") from exc
        required = "word/document.xml" if suffix == ".docx" else "xl/workbook.xml"
        if "[Content_Types].xml" not in names or required not in names:
            raise IngestionValidationError(415, "invalid Office Open XML document")
    elif suffix in {".doc", ".xls"}:
        if path.read_bytes()[:8] != bytes.fromhex("D0CF11E0A1B11AE1"):
            raise IngestionValidationError(415, "invalid legacy Office document")
    elif suffix in {".csv", ".txt", ".md", ".json", ".xml", ".html", ".htm"}:
        if b"\x00" in path.read_bytes()[:65536]:
            raise IngestionValidationError(415, "text upload contains binary data")
    else:
        raise IngestionValidationError(415, "unsupported document type")
