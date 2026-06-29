"""Extract plain text from PDF bytes when files are too large to send to Claude as documents."""

from __future__ import annotations


def extract_pdf_text(data: bytes, *, max_chars: int = 280_000) -> str:
    """Return concatenated page text from a PDF, capped at max_chars."""
    if not data or not data.startswith(b"%PDF"):
        return ""
    try:
        import fitz  # pymupdf
    except ImportError:
        return ""

    parts: list[str] = []
    total = 0
    try:
        doc = fitz.open(stream=data, filetype="pdf")
        for page in doc:
            text = page.get_text() or ""
            text = text.strip()
            if not text:
                continue
            remaining = max_chars - total
            if remaining <= 0:
                break
            if len(text) > remaining:
                parts.append(text[:remaining])
                total += remaining
                break
            parts.append(text)
            total += len(text)
    except Exception:
        return ""
    return "\n\n".join(parts)
