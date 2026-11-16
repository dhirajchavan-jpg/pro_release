"""Secure document-package loading and native extraction."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

from procurement.domain.schemas import DocumentRecord, ExtractedDocument, PageUnit
from procurement.domain.text import clean_text
from procurement.platform.ingestion import (
    SUPPORTED_DOCUMENT_SUFFIXES,
    unpack_upload as unpack_validated_upload,
    validate_document_container as validate_uploaded_document_container,
)
from procurement.understanding.layout import make_page_unit
from procurement.understanding.native import (
    extract_docx_layout,
    extract_pdf as extract_native_pdf,
    extract_xlsx_pages,
)
from procurement.understanding.quality import MAX_DOC_CHARS


MAX_EXTRACTED_FILES = int(os.getenv("MAX_EXTRACTED_FILES", "80"))
MAX_EXTRACTED_MB = int(os.getenv("MAX_EXTRACTED_MB", "250"))
MAX_ZIP_ENTRY_MB = int(os.getenv("MAX_ZIP_ENTRY_MB", "80"))
MAX_ZIP_COMPRESSION_RATIO = int(os.getenv("MAX_ZIP_COMPRESSION_RATIO", "200"))
MAX_PDF_PAGES = int(os.getenv("MAX_PDF_PAGES", "500"))
OFFICE_CONVERTER_BIN = os.getenv("OFFICE_CONVERTER_BIN", "libreoffice").strip()
OFFICE_CONVERTER_TIMEOUT_SECONDS = max(
    10, int(os.getenv("OFFICE_CONVERTER_TIMEOUT_SECONDS", "120"))
)


def load_upload_documents(
    upload_path: Path,
    extract_dir: Path,
    side: Literal["tender", "vendor"],
    *,
    input_role: Literal["tender_packet", "evaluation_guideline", "vendor_submission"] | None = None,
    document_id_prefix: str | None = None,
) -> list[ExtractedDocument]:
    files = unpack_upload(upload_path, extract_dir)
    if len(files) > MAX_EXTRACTED_FILES:
        raise ValueError(f"{side} upload contains more than {MAX_EXTRACTED_FILES} files")

    documents: list[ExtractedDocument] = []
    for index, file_path in enumerate(files, start=1):
        document_id = f"{document_id_prefix or side}-{index}"
        text, status, warnings, pages = extract_text(file_path, side, document_id)
        file_name = file_path.relative_to(extract_dir).as_posix()
        for page in pages:
            page.file_name = file_name
        record = DocumentRecord(
            document_id=document_id,
            file_name=file_name,
            upload_side=side,
            input_role=input_role or ("vendor_submission" if side == "vendor" else "tender_packet"),
            extraction_status=status,
            text_chars=len(text),
            page_count=len(pages),
            warnings=warnings,
        )
        documents.append(ExtractedDocument(record, text[:MAX_DOC_CHARS], pages, file_path))
    return documents


def unpack_upload(upload_path: Path, extract_dir: Path) -> list[Path]:
    return unpack_validated_upload(
        upload_path,
        extract_dir,
        allowed_suffixes=SUPPORTED_DOCUMENT_SUFFIXES,
        maximum_files=MAX_EXTRACTED_FILES,
        maximum_total_bytes=MAX_EXTRACTED_MB * 1024 * 1024,
        maximum_entry_bytes=MAX_ZIP_ENTRY_MB * 1024 * 1024,
        maximum_ratio=MAX_ZIP_COMPRESSION_RATIO,
    )


def extract_text(
    path: Path,
    side: Literal["tender", "vendor"] = "tender",
    document_id: str = "document",
) -> tuple[str, Literal["ok", "empty", "unsupported", "error"], list[str], list[PageUnit]]:
    try:
        suffix = path.suffix.lower()
        if suffix in {".doc", ".xls"}:
            converted = convert_legacy_office_document(path)
            return extract_text(converted, side, document_id)
        if suffix == ".pdf":
            validate_pdf_file(path)
            text, warnings, pages = extract_pdf(path, side, document_id)
            status = "ok" if text else "empty"
            if not text:
                warnings.append("no extractable text found")
            return text, status, warnings, pages
        if suffix == ".docx":
            validate_office_container(path)
            text, blocks, tables = extract_docx_layout(path, document_id)
            text = clean_text(text)
            warnings = [] if text else ["no extractable text found"]
            pages = [
                make_page_unit(
                    document_id,
                    path.name,
                    side,
                    1,
                    text,
                    "python_docx",
                    False,
                    warnings,
                    text,
                    blocks,
                    tables,
                )
            ]
            return (text, "ok", warnings, pages) if text else ("", "empty", warnings, pages)
        if suffix == ".xlsx":
            validate_office_container(path)
            text, pages = extract_xlsx_pages(path, side, document_id)
            warnings = [] if text else ["no extractable text found"]
            return (text, "ok", warnings, pages) if text else ("", "empty", warnings, pages)
        if suffix in {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm"}:
            text = path.read_text(encoding="utf-8", errors="ignore")
        else:
            return "", "unsupported", [f"unsupported file type: {suffix or 'none'}"], []
        text = clean_text(text)
        warnings = [] if text else ["no extractable text found"]
        pages = [make_page_unit(document_id, path.name, side, 1, text, "local", False, warnings)]
        return (text, "ok", warnings, pages) if text else ("", "empty", warnings, pages)
    except Exception as exc:
        return "", "error", [str(exc)], []


def validate_pdf_file(path: Path) -> None:
    validate_uploaded_document_container(path, ".pdf", maximum_pdf_pages=MAX_PDF_PAGES)


def convert_legacy_office_document(path: Path) -> Path:
    """Convert DOC/XLS only in the isolated worker directory; never invoke a shell."""
    validate_uploaded_document_container(path, path.suffix.lower())
    output_dir = path.parent / "converted"
    output_dir.mkdir(exist_ok=True)
    try:
        # ponytail: Windows may hold profile handles briefly; job-directory cleanup removes leftovers.
        with tempfile.TemporaryDirectory(
            prefix="libreoffice-", dir=output_dir, ignore_cleanup_errors=True
        ) as profile:
            subprocess.run(
                [
                    OFFICE_CONVERTER_BIN,
                    f"-env:UserInstallation={Path(profile).resolve().as_uri()}",
                    "--headless",
                    "--convert-to",
                    "pdf",
                    "--outdir",
                    str(output_dir),
                    str(path),
                ],
                check=True,
                capture_output=True,
                timeout=OFFICE_CONVERTER_TIMEOUT_SECONDS,
                text=True,
            )
    except FileNotFoundError as exc:
        raise ValueError(
            "DOC/XLS requires LibreOffice in the worker image (OFFICE_CONVERTER_BIN)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError("DOC/XLS conversion timed out") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"DOC/XLS conversion failed: {clean_text(exc.stderr)[:240]}") from exc
    converted = output_dir / f"{path.stem}.pdf"
    if not converted.is_file():
        raise ValueError("DOC/XLS conversion produced no PDF")
    validate_pdf_file(converted)
    return converted


def validate_office_container(path: Path) -> None:
    validate_uploaded_document_container(path, path.suffix.lower())


def extract_pdf(
    path: Path,
    side: Literal["tender", "vendor"],
    document_id: str,
) -> tuple[str, list[str], list[PageUnit]]:
    return extract_native_pdf(path, side, document_id, MAX_PDF_PAGES)
