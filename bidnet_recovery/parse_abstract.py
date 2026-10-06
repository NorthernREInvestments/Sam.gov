"""Parse BidNet public abstract / open-bid detail HTML (anonymous-accessible fields)."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from discovery.deadline import normalize_deadline


def _field_body(html: str, label: str) -> str | None:
    """Extract unlocked mets-field-body text for a label."""
    pat = (
        re.escape(label)
        + r"</span>\s*<div class=\"mets-field-body[^\"]*\">([\s\S]*?)</div>"
    )
    m = re.search(pat, html or "", re.I)
    if not m:
        return None
    raw = m.group(1)
    if "member-only-info" in raw.lower() or "registered members only" in raw.lower():
        return None
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _locked(html: str, label: str) -> bool:
    # Prefer class=locked field blocks
    pat_locked = (
        r'class="[^"]*locked[^"]*mets-field[^"]*"[\s\S]{0,200}?'
        + re.escape(label)
        + r"[\s\S]{0,400}?member-only-info"
    )
    if re.search(pat_locked, html or "", re.I):
        return True
    pat = re.escape(label) + r"</span>\s*<div class=\"mets-field-body[^\"]*\">([\s\S]{0,800}?)</div>"
    m = re.search(pat, html or "", re.I)
    if not m:
        return False
    return "member-only-info" in m.group(1).lower() or "registered members only" in m.group(1).lower()


def parse_bidnet_abstract(html: str, *, detail_url: str | None = None) -> dict[str, Any]:
    """Parse public BidNet abstract page.

    Anonymous pages typically expose: title, location, publication date, closing date,
    and a short AI/public overview. Org, solicitation number, full description, source URL,
    and documents are often AUTH-gated.
    """
    body = html or ""
    title = None
    tm = re.search(r"<title>([^<]+)</title>", body, re.I)
    if tm:
        title = re.sub(r"\s*[-|].*$", "", tm.group(1)).strip()
    hm = re.search(r"<h1[^>]*>([^<]{5,300})</h1>", body, re.I)
    if hm:
        title = re.sub(r"\s+", " ", hm.group(1)).strip() or title

    overview = None
    om = re.search(
        r'id="ai-public-overview-content"[^>]*>\s*([^<]{20,2500})\s*</div>',
        body,
        re.I,
    )
    if om:
        overview = re.sub(r"\s+", " ", om.group(1)).strip()
        if len(overview) < 20:
            overview = None

    publication = _field_body(body, "Publication Date")
    closing = _field_body(body, "Closing Date")
    location = _field_body(body, "Location")
    org = _field_body(body, "Issuing Organization")
    sol_num = _field_body(body, "Solicitation Number")
    description = _field_body(body, "Description") or overview
    source = _field_body(body, "Source")

    locked_fields = [
        lab
        for lab in (
            "Issuing Organization",
            "Solicitation Number",
            "Description",
            "Source",
        )
        if _locked(body, lab)
    ]

    deadline_raw = closing
    deadline_norm = normalize_deadline(closing) if closing else {
        "deadline_raw": None,
        "utc_deadline": None,
        "timezone_confidence": "UNKNOWN",
    }
    pub_norm = normalize_deadline(publication) if publication else {}

    # Document links (rare on anonymous teaser)
    docs: list[dict[str, Any]] = []
    for m in re.finditer(
        r'href="([^"]+\.(?:pdf|docx?|xlsx?|csv|zip)(?:\?[^"]*)?)"',
        body,
        re.I,
    ):
        href = m.group(1)
        url = urljoin(detail_url or "", href) if detail_url else href
        docs.append(
            {
                "document_type": "attachment",
                "document_name": href.rsplit("/", 1)[-1][:160],
                "document_url": url,
                "retrieval_status": "URL_DISCOVERED",
                "last_verified": None,
            }
        )
    for m in re.finditer(
        r'href="([^"]+)"[^>]*>([^<]{0,120}(?:Addendum|Amendment|Attachment|Specification|Bid\s*Sheet)[^<]{0,40})</a>',
        body,
        re.I,
    ):
        href, name = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
        if "login" in href.lower() or "register" in href.lower():
            continue
        url = urljoin(detail_url or "", href) if detail_url else href
        if not any(d.get("document_url") == url for d in docs):
            docs.append(
                {
                    "document_type": "linked",
                    "document_name": name[:160],
                    "document_url": url,
                    "retrieval_status": "URL_DISCOVERED",
                    "last_verified": None,
                }
            )

    auth_wall = bool(locked_fields) or bool(
        re.search(r"Registered members only|Get instant access|abstractRegisterNowButton", body, re.I)
    )
    # Material improvement signals vs discovery metadata
    material = bool(closing or overview or location or org or sol_num or description or docs)

    return {
        "title": title,
        "description": description,
        "overview": overview,
        "location": location,
        "issue_date_raw": publication,
        "issue_date": pub_norm.get("utc_deadline") or pub_norm.get("parsed_local"),
        "close_date_raw": closing,
        "close_date": deadline_norm.get("utc_deadline") or deadline_norm.get("parsed_local"),
        "deadline_raw": deadline_raw,
        "deadline": deadline_norm.get("utc_deadline") or deadline_norm.get("parsed_local"),
        "timezone": deadline_norm.get("timezone"),
        "timezone_confidence": deadline_norm.get("timezone_confidence"),
        "agency": org,
        "solicitation_number": sol_num,
        "agency_source_url": source if source and source.startswith("http") else None,
        "documents": docs,
        "locked_fields": locked_fields,
        "auth_wall": auth_wall,
        "material_improvement": material,
        "meta_description": (
            (re.search(r'name="description"\s+content="([^"]+)"', body, re.I) or [None, None])[1]
        ),
        "parse_ok": bool(title or closing or overview),
    }
