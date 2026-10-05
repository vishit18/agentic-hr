"""Builds synthetic CV files for tests. All names, numbers and employers are invented."""
from __future__ import annotations

import zipfile
from pathlib import Path

from docx import Document
from PIL import Image, ImageDraw, ImageFont

SAMPLE_CV_LINES = [
    "Jane Testwell",
    "jane.testwell@example.com | +973 3600 1234",
    "Manama, Bahrain",
    "EXPERIENCE",
    "Senior Accountant, Acme Holdings, Jan 2018 - Present",
    "Prepared monthly financial statements and managed audits.",
    "Accountant, Globex Bank, Mar 2013 - Dec 2017",
    "Reconciled accounts and supported regulatory reporting.",
    "EDUCATION",
    "BSc Accounting, University of Testland, 2009 - 2013",
    "CERTIFICATIONS",
    "Chartered Financial Analyst (CFA)",
    "SKILLS",
    "Excel, IFRS, Financial reporting",
]


def _pdf_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def write_text_pdf(path: Path, lines: list[str] = SAMPLE_CV_LINES, pages: int = 1) -> Path:
    """A minimal valid PDF with a real text layer (one page repeated `pages` times)."""
    content = "BT /F1 12 Tf 50 780 Td 16 TL\n" + "\n".join(f"({_pdf_escape(line)}) Tj T*" for line in lines) + "\nET"
    objects: list[str] = []
    page_ids = [4 + i for i in range(pages)]
    objects.append("<< /Type /Catalog /Pages 2 0 R >>")
    objects.append("<< /Type /Pages /Kids [" + " ".join(f"{i} 0 R" for i in page_ids) + f"] /Count {pages} >>")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for _ in page_ids:
        objects.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {4 + pages} 0 R >>"
        )
    objects.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
    out = "%PDF-1.4\n"
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out.encode("latin-1")))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out.encode("latin-1"))
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    path.write_bytes(out.encode("latin-1"))
    return path


def render_lines_image(lines: list[str]) -> Image.Image:
    font = ImageFont.load_default(size=34)
    img = Image.new("RGB", (1600, 60 + 56 * len(lines)), "white")
    draw = ImageDraw.Draw(img)
    for i, line in enumerate(lines):
        draw.text((40, 30 + 56 * i), line, fill="black", font=font)
    return img


def write_scanned_pdf(path: Path, lines: list[str] = SAMPLE_CV_LINES) -> Path:
    """An image-only PDF (no text layer), like a scan."""
    render_lines_image(lines).save(path, "PDF", resolution=150)
    return path


def write_png(path: Path, lines: list[str] = SAMPLE_CV_LINES) -> Path:
    render_lines_image(lines).save(path, "PNG")
    return path


def write_docx(path: Path, lines: list[str] = SAMPLE_CV_LINES) -> Path:
    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    doc.save(path)
    return path


def write_docx_with_macro(path: Path) -> Path:
    write_docx(path)
    with zipfile.ZipFile(path, "a") as z:
        z.writestr("word/vbaProject.bin", b"fake macro payload")
    return path
