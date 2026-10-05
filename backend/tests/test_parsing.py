import zipfile

import pytest

from app.config import settings
from app.parsing.extract import ParseError, extract_text
from app.parsing.safety import check_file, sanitize_filename
from app.parsing.text import clean_text, is_garbage, squash
from tests import factories


def test_pdf_with_text_layer(tmp_path) -> None:
    path = factories.write_text_pdf(tmp_path / "cv.pdf")
    check = check_file(path)
    assert check.ok and check.kind == "pdf"
    result = extract_text(path, check)
    assert result.method == "pdf_text"
    assert "Jane Testwell" in result.text and "jane.testwell@example.com" in result.text


def test_docx_text_is_extracted(tmp_path) -> None:
    path = factories.write_docx(tmp_path / "cv.docx")
    check = check_file(path)
    assert check.ok and check.kind == "docx"
    assert "Senior Accountant" in extract_text(path, check).text


def test_scanned_pdf_falls_back_to_ocr(tmp_path) -> None:
    path = factories.write_scanned_pdf(tmp_path / "scan.pdf")
    check = check_file(path)
    assert check.ok
    result = extract_text(path, check)
    assert result.method == "ocr"
    assert "testwell" in result.text.lower()
    assert "accountant" in result.text.lower()


def test_png_is_read_with_ocr(tmp_path) -> None:
    path = factories.write_png(tmp_path / "cv.png")
    check = check_file(path)
    assert check.ok and check.kind == "image"
    assert "accountant" in extract_text(path, check).text.lower()


def test_type_comes_from_magic_bytes_not_extension(tmp_path) -> None:
    # A PDF disguised as .jpg is still treated as a PDF
    path = factories.write_text_pdf(tmp_path / "disguised.jpg")
    assert check_file(path).kind == "pdf"
    # Plain text named .pdf is rejected
    fake = tmp_path / "fake.pdf"
    fake.write_text("just some text, not a pdf" * 10)
    result = check_file(fake)
    assert not result.ok and "Unsupported" in result.reason


def test_legacy_doc_goes_to_review_with_reason(tmp_path) -> None:
    path = tmp_path / "old.doc"
    path.write_bytes(bytes.fromhex("D0CF11E0A1B11AE1") + b"\x00" * 600)
    result = check_file(path)
    assert not result.ok
    assert "Legacy .doc" in result.reason


def test_macro_docx_is_rejected_and_never_opened(tmp_path) -> None:
    result = check_file(factories.write_docx_with_macro(tmp_path / "macro.docx"))
    assert not result.ok and "macros" in result.reason


def test_zip_without_word_document_is_rejected(tmp_path) -> None:
    path = tmp_path / "x.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("hello.txt", "hi")
    assert not check_file(path).ok


def test_corrupt_pdf_and_empty_file_are_rejected(tmp_path) -> None:
    corrupt = tmp_path / "bad.pdf"
    corrupt.write_bytes(b"%PDF-1.4\n garbage garbage garbage")
    assert not check_file(corrupt).ok
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert check_file(empty).reason == "File is empty."


def test_size_and_page_caps(tmp_path, monkeypatch) -> None:
    path = factories.write_text_pdf(tmp_path / "long.pdf", pages=3)
    monkeypatch.setattr(settings, "max_pdf_pages", 2)
    assert "3 pages" in check_file(path).reason
    monkeypatch.setattr(settings, "max_pdf_pages", 15)
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    assert "larger than" in check_file(path).reason


def test_blank_scan_is_a_parse_error_with_reason(tmp_path) -> None:
    from PIL import Image

    path = tmp_path / "blank.png"
    Image.new("RGB", (800, 600), "white").save(path)
    with pytest.raises(ParseError):
        extract_text(path, check_file(path))


@pytest.mark.parametrize(
    "dirty,clean",
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Users\\x\\My CV.PDF", "My CV.pdf"),
        ("cv<script>alert(1)</script>.pdf", "script.pdf"),  # the slash counts as a path separator
        ("....", "file"),
        ("nul\x00byte.docx", "nul_byte.docx"),
    ],
)
def test_sanitize_filename(dirty, clean) -> None:
    assert sanitize_filename(dirty) == clean


def test_sanitize_filename_bounds_length() -> None:
    assert len(sanitize_filename("a" * 500 + ".pdf")) <= 125


def test_clean_text_strips_markup_and_control_chars() -> None:
    out = clean_text("Hello\x00 <b>World</b>\r\n\r\n\r\n\r\nNext   line\u00a0here")
    assert out == "Hello World\n\nNext line here"
    assert "<script>" not in clean_text("<script>alert(1)</script> text")


def test_garbage_detection() -> None:
    assert is_garbage("")
    assert is_garbage("a b c")
    assert is_garbage("\ufffd" * 200)
    assert not is_garbage("Experienced accountant with eight years in banking. " * 5)


def test_squash_for_snippet_matching() -> None:
    assert squash("Senior   Accountant,\nAcme") == "senior accountant, acme"
