"""Offline unit tests for ocr.py — scanned-PDF detection and graceful degradation.

The OCR deps (rapidocr/pypdfium2) are NOT required: everything heavy is
monkeypatched. Run: python -m pytest tests/test_ocr_unit.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import ocr  # noqa: E402


def test_should_ocr_scanned(monkeypatch):
    monkeypatch.setattr(config, "PDF_OCR", "on", raising=False)
    # No text layer at all
    assert ocr.should_ocr("", 10)
    # Trace text (page markers only) still counts as scanned
    assert ocr.should_ocr("[Page 1]\nx", 5)


def test_should_ocr_text_pdf(monkeypatch):
    monkeypatch.setattr(config, "PDF_OCR", "on", raising=False)
    # A real text PDF: thousands of chars over a few pages
    assert not ocr.should_ocr("word " * 2000, 4)


def test_should_ocr_disabled(monkeypatch):
    monkeypatch.setattr(config, "PDF_OCR", "off", raising=False)
    assert not ocr.should_ocr("", 10)


def test_should_ocr_zero_pages(monkeypatch):
    monkeypatch.setattr(config, "PDF_OCR", "on", raising=False)
    assert not ocr.should_ocr("", 0)


def test_ocr_pdf_unavailable_returns_empty(monkeypatch):
    monkeypatch.setattr(config, "PDF_OCR", "on", raising=False)
    monkeypatch.setattr(ocr, "_AVAILABLE", False)
    monkeypatch.setattr(ocr, "_HINTED", True)
    assert ocr.ocr_pdf("/nonexistent.pdf") == ""


def test_ocr_pdf_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(config, "PDF_OCR", "off", raising=False)
    assert ocr.ocr_pdf("/nonexistent.pdf") == ""


def test_available_caches(monkeypatch):
    monkeypatch.setattr(ocr, "_AVAILABLE", True)
    assert ocr.available() is True
    monkeypatch.setattr(ocr, "_AVAILABLE", False)
    monkeypatch.setattr(ocr, "_HINTED", True)
    assert ocr.available() is False


def test_parse_pdf_uses_ocr_for_scanned(monkeypatch, tmp_path):
    """rag._parse_pdf falls through to OCR when the text layer is empty."""
    import rag

    class _FakePage:
        def extract_text(self):
            return ""

    class _FakeReader:
        def __init__(self, _):
            self.pages = [_FakePage(), _FakePage()]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)
    monkeypatch.setattr(config, "PDF_OCR", "on", raising=False)
    monkeypatch.setattr(ocr, "ocr_pdf", lambda fp, max_pages=None: "[Page 1]\nOCR TEXT")

    out = rag._parse_pdf(str(tmp_path / "fake.pdf"))
    assert "OCR TEXT" in out


def test_parse_pdf_keeps_pypdf_for_text(monkeypatch, tmp_path):
    """A PDF with a healthy text layer never invokes OCR."""
    import rag

    class _FakePage:
        def extract_text(self):
            return "real text " * 50

    class _FakeReader:
        def __init__(self, _):
            self.pages = [_FakePage()]

    import pypdf
    monkeypatch.setattr(pypdf, "PdfReader", _FakeReader)
    monkeypatch.setattr(config, "PDF_OCR", "on", raising=False)

    def _boom(fp, max_pages=None):
        raise AssertionError("OCR must not run for text PDFs")

    monkeypatch.setattr(ocr, "ocr_pdf", _boom)
    out = rag._parse_pdf(str(tmp_path / "fake.pdf"))
    assert "real text" in out
