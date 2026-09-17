"""Autonomous procurement document location — FIND, don't mirror."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin, quote_plus

from application_clock import CLOCK_HISTORICAL_SIMULATION, clock_mode
from information_need_router import InformationSourceRouter, NEED_SPECIFICATION, NEED_SOLICITATION_PACKAGE
from procurement_source_knowledge import (
    ProcurementSourceKnowledgeBase,
    preserve_signed_query_string,
    signed_url_would_break_if_stripped,
)
from solicitation_package_retrieval import classify_document

# Location statuses
FOUND_PUBLIC = "FOUND_PUBLIC"
FOUND_AUTH_REQUIRED = "FOUND_AUTH_REQUIRED"
FOUND_ALTERNATE_PUBLIC_SOURCE = "FOUND_ALTERNATE_PUBLIC_SOURCE"
NOT_FOUND = "NOT_FOUND"
SOURCE_BLOCKED = "SOURCE_BLOCKED"
SOURCE_ERROR = "SOURCE_ERROR"
UNKNOWN = "UNKNOWN"

# Auth
AUTH_PUBLIC = "PUBLIC"
AUTH_REQUIRED = "AUTH_REQUIRED"
AUTH_OPERATOR_DOWNLOAD = "OPERATOR_DOWNLOAD_REQUIRED"
AUTH_DENIED = "ACCESS_DENIED"
AUTH_UNKNOWN = "UNKNOWN"

# Doc classes (align with mission + reuse package classifier mapping)
DOC_SOLICITATION = "SOLICITATION"
DOC_SPECIFICATION = "SPECIFICATION"
DOC_ATTACHMENT = "ATTACHMENT"
DOC_AMENDMENT = "AMENDMENT"
DOC_Q_AND_A = "Q_AND_A"
DOC_PRICING_SHEET = "PRICING_SHEET"
DOC_BID_FORM = "BID_FORM"
DOC_TERMS = "TERMS_AND_CONDITIONS"
DOC_DRAWING = "DRAWING"
DOC_PRODUCT_SCHEDULE = "PRODUCT_SCHEDULE"
DOC_AWARD = "AWARD_DOCUMENT"
DOC_BID_TAB = "BID_TAB"
DOC_OTHER = "OTHER"

_CLASS_MAP = {
    "SOLICITATION": DOC_SOLICITATION,
    "SPECIFICATION": DOC_SPECIFICATION,
    "AMENDMENT": DOC_AMENDMENT,
    "Q_AND_A": DOC_Q_AND_A,
    "PRICING_SHEET": DOC_PRICING_SHEET,
    "BID_SCHEDULE": DOC_PRICING_SHEET,
    "LINE_ITEM_SCHEDULE": DOC_PRODUCT_SCHEDULE,
    "TERMS": DOC_TERMS,
    "DRAWING": DOC_DRAWING,
    "VENDOR_FORM": DOC_BID_FORM,
    "UNKNOWN": DOC_OTHER,
}


def map_package_doc_class(raw: str) -> str:
    return _CLASS_MAP.get(raw, DOC_OTHER)


def generate_search_keys(
    *,
    solicitation_number: str | None = None,
    agency: str | None = None,
    title: str | None = None,
    product_description: str | None = None,
    event_id: str | None = None,
    notice_id: str | None = None,
    contract_number: str | None = None,
) -> list[str]:
    """Carefully controlled search keys — no uncontrolled explosions."""
    keys: list[str] = []
    if solicitation_number:
        keys.append(solicitation_number.strip())
        # Limited Iowa-style variants
        compact = re.sub(r"\s+", "", solicitation_number)
        if compact != solicitation_number:
            keys.append(compact)
        # Hyphen normalization only
        if "_" in solicitation_number:
            keys.append(solicitation_number.replace("_", "-"))
    for v in (event_id, notice_id, contract_number):
        if v:
            keys.append(str(v).strip())
    if title:
        keys.append(title.strip()[:120])
    if product_description and product_description != title:
        keys.append(str(product_description).strip()[:80])
    if agency and solicitation_number:
        keys.append(f"{agency} {solicitation_number}")
    # Dedupe preserve order
    seen: set[str] = set()
    out = []
    for k in keys:
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:12]  # hard cap


def classify_located_document(
    *,
    title: str | None = None,
    filename: str | None = None,
    url: str | None = None,
    text_snippet: str | None = None,
) -> dict[str, Any]:
    raw = classify_document(title=title, filename=filename, url=url, text_snippet=text_snippet)
    mapped = map_package_doc_class(raw)
    controlling_uncertain = mapped in {DOC_OTHER, DOC_ATTACHMENT}
    return {
        "document_class": mapped,
        "package_classifier": raw,
        "likely_controlling": mapped in {DOC_SOLICITATION, DOC_SPECIFICATION, DOC_AMENDMENT},
        "controlling_uncertainty": controlling_uncertain,
    }


def locate_procurement_documents(
    *,
    source: str | None = None,
    source_url: str | None = None,
    solicitation_id: str | None = None,
    notice_id: str | None = None,
    agency: str | None = None,
    title: str | None = None,
    known_metadata: dict[str, Any] | None = None,
    known_document_links: list[dict[str, Any]] | None = None,
    knowledge: ProcurementSourceKnowledgeBase | None = None,
    router: InformationSourceRouter | None = None,
    need_type: str = NEED_SOLICITATION_PACKAGE,
    allow_benchmark_answer_key: bool = False,
    benchmark_hidden_urls: list[str] | None = None,
) -> dict[str, Any]:
    """Locate candidate documents using source knowledge + known links.

    Does NOT accept benchmark answer-key URLs into production logic.
    """
    if clock_mode() == CLOCK_HISTORICAL_SIMULATION:
        temporal_note = "HISTORICAL_SIMULATION: located refs still need temporal firewall before analysis use"
    else:
        temporal_note = "SYSTEM mode: live public location"

    kb = knowledge or ProcurementSourceKnowledgeBase()
    rtr = router or InformationSourceRouter(kb)
    meta = known_metadata or {}

    # Block benchmark cheat paths
    hidden = set(benchmark_hidden_urls or [])
    if not allow_benchmark_answer_key and hidden:
        # Ensure we never prefer these
        pass

    keys = generate_search_keys(
        solicitation_number=solicitation_id or meta.get("solicitation_number"),
        agency=agency or meta.get("agency"),
        title=title or meta.get("title"),
        product_description=meta.get("product_description"),
        event_id=meta.get("event_id"),
        notice_id=notice_id or meta.get("notice_id"),
        contract_number=meta.get("contract_number"),
    )

    route = rtr.route(
        need_type,
        opportunity={
            "agency": agency,
            "jurisdiction": meta.get("jurisdiction") or meta.get("state"),
            "source": source,
        },
        known_urls=[source_url] if source_url else None,
        agency=agency,
    )

    candidates: list[dict[str, Any]] = []
    status = NOT_FOUND

    # 1) Known document links from discovery (preserve signed URLs)
    for link in known_document_links or []:
        url = link.get("url") or link.get("href")
        if not url:
            continue
        if url in hidden and not allow_benchmark_answer_key:
            candidates.append(
                {
                    "url": None,
                    "rejected": True,
                    "reason": "benchmark_answer_key_url_blocked_from_production_locator",
                    "status": SOURCE_BLOCKED,
                }
            )
            continue
        url = preserve_signed_query_string(url)
        cls = classify_located_document(
            title=link.get("title") or title,
            filename=link.get("filename"),
            url=url,
        )
        auth = AUTH_PUBLIC
        if link.get("auth_required") or link.get("login_required"):
            auth = AUTH_REQUIRED
            status = FOUND_AUTH_REQUIRED
        else:
            status = FOUND_PUBLIC
        candidates.append(
            {
                "url": url,
                "canonical_url": url.split("?")[0] if url else None,
                "signed_query_preserved": signed_url_would_break_if_stripped(url),
                "document_type": cls["document_class"],
                "source": source or link.get("source") or "known_document_links",
                "authority_level": "AUTHORITATIVE_IF_AGENCY_HOSTED",
                "public_accessibility": auth,
                "authentication_requirement": auth,
                "version_amendment_relationship": link.get("amendment_of") or UNKNOWN,
                "temporal_metadata": {
                    "retrieved_listing_at": meta.get("retrieved_at"),
                    "publication_date": link.get("publication_date") or UNKNOWN,
                },
                "confidence": "HIGH" if "X-Amz-" in url or "event.pdf" in url.lower() else "MEDIUM",
                "retrieval_strategy": "http_get_preserve_query" if "X-Amz-" in url else "http_get",
                "classification": cls,
                "status": status,
                "reason_selected": "known_link_from_discovery_or_metadata",
            }
        )

    # 2) Source recipe suggests public event search URL (reference only — no fetch here)
    profiles = kb.match_by_jurisdiction(meta.get("state") or meta.get("jurisdiction"), agency)
    if not candidates and profiles:
        p = profiles[0]
        search_url = p.get("base_url") or p.get("search_url_pattern")
        candidates.append(
            {
                "url": search_url,
                "document_type": DOC_OTHER,
                "source": p.get("source_id"),
                "authority_level": p.get("authoritativeness"),
                "public_accessibility": AUTH_PUBLIC,
                "authentication_requirement": p.get("authentication_behavior", AUTH_UNKNOWN),
                "confidence": "LOW",
                "retrieval_strategy": p.get("event_lookup_method"),
                "status": UNKNOWN,
                "reason_selected": "source_recipe_search_entry_point",
                "search_keys": keys,
                "note": "Locator returns entry point; fetch happens in temporary retrieval",
            }
        )
        status = UNKNOWN

    if not candidates:
        status = NOT_FOUND

    # Aggregate status
    if any(c.get("status") == FOUND_PUBLIC for c in candidates):
        overall = FOUND_PUBLIC
    elif any(c.get("status") == FOUND_AUTH_REQUIRED for c in candidates):
        overall = FOUND_AUTH_REQUIRED
    elif candidates:
        overall = UNKNOWN
    else:
        overall = NOT_FOUND

    return {
        "kind": "DocumentLocationResult",
        "overall_status": overall,
        "candidates": candidates,
        "search_keys": keys,
        "route": route,
        "temporal_note": temporal_note,
        "clock_mode": clock_mode(),
        "benchmark_answer_key_used": False,
        "winning_vendor_seeded": False,
        "winning_price_seeded": False,
    }
