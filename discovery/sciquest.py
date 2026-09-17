"""SciQuest / Jaggaer public event listing parser — live HTML structure (2026-09)."""

from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from discovery.deadline import normalize_deadline
from discovery.opportunity_gate import (
    is_structurally_valid_opportunity,
    sanitize_deadline_raw,
    sanitize_solicitation_number,
)
from discovery.schema import CanonicalOpportunity

STATUS_WORDS = frozenset(
    {
        "open",
        "closed",
        "awarded",
        "pending",
        "cancelled",
        "canceled",
        "draft",
        "status",
        "type",
        "details",
        "title",
        "close",
        "contact",
        "events",
        "rfp",
        "rfb",
        "ifb",
        "rfi",
        "rfq",
    }
)

_CUSTOMER_ORG_AGENCY = {
    "DASIowa": "State of Iowa Department of Administrative Services",
    "StateOfMontana": "State of Montana",
}


def agency_from_list_url(list_url: str | None) -> str | None:
    if not list_url:
        return None
    qs = parse_qs(urlparse(list_url).query)
    org = (qs.get("CustomerOrg") or [None])[0]
    if not org:
        return None
    return _CUSTOMER_ORG_AGENCY.get(org) or org.replace("StateOf", "State of ").replace("DAS", "DAS ")


def sciquest_has_public_event_structure(body: str) -> bool:
    """True when page exposes public event listing chrome (not merely login words)."""
    text = body or ""
    signals = 0
    if re.search(r"status-badge", text, re.I):
        signals += 1
    if re.search(r"btn-link-header", text, re.I):
        signals += 1
    if re.search(r"Sourcingevent/\d+-event\.pdf", text, re.I):
        signals += 1
    if re.search(r"ViewSourcingEvent", text, re.I):
        signals += 1
    if re.search(r"SourcingPublicSite_LABEL_NUMBER", text, re.I):
        signals += 1
    if re.search(r"PHX_NAV_SourcingOpenForBid|Business Opportunities", text, re.I):
        signals += 1
    return signals >= 2


def _clean_title(raw: str) -> str:
    t = html_lib.unescape(raw or "")
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("\t", " ").replace("\r", " ").replace("\n", " ")
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(Open|Closed|Awarded)\s+", "", t, flags=re.I).strip()
    return t[:300]


def _is_status_word(val: str | None) -> bool:
    return bool(val) and val.strip().lower() in STATUS_WORDS


def _field_after_label(row_html: str, label: str) -> str | None:
    """Extract phx data-row-content following a Type/Number/Contact label."""
    m = re.search(
        rf">{re.escape(label)}</div>\s*</div>\s*</div>\s*"
        rf'<div class="phx table-cell-layout">\s*'
        rf'<div class="phx data-row-content">\s*([^<]+)',
        row_html,
        re.I | re.S,
    )
    if m:
        return html_lib.unescape(m.group(1)).strip()
    # Plain-text fallback after label
    plain = re.sub(r"<[^>]+>", " ", row_html)
    plain = html_lib.unescape(re.sub(r"\s+", " ", plain))
    m2 = re.search(rf"\b{re.escape(label)}\s+([A-Za-z0-9][A-Za-z0-9\-_.\/ ]{{2,80}}?)(?:\s+Contact|\s+Details|\s+Type|\s+Number|\s+Open|\s+Close|$)", plain, re.I)
    if m2:
        return m2.group(1).strip()
    return None


def _close_from_row(row_html: str) -> str | None:
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", row_html))
    plain = re.sub(r"\s+", " ", plain)
    m = re.search(
        r"Close\s+(\d{1,2}/\d{1,2}/\d{4}(?:\s*,?\s*\d{1,2}:\d{2}\s*(?:AM|PM))?)\s*([A-Z]{2,4})?",
        plain,
        re.I,
    )
    if not m:
        return None
    raw = m.group(1).strip()
    if m.group(2):
        raw = f"{raw} {m.group(2).strip()}"
    return sanitize_deadline_raw(raw) or raw


def _open_posted_from_row(row_html: str) -> str | None:
    plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", row_html))
    plain = re.sub(r"\s+", " ", plain)
    # Second "Open" is often the open-date label (status badge already consumed first)
    ms = list(
        re.finditer(
            r"Open\s+(\d{1,2}/\d{1,2}/\d{4}(?:\s*,?\s*\d{1,2}:\d{2}\s*(?:AM|PM))?)\s*([A-Z]{2,4})?",
            plain,
            re.I,
        )
    )
    if not ms:
        return None
    m = ms[0]
    raw = m.group(1).strip()
    if m.group(2):
        raw = f"{raw} {m.group(2).strip()}"
    return raw


def parse_sciquest_public_events(
    body: str,
    *,
    list_url: str,
    source_id: str = "live_jaggaer",
    trust_tier: int = 1,
) -> list[CanonicalOpportunity]:
    """
    Parse Jaggaer/SciQuest PublicEvent live HTML.
    Structure (observed 2026-09): table rows with mosaic status-badge, btn-link-header title,
    phx Type/Number fields, Close timestamps, Sourcingevent/{id}-event.pdf.
    """
    text = body or ""
    agency = agency_from_list_url(list_url)
    found: list[CanonicalOpportunity] = []

    rows = re.findall(r"<tr\b[^>]*>.*?</tr>", text, re.I | re.S)
    for row in rows:
        if "status-badge" not in row and not re.search(r"btn-link-header", row, re.I):
            continue

        sm = re.search(
            r'class="[^"]*status-badge[^"]*"[^>]*>\s*(Open|Closed|Awarded)\s*<',
            row,
            re.I,
        )
        status = sm.group(1).upper() if sm else "OPEN"

        tm = re.search(
            r'<a[^>]*class="[^"]*btn-link-header[^"]*"[^>]*id="([^"]*)"[^>]*href="([^"]+)"[^>]*>\s*([^<]+)\s*<',
            row,
            re.I | re.S,
        )
        if not tm:
            tm = re.search(
                r'<a[^>]*href="([^"]*ViewSourcingEvent[^"]*)"[^>]*class="[^"]*btn-link-header[^"]*"[^>]*id="([^"]*)"[^>]*>\s*([^<]+)',
                row,
                re.I | re.S,
            )
            if tm:
                detail_url, anchor_id, title_raw = tm.group(1), tm.group(2), tm.group(3)
            else:
                continue
        else:
            anchor_id, detail_url, title_raw = tm.group(1), tm.group(2), tm.group(3)

        title = _clean_title(title_raw)
        if len(title) < 8:
            continue

        number = _field_after_label(row, "Number")
        rtype = _field_after_label(row, "Type")
        number = sanitize_solicitation_number(number)
        if number and _is_status_word(number):
            number = None

        deadline_raw = _close_from_row(row)
        posted_raw = _open_posted_from_row(row)
        dl = normalize_deadline(deadline_raw) if deadline_raw else normalize_deadline(None)

        pdf_ids = re.findall(r"Sourcingevent/(\d+)-event\.pdf", row, re.I)
        if pdf_ids:
            event_id = pdf_ids[0]
            external_id = f"sciquest:{event_id}"
        elif number:
            external_id = f"sciquest:{number}"
        else:
            external_id = f"sciquest:title:{re.sub(r'[^a-z0-9]+', '-', title.lower())[:80]}"

        solicitation_number = number or (pdf_ids[0] if pdf_ids else None)
        if solicitation_number and _is_status_word(solicitation_number):
            solicitation_number = pdf_ids[0] if pdf_ids else None

        doc_links: list[dict[str, Any]] = []
        # Prefer href= capture so AWS signed query strings are preserved.
        # Unsigned S3 URLs return 403; listing HTML embeds time-limited signatures.
        for pm in re.finditer(
            r'href=["\'](https?://[^"\']+Sourcingevent/\d+-event\.pdf[^"\']*)["\']',
            row,
            re.I,
        ):
            signed = html_lib.unescape(pm.group(1)).strip()
            canonical = signed.split("?")[0]
            doc_links.append(
                {
                    "url": signed,
                    "canonical_url": canonical,
                    "kind": "event_pdf",
                    "signed": "?" in signed and "X-Amz" in signed,
                    "discovered_only": True,
                    "document_fetched": False,
                }
            )
        if not doc_links:
            for pm in re.finditer(
                r'(https?://[^\s"\'<>]+Sourcingevent/\d+-event\.pdf(?:\?[^\s"\'<>]*)?)',
                row,
                re.I,
            ):
                signed = html_lib.unescape(pm.group(1)).strip()
                canonical = signed.split("?")[0]
                doc_links.append(
                    {
                        "url": signed,
                        "canonical_url": canonical,
                        "kind": "event_pdf",
                        "signed": "?" in signed and "X-Amz" in signed,
                        "discovered_only": True,
                        "document_fetched": False,
                    }
                )

        detail = html_lib.unescape(detail_url or "")
        if detail.startswith("/"):
            detail = "https://app01.jaggaer.com" + detail

        local_agency = agency
        # Prefer description agency mention
        plain = html_lib.unescape(re.sub(r"<[^>]+>", " ", row))
        am = re.search(
            r"((?:The\s+)?State of [A-Za-z]+(?:\s+[A-Za-z]+)*(?:\s+Department of [A-Za-z &]+)?)",
            plain,
            re.I,
        )
        if am:
            local_agency = re.sub(r"^The\s+", "", am.group(1), flags=re.I).strip(" .")
        if not local_agency:
            local_agency = agency

        gate = is_structurally_valid_opportunity(
            {
                "title": title,
                "solicitation_number": solicitation_number,
                "external_id": external_id,
                "deadline_raw": deadline_raw,
                "detail_url": detail,
                "agency": local_agency,
                "status": status,
            }
        )
        if not gate["valid"]:
            continue

        found.append(
            CanonicalOpportunity(
                external_id=str(external_id)[:200],
                source_id=source_id,
                source_url=list_url,
                detail_url=detail[:500] if detail else (doc_links[0]["url"] if doc_links else None),
                title=title,
                solicitation_number=gate["sanitized_solicitation_number"] or solicitation_number,
                agency=local_agency,
                jurisdiction="STATE",
                buyer_type="STATE",
                status=status,
                deadline_raw=gate["sanitized_deadline_raw"] or deadline_raw,
                deadline_timezone=dl.get("timezone"),
                deadline_tz_confidence=dl.get("timezone_confidence"),
                document_links=doc_links,
                trust_tier=trust_tier,
                raw_metadata={
                    "notice_type": "OPEN_SOLICITATION" if status == "OPEN" else status,
                    "platform": "Jaggaer",
                    "rtype": (rtype or "").strip().upper() or None,
                    "anchor_id": anchor_id,
                    "posted_raw": posted_raw,
                    "document_link_discovered": bool(doc_links),
                    "document_fetched": False,
                    "customer_org": (parse_qs(urlparse(list_url).query).get("CustomerOrg") or [None])[0],
                    "structural_gate": gate,
                },
            )
        )

    # Dedup by external_id
    seen: set[str] = set()
    out: list[CanonicalOpportunity] = []
    for o in found:
        key = (o.external_id or "").lower()
        if not key or key in seen:
            continue
        if _is_status_word(o.solicitation_number or ""):
            continue
        seen.add(key)
        out.append(o)
    return out
