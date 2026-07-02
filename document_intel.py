"""PIEE and external document portal detection for dashboard cards and alerts."""

from __future__ import annotations

from typing import Any

from models import Contract


def piee_mentioned_in_text(*parts: str | None) -> bool:
    from piee_client import _piee_mentioned_in_text

    return _piee_mentioned_in_text(*parts)


def contract_piee_intel(row: Contract, session=None) -> dict[str, Any]:
    """PIEE status for one contract — reads DB only, no live SAM/PIEE calls."""
    from attachment_storage import has_stored_pdfs
    from piee_client import collect_link_urls, find_piee_notice_url, is_piee_related_url
    from sam_enrich import detect_external_portals

    sam_raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
    analysis = row.analysis if isinstance(row.analysis, dict) else {}
    doc_access = sam_raw.get("documentAccess") if isinstance(sam_raw.get("documentAccess"), dict) else {}

    piee_attachments = sam_raw.get("pieeAttachments") or []
    piee_count = len(piee_attachments) if isinstance(piee_attachments, list) else 0
    link_urls = collect_link_urls(sam_raw)
    has_piee_link = any(is_piee_related_url(url) for url in link_urls)
    for item in sam_raw.get("opportunityAttachments") or []:
        if isinstance(item, dict) and is_piee_related_url(str(item.get("url") or "")):
            has_piee_link = True
            break
    text_portals = detect_external_portals(
        row.description,
        row.title,
        sam_raw.get("descriptionText"),
        sam_raw.get("title"),
        " ".join(link_urls),
    )

    submission = (row.submission_method or analysis.get("submission_method") or "").upper()
    is_piee = (
        "PIEE" in text_portals
        or piee_count > 0
        or has_piee_link
        or piee_mentioned_in_text(row.description, row.title, sam_raw.get("descriptionText"))
        or submission == "PIEE"
    )

    scrape_status = sam_raw.get("scrapeStatus")
    if scrape_status in ("metadata_ready", "complete") and doc_access:
        if "PIEE" in (doc_access.get("external_portals") or []):
            is_piee = True
        if doc_access.get("piee_notice_url"):
            is_piee = True
        if doc_access.get("status") == "external_portal" and piee_mentioned_in_text(doc_access.get("summary")):
            is_piee = True

    notice_url = None
    if is_piee:
        notice_url = (
            sam_raw.get("pieeNoticeUrl")
            or doc_access.get("piee_notice_url")
            or find_piee_notice_url(sam_raw)
        )
        if not notice_url and not has_piee_link and "PIEE" not in text_portals:
            is_piee = False

    all_portals = list(dict.fromkeys([*(doc_access.get("external_portals") or []), *text_portals]))
    if is_piee and "PIEE" not in all_portals:
        all_portals.insert(0, "PIEE")

    pdfs_in_db = False
    if row.id and session is not None:
        pdfs_in_db = has_stored_pdfs(session, row.id)

    action_required = is_piee and not pdfs_in_db

    if piee_count and notice_url:
        summary = (
            f"{piee_count} solicitation PDF(s) on PIEE — "
            "money on the table: open PIEE and pull the Statement of Work before bidding."
        )
    elif notice_url:
        summary = (
            "Documents are on PIEE, not SAM.gov — "
            "open the PIEE solicitation to download attachments before you bid."
        )
    elif is_piee:
        summary = (
            "This posting references PIEE — solicitation documents are likely on the PIEE portal, not SAM.gov."
        )
    else:
        summary = ""

    return {
        "is_piee": is_piee,
        "action_required": action_required,
        "notice_url": notice_url,
        "piee_attachment_count": piee_count,
        "pdfs_in_db": pdfs_in_db,
        "external_portals": all_portals,
        "summary": summary,
        "submission_method": row.submission_method or analysis.get("submission_method"),
    }


def clear_piee_hints(raw: dict[str, Any]) -> dict[str, Any]:
    """Remove stored PIEE flags so they can be recomputed from source metadata."""
    if not raw:
        return {}
    updated = dict(raw)
    updated.pop("pieeNoticeUrl", None)
    updated.pop("pieeAttachments", None)
    access = dict(updated.get("documentAccess") or {})
    for key in ("piee_notice_url", "requires_piee_action", "piee_attachment_count"):
        access.pop(key, None)
    portals = [p for p in (access.get("external_portals") or []) if p != "PIEE"]
    if portals:
        access["external_portals"] = portals
    else:
        access.pop("external_portals", None)
    summary = str(access.get("summary") or "")
    if summary.startswith("Documents on PIEE") or summary.startswith("This posting references PIEE"):
        access.pop("summary", None)
    if access.get("requires_external_portal") and not access.get("file_attachment_count"):
        access.pop("requires_external_portal", None)
    if access:
        updated["documentAccess"] = access
    else:
        updated.pop("documentAccess", None)
    return updated


def repair_stored_piee_hints(session=None) -> int:
    """Persist PIEE hints on existing sam_raw rows (no SAM API calls)."""
    from database import SessionLocal
    from piee_client import stamp_piee_hints

    own_session = session is None
    if own_session:
        session = SessionLocal()
    updated = 0
    try:
        rows = session.query(Contract).all()
        for row in rows:
            raw = row.sam_raw if isinstance(row.sam_raw, dict) else {}
            if not raw:
                continue
            cleaned = clear_piee_hints(raw)
            stamped = stamp_piee_hints(cleaned)
            if stamped != raw:
                row.sam_raw = stamped
                updated += 1
        if updated:
            session.commit()
    finally:
        if own_session:
            session.close()
    return updated
