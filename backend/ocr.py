"""Scanned-PDF OCR fallback — RapidOCR (onnxruntime) + pypdfium2.

Pure pip dependencies (`rapidocr-onnxruntime`, `pypdfium2`), no system
packages: fits the requirements.txt deploy flow and runs CPU-only.

Used by `rag._parse_pdf` (KB ingestion) and `/api/extract-pdf` (chat
uploads): when pypdf finds no meaningful text layer, pages are rendered
to images and OCR'd. Degrades gracefully — if the OCR deps are missing
the caller keeps pypdf's (empty) result and a hint is logged once.

All functions here are synchronous and CPU-heavy; call them from a worker
thread (`asyncio.to_thread`), never directly on the event loop.
"""

import config

_ENGINE = None          # RapidOCR engine singleton (lazy, ~1s init)
_AVAILABLE: bool | None = None
_HINTED = False

# Below this many extracted chars per page, a PDF is treated as scanned.
# Text PDFs yield thousands of chars/page; scans yield 0 or OCR-artifact noise.
_SCANNED_CHARS_PER_PAGE = 40


def enabled() -> bool:
    return str(getattr(config, "PDF_OCR", "on")).strip().lower() in {"1", "true", "on", "yes"}


def available() -> bool:
    """True when the OCR deps import; caches the answer, hints once if not."""
    global _AVAILABLE, _HINTED
    if _AVAILABLE is None:
        try:
            import pypdfium2  # noqa: F401
            from rapidocr_onnxruntime import RapidOCR  # noqa: F401
            _AVAILABLE = True
        except ImportError:
            _AVAILABLE = False
    if not _AVAILABLE and not _HINTED:
        _HINTED = True
        print("[OCR] scanned-PDF OCR unavailable — pip install rapidocr-onnxruntime pypdfium2 "
              "(--break-system-packages on the host)")
    return _AVAILABLE


def should_ocr(extracted_text: str, num_pages: int) -> bool:
    """Does this pypdf extraction look like a scanned PDF (no real text layer)?"""
    if not enabled() or num_pages <= 0:
        return False
    chars = len((extracted_text or "").strip())
    return (chars / num_pages) < _SCANNED_CHARS_PER_PAGE


def _get_engine():
    global _ENGINE
    if _ENGINE is None:
        from rapidocr_onnxruntime import RapidOCR
        _ENGINE = RapidOCR()
    return _ENGINE


def ocr_pdf(filepath: str, max_pages: int | None = None) -> str:
    """Render PDF pages and OCR them. Returns "[Page n]\\n..." formatted text
    like `rag._parse_pdf`, or "" when OCR is unavailable or fails.

    Synchronous and CPU-bound — run via asyncio.to_thread. Page count is
    capped (config PDF_OCR_MAX_PAGES) so a 500-page scan can't pin a core
    for an hour; truncation is noted in the output text.
    """
    if not enabled() or not available():
        return ""
    import numpy as np
    import pypdfium2 as pdfium

    cap = max_pages if max_pages is not None else int(getattr(config, "PDF_OCR_MAX_PAGES", 50) or 50)
    try:
        engine = _get_engine()
        pdf = pdfium.PdfDocument(filepath)
    except Exception as e:
        print(f"[OCR] failed to open {filepath}: {e}")
        return ""
    try:
        total = len(pdf)
        n = min(total, max(1, cap))
        pages_out = []
        for i in range(n):
            try:
                page = pdf[i]
                # scale 2.5 ≈ 180 DPI — enough for print scans, keeps renders small
                bitmap = page.render(scale=2.5)
                img = np.asarray(bitmap.to_pil().convert("RGB"))
                result, _elapse = engine(img)
                lines = [str(item[1]).strip() for item in (result or []) if len(item) > 1 and str(item[1]).strip()]
                if lines:
                    pages_out.append(f"[Page {i + 1}]\n" + "\n".join(lines))
            except Exception as e:
                print(f"[OCR] page {i + 1} failed for {filepath}: {e}")
        if not pages_out:
            return ""
        if total > n:
            pages_out.append(f"[OCR truncated: {total - n} of {total} pages not processed "
                             f"(PDF_OCR_MAX_PAGES={cap})]")
        text = "\n\n".join(pages_out)
        print(f"[OCR] {filepath}: OCR'd {n}/{total} pages → {len(text)} chars")
        return text
    finally:
        try:
            pdf.close()
        except Exception:
            pass
