"""Contract screening: attachments first, then full Claude analysis for ranking."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from models import Contract

SKIP_LOW_SCORE_LABEL = "Skipped — Low Score"
FAR_SUBCONTRACTING_SKIP_LABEL = "Skipped — FAR 52.219-14 Limitations on Subcontracting applies"


def full_analysis_min_score() -> int:
    """Legacy threshold — used for auto sub-search hints, not intake gating."""
    raw = os.getenv("FULL_ANALYSIS_MIN_SCORE", "6").strip()
    try:
        return max(1, min(10, int(raw)))
    except ValueError:
        return 6


def analysis_stage(analysis: dict[str, Any] | None) -> str | None:
    if not isinstance(analysis, dict):
        return None
    return analysis.get("screening_stage")


def pdfs_expected_on_contract(row: Contract) -> bool:
    """True when SAM or PIEE lists downloadable solicitation PDFs for this contract."""
    raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
    if raw.get("pieeAttachments"):
        return True
    for item in raw.get("opportunityAttachments") or []:
        if isinstance(item, dict) and item.get("type") == "file":
            return True
    return bool(raw.get("attachmentDownloadUrls"))


def pdfs_read_in_analysis(analysis: dict[str, Any] | None) -> bool:
    if not isinstance(analysis, dict):
        return False
    return int(analysis.get("pdfs_sent_to_claude") or analysis.get("piee_pdfs_sent") or 0) > 0


def is_full_analysis_complete(
    analysis: dict[str, Any] | None,
    row: Contract | None = None,
) -> bool:
    """True only when Claude finished AND expected PDFs were read (or scope is persisted)."""
    if not isinstance(analysis, dict):
        return False
    if analysis_stage(analysis) not in ("full",) and not analysis.get("plain_english_summary"):
        return False

    if row is not None and pdfs_expected_on_contract(row):
        from pws_fields import contract_pws_missing

        if contract_pws_missing(row) and not pdfs_read_in_analysis(analysis):
            return False

    if analysis_stage(analysis) == "full":
        return True
    # Legacy rows with summary and scope already on the contract row.
    if analysis.get("plain_english_summary") and row is not None:
        from pws_fields import contract_pws_missing

        if not contract_pws_missing(row):
            return True
    return analysis_stage(analysis) == "full"


def text_score_from_analysis(analysis: dict[str, Any] | None) -> int | None:
    if not isinstance(analysis, dict):
        return None
    for key in ("text_score", "score"):
        val = analysis.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    return None


def has_attachments_ready(row: Contract) -> bool:
    from attachment_pipeline import is_attachment_extraction_ready
    from database import SessionLocal
    from sam_enrich import is_scrape_complete

    raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
    session = SessionLocal()
    try:
        return is_scrape_complete(raw) and is_attachment_extraction_ready(row, session)
    finally:
        session.close()


def qualifies_for_full_analysis(
    analysis: dict[str, Any] | None,
    row: Contract | None = None,
    *,
    force: bool = False,
) -> bool:
    if force:
        return True
    return not is_full_analysis_complete(analysis, row)


def needs_text_screening(analysis: dict[str, Any] | None) -> bool:
    if not analysis:
        return True
    return analysis_stage(analysis) not in ("text", "full")


def needs_intake(row: Contract, *, force: bool = False) -> bool:
    if force:
        return True
    if not has_attachments_ready(row):
        return False
    from pws_fields import contract_pws_missing

    if contract_pws_missing(row):
        return True
    analysis = row.analysis if isinstance(row.analysis, dict) else None
    return qualifies_for_full_analysis(analysis, row)


def mark_low_text_score(row: Contract, analysis: dict[str, Any]) -> None:
    analysis["screening_stage"] = "text"
    analysis["skip_reason"] = SKIP_LOW_SCORE_LABEL
    analysis["pursue"] = False
    row.analysis = analysis
    row.status = "skipped"
    row.last_updated_at = datetime.now(timezone.utc)


def is_dashboard_ready(row: Contract) -> bool:
    """Contract may appear on the dashboard only when attachments are read and scope is extracted."""
    if getattr(row, "subcontracting_limitation_check", None) == "FOUND":
        return False
    if not has_attachments_ready(row):
        return False
    from pws_fields import contract_pws_missing

    if contract_pws_missing(row):
        return False
    analysis = row.analysis if isinstance(row.analysis, dict) else {}
    if not is_full_analysis_complete(analysis, row):
        return False
    if pdfs_expected_on_contract(row) and not pdfs_read_in_analysis(analysis):
        return False
    return True


def mark_pending_full_analysis(row: Contract, analysis: dict[str, Any]) -> None:
    analysis["screening_stage"] = "text"
    analysis.pop("skip_reason", None)
    if row.status in (None, "new", "skipped"):
        row.status = "reviewing"
    row.analysis = analysis
    row.last_updated_at = datetime.now(timezone.utc)


def finalize_full_analysis(row: Contract, analysis: dict[str, Any]) -> None:
    analysis["screening_stage"] = "full"
    analysis.pop("skip_reason", None)
    if analysis.get("text_score") is None and analysis.get("score") is not None:
        analysis["text_score"] = analysis.get("score")
    row.analysis = analysis
    row.last_updated_at = datetime.now(timezone.utc)
    if analysis.get("pursue") is False:
        row.status = "skipped"
    elif row.status in (None, "new", "skipped"):
        row.status = "reviewing"
