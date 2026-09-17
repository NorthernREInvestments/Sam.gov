"""Solicitation document acquisition + structured extraction (deterministic)."""

from __future__ import annotations
from application_clock import now_utc

import hashlib
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlparse

from deep_deal_constants import (
    DEAL_CONSTRUCTION,
    DEAL_COOP_MASTER,
    DEAL_ONE_TIME_PRODUCT,
    DEAL_PRODUCT_PLUS_INSTALL,
    DEAL_SERVICE,
    DEAL_UNKNOWN,
    EV_ASSESSMENT,
    EV_UNKNOWN,
    EV_VERIFIED_DOCUMENT,
    EV_VERIFIED_PUBLIC,
    PROD_BRAND_EQUAL,
    PROD_EXACT,
    PROD_SPEC,
    PROD_UNKNOWN,
)

_MARKETING_DOC_SKIP = (
    r"vendor\s+registration",
    r"how\s+to\s+bid\s+training",
    r"webinar",
    r"marketing",
    r"cookie",
)


def _utc() -> str:
    return now_utc().isoformat()


def content_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    return hashlib.sha256(data).hexdigest()


def should_skip_document_url(url: str, *, title: str | None = None) -> bool:
    blob = f"{url} {title or ''}".lower()
    return any(re.search(p, blob, re.I) for p in _MARKETING_DOC_SKIP)


def discover_document_links(
    html: str,
    *,
    base_url: str,
    opportunity_id: str | None = None,
) -> list[dict[str, Any]]:
    """Extract likely solicitation document links from HTML."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for m in re.finditer(
        r'href=["\']([^"\']+\.(?:pdf|docx?|xlsx?|csv|zip))(?:\?[^"\']*)?["\']',
        html or "",
        re.I,
    ):
        href = m.group(1)
        url = urljoin(base_url, href)
        if url in seen or should_skip_document_url(url):
            continue
        seen.add(url)
        name = urlparse(url).path.split("/")[-1]
        out.append(
            {
                "document_url": url,
                "filename": name,
                "document_type": _guess_doc_type(name),
                "source_opportunity_id": opportunity_id,
                "discovered_at": _utc(),
            }
        )
    # Sourcewell-style solicitation pages often link "Download documents"
    for m in re.finditer(r'href=["\']([^"\']+)["\'][^>]*>([^<]{0,80}(?:document|solicitation|RFP|RFQ|addendum)[^<]{0,40})<', html or "", re.I):
        href, label = m.group(1), re.sub(r"\s+", " ", m.group(2)).strip()
        url = urljoin(base_url, href)
        if url in seen or should_skip_document_url(url, title=label):
            continue
        if any(x in url.lower() for x in (".pdf", "document", "file", "attachment", "solicitation")):
            seen.add(url)
            out.append(
                {
                    "document_url": url,
                    "filename": label[:120],
                    "document_type": _guess_doc_type(label),
                    "source_opportunity_id": opportunity_id,
                    "discovered_at": _utc(),
                    "link_label": label,
                }
            )
    return out


def _guess_doc_type(name: str) -> str:
    low = (name or "").lower()
    if "amend" in low or "addend" in low:
        return "AMENDMENT"
    if "price" in low or "bid schedule" in low or "pricing" in low:
        return "PRICING_SHEET"
    if "spec" in low:
        return "SPECIFICATION"
    if low.endswith(".xlsx") or low.endswith(".xls") or low.endswith(".csv"):
        return "SPREADSHEET"
    if low.endswith(".pdf"):
        return "PDF"
    if low.endswith(".docx") or low.endswith(".doc"):
        return "DOCX"
    if low.endswith(".zip"):
        return "ZIP"
    return "ATTACHMENT"


def retrieve_solicitation_page(
    url: str,
    *,
    client: Any | None = None,
    authorize_live: bool = False,
    source_id: str = "deep_deal",
) -> dict[str, Any]:
    """Fetch solicitation HTML page. Uses discovery HTTP client when provided."""
    result: dict[str, Any] = {
        "source_url": url,
        "retrieved_at": _utc(),
        "ok": False,
        "body": None,
        "status_code": None,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
    }
    if not authorize_live and client is None:
        result["error"] = "live_not_authorized"
        return result
    try:
        if client is not None:
            resp = client.get(url, source_id=source_id)
            body = resp.text or ""
            result.update(
                {
                    "ok": True,
                    "body": body,
                    "status_code": getattr(resp, "status_code", None),
                    "content_hash": content_hash(body),
                    "LIVE_API_REQUESTS": 1,
                }
            )
            return result
        import httpx

        with httpx.Client(timeout=30.0, follow_redirects=True) as http:
            r = http.get(url)
            body = r.text or ""
            result.update(
                {
                    "ok": True,
                    "body": body,
                    "status_code": r.status_code,
                    "content_hash": content_hash(body),
                    "LIVE_API_REQUESTS": 1,
                }
            )
            return result
    except Exception as exc:
        result["error"] = str(exc)
        return result


def extract_solicitation_facts(
    text: str,
    *,
    source_url: str | None = None,
    document_name: str | None = None,
) -> dict[str, Any]:
    """
    Deterministic extraction from solicitation HTML/PDF text.
    Does not invent absent clauses. Each fact carries provenance.
    """
    t = text or ""
    low = t.lower()
    facts: dict[str, Any] = {}
    provenance: dict[str, Any] = {}

    def put(key: str, value: Any, *, status: str, snippet: str | None = None) -> None:
        facts[key] = value
        provenance[key] = {
            "verification_status": status,
            "source_url": source_url,
            "document_name": document_name,
            "snippet": (snippet or "")[:240] if snippet else None,
        }

    # Solicitation ID
    m = re.search(r"(?:solicitation|event|rfp|rfq|ifb|rfb)\s*(?:#|number|no\.?)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-_/]{3,40})", t, re.I)
    if m:
        put("solicitation_id", m.group(1).strip(), status=EV_VERIFIED_DOCUMENT, snippet=m.group(0))

    # Due date
    m = re.search(
        r"(?:due|closing|closes|response\s+deadline|proposal\s+due)[^.\n]{0,40}?(\b(?:january|february|march|april|may|june|july|august|september|october|november|december)\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{4})",
        t,
        re.I,
    )
    if m:
        put("due_date_raw", m.group(1).strip(), status=EV_VERIFIED_DOCUMENT, snippet=m.group(0))

    # Timezone
    m = re.search(r"\b(CT|CST|CDT|ET|EST|EDT|MT|MST|MDT|PT|PST|PDT|UTC)\b", t)
    if m:
        put("timezone", m.group(1), status=EV_VERIFIED_DOCUMENT, snippet=m.group(0))

    # Contact
    m = re.search(r"(?:contact|questions?\s+to)[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})", t)
    if m:
        put("contact_name", m.group(1).strip(), status=EV_ASSESSMENT, snippet=m.group(0))
    m = re.search(r"(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", t)
    if m:
        put("contact_phone", m.group(0), status=EV_ASSESSMENT, snippet=m.group(0))

    # Set-aside
    if re.search(r"\bsmall\s+business\s+set[\s-]aside\b", low):
        put("set_aside", "SMALL_BUSINESS", status=EV_VERIFIED_DOCUMENT, snippet="small business set-aside")
    elif re.search(r"\b8\s*\(\s*a\s*\)\b", low):
        put("set_aside", "8A", status=EV_VERIFIED_DOCUMENT)
    elif re.search(r"\bunrestricted\b|\bfull\s+and\s+open\b", low):
        put("set_aside", "UNRESTRICTED", status=EV_VERIFIED_DOCUMENT)
    else:
        put("set_aside", None, status=EV_UNKNOWN)

    # Buy American / TAA
    if re.search(r"\bbuy\s+american\b", low):
        put("buy_american", True, status=EV_VERIFIED_DOCUMENT)
    else:
        put("buy_american", None, status=EV_UNKNOWN)
    if re.search(r"\btrade\s+agreements\s+act\b|\btaa\b", low):
        put("taa", True, status=EV_VERIFIED_DOCUMENT)
    else:
        put("taa", None, status=EV_UNKNOWN)

    # Bonds / insurance
    put("bid_bond_required", bool(re.search(r"\bbid\s+bond\b", low)), status=EV_VERIFIED_DOCUMENT if re.search(r"\bbid\s+bond\b", low) else EV_UNKNOWN)
    put("performance_bond_required", bool(re.search(r"\bperformance\s+bond\b", low)), status=EV_VERIFIED_DOCUMENT if re.search(r"\bperformance\s+bond\b", low) else EV_UNKNOWN)
    put("insurance_required", bool(re.search(r"\binsurance\s+required\b|\bliability\s+insurance\b", low)), status=EV_ASSESSMENT if re.search(r"\binsurance\b", low) else EV_UNKNOWN)

    # Installation / construction
    put("installation_required", bool(re.search(r"\binstall(?:ation)?\b", low)), status=EV_ASSESSMENT if "install" in low else EV_UNKNOWN)
    put("site_visit_required", bool(re.search(r"\bmandatory\s+site\s+visit\b|\bsite\s+visit\s+required\b", low)), status=EV_VERIFIED_DOCUMENT if re.search(r"\bsite\s+visit\b", low) else EV_UNKNOWN)

    # Brand / equal
    if re.search(r"\bbrand\s+name\s+only\b|\bno\s+substitutes?\b", low):
        put("brand_name_only", True, status=EV_VERIFIED_DOCUMENT)
        put("product_id_certainty", PROD_EXACT if re.search(r"\bmodel\b|\bpart\s*(?:#|number)\b", low) else PROD_UNKNOWN, status=EV_ASSESSMENT)
    elif re.search(r"\bor\s+equal\b|\bbrand\s+name\s+or\s+equal\b", low):
        put("brand_or_equal", True, status=EV_VERIFIED_DOCUMENT)
        put("product_id_certainty", PROD_BRAND_EQUAL, status=EV_VERIFIED_DOCUMENT)
    elif re.search(r"\bspecification\b|\bmust\s+meet\b", low):
        put("product_id_certainty", PROD_SPEC, status=EV_ASSESSMENT)
    else:
        put("product_id_certainty", PROD_UNKNOWN, status=EV_UNKNOWN)

    # FOB / freight
    m = re.search(r"\bf\.?o\.?b\.?\s+([a-z ]{3,40})", low)
    if m:
        put("fob_terms", m.group(0).strip(), status=EV_VERIFIED_DOCUMENT, snippet=m.group(0))
    else:
        put("fob_terms", None, status=EV_UNKNOWN)

    # Payment
    if re.search(r"\bnet\s*30\b", low):
        put("payment_terms", "NET30", status=EV_ASSESSMENT)
    else:
        put("payment_terms", None, status=EV_UNKNOWN)

    # Contract / coop language
    if re.search(r"\bcooperative\b|\bsourcewell\b|\bmaster\s+agreement\b", low):
        put("inferred_deal_type_hint", DEAL_COOP_MASTER, status=EV_VERIFIED_PUBLIC)
    elif re.search(r"\bhvac\b.*\bupgrade\b|\bconstruction\b", low):
        put("inferred_deal_type_hint", DEAL_CONSTRUCTION, status=EV_ASSESSMENT)
    elif re.search(r"\bsupply\s+and\s+install\b", low):
        put("inferred_deal_type_hint", DEAL_PRODUCT_PLUS_INSTALL, status=EV_ASSESSMENT)
    elif re.search(r"\bpurchase\s+of\b|\brfq\b", low):
        put("inferred_deal_type_hint", DEAL_ONE_TIME_PRODUCT, status=EV_ASSESSMENT)
    else:
        put("inferred_deal_type_hint", DEAL_UNKNOWN, status=EV_UNKNOWN)

    # Line item sketches (simple numbered lines)
    line_items = []
    for lm in re.finditer(
        r"(?:^|\n)\s*(?:CLIN|Item|Line)?\s*(\d{1,3})[\).:\-\s]+(.{10,160}?)(?:\s+(\d+)\s*(ea|each|units?|pcs?))?",
        t,
        re.I,
    ):
        if len(line_items) >= 40:
            break
        line_items.append(
            {
                "line_number": lm.group(1),
                "description": re.sub(r"\s+", " ", lm.group(2)).strip()[:200],
                "quantity": int(lm.group(3)) if lm.group(3) else None,
                "uom": lm.group(4),
                "manufacturer": None,
                "model": None,
                "part_number": None,
                "source_document": document_name,
                "source_url": source_url,
                "verification_status": EV_ASSESSMENT,
            }
        )
    facts["line_items"] = line_items
    provenance["line_items"] = {"verification_status": EV_ASSESSMENT if line_items else EV_UNKNOWN, "source_url": source_url}

    # NAICS
    m = re.search(r"\bNAICS\s*:?\s*(\d{6})\b", t, re.I)
    if m:
        put("naics", m.group(1), status=EV_VERIFIED_DOCUMENT, snippet=m.group(0))
    else:
        put("naics", None, status=EV_UNKNOWN)

    return {
        "facts": facts,
        "provenance": provenance,
        "extraction_method": "deterministic_regex",
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def reclassify_from_documents(
    *,
    listing_classification: str | None,
    extracted: dict[str, Any],
    deal_type: str | None = None,
) -> dict[str, Any]:
    """Document classification supersedes listing only with stronger evidence."""
    facts = (extracted or {}).get("facts") or {}
    listing = listing_classification or "UNKNOWN"
    doc_class = listing
    reason = "no_stronger_document_evidence"
    changed = False

    hint = facts.get("inferred_deal_type_hint")
    install = facts.get("installation_required")
    items = facts.get("line_items") or []

    # Construction / upgrade must not be kept as CORE_PRODUCT merely because equipment words exist
    if deal_type == DEAL_CONSTRUCTION or hint == DEAL_CONSTRUCTION:
        doc_class = "PRODUCT_PLUS_SERVICE" if items else "SERVICE"
        reason = "document_or_deal_type_construction_upgrade"
        changed = doc_class != listing
        return {
            "listing_classification": listing,
            "document_classification": doc_class,
            "classification_changed": changed,
            "classification_reason": reason,
            "product_id_certainty": facts.get("product_id_certainty") or PROD_UNKNOWN,
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
        }

    # Do not demote confirmed one-time product listings to SERVICE from portal chrome noise
    if deal_type == DEAL_ONE_TIME_PRODUCT and listing in {"CORE_PRODUCT", "UNKNOWN", "PRODUCT_PLUS_SERVICE"}:
        if hint == DEAL_SERVICE and not items:
            # Weak service hint on a product solicitation page — keep product unless exclusive
            doc_class = "CORE_PRODUCT" if listing != "PRODUCT_PLUS_SERVICE" else "PRODUCT_PLUS_SERVICE"
            reason = "one_time_product_preserved_despite_weak_service_chrome"
            changed = doc_class != listing
            return {
                "listing_classification": listing,
                "document_classification": doc_class,
                "classification_changed": changed,
                "classification_reason": reason,
                "product_id_certainty": facts.get("product_id_certainty") or PROD_UNKNOWN,
                "LIVE_API_REQUESTS": 0,
                "OpenAI": 0,
            }

    if hint == DEAL_SERVICE:
        doc_class = "SERVICE"
        reason = "document_service_signals"
        changed = doc_class != listing
    elif hint == DEAL_PRODUCT_PLUS_INSTALL or install is True:
        doc_class = "PRODUCT_PLUS_SERVICE"
        reason = "document_installation_required"
        changed = doc_class != listing
    elif items and any(re.search(r"equipment|supply|hardware|part|model", str(i.get("description") or ""), re.I) for i in items):
        doc_class = "CORE_PRODUCT"
        reason = "document_line_items_product"
        changed = doc_class != listing
    elif hint == DEAL_ONE_TIME_PRODUCT and listing == "UNKNOWN":
        doc_class = "CORE_PRODUCT"
        reason = "document_one_time_product_hint"
        changed = True

    if deal_type == DEAL_COOP_MASTER and listing == "CORE_PRODUCT":
        reason = f"{reason};cooperative_master_vehicle"

    return {
        "listing_classification": listing,
        "document_classification": doc_class,
        "classification_changed": changed,
        "classification_reason": reason,
        "product_id_certainty": facts.get("product_id_certainty") or PROD_UNKNOWN,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }
