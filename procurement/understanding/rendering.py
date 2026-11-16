"""PDF page rendering shared by understanding and evidence review."""

from __future__ import annotations

import base64
import io
import os
import threading
from functools import lru_cache
from pathlib import Path


# ponytail: pypdfium2's Windows DLL is not safe under concurrent page rendering; use processes if profiling later requires parallel renders.
PDF_RENDER_LOCK = threading.Lock()


def pdf_page_base64(path: Path, zero_based_page: int) -> str:
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(str(path))
    writer = PdfWriter()
    writer.add_page(reader.pages[zero_based_page])
    buffer = io.BytesIO()
    writer.write(buffer)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


@lru_cache(maxsize=max(1, int(os.getenv("EVIDENCE_PDF_RENDER_CACHE_PAGES", "48"))))
def render_pdf_page_png_b64(path: Path, zero_based_page: int) -> str:
    import pypdfium2 as pdfium

    with PDF_RENDER_LOCK:
        pdf = pdfium.PdfDocument(str(path))
        try:
            page = pdf[zero_based_page]
            try:
                image = page.render(scale=2).to_pil()
                try:
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    return base64.b64encode(buffer.getvalue()).decode("ascii")
                finally:
                    image.close()
            finally:
                page.close()
        finally:
            pdf.close()
