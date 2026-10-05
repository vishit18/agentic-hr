"""File safety checks. Type comes from magic bytes, never the extension.

A failed check is not an error: the file goes to the review queue with a plain-English reason.
Nothing here executes content (no macros, no scripts); files are only inspected.
"""
from __future__ import annotations

import re
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path

import magic
import pypdfium2 as pdfium
from PIL import Image

from app.config import settings

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
IMAGE_MIMES = {"image/jpeg", "image/png", "image/tiff", "image/webp", "image/bmp"}
LEGACY_DOC_MIMES = {"application/msword", "application/x-ole-storage", "application/CDFV2"}


@dataclass
class FileCheck:
    ok: bool
    kind: str | None = None  # pdf | docx | image
    mime: str | None = None
    reason: str | None = None


def sanitize_filename(name: str) -> str:
    """Safe display/storage name: no path parts, no control or odd characters, bounded length."""
    name = unicodedata.normalize("NFKC", name).replace("\\", "/").split("/")[-1]
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[^\w\- ()\[\]]+", "_", stem, flags=re.UNICODE).strip(" ._") or "file"
    ext = re.sub(r"[^A-Za-z0-9]", "", ext)[:8].lower()
    stem = stem[:120]
    return f"{stem}.{ext}" if ext else stem


def _reject(reason: str, mime: str | None = None) -> FileCheck:
    return FileCheck(ok=False, mime=mime, reason=reason)


def _check_docx(path: Path) -> FileCheck | None:
    """Return a rejection, or None if the zip looks like a safe .docx."""
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            if "word/document.xml" not in names:
                return _reject("Not a Word document (zip without word/document.xml).")
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                return _reject("Word file contains macros, which are never opened.")
            total = sum(i.file_size for i in z.infolist())
            if total > settings.max_zip_uncompressed_mb * 1024 * 1024:
                return _reject("Word file expands to an unreasonable size and was not opened.")
    except zipfile.BadZipFile:
        return _reject("Word file is corrupt and could not be opened.")
    return None


def check_file(path: Path) -> FileCheck:
    size = path.stat().st_size
    if size == 0:
        return _reject("File is empty.")
    if size > settings.max_upload_mb * 1024 * 1024:
        return _reject(f"File is larger than the {settings.max_upload_mb} MB limit.")

    mime = magic.from_file(str(path), mime=True)

    if mime in LEGACY_DOC_MIMES:
        return _reject("Legacy .doc format is not supported. Open it in Word and save as .docx or PDF.", mime)

    if mime == "application/pdf":
        try:
            pdf = pdfium.PdfDocument(str(path))
            pages = len(pdf)
            pdf.close()
        except pdfium.PdfiumError as exc:
            if "password" in str(exc).lower():
                return _reject("PDF is password protected.", mime)
            return _reject("PDF is corrupt and could not be opened.", mime)
        if pages > settings.max_pdf_pages:
            return _reject(f"PDF has {pages} pages; the limit is {settings.max_pdf_pages}.", mime)
        return FileCheck(ok=True, kind="pdf", mime=mime)

    if mime == DOCX_MIME or (mime in {"application/zip", "application/octet-stream"} and zipfile.is_zipfile(path)):
        rejection = _check_docx(path)
        if rejection:
            rejection.mime = mime
            return rejection
        return FileCheck(ok=True, kind="docx", mime=DOCX_MIME)

    if mime in IMAGE_MIMES:
        try:
            with Image.open(path) as img:
                width, height = img.size
        except Exception:  # noqa: BLE001 - Pillow raises many types for bad images
            return _reject("Image is corrupt and could not be opened.", mime)
        if width * height > settings.max_image_pixels:
            return _reject("Image is too large to process safely.", mime)
        return FileCheck(ok=True, kind="image", mime=mime)

    return _reject(f"Unsupported file type (detected: {mime}).", mime)
