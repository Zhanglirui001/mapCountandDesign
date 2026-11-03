"""PDF upload validation helpers for AI statistics."""

from __future__ import annotations

from enum import Enum
from pathlib import Path

from PySide6.QtPdf import QPdfDocument

MAX_PDF_BYTES = 500 * 1024 * 1024


class PdfUploadError(Enum):
    UNSUPPORTED_FILE_TYPE = "unsupported_file_type"
    PDF_ENCRYPTED = "pdf_encrypted"
    FILE_TOO_LARGE = "file_too_large"
    NO_READABLE_PAGES = "no_readable_pages"
    PAGE_RENDER_FAILED = "page_render_failed"
    UPLOAD_INTERRUPTED = "upload_interrupted"


def validate_pdf_path(path: Path) -> PdfUploadError | None:
    """Return a pre-load validation error, or ``None`` when the path looks valid."""
    if path.suffix.lower() != ".pdf":
        return PdfUploadError.UNSUPPORTED_FILE_TYPE
    if not path.is_file():
        return PdfUploadError.PAGE_RENDER_FAILED
    try:
        size = path.stat().st_size
    except OSError:
        return PdfUploadError.PAGE_RENDER_FAILED
    if size == 0:
        return PdfUploadError.NO_READABLE_PAGES
    if size > MAX_PDF_BYTES:
        return PdfUploadError.FILE_TOO_LARGE
    return None


def map_document_error(error: QPdfDocument.Error) -> PdfUploadError:
    if error in (
        QPdfDocument.Error.IncorrectPassword,
        QPdfDocument.Error.UnsupportedSecurityScheme,
    ):
        return PdfUploadError.PDF_ENCRYPTED
    if error == QPdfDocument.Error.InvalidFileFormat:
        return PdfUploadError.UNSUPPORTED_FILE_TYPE
    return PdfUploadError.PAGE_RENDER_FAILED


def load_pdf_document(
    document: QPdfDocument,
    path: Path,
) -> tuple[PdfUploadError | None, int]:
    """Validate and load *path* into *document*."""
    pre_error = validate_pdf_path(path)
    if pre_error is not None:
        return pre_error, 0

    error = document.load(str(path))
    if error != QPdfDocument.Error.None_:
        return map_document_error(error), 0

    page_count = document.pageCount()
    if page_count <= 0:
        return PdfUploadError.NO_READABLE_PAGES, 0
    return None, page_count
