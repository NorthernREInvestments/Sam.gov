"""Procurement record identity verification (Build 002G).

Discovery remains unrestricted. Identity verification decides whether a
discovered record represents an individual solicitation vs a procurement
artifact (listing, forecast, award history, portal, etc.).

This is NOT product/business suitability filtering.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

RECORD_TYPES = {
    "INDIVIDUAL_SOLICITATION",
    "PROCUREMENT_LISTING",
    "PROCUREMENT_PORTAL",
    "HISTORICAL_AWARD",
    "PROCUREMENT_FORECAST",
    "PURCHASING_PLAN",
    "VENDOR_REGISTRATION",
    "PROCUREMENT_ANNOUNCEMENT",
    "GENERAL_PROCUREMENT_INFORMATION",
    "SUPPORTING_DOCUMENT",
    "UNRELATED_CONTENT",
    "UNRESOLVED_RECORD",
}

# Population buckets used for acceptance reconciliation
VERIFIED_SOLICITATION_TYPES = {"INDIVIDUAL_SOLICITATION"}
LEAD_TYPES = {
    "PROCUREMENT_LISTING",
    "PROCUREMENT_ANNOUNCEMENT",
    "UNRESOLVED_RECORD",
    "SUPPORTING_DOCUMENT",
}
NON_OPPORTUNITY_TYPES = {
    "PROCUREMENT_PORTAL",
    "HISTORICAL_AWARD",
    "PROCUREMENT_FORECAST",
    "PURCHASING_PLAN",
    "VENDOR_REGISTRATION",
    "GENERAL_PROCUREMENT_INFORMATION",
    "UNRELATED_CONTENT",
}

JUNK_TITLE_RE = re.compile(
    r"(?i)^(glossary|rfp\s*vs\s*rfq|faq|home|welcome|privacy|login|sign\s*in|"
    r"cookie|accessibility|contact\s*us|about\s*us|news|blog|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|"
    r"delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|"
    r"kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|"
    r"mississippi|missouri|montana|nebraska|nevada|new\s*hampshire|"
    r"new\s*jersey|new\s*mexico|new\s*york|north\s*carolina|north\s*dakota|"
    r"ohio|oklahoma|oregon|pennsylvania|rhode\s*island|south\s*carolina|"
    r"south\s*dakota|tennessee|texas|utah|vermont|virginia|washington|"
    r"west\s*virginia|wisconsin|wyoming|district\s*of\s*columbia|"
    r"commodity\s*codes?|vendor\s*registration|how\s*to\s*bid)$"
)

PORTAL_PATH_RE = re.compile(
    r"(?i)/(?:home|index|default|login|register|signup|vendor.?reg|"
    r"about|contact|faq|privacy)(?:\.html?)?/?$"
)
LISTING_MARKERS = (
    "/bid-opportunities",
    "/bids/",
    "/current-bids",
    "/open-bids",
    "/solicitations",
    "/procurement",
    "/purchasing",
    "procuretap.com/bids/",
    "/commodity",
    "resource/",
    "/buying-plan",
)
DETAIL_MARKERS = (
    "/solicitation/",
    "/opportunity/",
    "/opportunities/",
    "/bid/",
    "/rfp/",
    "/rfq/",
    "/ifb/",
    "/project/",
    "/projects/",
    "noticeid=",
    "solicitationid=",
    "opportunityid=",
    "opportunity-details",
    "/bids/details/",
    "biddetail",
    "bidview",
    "bidid=",
    "docid=",
    "syndicatedbid",
    "folder_name=",
    "bo-detail",
    "eventdetails",
    "esourcenumber=",
    "/event/",
    "event-details",
)
DOC_EXTS = (".pdf", ".docx", ".xlsx", ".xls", ".doc", ".rtf", ".zip")
FORECAST_MARKERS = re.compile(
    r"(?i)\b(forecast|buying\s*plan|anticipated\s*(?:rfp|procurement)|"
    r"upcoming\s*procurement|eye\s*on\s*the\s*future|future\s*solicit)\b"
)
AWARD_MARKERS = re.compile(
    r"(?i)\b(award(?:ed)?|bid\s*tabulation|intent\s*to\s*(?:award|execute)|"
    r"contract\s*award|historical\s*award|vendor\s*payment)\b"
)
REG_MARKERS = re.compile(
    r"(?i)\b(vendor\s*registration|supplier\s*registration|create\s*an?\s*account|"
    r"how\s*to\s*register|become\s*a\s*vendor)\b"
)
OPEN_DATA_HOSTS = (
    "data.cityofnewyork.us",
    "datacatalog.cookcountyil.gov",
    "citydata.mesaaz.gov",
    "data.illinois.gov",
    "data.ny.gov",
    "internal-sandiegocounty.data.socrata.com",
)


def _host(url: str) -> str:
    return (urlparse(url or "").hostname or "").lower()


def _path(url: str) -> str:
    return (urlparse(url or "").path or "").lower()


def _is_document_url(url: str) -> bool:
    path = _path(url)
    return any(path.endswith(ext) for ext in DOC_EXTS)


def _looks_like_detail(url: str) -> bool:
    low = (url or "").lower()
    if any(m in low for m in DETAIL_MARKERS):
        if not any(m in low for m in ("/bid-opportunities", "procuretap.com/bids/", "/current-bids")):
            return True
    return False


def _looks_like_listing(url: str) -> bool:
    low = (url or "").lower()
    path = _path(url)
    # Query-param detail pages are not board listings
    if any(
        m in low
        for m in (
            "bidid=",
            "docid=",
            "noticeid=",
            "solicitationid=",
            "opportunityid=",
            "syndicatedbid",
            "biddetail",
            "bidview",
            "folder_name=",
        )
    ):
        return False
    if any(m in low for m in LISTING_MARKERS):
        return True
    parts = [p for p in path.split("/") if p]
    if path in {"", "/"} or len(parts) <= 1:
        return True
    return False


def _has_solicitation_identity(item: dict[str, Any]) -> bool:
    sn = str(item.get("solicitation_number") or "").strip()
    if sn and len(sn) >= 3:
        return True
    title = str(item.get("title") or "").strip()
    buyer = str(item.get("buyer_name") or item.get("buyer") or "").strip()
    if title and JUNK_TITLE_RE.match(title):
        return False
    # Strong procurement phrasing is identity evidence even without a buyer field
    if title and len(title) >= 16 and re.search(
        r"(?i)\b("
        r"supply\s+and\s+delivery|purchase\s+of|procurement\s+of|"
        r"invitation\s+to\s+bid|request\s+for\s+(?:proposal|quote|bid|tender)|"
        r"sealed\s+bid|notice\s+to\s+bidders"
        r")\b",
        title,
    ):
        return True
    # Title alone is weak; require title + buyer + non-junk
    if title and len(title) >= 12 and buyer and len(buyer) >= 3:
        # Prefer titles that look like procurements
        if re.search(r"(?i)\b(rfp|rfq|ifb|itb|rfb|bid|solicitation|invitation|quote)\b", title):
            return True
        if re.search(r"(?i)\b(supply|supplies|equipment|materials|furniture|commodity)\b", title):
            return True
    # Folder / bid-board detail URLs often encode the solicitation id
    url = str(item.get("official_url") or "")
    if re.search(r"(?i)(folder_name=|bidid=|solicitation[=/]|opportunity[=/]|bids?/details?/)", url):
        if title and len(title) >= 12:
            return True
    return False


def verify_record_identity(item: dict[str, Any], *, source_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify procurement record type with evidence. Does not invent fields."""
    cfg = dict(source_config or {})
    title = str(item.get("title") or "").strip()
    url = str(item.get("official_url") or item.get("authoritative_url") or "").strip()
    status = str(item.get("solicitation_status") or "unknown").lower()
    evidence: list[str] = []
    host = _host(url)
    purpose = str(
        item.get("source_purpose")
        or cfg.get("record_purpose")
        or cfg.get("dataset_purpose")
        or ""
    ).lower()

    # Config / adapter declared purpose (source intelligence)
    if cfg.get("is_forecast") or purpose in {"forecast", "procurement_forecast", "purchasing_plan"} or status == "forecast":
        rtype = "PURCHASING_PLAN" if purpose == "purchasing_plan" or "buying plan" in title.lower() else "PROCUREMENT_FORECAST"
        evidence.append("source_or_status_marked_forecast")
        return _result(rtype, evidence, verified_solicitation=False)

    if cfg.get("is_historical") or purpose in {"historical_award", "award_history", "bid_tabulation"}:
        evidence.append("source_marked_historical")
        return _result("HISTORICAL_AWARD", evidence, verified_solicitation=False)

    if purpose in {"vendor_registration"}:
        return _result("VENDOR_REGISTRATION", ["source_purpose_vendor_registration"], False)

    if purpose in {"portal", "procurement_portal"}:
        return _result("PROCUREMENT_PORTAL", ["source_purpose_portal"], False)

    if purpose in {"listing", "procurement_listing"}:
        return _result("PROCUREMENT_LISTING", ["source_purpose_listing"], False)

    # Explicit status
    if status in {"historical_award", "awarded"}:
        return _result("HISTORICAL_AWARD", ["status_historical_award"], False)
    if status in {"closed", "cancelled", "closed_by_deadline"} and not _looks_like_detail(url) and not _is_document_url(url):
        # Closed without a detail/doc URL is typically archive metadata
        if host in OPEN_DATA_HOSTS or "resource/" in url:
            evidence.append("closed_open_data_row")
            return _result("HISTORICAL_AWARD", evidence, False)

    # Junk / unrelated titles (common AI harvest false positives)
    if title and JUNK_TITLE_RE.match(title):
        evidence.append("junk_or_state_name_title")
        return _result("UNRELATED_CONTENT", evidence, False)

    blob = f"{title} {url}".lower()
    if REG_MARKERS.search(blob):
        return _result("VENDOR_REGISTRATION", ["vendor_registration_language"], False)
    if FORECAST_MARKERS.search(blob) and not _looks_like_detail(url):
        return _result("PROCUREMENT_FORECAST", ["forecast_language"], False)
    if AWARD_MARKERS.search(blob) and status not in {"open", "amended"}:
        return _result("HISTORICAL_AWARD", ["award_language"], False)

    # URL shape
    if not url or not url.startswith("http"):
        if _has_solicitation_identity(item) and status in {"open", "amended", "unknown"}:
            evidence.append("identity_fields_without_url")
            return _result("UNRESOLVED_RECORD", evidence, False)
        return _result("UNRESOLVED_RECORD", ["missing_url"], False)

    if PORTAL_PATH_RE.search(_path(url)) and not _looks_like_detail(url):
        return _result("PROCUREMENT_PORTAL", ["portal_homepage_path"], False)

    if _is_document_url(url):
        evidence.append("direct_document_url")
        if _has_solicitation_identity(item) or re.search(r"(?i)\b(rfp|rfq|ifb|bid|solicitation)\b", title):
            evidence.append("document_with_solicitation_signals")
            return _result("INDIVIDUAL_SOLICITATION", evidence, True, identity_strength="document")
        evidence.append("document_without_clear_solicitation_boundary")
        return _result("SUPPORTING_DOCUMENT", evidence, False)

    if _looks_like_detail(url):
        evidence.append("detail_url_markers")
        if _has_solicitation_identity(item) or len(title) >= 12:
            evidence.append("detail_url_with_title_or_number")
            return _result("INDIVIDUAL_SOLICITATION", evidence, True, identity_strength="detail_url")
        return _result("UNRESOLVED_RECORD", evidence + ["detail_url_thin_title"], False)

    # Open-data resource URLs without detail mapping
    if "resource/" in url or host.endswith(".socrata.com") or host in OPEN_DATA_HOSTS:
        evidence.append("open_data_resource_url")
        # Build 002Q: open/amended rows with solicitation identity are individual
        # solicitations evidenced by the official open-data record itself.
        # Detail-URL enrichment remains desirable but absence is missing docs, not non-identity.
        if _has_solicitation_identity(item) and status in {"open", "amended", "upcoming"}:
            evidence.append("open_data_row_with_solicitation_identity_and_active_status")
            return _result(
                "INDIVIDUAL_SOLICITATION",
                evidence,
                True,
                identity_strength="open_data_row",
            )
        if _has_solicitation_identity(item) and status == "unknown":
            evidence.append("open_data_row_with_id_status_unknown")
            return _result("UNRESOLVED_RECORD", evidence, False)
        if status == "forecast" or cfg.get("is_forecast"):
            return _result("PROCUREMENT_FORECAST", evidence, False)
        if status in {"closed", "historical_award", "cancelled"}:
            return _result("HISTORICAL_AWARD", evidence, False)
        return _result("GENERAL_PROCUREMENT_INFORMATION", evidence + ["thin_open_data_row"], False)

    if _looks_like_listing(url):
        evidence.append("listing_or_board_url")
        return _result("PROCUREMENT_LISTING", evidence, False)

    # Generic HTML harvest: treat as unresolved lead unless strong signals
    if item.get("discovery_stage") == "unrestricted" and item.get("source_key", "").startswith(
        ("ai_", "supplier_")
    ):
        evidence.append("ai_or_supplier_harvest")
        if _has_solicitation_identity(item) and _looks_like_detail(url):
            return _result("INDIVIDUAL_SOLICITATION", evidence, True)
        return _result("UNRESOLVED_RECORD", evidence, False)

    if _has_solicitation_identity(item):
        # Active solicitations with identity fields + an official URL are individual
        # solicitations evidenced by the source row (Build 002Q). Missing richer detail
        # pages is a document gap, not proof the row is non-individual.
        if status in {"open", "amended", "upcoming"} and url.startswith("http"):
            evidence.append("identity_fields_present_active_status_official_url")
            return _result(
                "INDIVIDUAL_SOLICITATION",
                evidence,
                True,
                identity_strength="active_identity_fields",
            )
        if status == "unknown" and url.startswith("http") and _looks_like_detail(url):
            evidence.append("identity_fields_detail_url_status_unknown")
            return _result(
                "INDIVIDUAL_SOLICITATION",
                evidence,
                True,
                identity_strength="detail_url",
            )
        evidence.append("identity_fields_present_url_ambiguous")
        return _result("UNRESOLVED_RECORD", evidence, False)

    return _result("UNRESOLVED_RECORD", evidence or ["insufficient_identity_evidence"], False)


def _result(
    record_type: str,
    evidence: list[str],
    verified_solicitation: bool,
    *,
    identity_strength: str = "none",
) -> dict[str, Any]:
    assert record_type in RECORD_TYPES
    return {
        "record_type": record_type,
        "record_type_evidence": evidence[:12],
        "verified_individual_solicitation": bool(verified_solicitation),
        "identity_strength": identity_strength if verified_solicitation else "none",
        "may_lead_to_solicitation": record_type in LEAD_TYPES or verified_solicitation,
        "eligible_for_full_package": bool(verified_solicitation),
        "eligible_for_requirement_classification": bool(
            verified_solicitation or record_type in LEAD_TYPES | {"SUPPORTING_DOCUMENT"}
        ),
    }


def apply_identity(item: dict[str, Any], *, source_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return a copy of item with identity fields applied."""
    out = dict(item)
    ident = verify_record_identity(out, source_config=source_config)
    out.update(ident)
    # Align status for forecasts / historical when identity is definitive
    if ident["record_type"] == "PROCUREMENT_FORECAST" and (out.get("solicitation_status") or "unknown") == "unknown":
        out["solicitation_status"] = "forecast"
    if ident["record_type"] == "PURCHASING_PLAN" and (out.get("solicitation_status") or "unknown") == "unknown":
        out["solicitation_status"] = "forecast"
    if ident["record_type"] == "HISTORICAL_AWARD" and (out.get("solicitation_status") or "unknown") in {
        "unknown",
        "open",
    }:
        # Do not flip open→historical without evidence; only fill unknown
        if (item.get("solicitation_status") or "unknown") == "unknown":
            out["solicitation_status"] = "historical_award"
    return out


def apply_identity_batch(
    items: list[dict[str, Any]],
    *,
    source_configs: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply identity to every item; return items + breakdown."""
    source_configs = source_configs or {}
    out: list[dict[str, Any]] = []
    breakdown: dict[str, int] = {}
    verified = 0
    for it in items:
        sk = str(it.get("source_key") or "")
        cfg = source_configs.get(sk) or {}
        tagged = apply_identity(it, source_config=cfg)
        out.append(tagged)
        rt = tagged.get("record_type") or "UNRESOLVED_RECORD"
        breakdown[rt] = breakdown.get(rt, 0) + 1
        if tagged.get("verified_individual_solicitation"):
            verified += 1
    return {
        "items": out,
        "identity_breakdown": breakdown,
        "verified_individual_solicitations": verified,
        "total": len(out),
    }


def summarize_identity(items: list[dict[str, Any]]) -> dict[str, Any]:
    breakdown: dict[str, int] = {}
    verified = 0
    for it in items:
        rt = it.get("record_type") or "UNRESOLVED_RECORD"
        breakdown[rt] = breakdown.get(rt, 0) + 1
        if it.get("verified_individual_solicitation"):
            verified += 1
    return {
        "total": len(items),
        "identity_breakdown": breakdown,
        "verified_individual_solicitations": verified,
        "leads": sum(breakdown.get(t, 0) for t in LEAD_TYPES),
        "non_opportunity": sum(breakdown.get(t, 0) for t in NON_OPPORTUNITY_TYPES),
    }


# --- NJ / multi-notice PDF helpers (identity boundaries, not fabrication) ---

NOTICE_SPLIT_RE = re.compile(
    r"(?i)(?:^|\n)\s*(?:solicitation|bid|rfp|rfq|ifb|invitation)\s*(?:number|#|no\.?)[:\s]+([A-Z0-9][A-Z0-9\-_/]{3,})"
)


def split_multi_notice_text(text: str) -> list[dict[str, Any]]:
    """Detect multiple solicitation numbers in one document text.

    Returns boundary candidates with evidence. Does not invent missing notices.
    """
    if not text or len(text) < 40:
        return []
    matches = list(NOTICE_SPLIT_RE.finditer(text))
    if len(matches) < 2:
        # Single or none — not a multi-notice split
        if len(matches) == 1:
            return [
                {
                    "solicitation_number": matches[0].group(1),
                    "offset": matches[0].start(),
                    "evidence": ["single_notice_marker"],
                }
            ]
        return []
    notices: list[dict[str, Any]] = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        chunk = text[start:end]
        notices.append(
            {
                "solicitation_number": m.group(1),
                "offset": start,
                "excerpt": chunk[:1500],
                "evidence": ["multi_notice_boundary_detected", f"notice_index:{i}"],
            }
        )
    return notices
