"""Document Recovery + Procurement Evidence Engine — research only.

Recover authoritative procurement evidence before classifying opportunities
as incomplete. Never fabricate values, quantities, accessories, or prices.
Do not hammer permanently blocked sources. VA-operable queues; no outreach.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any

from application_clock import now_utc
from m3_commercial_engine import (
    PRICE_LEVEL_4_UNKNOWN,
    QTY_RE,
    SCORE_HIGH,
    SCORE_MEDIUM,
)
from m3_evidence_constants import (
    AUTH_REQUIRED,
    REGISTRATION_REQUIRED,
    SOURCE_BLOCKED,
    TIER_3_WEB,
)
from m3_procurement_package import (
    MATCH_HIGH,
    MATCH_LOW,
    MATCH_MEDIUM,
    MATCH_UNKNOWN,
    VAL_ESTIMATED,
    VAL_UNKNOWN,
    VAL_VERIFIED,
    build_package_product_identity,
    build_procurement_package,
)
from m3_product_pricing import build_contract_value_model

log = logging.getLogger("govtracker.m3_document_evidence")

EVIDENCE_INDEX_KEY = "m3_document_evidence_v1"

DOC_TYPES = (
    "solicitation",
    "amendment",
    "attachment",
    "pricing_schedule",
    "spreadsheet",
    "bom",
    "technical_specification",
    "award_notice",
    "contract_file",
    "historical_award_document",
    "unknown",
)

RECOVERY_PRIORITY = (
    "pricing_schedule",
    "spreadsheet",
    "bom",
    "technical_specification",
    "attachment",
    "award_notice",
    "historical_award_document",
    "solicitation",
    "amendment",
    "contract_file",
)

ATT_FOUND = "Found"
ATT_NOT_FOUND = "Not found"
ATT_ACCESS_BLOCKED = "Access blocked"
ATT_AUTH_REQUIRED = "Requires authentication"
ATT_EXPIRED = "Expired"
ATT_UNAVAILABLE = "Unavailable"

COMPLETE_PURCHASE_PACKAGE = "COMPLETE_PURCHASE_PACKAGE"
READY_FOR_PRICING = "READY_FOR_PRICING"
READY_FOR_SUPPLIER_RESEARCH = "READY_FOR_SUPPLIER_RESEARCH"
PARTIAL = "PARTIAL"
DOCUMENT_RECOVERY_REQUIRED = "DOCUMENT_RECOVERY_REQUIRED"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

Q_NEEDS_DOCUMENT_RECOVERY = "NEEDS_DOCUMENT_RECOVERY"
Q_NEEDS_PRODUCT_IDENTITY = "NEEDS_PRODUCT_IDENTITY"
Q_NEEDS_VALUE = "NEEDS_VALUE"
Q_NEEDS_QUANTITY = "NEEDS_QUANTITY"
Q_NEEDS_CONFIGURATION = "NEEDS_CONFIGURATION"
Q_NEEDS_PRICING = "NEEDS_PRICING"

HEALTH_ACCESS_BLOCKED = "ACCESS_BLOCKED"
HEALTH_DOCUMENT_REMOVED = "DOCUMENT_REMOVED"
HEALTH_AUTH_REQUIRED = "AUTH_REQUIRED"
HEALTH_REGISTRATION_REQUIRED = "REGISTRATION_REQUIRED"
HEALTH_NO_ATTACHMENT = "NO_ATTACHMENT_FOUND"
HEALTH_PARSER_FAILURE = "PARSER_FAILURE"
HEALTH_UNKNOWN = "UNKNOWN"
HEALTH_OK = "OK"

AUTH_OFFICIAL = "AUTHORITATIVE"
AUTH_SECONDARY = "SECONDARY"
AUTH_HISTORICAL = "HISTORICAL"
AUTH_UNKNOWN = "UNKNOWN"

PROC_PENDING = "PENDING"
PROC_PROCESSED = "PROCESSED"

VA_ALLOWED_ACTIONS = frozenset(
    {"ATTACH_EVIDENCE", "UPDATE_STATUS", "MARK_RECOVERY_ATTEMPTED", "ESCALATE", "NOTE"}
)
VA_FORBIDDEN_ACTIONS = frozenset(
    {"CHANGE_SCORING_RULES", "APPROVE_DEAL", "SUBMIT_BID", "CONTACT_SUPPLIER", "SPEND_MONEY"}
)

VALUE_RE = re.compile(
    r"(?:estimated?\s+(?:value|cost|amount)|award\s+(?:amount|value)|"
    r"contract\s+(?:value|amount)|ceiling|not\s+to\s+exceed|NTE|"
    r"total\s+(?:price|amount)|budget)[:\s]*\$?\s*([\d,]+(?:\.\d{2})?)",
    re.I,
)
UNIT_PRICE_RE = re.compile(
    r"(?:unit\s+price|each|\$/EA|price\s+each)[:\s]*\$?\s*([\d,]+(?:\.\d{2})?)",
    re.I,
)
CEILING_RE = re.compile(
    r"(?:ceiling|maximum|not\s+to\s+exceed|NTE)[:\s]*\$?\s*([\d,]+(?:\.\d{2})?)",
    re.I,
)
UOM_RE = re.compile(r"\b(EA|EACH|BOX|CS|CASE|SET|KIT|LB|KG|FT|GAL|PAIR|PK|PACK|LOT)\b", re.I)
MULTIYEAR_RE = re.compile(r"\b(base\s+year|option\s+year\s*\d*|multi[- ]year|FY\s*\d{2,4})\b", re.I)

DOC_TYPE_HINTS: list[tuple[str, re.Pattern[str]]] = [
    ("pricing_schedule", re.compile(r"pric(e|ing)\s*(schedule|sheet|list|table)", re.I)),
    ("spreadsheet", re.compile(r"\.(xlsx?|csv)\b|spreadsheet|line[\s_-]?item", re.I)),
    ("bom", re.compile(r"\bbom\b|bill\s+of\s+materials|parts\s+list", re.I)),
    ("technical_specification", re.compile(r"spec(ification)?s?|technical\s+data|salient\s+char", re.I)),
    ("award_notice", re.compile(r"award\s+notice|award\s+of\s+contract|justification", re.I)),
    ("historical_award_document", re.compile(r"historical\s+award|prior\s+award|past\s+performance", re.I)),
    ("amendment", re.compile(r"amendment|addendum|modification", re.I)),
    ("solicitation", re.compile(r"solicitation|invitation\s+for\s+bid|IFB|RFQ|RFP", re.I)),
    ("contract_file", re.compile(r"contract\s+file|executed\s+contract", re.I)),
    ("attachment", re.compile(r"attachment|exhibit|appendix", re.I)),
]

BLOCKED_RETRY_CAP = 2


def _utc() -> str:
    return now_utc().isoformat()


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(str(v).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _known(v: Any) -> bool:
    return v not in {None, "", "UNKNOWN", "unknown"}


def _blob(row: dict[str, Any], *, max_chars: int = 14000) -> str:
    parts = [str(row.get("title") or ""), str(row.get("description") or "")[:3000]]
    for key in (
        "solicitation_text",
        "attachment_text",
        "governing_text",
        "pws_text",
        "evidence_text_excerpt",
    ):
        if row.get(key):
            parts.append(str(row.get(key))[:4000])
    for d in row.get("documents") or []:
        if isinstance(d, dict):
            parts.append(str(d.get("name") or d.get("filename") or d.get("title") or ""))
            parts.append(str(d.get("extracted_text") or d.get("text") or "")[:2500])
    for li in row.get("line_items") or row.get("bom") or []:
        if isinstance(li, dict):
            parts.append(str(li.get("description") or ""))
            parts.append(str(li.get("part_number") or ""))
    return " ".join(parts)[:max_chars]


def classify_document_type(name: str, url: str = "", meta: dict[str, Any] | None = None) -> str:
    meta = meta or {}
    explicit = str(meta.get("document_type") or meta.get("type") or "").lower().strip()
    if explicit in DOC_TYPES:
        return explicit
    hay = f"{name} {url} {meta.get('description') or ''}"
    for dtype, pat in DOC_TYPE_HINTS:
        if pat.search(hay):
            return dtype
    if str(url).lower().endswith((".xlsx", ".xls", ".csv")):
        return "spreadsheet"
    if str(url).lower().endswith(".pdf"):
        return "attachment"
    return "unknown"


def authority_level(doc: dict[str, Any], row: dict[str, Any]) -> str:
    src = str(doc.get("source") or doc.get("authority") or "").lower()
    if any(x in src for x in ("official", "portal", "government", "authoritative", "sam.gov", "agency")):
        return AUTH_OFFICIAL
    if "historical" in src or "award" in str(doc.get("document_type") or "").lower():
        return AUTH_HISTORICAL
    if doc.get("bytes_recovered") or doc.get("url"):
        return AUTH_SECONDARY
    return AUTH_UNKNOWN


def build_document_evidence_profile(row: dict[str, Any]) -> dict[str, Any]:
    docs_in = row.get("documents") if isinstance(row.get("documents"), list) else []
    available: list[dict[str, Any]] = []
    for d in docs_in:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name") or d.get("filename") or d.get("title") or "document")
        url = str(d.get("url") or d.get("href") or d.get("download_url") or "")
        dtype = classify_document_type(name, url, d)
        available.append(
            {
                "name": name,
                "Document_URL": url or "UNKNOWN",
                "Document_type": dtype,
                "Document_source": d.get("source") or row.get("source_id") or "UNKNOWN",
                "Retrieval_date": d.get("retrieved_at") or d.get("at") or "UNKNOWN",
                "Authority_level": authority_level(d, row),
                "Processing_status": PROC_PROCESSED
                if (d.get("bytes_recovered") or d.get("extracted_text") or d.get("text"))
                else PROC_PENDING,
                "bytes_recovered": bool(d.get("bytes_recovered")),
                "has_text": bool(d.get("extracted_text") or d.get("text")),
            }
        )

    if row.get("line_items") or row.get("bom"):
        available.append(
            {
                "name": "line_items_or_bom",
                "Document_URL": "UNKNOWN",
                "Document_type": "bom",
                "Document_source": "pipeline_row",
                "Retrieval_date": _utc(),
                "Authority_level": AUTH_OFFICIAL,
                "Processing_status": PROC_PROCESSED,
                "bytes_recovered": True,
                "has_text": True,
            }
        )

    present_types = {a["Document_type"] for a in available}
    expected = ["pricing_schedule", "spreadsheet", "bom", "technical_specification", "solicitation"]
    missing = [t for t in expected if t not in present_types]
    return {
        "kind": "DOCUMENT_EVIDENCE_PROFILE",
        "Available_documents": available,
        "Missing_documents": missing,
        "document_count": len(available),
        "documents_with_bytes": sum(1 for a in available if a.get("bytes_recovered")),
        "priority_gaps": [t for t in RECOVERY_PRIORITY if t in missing][:5],
    }


def attachment_discovery_status(row: dict[str, Any], inventory: dict[str, Any]) -> dict[str, Any]:
    access = str(row.get("source_access_state") or row.get("package_access") or "").upper()
    fail = row.get("evidence_failure") if isinstance(row.get("evidence_failure"), dict) else {}
    primary = str(fail.get("primary_reason") or fail.get("reason") or "").upper()
    docs = inventory.get("Available_documents") or []
    with_bytes = [d for d in docs if d.get("bytes_recovered")]
    urls_only = [d for d in docs if _known(d.get("Document_URL")) and not d.get("bytes_recovered")]

    if access in {"AUTH_REQUIRED", "AUTH_GATED"} or primary in {AUTH_REQUIRED, "AUTH_REQUIRED"}:
        state = ATT_AUTH_REQUIRED
    elif access in {"REGISTRATION_REQUIRED"} or primary in {REGISTRATION_REQUIRED, "REGISTRATION_REQUIRED"}:
        state = ATT_AUTH_REQUIRED
    elif primary in {SOURCE_BLOCKED, "SOURCE_BLOCKED", "BOT_PROTECTED", "HTTP_403"}:
        state = ATT_ACCESS_BLOCKED
    elif "EXPIRED" in primary or "REMOVED" in primary or "404" in primary:
        state = ATT_EXPIRED if "EXPIRED" in primary else ATT_UNAVAILABLE
    elif with_bytes:
        state = ATT_FOUND
    else:
        state = ATT_NOT_FOUND

    return {
        "kind": "ATTACHMENT_DISCOVERY",
        "status": state,
        "found_count": len(with_bytes),
        "linked_unfetched": len(urls_only),
        "linked_urls": [d.get("Document_URL") for d in urls_only[:10]],
        "recovery_priority_order": list(RECOVERY_PRIORITY),
        "next_priority_targets": inventory.get("priority_gaps") or [],
    }


def source_health(row: dict[str, Any], discovery: dict[str, Any]) -> dict[str, Any]:
    access = str(row.get("source_access_state") or "").upper()
    fail = row.get("evidence_failure") if isinstance(row.get("evidence_failure"), dict) else {}
    primary = str(fail.get("primary_reason") or "").upper()
    attempts = row.get("evidence_recovery_attempts") or []
    blocked_attempts = sum(
        1
        for a in attempts
        if isinstance(a, dict)
        and str(a.get("failure") or a.get("status") or "").upper()
        in {"BLOCKED", "AUTH_REQUIRED", "REGISTRATION_REQUIRED", "BOT_PROTECTED"}
    )

    if discovery.get("status") == ATT_AUTH_REQUIRED or access in {"AUTH_REQUIRED", "AUTH_GATED"}:
        health = HEALTH_AUTH_REQUIRED
    elif access == "REGISTRATION_REQUIRED" or primary == REGISTRATION_REQUIRED:
        health = HEALTH_REGISTRATION_REQUIRED
    elif discovery.get("status") == ATT_ACCESS_BLOCKED or primary in {SOURCE_BLOCKED, "BOT_PROTECTED"}:
        health = HEALTH_ACCESS_BLOCKED
    elif "PARSER" in primary or primary == "PARSER_FAILURE":
        health = HEALTH_PARSER_FAILURE
    elif "REMOVED" in primary or "404" in primary:
        health = HEALTH_DOCUMENT_REMOVED
    elif discovery.get("status") in {ATT_NOT_FOUND, ATT_UNAVAILABLE} and not (discovery.get("found_count") or 0):
        health = HEALTH_NO_ATTACHMENT
    elif discovery.get("found_count"):
        health = HEALTH_OK
    else:
        health = HEALTH_UNKNOWN

    skip_retry = health in {
        HEALTH_ACCESS_BLOCKED,
        HEALTH_AUTH_REQUIRED,
        HEALTH_REGISTRATION_REQUIRED,
        HEALTH_DOCUMENT_REMOVED,
    } and blocked_attempts >= BLOCKED_RETRY_CAP

    return {
        "kind": "SOURCE_HEALTH",
        "status": health,
        "blocked_attempts": blocked_attempts,
        "skip_further_automated_retry": skip_retry,
        "last_recovery_attempt": (
            attempts[-1].get("at") if attempts and isinstance(attempts[-1], dict) else "UNKNOWN"
        ),
        "reasons": fail.get("reasons") or ([primary] if primary else []),
    }


def extract_value_evidence(row: dict[str, Any]) -> dict[str, Any]:
    contract = build_contract_value_model(row)
    blob = _blob(row)
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    values: list[dict[str, Any]] = []

    def _add(kind: str, val: Any, source: str, conf: str) -> None:
        n = _num(val)
        if n is None:
            return
        values.append({"kind": kind, "Value": n, "Source_document": source, "Confidence": conf})

    if contract.get("award_amount") not in {None, "UNKNOWN"}:
        _add("award_amount", contract["award_amount"], "award_metadata", VAL_VERIFIED)
    if contract.get("estimated_value") not in {None, "UNKNOWN"}:
        _add("estimated_value", contract["estimated_value"], "solicitation_metadata", VAL_ESTIMATED)
    if contract.get("contract_value") not in {None, "UNKNOWN"} and contract.get("confidence") != VAL_UNKNOWN:
        _add(
            "contract_value",
            contract["contract_value"],
            "contract_value_model",
            contract.get("confidence") or VAL_ESTIMATED,
        )

    ceiling = _num(meta.get("awardCeiling") or meta.get("ceiling") or row.get("ceiling_value"))
    if ceiling is not None:
        _add("ceiling_value", ceiling, "raw_metadata", VAL_ESTIMATED)
    else:
        cm = CEILING_RE.search(blob)
        if cm:
            _add("ceiling_value", cm.group(1), "solicitation_text", VAL_ESTIMATED)

    unit = _num(row.get("unit_price") or meta.get("unit_price"))
    if unit is None:
        um = UNIT_PRICE_RE.search(blob)
        unit = _num(um.group(1)) if um else None
    if unit is not None:
        _add("unit_price", unit, "pricing_or_text", VAL_ESTIMATED)

    hist = []
    for a in row.get("historical_awards") or row.get("award_history") or []:
        if isinstance(a, dict):
            amt = _num(a.get("amount") or a.get("award_amount"))
            if amt is not None:
                hist.append(amt)
                _add("historical_award", amt, "historical_award_document", VAL_ESTIMATED)

    line_total = None
    qty_for_calc = None
    for li in row.get("line_items") or row.get("bom") or []:
        if not isinstance(li, dict):
            continue
        q = _num(li.get("quantity"))
        up = _num(li.get("unit_price") or li.get("price"))
        if q is not None:
            qty_for_calc = q if qty_for_calc is None else qty_for_calc
        if q is not None and up is not None:
            line_total = (line_total or 0) + q * up
    if line_total is not None:
        _add("line_item_totals", round(line_total, 2), "line_items", VAL_ESTIMATED)
    elif unit is not None and qty_for_calc is not None:
        _add("quantity_x_unit_price", round(unit * qty_for_calc, 2), "calculated", VAL_ESTIMATED)

    if not values:
        for m in VALUE_RE.finditer(blob):
            _add("estimated_value_text", m.group(1), "solicitation_text", VAL_ESTIMATED)
            break

    best = None
    best_conf = VAL_UNKNOWN
    for pref in (
        "award_amount",
        "contract_value",
        "estimated_value",
        "ceiling_value",
        "line_item_totals",
        "quantity_x_unit_price",
        "historical_award",
        "estimated_value_text",
    ):
        hit = next((v for v in values if v["kind"] == pref), None)
        if hit:
            best = hit["Value"]
            best_conf = hit["Confidence"]
            break

    return {
        "kind": "VALUE_EVIDENCE",
        "primary_value": best if best is not None else "UNKNOWN",
        "confidence": best_conf if best is not None else VAL_UNKNOWN,
        "items": values[:20],
        "historical_award_values": hist[:10] or "UNKNOWN",
        "notes": ["never_create_economics_from_unverified_only", "text_extractions_are_ESTIMATED"],
    }


def extract_quantity_evidence(row: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    blob = _blob(row)
    for li in row.get("line_items") or row.get("bom") or []:
        if not isinstance(li, dict):
            continue
        q = _num(li.get("quantity"))
        if q is not None:
            items.append(
                {
                    "Value": q,
                    "Unit_of_measure": li.get("unit") or li.get("uom") or "UNKNOWN",
                    "Source": "line_items",
                    "Confidence": VAL_VERIFIED,
                    "kind": "line_quantity",
                }
            )
    if not items:
        qm = QTY_RE.search(blob)
        if qm:
            items.append(
                {
                    "Value": float(qm.group(1)),
                    "Unit_of_measure": (
                        UOM_RE.search(blob).group(1).upper() if UOM_RE.search(blob) else "UNKNOWN"
                    ),
                    "Source": "solicitation_text",
                    "Confidence": VAL_ESTIMATED,
                    "kind": "text_quantity",
                }
            )
    uom = UOM_RE.search(blob)
    primary = items[0] if items else None
    return {
        "kind": "QUANTITY_EVIDENCE",
        "Quantity": primary["Value"] if primary else "UNKNOWN",
        "Unit_of_measure": (primary.get("Unit_of_measure") if primary else None)
        or (uom.group(1).upper() if uom else "UNKNOWN"),
        "Confidence": primary["Confidence"] if primary else VAL_UNKNOWN,
        "Options_or_alternates": "UNKNOWN",
        "Multi_year_quantities": bool(MULTIYEAR_RE.search(blob)),
        "items": items[:20],
        "notes": ["do_not_infer_quantity_without_evidence"],
    }


def extract_product_evidence(row: dict[str, Any]) -> dict[str, Any]:
    identity = build_package_product_identity(row)
    return {
        "kind": "PRODUCT_EVIDENCE",
        "Manufacturer": identity.get("Manufacturer"),
        "Model": identity.get("Model_number"),
        "Part_number": identity.get("Manufacturer_part_number"),
        "NSN": identity.get("NSN"),
        "SKU": identity.get("SKU"),
        "CAGE": identity.get("CAGE"),
        "Technical_specifications": identity.get("Specifications"),
        "Configuration_requirements": identity.get("Configuration_requirements"),
        "Identity_confidence": identity.get("Identity_confidence"),
        "IDENTIFIER_CONFIDENCE": identity.get("IDENTIFIER_CONFIDENCE") or [],
        "rejected_identifiers": identity.get("rejected_identifiers") or [],
        "priority_sources_used": ["line_items", "official_attachments", "title_description"],
        "sufficient_for_pricing": identity.get("sufficient_for_pricing_research"),
    }


def procurement_completeness_score(
    inventory: dict[str, Any],
    values: dict[str, Any],
    quantities: dict[str, Any],
    product: dict[str, Any],
    package: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = (package or {}).get("PRODUCT_CONFIGURATION") or {}
    bom = (package or {}).get("PROCUREMENT_BOM") or {}
    market = (package or {}).get("MARKET_PRICING") or {}
    flags = {
        "contract_value_available": values.get("confidence") != VAL_UNKNOWN,
        "quantity_available": quantities.get("Confidence") != VAL_UNKNOWN,
        "exact_product_available": product.get("Identity_confidence") == MATCH_HIGH,
        "part_number_available": _known(product.get("Part_number")) or _known(product.get("NSN")),
        "configuration_available": bool(cfg.get("PRIMARY_EQUIPMENT")) or bool(cfg.get("configuration_complete")),
        "bom_available": int(bom.get("line_count") or 0) >= 1
        and (int(bom.get("confirmed_lines") or 0) + int(bom.get("likely_lines") or 0)) >= 1,
        "pricing_information_available": bool(market)
        and market.get("primary_level") not in {None, PRICE_LEVEL_4_UNKNOWN},
    }
    docs_thin = int(inventory.get("documents_with_bytes") or 0) == 0 and not flags["bom_available"]
    if (
        flags["contract_value_available"]
        and flags["quantity_available"]
        and flags["part_number_available"]
        and flags["pricing_information_available"]
        and product.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM}
    ):
        status = COMPLETE_PURCHASE_PACKAGE
    elif (
        flags["contract_value_available"]
        and flags["part_number_available"]
        and product.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM}
    ):
        status = READY_FOR_PRICING
    elif flags["part_number_available"] and product.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM}:
        status = READY_FOR_SUPPLIER_RESEARCH
    elif docs_thin and not flags["contract_value_available"] and not flags["part_number_available"]:
        status = DOCUMENT_RECOVERY_REQUIRED
    elif any(flags.values()):
        status = PARTIAL
    else:
        status = INSUFFICIENT_DATA
    return {
        "kind": "PROCUREMENT_COMPLETENESS_SCORE",
        "PROCUREMENT_COMPLETENESS": status,
        "flags": flags,
        "missing": [k for k, v in flags.items() if not v],
        "documents_thin": docs_thin,
    }


def commercial_readiness_score(
    inventory: dict[str, Any],
    values: dict[str, Any],
    quantities: dict[str, Any],
    product: dict[str, Any],
    completeness: dict[str, Any],
    discovery: dict[str, Any],
) -> dict[str, Any]:
    score = 0
    reasons: list[str] = []
    docs = int(inventory.get("documents_with_bytes") or 0)
    if docs >= 2:
        score += 25
        reasons.append("multiple_documents_recovered")
    elif docs == 1:
        score += 15
        reasons.append("one_document_recovered")
    elif discovery.get("status") == ATT_FOUND:
        score += 10
        reasons.append("attachments_found")
    else:
        reasons.append("documents_missing")

    conf = product.get("Identity_confidence")
    if conf == MATCH_HIGH:
        score += 30
        reasons.append("exact_product_identity")
    elif conf == MATCH_MEDIUM:
        score += 20
        reasons.append("manufacturer_plus_model_or_family")
    elif conf == MATCH_LOW:
        score += 10
        reasons.append("manufacturer_or_category_only")
    else:
        reasons.append("product_identity_unknown")

    if values.get("confidence") == VAL_VERIFIED:
        score += 20
        reasons.append("verified_contract_value")
    elif values.get("confidence") == VAL_ESTIMATED:
        score += 12
        reasons.append("estimated_contract_value")
    else:
        reasons.append("contract_value_missing")

    if quantities.get("Confidence") == VAL_VERIFIED:
        score += 15
        reasons.append("verified_quantity")
    elif quantities.get("Confidence") == VAL_ESTIMATED:
        score += 8
        reasons.append("estimated_quantity")
    else:
        reasons.append("quantity_missing")

    st = completeness.get("PROCUREMENT_COMPLETENESS")
    if st in {COMPLETE_PURCHASE_PACKAGE, READY_FOR_PRICING}:
        score += 10
        reasons.append("ready_for_pricing_or_complete")
    elif st == READY_FOR_SUPPLIER_RESEARCH:
        score += 6
        reasons.append("ready_for_supplier_research")
    elif st == PARTIAL:
        score += 3

    score = min(100, score)
    return {
        "kind": "COMMERCIAL_READINESS_SCORE",
        "score": score,
        "explanation": reasons,
        "summary": f"Score: {score}. " + "; ".join(reasons[:6]),
    }


def assign_recovery_queue(
    completeness: dict[str, Any],
    values: dict[str, Any],
    quantities: dict[str, Any],
    product: dict[str, Any],
    health: dict[str, Any],
) -> str:
    st = completeness.get("PROCUREMENT_COMPLETENESS")
    if st == DOCUMENT_RECOVERY_REQUIRED or (
        health.get("status")
        in {HEALTH_NO_ATTACHMENT, HEALTH_AUTH_REQUIRED, HEALTH_REGISTRATION_REQUIRED}
        and values.get("confidence") == VAL_UNKNOWN
        and not _known(product.get("Part_number"))
    ):
        return Q_NEEDS_DOCUMENT_RECOVERY
    if product.get("Identity_confidence") in {MATCH_UNKNOWN, MATCH_LOW} or not (
        _known(product.get("Part_number")) or _known(product.get("NSN"))
    ):
        if st in {DOCUMENT_RECOVERY_REQUIRED, INSUFFICIENT_DATA}:
            return Q_NEEDS_DOCUMENT_RECOVERY
        return Q_NEEDS_PRODUCT_IDENTITY
    if values.get("confidence") == VAL_UNKNOWN:
        return Q_NEEDS_VALUE
    if quantities.get("Confidence") == VAL_UNKNOWN:
        return Q_NEEDS_QUANTITY
    if "configuration_available" in (completeness.get("missing") or []):
        return Q_NEEDS_CONFIGURATION
    if st in {READY_FOR_PRICING, READY_FOR_SUPPLIER_RESEARCH, COMPLETE_PURCHASE_PACKAGE}:
        if not completeness.get("flags", {}).get("pricing_information_available"):
            return Q_NEEDS_PRICING
    return Q_NEEDS_PRICING if st == READY_FOR_PRICING else Q_NEEDS_DOCUMENT_RECOVERY


def next_recovery_action(
    queue: str,
    discovery: dict[str, Any],
    health: dict[str, Any],
    inventory: dict[str, Any],
) -> str:
    if health.get("skip_further_automated_retry"):
        return (
            "Escalate to VA: source blocked/auth — manual credential or alternate route "
            "(no automated retry)"
        )
    if queue == Q_NEEDS_DOCUMENT_RECOVERY:
        targets = ", ".join(
            inventory.get("priority_gaps")
            or discovery.get("next_priority_targets")
            or ["pricing schedule / BOM"]
        )
        return f"Recover priority documents: {targets}"
    if queue == Q_NEEDS_PRODUCT_IDENTITY:
        return "Extract exact manufacturer part / model / NSN from recovered attachments"
    if queue == Q_NEEDS_VALUE:
        return "Recover pricing schedule or award amount from official documents"
    if queue == Q_NEEDS_QUANTITY:
        return "Extract quantity and UOM from line items / pricing schedule"
    if queue == Q_NEEDS_CONFIGURATION:
        return "Extract required accessories/options from technical exhibits — do not assume"
    if queue == Q_NEEDS_PRICING:
        return "Run exact-configuration market pricing research (no generic category quotes)"
    return "Continue evidence recovery"


def build_queue_card(row: dict[str, Any], pkg: dict[str, Any]) -> dict[str, Any]:
    readiness = pkg.get("COMMERCIAL_READINESS") or {}
    completeness = pkg.get("PROCUREMENT_COMPLETENESS") or {}
    health = pkg.get("SOURCE_HEALTH") or {}
    discovery = pkg.get("ATTACHMENT_DISCOVERY") or {}
    inventory = pkg.get("DOCUMENT_EVIDENCE") or {}
    return {
        "Opportunity": row.get("title"),
        "canonical_id": row.get("canonical_id"),
        "Missing_information": completeness.get("missing") or [],
        "Last_recovery_attempt": health.get("last_recovery_attempt") or "UNKNOWN",
        "Possible_source": row.get("detail_url") or row.get("source_url") or row.get("source_id") or "UNKNOWN",
        "Recommended_next_action": pkg.get("Next_Action"),
        "Queue": (pkg.get("RECOVERY_QUEUE") or {}).get("queue"),
        "Commercial_readiness": readiness.get("score"),
        "Attachment_status": discovery.get("status"),
        "Source_health": health.get("status"),
        "Documents_found": inventory.get("document_count"),
        "VA_allowed_actions": sorted(VA_ALLOWED_ACTIONS),
        "VA_forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
    }


def economics_handoff_from_evidence(
    row: dict[str, Any],
    values: dict[str, Any],
    quantities: dict[str, Any],
    product: dict[str, Any],
) -> dict[str, Any]:
    enriched = deepcopy(row)
    patched_fields: list[str] = []

    if values.get("confidence") != VAL_UNKNOWN and values.get("primary_value") != "UNKNOWN":
        if values.get("confidence") == VAL_VERIFIED:
            if not _num(enriched.get("award_amount")):
                enriched["award_amount"] = values["primary_value"]
                patched_fields.append("award_amount")
        else:
            if not _num(enriched.get("estimated_value")):
                enriched["estimated_value"] = values["primary_value"]
                patched_fields.append("estimated_value")

    if quantities.get("Confidence") != VAL_UNKNOWN and quantities.get("Quantity") != "UNKNOWN":
        lines = list(enriched.get("line_items") or [])
        if not lines:
            enriched["line_items"] = [
                {
                    "description": product.get("Manufacturer") or enriched.get("title") or "UNKNOWN",
                    "part_number": product.get("Part_number") if _known(product.get("Part_number")) else None,
                    "manufacturer": product.get("Manufacturer") if _known(product.get("Manufacturer")) else None,
                    "quantity": quantities["Quantity"],
                    "unit": quantities.get("Unit_of_measure") or "EA",
                    "source": "document_evidence_recovery",
                }
            ]
            patched_fields.append("line_items_quantity")
        elif isinstance(lines[0], dict) and _num(lines[0].get("quantity")) is None:
            lines[0] = {**lines[0], "quantity": quantities["Quantity"]}
            enriched["line_items"] = lines
            patched_fields.append("line_item_quantity_patch")

    can_handoff = values.get("confidence") in {VAL_VERIFIED, VAL_ESTIMATED} and product.get(
        "Identity_confidence"
    ) in {MATCH_HIGH, MATCH_MEDIUM}

    economics = None
    if can_handoff:
        try:
            package = build_procurement_package(enriched, allow_paid_web=False)
            economics = package.get("ECONOMICS_HANDOFF")
        except Exception as exc:
            economics = {"error": str(exc), "can_calculate": False}

    missing = []
    if values.get("confidence") == VAL_UNKNOWN:
        missing.append("contract value")
    if quantities.get("Confidence") == VAL_UNKNOWN:
        missing.append("quantity")
    if not (_known(product.get("Part_number")) or _known(product.get("NSN"))):
        missing.append("exact part number")
    if not can_handoff or not (economics or {}).get("can_calculate"):
        if "acquisition price" not in missing:
            missing.append("acquisition price")

    return {
        "patched_fields": patched_fields,
        "enriched_row_fields": {
            k: enriched.get(k)
            for k in ("estimated_value", "award_amount", "line_items")
            if k in patched_fields or enriched.get(k) != row.get(k)
        },
        "can_handoff": can_handoff,
        "moved_into_economics": bool((economics or {}).get("can_calculate")),
        "economics": economics,
        "missing": missing if not (economics or {}).get("can_calculate") else [],
        "message": (economics or {}).get("message")
        or (
            "Cannot calculate:\nMissing:\n- " + "\n- ".join(missing)
            if missing
            else "Evidence insufficient for economics handoff"
        ),
    }


def build_document_evidence_intelligence(
    row: dict[str, Any],
    *,
    run_recovery: bool = False,
    allow_paid: bool = False,
) -> dict[str, Any]:
    working = deepcopy(row)
    recovery_meta: dict[str, Any] = {"executed": False, "paid": 0, "reason": None, "improved": False}

    inv0 = build_document_evidence_profile(working)
    disc0 = attachment_discovery_status(working, inv0)
    health_preview = source_health(working, disc0)

    if run_recovery and not health_preview.get("skip_further_automated_retry"):
        try:
            from m3_evidence_acquisition import acquire_evidence

            kwargs: dict[str, Any] = {"allow_paid": bool(allow_paid)}
            if not allow_paid:
                kwargs["max_tier"] = TIER_3_WEB
            result = acquire_evidence(working, **kwargs)
            working = result.get("row") or working
            recovery_meta = {
                "executed": True,
                "paid": int(result.get("paid_actions") or 0),
                "reason": None,
                "improved": bool((result.get("result") or {}).get("improved")),
                "primary_failure": (result.get("failure") or {}).get("primary_reason"),
            }
        except Exception as exc:
            recovery_meta = {"executed": True, "paid": 0, "reason": str(exc), "improved": False}
    elif run_recovery and health_preview.get("skip_further_automated_retry"):
        recovery_meta = {
            "executed": False,
            "paid": 0,
            "reason": "skip_permanently_blocked_or_auth",
            "improved": False,
        }

    inventory = build_document_evidence_profile(working)
    discovery = attachment_discovery_status(working, inventory)
    health = source_health(working, discovery)
    values = extract_value_evidence(working)
    quantities = extract_quantity_evidence(working)
    product = extract_product_evidence(working)
    try:
        package = build_procurement_package(working, allow_paid_web=False)
    except Exception:
        package = {}

    completeness = procurement_completeness_score(inventory, values, quantities, product, package)
    readiness = commercial_readiness_score(inventory, values, quantities, product, completeness, discovery)
    queue = assign_recovery_queue(completeness, values, quantities, product, health)
    action = next_recovery_action(queue, discovery, health, inventory)
    handoff = economics_handoff_from_evidence(working, values, quantities, product)

    for field, val in (handoff.get("enriched_row_fields") or {}).items():
        if field == "line_items" and val:
            working["line_items"] = val
        elif field in {"estimated_value", "award_amount"} and val is not None:
            working[field] = val

    return {
        "kind": "M3DocumentEvidenceIntelligence",
        "generated_at": _utc(),
        "DOCUMENT_EVIDENCE": inventory,
        "ATTACHMENT_DISCOVERY": discovery,
        "SOURCE_HEALTH": health,
        "VALUE_EVIDENCE": values,
        "QUANTITY_EVIDENCE": quantities,
        "PRODUCT_EVIDENCE": product,
        "PROCUREMENT_COMPLETENESS": completeness,
        "COMMERCIAL_READINESS": readiness,
        "RECOVERY_QUEUE": {"queue": queue},
        "ECONOMICS_HANDOFF": handoff,
        "RECOVERY_RUN": recovery_meta,
        "Next_Action": action,
        "working_row_patch": {
            "estimated_value": working.get("estimated_value"),
            "award_amount": working.get("award_amount"),
            "line_items": working.get("line_items"),
            "documents": working.get("documents"),
            "evidence_acquisition": working.get("evidence_acquisition"),
            "evidence_failure": working.get("evidence_failure"),
            "evidence_recovery_attempts": working.get("evidence_recovery_attempts"),
            "source_access_state": working.get("source_access_state"),
            "attachment_text": working.get("attachment_text"),
            "governing_text": working.get("governing_text"),
            "solicitation_text": working.get("solicitation_text"),
        },
        "VA": {
            "allowed_actions": sorted(VA_ALLOWED_ACTIONS),
            "forbidden_actions": sorted(VA_FORBIDDEN_ACTIONS),
            "role": "EVIDENCE_RECOVERY_OPERATOR",
            "may_approve_deals": False,
            "may_contact_suppliers": False,
            "may_submit_bids": False,
            "may_change_scoring": False,
        },
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def load_evidence_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == EVIDENCE_INDEX_KEY).one_or_none()
            if not row or not row.value:
                return {}
            data = json.loads(row.value)
            return data if isinstance(data, dict) else {}
        finally:
            db.close()
    except Exception:
        log.exception("load evidence index failed")
        return {}


def save_evidence_index(by_id: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = {
            "kind": "M3DocumentEvidenceIndex",
            "updated_at": _utc(),
            "by_id": by_id,
            "count": len(by_id),
        }
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == EVIDENCE_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=EVIDENCE_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        log.exception("save evidence index failed")
        return False


def get_persisted_evidence(canonical_id: str) -> dict[str, Any] | None:
    data = load_evidence_index()
    by_id = data.get("by_id") if isinstance(data.get("by_id"), dict) else {}
    v = by_id.get(canonical_id)
    return v if isinstance(v, dict) else None


def _priority_seed(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    scored = []
    for r in rows:
        if not r.get("canonical_id"):
            continue
        ci = r.get("commercial_intelligence") or {}
        commercial = (
            70
            if (ci.get("COMMERCIAL_OPPORTUNITY_SCORE") or r.get("commercial_opportunity_score")) == SCORE_HIGH
            else (
                50
                if (ci.get("COMMERCIAL_OPPORTUNITY_SCORE") or r.get("commercial_opportunity_score"))
                == SCORE_MEDIUM
                else 20
            )
        )
        docs = r.get("documents") if isinstance(r.get("documents"), list) else []
        thin = 30 if not any(isinstance(d, dict) and d.get("bytes_recovered") for d in docs) else 5
        has_value = 10 if _num(r.get("estimated_value") or r.get("award_amount")) else 20
        scored.append((commercial + thin + has_value, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def analyze_document_evidence_top(
    store: Any,
    *,
    limit: int = 25,
    run_recovery: bool = True,
    allow_paid: bool = False,
    paid_limit: int = 2,
    recovery_limit: int = 10,
) -> dict[str, Any]:
    rows = store.all() if hasattr(store, "all") else list(store)
    targets = _priority_seed(rows, limit)

    results = []
    index_updates: dict[str, Any] = {}
    queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
    completeness_counts: Counter[str] = Counter()
    health_counts: Counter[str] = Counter()
    readiness_scores: list[int] = []
    docs_recovered = 0
    attachments_recovered = 0
    pricing_docs = 0
    award_docs = 0
    complete_pkgs = 0
    ready_pricing = 0
    partial = 0
    blocked = 0
    econ_moved = 0
    paid_total = 0
    recovery_used = 0
    improvements: list[dict[str, Any]] = []

    for row in targets:
        before_docs = sum(
            1 for d in (row.get("documents") or []) if isinstance(d, dict) and d.get("bytes_recovered")
        )
        try:
            before_pkg = build_document_evidence_intelligence(row, run_recovery=False, allow_paid=False)
            before_ready = int((before_pkg.get("COMMERCIAL_READINESS") or {}).get("score") or 0)
        except Exception:
            before_ready = 0

        do_recovery = run_recovery and recovery_used < recovery_limit
        use_paid = allow_paid and paid_total < paid_limit and do_recovery
        pkg = build_document_evidence_intelligence(row, run_recovery=do_recovery, allow_paid=use_paid)
        if do_recovery:
            recovery_used += 1
        paid_total += int((pkg.get("RECOVERY_RUN") or {}).get("paid") or 0)

        patch = pkg.get("working_row_patch") or {}
        full = {**(store.get(row["canonical_id"]) or row)}
        for k, v in patch.items():
            if v is not None:
                full[k] = v
        full["document_evidence_intelligence"] = pkg
        store._rows[row["canonical_id"]] = full
        index_updates[row["canonical_id"]] = pkg

        inv = pkg.get("DOCUMENT_EVIDENCE") or {}
        docs_recovered += int(inv.get("documents_with_bytes") or 0)
        for d in inv.get("Available_documents") or []:
            if d.get("Document_type") == "pricing_schedule":
                pricing_docs += 1
            if d.get("Document_type") in {"award_notice", "historical_award_document"}:
                award_docs += 1
            if d.get("bytes_recovered") and d.get("Document_type") in {
                "attachment",
                "pricing_schedule",
                "spreadsheet",
                "bom",
                "technical_specification",
                "solicitation",
            }:
                attachments_recovered += 1

        st = (pkg.get("PROCUREMENT_COMPLETENESS") or {}).get("PROCUREMENT_COMPLETENESS") or INSUFFICIENT_DATA
        completeness_counts[st] += 1
        if st == COMPLETE_PURCHASE_PACKAGE:
            complete_pkgs += 1
        elif st == READY_FOR_PRICING:
            ready_pricing += 1
        elif st == PARTIAL:
            partial += 1
        elif st == DOCUMENT_RECOVERY_REQUIRED:
            blocked += 1

        health = (pkg.get("SOURCE_HEALTH") or {}).get("status") or HEALTH_UNKNOWN
        health_counts[health] += 1
        score = int((pkg.get("COMMERCIAL_READINESS") or {}).get("score") or 0)
        readiness_scores.append(score)
        delta = score - before_ready
        if delta > 0 or (pkg.get("RECOVERY_RUN") or {}).get("improved"):
            improvements.append(
                {
                    "canonical_id": row.get("canonical_id"),
                    "Opportunity": row.get("title"),
                    "before": before_ready,
                    "after": score,
                    "delta": delta,
                    "docs_before": before_docs,
                    "docs_after": inv.get("documents_with_bytes"),
                }
            )

        if (pkg.get("ECONOMICS_HANDOFF") or {}).get("moved_into_economics"):
            econ_moved += 1

        card = build_queue_card(full, pkg)
        queues[card["Queue"] or Q_NEEDS_DOCUMENT_RECOVERY].append(card)

        product = pkg.get("PRODUCT_EVIDENCE") or {}
        values = pkg.get("VALUE_EVIDENCE") or {}
        qty = pkg.get("QUANTITY_EVIDENCE") or {}
        results.append(
            {
                "canonical_id": row.get("canonical_id"),
                "Opportunity": row.get("title"),
                "Documents_recovered": inv.get("documents_with_bytes"),
                "Documents_found": inv.get("document_count"),
                "Attachment_status": (pkg.get("ATTACHMENT_DISCOVERY") or {}).get("status"),
                "Product": product.get("Manufacturer") or product.get("Model") or "UNKNOWN",
                "Part_number": product.get("Part_number"),
                "Quantity": qty.get("Quantity"),
                "Value": values.get("primary_value"),
                "Value_confidence": values.get("confidence"),
                "Completeness": st,
                "Commercial_readiness": score,
                "Readiness_explanation": (pkg.get("COMMERCIAL_READINESS") or {}).get("summary"),
                "Source_health": health,
                "Queue": card["Queue"],
                "Missing": (pkg.get("PROCUREMENT_COMPLETENESS") or {}).get("missing"),
                "Next_action": pkg.get("Next_Action"),
                "Economics_moved": (pkg.get("ECONOMICS_HANDOFF") or {}).get("moved_into_economics"),
                "delta_readiness": delta,
            }
        )

    results.sort(key=lambda r: (-int(r.get("Commercial_readiness") or 0), -int(r.get("Documents_recovered") or 0)))
    improvements.sort(key=lambda x: -int(x.get("delta") or 0))

    try:
        existing = load_evidence_index()
        by = existing.get("by_id") if isinstance(existing.get("by_id"), dict) else {}
        by.update(index_updates)
        save_evidence_index(by)
    except Exception:
        log.exception("evidence index save failed")
    try:
        store.save()
    except Exception:
        log.exception("pipeline save failed")

    missing_inputs: Counter[str] = Counter()
    for r in results:
        for m in r.get("Missing") or []:
            missing_inputs[str(m)] += 1

    avg_ready = round(sum(readiness_scores) / max(1, len(readiness_scores)), 1)
    return {
        "kind": "M3DocumentEvidenceRun",
        "generated_at": _utc(),
        "analyzed": len(results),
        "documents_recovered": docs_recovered,
        "attachments_recovered": attachments_recovered,
        "pricing_documents": pricing_docs,
        "award_documents": award_docs,
        "evidence_completeness": {
            "complete_packages": complete_pkgs,
            "ready_for_pricing": ready_pricing,
            "partial": partial,
            "blocked": blocked,
            "breakdown": dict(completeness_counts),
        },
        "commercial_readiness": {
            "average_score": avg_ready,
            "top_opportunities_improved": improvements[:10],
            "biggest_improvements": improvements[:5],
        },
        "recovery_failures": {
            "access_blocked": health_counts.get(HEALTH_ACCESS_BLOCKED, 0),
            "auth_required": health_counts.get(HEALTH_AUTH_REQUIRED, 0)
            + health_counts.get(HEALTH_REGISTRATION_REQUIRED, 0),
            "missing_documents": health_counts.get(HEALTH_NO_ATTACHMENT, 0),
            "parser_issues": health_counts.get(HEALTH_PARSER_FAILURE, 0),
            "unavailable": health_counts.get(HEALTH_DOCUMENT_REMOVED, 0),
            "breakdown": dict(health_counts),
        },
        "economics_impact": {
            "moved_into_economics": econ_moved,
            "still_blocked": len(results) - econ_moved,
            "missing_inputs": dict(missing_inputs),
        },
        "queues": {k: v[:15] for k, v in queues.items()},
        "TOP_OPPORTUNITIES": results[:10],
        "ALL_SCORED": results,
        "paid": paid_total,
        "OpenAI": paid_total,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
        "VA_OPERATING_MODEL": {
            "allowed": sorted(VA_ALLOWED_ACTIONS),
            "forbidden": sorted(VA_FORBIDDEN_ACTIONS),
            "note": (
                "VA recovers/attaches evidence and escalates; cannot approve deals, bid, "
                "contact suppliers, or spend"
            ),
        },
    }


def apply_va_evidence_update(
    store: Any,
    canonical_id: str,
    *,
    action: str,
    note: str | None = None,
    status: str | None = None,
    attached_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    action_u = str(action or "").upper().strip()
    if action_u in VA_FORBIDDEN_ACTIONS or action_u not in VA_ALLOWED_ACTIONS:
        return {
            "ok": False,
            "error": "VA_ACTION_NOT_PERMITTED",
            "action": action_u,
            "allowed": sorted(VA_ALLOWED_ACTIONS),
            "forbidden": sorted(VA_FORBIDDEN_ACTIONS),
        }
    row = store.get(canonical_id) if hasattr(store, "get") else None
    if not row:
        return {"ok": False, "error": "OPPORTUNITY_NOT_FOUND"}

    full = dict(row)
    va_log = list(full.get("va_evidence_log") or [])
    entry: dict[str, Any] = {"at": _utc(), "action": action_u, "note": note or None, "status": status or None}
    if action_u == "ATTACH_EVIDENCE" and isinstance(attached_document, dict):
        docs = list(full.get("documents") or [])
        doc = {
            "name": attached_document.get("name") or "va_upload",
            "url": attached_document.get("url") or "UNKNOWN",
            "source": "va_operator",
            "document_type": attached_document.get("document_type") or "attachment",
            "bytes_recovered": bool(attached_document.get("bytes_recovered") or attached_document.get("text")),
            "extracted_text": attached_document.get("text") or attached_document.get("extracted_text"),
            "retrieved_at": _utc(),
            "authority": AUTH_SECONDARY,
        }
        docs.append(doc)
        full["documents"] = docs
        entry["document"] = {"name": doc["name"], "type": doc["document_type"]}
    if action_u == "UPDATE_STATUS" and status:
        full["va_evidence_status"] = status
    if action_u == "ESCALATE":
        full["va_escalated"] = True
        full["va_escalated_at"] = _utc()
        full["va_escalation_note"] = note or "Escalated by VA"
    if action_u == "MARK_RECOVERY_ATTEMPTED":
        attempts = list(full.get("evidence_recovery_attempts") or [])
        attempts.append({"tier": "VA_MANUAL", "at": _utc(), "note": note or "manual_attempt"})
        full["evidence_recovery_attempts"] = attempts[-20:]
    if note and action_u == "NOTE":
        full["va_evidence_note"] = note

    va_log.append(entry)
    full["va_evidence_log"] = va_log[-50:]
    pkg = build_document_evidence_intelligence(full, run_recovery=False, allow_paid=False)
    full["document_evidence_intelligence"] = pkg
    store._rows[canonical_id] = full
    try:
        existing = load_evidence_index()
        by = existing.get("by_id") if isinstance(existing.get("by_id"), dict) else {}
        by[canonical_id] = pkg
        save_evidence_index(by)
        store.save()
    except Exception:
        log.exception("va update persist failed")

    return {
        "ok": True,
        "action": action_u,
        "canonical_id": canonical_id,
        "Commercial_readiness": (pkg.get("COMMERCIAL_READINESS") or {}).get("score"),
        "Completeness": (pkg.get("PROCUREMENT_COMPLETENESS") or {}).get("PROCUREMENT_COMPLETENESS"),
        "Next_Action": pkg.get("Next_Action"),
        "queue_card": build_queue_card(full, pkg),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def deal_room_evidence_recovery_section(row: dict[str, Any]) -> dict[str, Any]:
    pkg = row.get("document_evidence_intelligence")
    if not isinstance(pkg, dict) or pkg.get("kind") != "M3DocumentEvidenceIntelligence":
        persisted = get_persisted_evidence(str(row.get("canonical_id") or ""))
        if isinstance(persisted, dict) and persisted.get("kind") == "M3DocumentEvidenceIntelligence":
            pkg = persisted
        else:
            pkg = build_document_evidence_intelligence(row, run_recovery=False, allow_paid=False)

    inv = pkg.get("DOCUMENT_EVIDENCE") or {}
    values = pkg.get("VALUE_EVIDENCE") or {}
    qty = pkg.get("QUANTITY_EVIDENCE") or {}
    product = pkg.get("PRODUCT_EVIDENCE") or {}
    completeness = pkg.get("PROCUREMENT_COMPLETENESS") or {}
    readiness = pkg.get("COMMERCIAL_READINESS") or {}
    return {
        "kind": "M3DealRoomEvidenceRecovery",
        "Documents_found": inv.get("Available_documents") or [],
        "Documents_missing": inv.get("Missing_documents") or [],
        "Recovered_values": {
            "primary": values.get("primary_value"),
            "confidence": values.get("confidence"),
            "items": (values.get("items") or [])[:8],
        },
        "Recovered_quantities": {
            "quantity": qty.get("Quantity"),
            "uom": qty.get("Unit_of_measure"),
            "confidence": qty.get("Confidence"),
        },
        "Product_identifiers": {
            "Manufacturer": product.get("Manufacturer"),
            "Model": product.get("Model"),
            "Part_number": product.get("Part_number"),
            "NSN": product.get("NSN"),
            "Identity_confidence": product.get("Identity_confidence"),
        },
        "Completeness_score": completeness.get("PROCUREMENT_COMPLETENESS"),
        "Commercial_readiness": readiness.get("score"),
        "Readiness_explanation": readiness.get("summary"),
        "Missing_information": completeness.get("missing") or [],
        "Source_health": (pkg.get("SOURCE_HEALTH") or {}).get("status"),
        "Attachment_status": (pkg.get("ATTACHMENT_DISCOVERY") or {}).get("status"),
        "Queue": (pkg.get("RECOVERY_QUEUE") or {}).get("queue"),
        "Next_Action": pkg.get("Next_Action"),
        "VA": pkg.get("VA"),
        "full": pkg,
    }
