"""Download solicitation PDFs, persist file bytes, extract text, FAR 52.219-14 checks."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from models import Contract

MIN_TEXT_FOR_FAR_CHECK = 500

FAR_CLAUSE_MARKERS = (
    "52.219-14",
    "52.219‑14",
    "52.219–14",
    "limitations on subcontracting",
)
PERCENTAGE_NEARBY_PATTERN = re.compile(
    r"(?:at\s+least\s+)?(\d{1,3})\s*(?:percent|%)\s*(?:of\s+)?(?:the\s+)?"
    r"(?:cost|amount|price|value)?\s*(?:of\s+)?(?:contract\s+)?(?:performance|incurred|work)?",
    re.IGNORECASE,
)
CLAUSE_NUMBER_RE = re.compile(r"52\.219[\-\u2011\u2013]14", re.IGNORECASE)
UNCHECKED_BOX_MARKERS = ("☐", "□", "[ ]")
CHECKED_BOX_MARKERS = ("☒", "☑", "[x]", "[X]", "✓", "✔")


def _sanitize_pdf_text(text: str) -> str:
    """PostgreSQL TEXT columns reject NUL bytes from some PDF extractors."""
    return (text or "").replace("\x00", "")


@dataclass
class AttachmentExtractionResult:
    text: str
    char_count: int
    method: str  # text | ocr_needed | failed | no_pdfs_expected
    note: str | None
    pdfs_attempted: int
    pdfs_with_text: int
    pdf_labels: list[str]
    skipped: list[str]
    files_stored: int
    bytes_stored: int


@dataclass
class SubcontractingCheckResult:
    check: str  # FOUND | NOT_FOUND | EXTRACTION_FAILED
    context: str | None
    percentage: float | None
    matched_marker: str | None


def _normalize_for_search(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    return normalized.replace("\u2011", "-").replace("\u2013", "-").replace("\u2014", "-")


def _checkbox_before_clause(line: str, clause_pos: int) -> str | None:
    """Return 'checked', 'unchecked', or None if no checkbox glyph before the clause on this line."""
    prefix = line[:clause_pos]
    last_checked = -1
    last_unchecked = -1
    for marker in CHECKED_BOX_MARKERS:
        pos = prefix.rfind(marker)
        if pos > last_checked:
            last_checked = pos
    for marker in UNCHECKED_BOX_MARKERS:
        pos = prefix.rfind(marker)
        if pos > last_unchecked:
            last_unchecked = pos
    if last_checked < 0 and last_unchecked < 0:
        stripped = prefix.strip()
        if stripped in ("X", "x"):
            return "checked"
        return None
    if last_checked > last_unchecked:
        return "checked"
    return "unchecked"


def _find_applicable_subcontracting_match(text: str) -> tuple[int, str, str] | None:
    """
    Find 52.219-14 only when it applies to the contract — not when listed unchecked in a FAR matrix.
    Returns (match_index, matched_line, reason).
    """
    haystack = _normalize_for_search(text)
    lines_with_offsets: list[tuple[int, str]] = []
    offset = 0
    for line in haystack.splitlines():
        lines_with_offsets.append((offset, line))
        offset += len(line) + 1

    applicable: list[tuple[int, str, str]] = []
    listed_unchecked: list[str] = []

    for line_start, line in lines_with_offsets:
        low = line.lower()
        if "52.219-14" not in low and "limitations on subcontracting" not in low:
            continue

        clause_match = CLAUSE_NUMBER_RE.search(line)
        clause_pos = clause_match.start() if clause_match else low.find("limitations on subcontracting")
        if clause_pos < 0:
            continue

        box = _checkbox_before_clause(line, clause_pos)
        if box == "unchecked":
            listed_unchecked.append(line.strip())
            continue
        if box == "checked":
            applicable.append((line_start + clause_pos, line.strip(), "checked_in_clause_matrix"))
            continue

        # No checkbox — treat as incorporated clause text if the line is substantial (not just a title)
        if clause_match and len(line.strip()) > 80:
            applicable.append((line_start + clause_pos, line.strip(), "full_clause_text"))
            continue
        if "limitations on subcontracting" in low and len(line.strip()) > 80:
            applicable.append((line_start + clause_pos, line.strip(), "full_clause_text"))

    if applicable:
        idx, line, reason = applicable[0]
        return idx, line, reason

    if listed_unchecked:
        return None  # explicitly listed but not checked — NOT applicable

    # Fallback: marker in text but not in recognizable matrix format
    for marker in FAR_CLAUSE_MARKERS:
        idx = haystack.lower().find(marker.lower())
        if idx >= 0:
            return idx, haystack[max(0, idx - 40) : idx + 120].strip(), "text_match_unverified"

    return None


def extract_contract_attachment_text(
    contract: Contract,
    session: Session,
    *,
    max_pdfs: int = 12,
) -> AttachmentExtractionResult:
    """Download PDFs, persist bytes to PostgreSQL, extract and merge plain text."""
    from attachment_storage import (
        attachment_storage_summary,
        download_and_persist_attachments,
        get_contract_pdf_bytes,
        persist_attachment_files,
    )
    from claude_client import is_drawing_pdf
    from pdf_text import extract_pdf_text
    from screening_pipeline import pdfs_expected_on_contract

    if not pdfs_expected_on_contract(contract):
        return AttachmentExtractionResult(
            text="",
            char_count=0,
            method="no_pdfs_expected",
            note="No downloadable PDF attachments listed on SAM.gov or PIEE.",
            pdfs_attempted=0,
            pdfs_with_text=0,
            pdf_labels=[],
            skipped=[],
            files_stored=0,
            bytes_stored=0,
        )

    parts: list[str] = []
    labels: list[str] = []
    skipped: list[str] = []
    extracted_by_name: dict[str, str] = {}
    ocr_only = False
    pdfs_with_text = 0

    try:
        pdf_items = get_contract_pdf_bytes(session, contract, max_pdfs=max_pdfs)
        if not pdf_items:
            pdf_items = download_and_persist_attachments(session, contract, max_pdfs=max_pdfs)
    except Exception as exc:
        return AttachmentExtractionResult(
            text="",
            char_count=0,
            method="failed",
            note=f"PDF download failed: {exc}",
            pdfs_attempted=0,
            pdfs_with_text=0,
            pdf_labels=[],
            skipped=[str(exc)],
            files_stored=0,
            bytes_stored=0,
        )

    if not pdf_items:
        return AttachmentExtractionResult(
            text="",
            char_count=0,
            method="failed",
            note="PDFs were expected but none could be downloaded or stored.",
            pdfs_attempted=0,
            pdfs_with_text=0,
            pdf_labels=[],
            skipped=["download_returned_empty"],
            files_stored=0,
            bytes_stored=0,
        )

    for name, data in pdf_items:
        label = name or "document.pdf"
        labels.append(label)
        if not data.startswith(b"%PDF"):
            skipped.append(f"{label} (not a PDF)")
            continue
        text = _sanitize_pdf_text(extract_pdf_text(data, max_chars=350_000))
        if text.strip():
            pdfs_with_text += 1
            extracted_by_name[label] = text.strip()
            parts.append(f"--- {label} ---\n{text.strip()}")
        elif is_drawing_pdf(label, data):
            ocr_only = True
            skipped.append(f"{label} (image-only PDF — OCR needed)")
        else:
            skipped.append(f"{label} (no extractable text layer)")

    # Re-persist with per-file extracted text
    persist_rows = [(label, data, "stored", None) for label, data in pdf_items if data.startswith(b"%PDF")]
    if persist_rows:
        persist_attachment_files(session, contract, persist_rows, extracted_by_name=extracted_by_name)

    storage = attachment_storage_summary(session, contract.id) if contract.id else {"count": 0, "total_bytes": 0}

    merged = "\n\n".join(parts).strip()
    if merged:
        method = "text"
        note = None
        if ocr_only:
            note = "Some attachments are image-only scans; OCR may be required for full coverage."
    elif ocr_only:
        method = "ocr_needed"
        note = "PDFs stored but appear to be image-only scans with no text layer."
    else:
        method = "failed"
        note = "PDFs stored but no text could be extracted."

    return AttachmentExtractionResult(
        text=merged,
        char_count=len(merged),
        method=method,
        note=note,
        pdfs_attempted=len(labels),
        pdfs_with_text=pdfs_with_text,
        pdf_labels=labels,
        skipped=skipped,
        files_stored=storage.get("count", 0),
        bytes_stored=storage.get("total_bytes", 0),
    )


def check_subcontracting_limitation(
    attachment_text: str | None,
    *,
    char_count: int | None = None,
) -> SubcontractingCheckResult:
    """Detect when FAR 52.219-14 is checked/applicable — not merely listed unchecked in a clause matrix."""
    text = attachment_text or ""
    count = char_count if char_count is not None else len(text)
    if count < MIN_TEXT_FOR_FAR_CHECK:
        return SubcontractingCheckResult(
            check="EXTRACTION_FAILED",
            context=None,
            percentage=None,
            matched_marker=None,
        )

    match = _find_applicable_subcontracting_match(text)
    if match is None:
        return SubcontractingCheckResult(
            check="NOT_FOUND",
            context=None,
            percentage=None,
            matched_marker=None,
        )

    match_index, matched_line, reason = match
    if reason == "text_match_unverified":
        # String appears but no checkbox context — manual review, not auto-FAIL pursue
        return SubcontractingCheckResult(
            check="EXTRACTION_FAILED",
            context=matched_line[:500],
            percentage=None,
            matched_marker="52.219-14",
        )

    start = max(0, match_index - 250)
    end = min(len(text), match_index + 250)
    context = f"[{reason}] {text[start:end].strip()}"
    window = text[max(0, match_index - 500) : min(len(text), match_index + 500)]
    pct_match = PERCENTAGE_NEARBY_PATTERN.search(window)
    percentage = float(pct_match.group(1)) if pct_match else None

    return SubcontractingCheckResult(
        check="FOUND",
        context=context,
        percentage=percentage,
        matched_marker="52.219-14",
    )


def persist_attachment_and_compliance(
    row: Contract,
    extraction: AttachmentExtractionResult,
) -> SubcontractingCheckResult:
    """Write extraction + compliance fields onto the contract row."""
    row.attachment_text = extraction.text or None
    row.attachment_extraction_method = extraction.method
    row.attachment_extraction_note = extraction.note
    row.attachment_text_extracted_at = datetime.now(timezone.utc)

    check = check_subcontracting_limitation(extraction.text, char_count=extraction.char_count)
    _apply_subcontracting_compliance(row, check)
    return check


def _apply_subcontracting_compliance(row: Contract, check: SubcontractingCheckResult) -> None:
    row.subcontracting_limitation_check = check.check
    row.subcontracting_limitation_context = check.context
    row.subcontracting_limitation_percentage = check.percentage
    if check.check != "FOUND":
        return
    from screening_pipeline import FAR_SUBCONTRACTING_SKIP_LABEL

    analysis = dict(row.analysis) if isinstance(row.analysis, dict) else {}
    analysis["pursue"] = False
    analysis["skip_reason"] = FAR_SUBCONTRACTING_SKIP_LABEL
    row.analysis = analysis
    row.status = "skipped"


def rerun_subcontracting_check(row: Contract) -> SubcontractingCheckResult:
    """Re-run FAR 52.219-14 check on stored attachment_text without re-downloading."""
    check = check_subcontracting_limitation(row.attachment_text, char_count=len(row.attachment_text or ""))
    _apply_subcontracting_compliance(row, check)
    return check


def run_attachment_pipeline(
    row: Contract,
    session: Session,
    *,
    max_pdfs: int = 12,
) -> dict[str, Any]:
    """Download, persist file bytes, extract text, run FAR 52.219-14 check."""
    extraction = extract_contract_attachment_text(row, session, max_pdfs=max_pdfs)
    check = persist_attachment_and_compliance(row, extraction)
    _sync_scrape_status_with_extraction(row, extraction)
    return {
        "attachment_text_chars": extraction.char_count,
        "attachment_extraction_method": extraction.method,
        "attachment_extraction_note": extraction.note,
        "pdfs_attempted": extraction.pdfs_attempted,
        "pdfs_with_text": extraction.pdfs_with_text,
        "files_stored": extraction.files_stored,
        "bytes_stored": extraction.bytes_stored,
        "skipped": extraction.skipped,
        "subcontracting_limitation_check": check.check,
        "subcontracting_limitation_percentage": check.percentage,
    }


def _sync_scrape_status_with_extraction(row: Contract, extraction: AttachmentExtractionResult) -> None:
    raw = dict(row.sam_raw) if isinstance(row.sam_raw, dict) else {}
    from screening_pipeline import pdfs_expected_on_contract

    files_ok = extraction.files_stored > 0 or not pdfs_expected_on_contract(row)
    if extraction.method in ("text", "ocr_needed", "no_pdfs_expected") and files_ok:
        raw["scrapeStatus"] = "complete"
        raw.pop("scrapeError", None)
        raw["attachmentExtraction"] = {
            "method": extraction.method,
            "char_count": extraction.char_count,
            "pdfs_attempted": extraction.pdfs_attempted,
            "pdfs_with_text": extraction.pdfs_with_text,
            "files_stored": extraction.files_stored,
            "bytes_stored": extraction.bytes_stored,
            "skipped": extraction.skipped,
        }
    else:
        raw["scrapeStatus"] = "incomplete"
        raw["scrapeError"] = extraction.note or "attachment_download_or_extraction_failed"
        raw["attachmentExtraction"] = {
            "method": extraction.method,
            "char_count": extraction.char_count,
            "files_stored": extraction.files_stored,
            "skipped": extraction.skipped,
        }
    row.sam_raw = raw


def is_attachment_extraction_ready(row: Contract, session: Session | None = None) -> bool:
    from screening_pipeline import pdfs_expected_on_contract

    if pdfs_expected_on_contract(row) and session is not None and row.id:
        from attachment_storage import attachment_storage_summary

        if attachment_storage_summary(session, row.id)["pdf_count"] < 1:
            return False

    method = getattr(row, "attachment_extraction_method", None)
    if method == "text":
        return bool(row.attachment_text and len(row.attachment_text.strip()) > 0)
    if method in ("ocr_needed", "no_pdfs_expected"):
        return True
    return False
