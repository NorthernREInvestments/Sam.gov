"""Transactional BOM / requirement extraction and product identification."""

from __future__ import annotations
from application_clock import now_utc

import re
from datetime import datetime, timezone
from typing import Any

from solicitation_package_constants import (
    COMPLETE_ENOUGH_FOR_PRODUCT_ID,
    CRITICAL_DOCUMENT_MISSING,
    LIKELY_COMPLETE,
    PARTIAL,
    PRODUCT_BRAND_OR_EQUAL,
    PRODUCT_CUSTOM,
    PRODUCT_EXACT,
    PRODUCT_INSUFFICIENT,
    PRODUCT_MULTIPLE,
    PRODUCT_PART_NUMBER,
    PRODUCT_SPEC_COMMODITY,
    AUTH_BLOCKED,
    COMPLETENESS_UNKNOWN,
    PRICE_TO_BE_PROVIDED,
)


def _utc() -> str:
    return now_utc().isoformat()


def _fact(
    value: Any,
    *,
    confidence: str = "UNKNOWN",
    source_document: str | None = None,
    source_location: str | None = None,
    extraction_method: str | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "confidence": confidence if value is not None and value != "" else "UNKNOWN",
        "source_document": source_document,
        "source_location": source_location,
        "extraction_method": extraction_method,
    }


def empty_line_item(**overrides: Any) -> dict[str, Any]:
    base = {
        "line_number": None,
        "CLIN_or_item_number": None,
        "description": None,
        "manufacturer": None,
        "brand": None,
        "model": None,
        "part_number": None,
        "NSN": None,
        "SKU": None,
        "quantity": None,
        "unit_of_measure": None,
        "pack_size": None,
        "dimensions": None,
        "material": None,
        "performance_specifications": None,
        "compatibility_requirements": None,
        "required_certifications": None,
        "brand_name_only": None,
        "brand_name_or_equal": None,
        "approved_equal_language": None,
        "country_of_origin_requirement": None,
        "warranty_requirement": None,
        "accessories": None,
        "consumables": None,
        "installation_required": None,
        "assembly_required": None,
        "training_required": None,
        "maintenance_required": None,
        "delivery_location": None,
        "delivery_deadline": None,
        "FOB_terms": None,
        "shipping_terms": None,
        "inspection_acceptance": None,
        "special_packaging": None,
        "notes": None,
        "unit_price": {"value": None, "status": PRICE_TO_BE_PROVIDED},
        "field_provenance": {},
    }
    base.update(overrides)
    return base


def extract_sciquest_product_line_items(
    text: str,
    *,
    source_document: str | None = None,
) -> list[dict[str, Any]]:
    """
    Extract SciQuest/Jaggaer 'Product Line Items' blocks.
    Pattern observed: P1 / item code - description / qty / UOM
    """
    t = text or ""
    # Isolate product line items section when present
    section = t
    m = re.search(r"Product Line Items\s*(.*?)(?:Service Line Items|\Z)", t, re.I | re.S)
    if m:
        section = m.group(1)

    items: list[dict[str, Any]] = []
    # Primary pattern: Px / code - desc (possibly multi-line) / qty / UOM
    # Descriptions may wrap, e.g. "Tungsten-Carbide BLADE 3 FT. SECTION\n(rubber encased)"
    pattern = re.compile(
        r"\b(P\d+)\s*\n\s*"
        r"(.+?)\s*\n\s*"
        r"(\d+(?:\.\d+)?)\s*\n\s*"
        r"((?:EA|LB|LS|YR|LOT|SET|HR|CS|BX|GAL|FT|IN|KIT|PR|PK|TO|MO|WK|DY|"
        r"Each|Pound|Pounds|Hours?|Lump\s*Sum|Year|Years?|Gallon|Feet|Pair|Pack)\s*"
        r"(?:-\s*[A-Za-z ]+)?)\b",
        re.I | re.S,
    )
    for match in pattern.finditer(section):
        line_id = match.group(1).upper()
        raw_desc = re.sub(r"\s+", " ", match.group(2)).strip()
        # Guard against swallowing subsequent P-lines
        if re.search(r"\bP\d+\b", raw_desc):
            raw_desc = re.split(r"\bP\d+\b", raw_desc)[0].strip()
        qty = float(match.group(3))
        uom_raw = re.sub(r"\s+", " ", match.group(4)).strip()
        uom = uom_raw.split("-")[0].strip().upper() if "-" in uom_raw else uom_raw.upper()
        if uom in {"EACH"}:
            uom = "EA"
        if uom in {"POUND", "POUNDS"}:
            uom = "LB"
        if uom in {"LUMP SUM", "LUMPSUM"}:
            uom = "LS"
        if uom in {"YEAR", "YEARS"}:
            uom = "YR"

        item_number = None
        description = raw_desc
        # "002367300 - Tungsten-Carbide BLADE..." or "324.3 Pounds- Big bluestem"
        code_m = re.match(r"^(\d{5,})\s*[-–]\s*(.+)$", raw_desc)
        if code_m:
            item_number = code_m.group(1)
            description = code_m.group(2).strip()
        else:
            # Seed-style: quantity embedded in title "324.3 Pounds- Big bluestem..."
            seed_m = re.match(
                r"^([\d,.]+)\s*-?\s*(?:Pounds?|Lbs?)\s*[-–]\s*(.+)$",
                raw_desc,
                re.I,
            )
            if seed_m:
                # Desired quantity is in the title; bid UOM is per-pound
                try:
                    desired = float(seed_m.group(1).replace(",", ""))
                except ValueError:
                    desired = None
                description = seed_m.group(2).strip()
                # Keep line qty as desired pounds when present; note bid is unit-priced
                if desired is not None:
                    qty = desired
                    uom = "LB"

        # Manufacturer/model blanks in PDF mean UNKNOWN, not empty string facts
        li = empty_line_item(
            line_number=line_id,
            CLIN_or_item_number=item_number,
            description=description,
            quantity=qty,
            unit_of_measure=uom,
            unit_price={"value": None, "status": PRICE_TO_BE_PROVIDED},
            notes="bidder_price_fields_blank",
        )
        li["field_provenance"] = {
            "description": _fact(description, confidence="VERIFIED_DOCUMENT", source_document=source_document, source_location="Product Line Items", extraction_method="sciquest_pdf_regex"),
            "quantity": _fact(qty, confidence="VERIFIED_DOCUMENT", source_document=source_document, source_location="Product Line Items", extraction_method="sciquest_pdf_regex"),
            "unit_of_measure": _fact(uom, confidence="VERIFIED_DOCUMENT", source_document=source_document, source_location="Product Line Items", extraction_method="sciquest_pdf_regex"),
            "CLIN_or_item_number": _fact(item_number, confidence="VERIFIED_DOCUMENT" if item_number else "UNKNOWN", source_document=source_document, extraction_method="sciquest_pdf_regex"),
        }
        items.append(li)

    # Service line items (S1...) — capture but flag as service
    svc_section = ""
    sm = re.search(r"Service Line Items\s*(.*)\Z", t, re.I | re.S)
    if sm:
        svc_section = sm.group(1)
    svc_pat = re.compile(
        r"\b(S\d+)\s*\n\s*"
        r"([^\n]+?)\s*\n\s*"
        r"(\d+(?:\.\d+)?)\s*\n\s*"
        r"([A-Z]{1,4}\s*-\s*[A-Za-z ]+|EA|Each|HR|Hours?)\b",
        re.I,
    )
    for match in svc_pat.finditer(svc_section):
        if "There are no Items" in match.group(0):
            continue
        desc = re.sub(r"\s+", " ", match.group(2)).strip()
        if desc.lower().startswith("there are no"):
            continue
        li = empty_line_item(
            line_number=match.group(1).upper(),
            description=desc,
            quantity=float(match.group(3)),
            unit_of_measure=re.sub(r"\s+", " ", match.group(4)).split("-")[0].strip().upper(),
            notes="SERVICE_LINE_ITEM",
            maintenance_required=True if "repair" in desc.lower() else None,
        )
        li["is_service_line"] = True
        items.append(li)

    return items


def extract_delivery_and_terms(text: str, *, source_document: str | None = None) -> dict[str, Any]:
    t = text or ""
    out: dict[str, Any] = {}

    fob = None
    m = re.search(r"F\.?O\.?B\.?\s+Destination[^\n.]{0,80}", t, re.I)
    if m:
        fob = re.sub(r"\s+", " ", m.group(0)).strip()
    out["FOB_terms"] = _fact(fob, confidence="VERIFIED_DOCUMENT" if fob else "UNKNOWN", source_document=source_document, extraction_method="regex")

    loc = None
    m = re.search(
        r"(?:Deliver(?:y|ies)\s+(?:Address|Location|to)|Ship\s+To|Delivery\s+Location)\s*[:\n]\s*"
        r"([^\n]{8,120})",
        t,
        re.I,
    )
    if not m:
        m = re.search(
            r"(?:Address|Deliver(?:y|ies)\s+(?:to|shall be)[^:\n]{0,40}:?\s*)"
            r"(\d+[^\n]{5,80}(?:Ames|IA|MT|Montana)[^\n]{0,40})",
            t,
            re.I,
        )
    if not m:
        # Street address only when clearly an address (avoid commodity-code false positives)
        m = re.search(
            r"(\d{1,5}\s+[A-Za-z0-9 .]+(?:Street|St\.|Avenue|Ave\.|Road|Rd\.|Drive|Dr\.|Boulevard|Blvd\.)"
            r"[^\n]{0,60}(?:,\s*[A-Z]{2}\s+\d{5})?)",
            t,
            re.I,
        )
    if m:
        loc = re.sub(r"\s+", " ", m.group(1)).strip()
        # Reject false positives that look like commodity catalog lines
        if re.search(r"Recycled Road and Highway|Commodity Code|Equipment and Supplies \(Not", loc, re.I):
            loc = None
    # Explicit TBD delivery
    if not loc and re.search(
        r"Delivery information will be provided prior|delivery location.{0,40}to be (?:provided|determined)",
        t,
        re.I,
    ):
        out["delivery_location"] = _fact(
            "TBD_BY_BUYER_PRIOR_TO_DELIVERY",
            confidence="VERIFIED_DOCUMENT",
            source_document=source_document,
            source_location="delivery_information_clause",
            extraction_method="regex",
        )
    else:
        out["delivery_location"] = _fact(
            loc, confidence="VERIFIED_DOCUMENT" if loc else "UNKNOWN", source_document=source_document
        )

    deadline = None
    m = re.search(r"Close\s*\n\s*([0-9/]+,\s*[0-9:]+\s*[AP]M\s*[A-Z]{2,4})", t)
    if not m:
        m = re.search(r"(?:Close|Due|Sealed Until)\s*[:\n]\s*([0-9/]+[^\n]{0,40})", t, re.I)
    if m:
        deadline = re.sub(r"\s+", " ", m.group(1)).strip()
    out["bid_deadline"] = _fact(deadline, confidence="VERIFIED_DOCUMENT" if deadline else "UNKNOWN", source_document=source_document)

    brand_equal = bool(re.search(r"comparable brands|brand name or equal|or equal|not intended to be restrictive", t, re.I))
    brand_only = bool(re.search(r"brand name only|no substitutes", t, re.I))
    out["brand_name_or_equal"] = _fact(brand_equal or None, confidence="VERIFIED_DOCUMENT" if brand_equal else "UNKNOWN", source_document=source_document)
    out["brand_name_only"] = _fact(True if brand_only else (False if brand_equal else None), confidence="VERIFIED_DOCUMENT" if (brand_only or brand_equal) else "UNKNOWN", source_document=source_document)
    if brand_equal:
        m = re.search(r"Approved Brands.{0,400}", t, re.I | re.S)
        out["approved_equal_language"] = _fact(
            re.sub(r"\s+", " ", m.group(0)).strip()[:400] if m else "approved_brands_or_comparable",
            confidence="VERIFIED_DOCUMENT",
            source_document=source_document,
        )

    mill = bool(re.search(r"mill certification", t, re.I))
    out["required_certifications"] = _fact(
        ["mill_certification"] if mill else None,
        confidence="VERIFIED_DOCUMENT" if mill else "UNKNOWN",
        source_document=source_document,
    )

    samples = bool(re.search(r"\bsamples?\b", t, re.I))
    out["samples_may_be_required"] = _fact(samples or None, confidence="VERIFIED_DOCUMENT" if samples else "UNKNOWN", source_document=source_document)

    # Buyer attachments listed by name
    attachments: list[str] = []
    am = re.search(r"Buyer Attachments\s*(.*?)(?:Questions|Product Line Items|Required to View|\Z)", t, re.I | re.S)
    if am:
        for lm in re.finditer(r"\d+\.\s*\n?\s*([^\n]+\.(?:pdf|docx?|xlsx?|zip))", am.group(1), re.I):
            attachments.append(re.sub(r"\s+", " ", lm.group(1)).strip())
    out["listed_buyer_attachments"] = _fact(attachments or None, confidence="VERIFIED_DOCUMENT" if attachments else "UNKNOWN", source_document=source_document)

    payment = None
    m = re.search(r"Payment\s*Terms\s*\n\s*([^\n]+)", t, re.I)
    if m:
        payment = re.sub(r"\s+", " ", m.group(1)).strip()
    out["payment_terms"] = _fact(payment, confidence="VERIFIED_DOCUMENT" if payment else "UNKNOWN", source_document=source_document)

    tied = bool(re.search(r"All bid lines are tied", t, re.I))
    out["bid_lines_tied"] = _fact(tied or None, confidence="VERIFIED_DOCUMENT" if tied else "UNKNOWN", source_document=source_document)

    return out


def identify_product(
    *,
    line_items: list[dict[str, Any]],
    terms: dict[str, Any] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Determine product identification state from extracted evidence only."""
    terms = terms or {}
    product_items = [li for li in line_items if not li.get("is_service_line")]
    service_items = [li for li in line_items if li.get("is_service_line")]

    if not product_items:
        return {
            "product_id_state": PRODUCT_INSUFFICIENT,
            "manufacturer": None,
            "model": None,
            "part_number": None,
            "equivalents": [],
            "sourcing_description": None,
            "service_lines_present": bool(service_items),
            "rationale": "no_product_line_items",
        }

    has_mfr = any(li.get("manufacturer") for li in product_items)
    has_model = any(li.get("model") for li in product_items)
    has_part = any(li.get("part_number") or li.get("CLIN_or_item_number") for li in product_items)
    brand_equal = (terms.get("brand_name_or_equal") or {}).get("value")
    brand_only = (terms.get("brand_name_only") or {}).get("value")

    descriptions = [li.get("description") for li in product_items if li.get("description")]
    qty_known = all(li.get("quantity") is not None for li in product_items)

    # Agency stock numbers (Iowa 002367300 style) are identifiers but not commercial part numbers
    agency_codes = [li.get("CLIN_or_item_number") for li in product_items if li.get("CLIN_or_item_number")]

    if has_mfr and has_model and brand_only:
        state = PRODUCT_EXACT
    elif has_part and has_mfr:
        state = PRODUCT_PART_NUMBER
    elif brand_equal and descriptions and qty_known:
        state = PRODUCT_BRAND_OR_EQUAL
    elif descriptions and qty_known and not has_mfr:
        # Spec/commodity described with quantities
        if len(descriptions) > 1:
            state = PRODUCT_MULTIPLE if brand_equal else PRODUCT_SPEC_COMMODITY
        else:
            state = PRODUCT_SPEC_COMMODITY
    elif descriptions:
        state = PRODUCT_SPEC_COMMODITY if qty_known else PRODUCT_INSUFFICIENT
    else:
        state = PRODUCT_INSUFFICIENT

    # Sourcing description from evidence
    parts: list[str] = []
    for li in product_items[:8]:
        bit = li.get("description") or ""
        if li.get("quantity") is not None and li.get("unit_of_measure"):
            bit = f"{bit} (qty {li['quantity']} {li['unit_of_measure']})"
        if bit:
            parts.append(bit)
    if title and not parts:
        parts.append(title)
    sourcing = "; ".join(parts) if parts else None

    return {
        "product_id_state": state,
        "manufacturer": next((li.get("manufacturer") for li in product_items if li.get("manufacturer")), None),
        "model": next((li.get("model") for li in product_items if li.get("model")), None),
        "part_number": next((li.get("part_number") for li in product_items if li.get("part_number")), None),
        "agency_item_codes": [c for c in agency_codes if c],
        "equivalents": [],
        "sourcing_description": sourcing,
        "service_lines_present": bool(service_items),
        "product_line_count": len(product_items),
        "service_line_count": len(service_items),
        "rationale": "extracted_from_solicitation_line_items",
    }


def assess_document_completeness(
    *,
    documents: list[dict[str, Any]],
    line_items: list[dict[str, Any]],
    terms: dict[str, Any] | None = None,
    product_id: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Transactional completeness assessment."""
    terms = terms or {}
    product_id = product_id or {}
    fetched = [d for d in documents if d.get("access_status") == "PUBLIC_FETCHED"]
    auth_blocked = [d for d in documents if d.get("access_status") in {"AUTH_REQUIRED", "LOGIN_REQUIRED"}]
    listed_missing = [d for d in documents if d.get("access_status") == "PUBLIC_LISTED_NOT_FETCHED"]

    knows = {
        "what_product": bool(product_id.get("sourcing_description"))
        and product_id.get("product_id_state") not in {None, PRODUCT_INSUFFICIENT},
        "quantity": bool(line_items)
        and all(
            li.get("quantity") is not None
            for li in line_items
            if not li.get("is_service_line")
        ),
        "specifications": bool((terms.get("listed_buyer_attachments") or {}).get("value"))
        or bool(product_id.get("sourcing_description")),
        "delivery_destination": bool((terms.get("delivery_location") or {}).get("value")),
        "delivery_deadline": False,  # often "see attached" / TBD in these PDFs
        "bid_deadline": bool((terms.get("bid_deadline") or {}).get("value")),
        "pricing_structure": bool(line_items),  # unit-price blank fields present
        "material_compliance": bool((terms.get("required_certifications") or {}).get("value")),
    }
    # Spec attachment listed but not fetched → specifications incomplete for quote
    spec_listed = (terms.get("listed_buyer_attachments") or {}).get("value") or []
    critical_spec_missing = bool(spec_listed) and any(
        d.get("document_class") == "SPECIFICATION"
        and d.get("access_status") in {"PUBLIC_LISTED_NOT_FETCHED", "AUTH_REQUIRED", "LOGIN_REQUIRED", "NOT_FOUND"}
        for d in documents
    ) or (bool(spec_listed) and not any(
        d.get("document_class") == "SPECIFICATION" and d.get("access_status") == "PUBLIC_FETCHED"
        for d in documents
    ))

    if auth_blocked and not fetched:
        status = AUTH_BLOCKED
    elif knows["what_product"] and knows["quantity"] and knows["bid_deadline"] and not critical_spec_missing:
        status = COMPLETE_ENOUGH_FOR_PRODUCT_ID
    elif knows["what_product"] and knows["quantity"]:
        # Spec PDF missing but line descriptions often enough for commodity ID
        if critical_spec_missing:
            status = PARTIAL if product_id.get("product_id_state") == PRODUCT_INSUFFICIENT else LIKELY_COMPLETE
        else:
            status = LIKELY_COMPLETE
    elif critical_spec_missing and not knows["what_product"]:
        status = CRITICAL_DOCUMENT_MISSING
    elif fetched:
        status = PARTIAL
    else:
        status = COMPLETENESS_UNKNOWN

    # For brand-or-equal tungsten blades etc., line items alone may be enough for product ID
    if (
        status in {PARTIAL, LIKELY_COMPLETE}
        and knows["what_product"]
        and knows["quantity"]
        and product_id.get("product_id_state")
        in {PRODUCT_SPEC_COMMODITY, PRODUCT_BRAND_OR_EQUAL, PRODUCT_MULTIPLE, PRODUCT_EXACT, PRODUCT_PART_NUMBER}
    ):
        status = COMPLETE_ENOUGH_FOR_PRODUCT_ID

    missing: list[str] = []
    if critical_spec_missing:
        missing.extend([f"specification_attachment:{n}" for n in spec_listed])
    for k, v in knows.items():
        if not v:
            missing.append(k)

    return {
        "document_completeness": status,
        "knows": knows,
        "missing": missing,
        "fetched_count": len(fetched),
        "auth_blocked_count": len(auth_blocked),
        "listed_not_fetched": len(listed_missing),
        "critical_spec_missing": critical_spec_missing,
        "assessed_at": _utc(),
    }


def build_transactional_requirement(
    *,
    solicitation_number: str,
    agency: str | None = None,
    buyer: str | None = None,
    source: str | None = None,
    deal_type: str | None = None,
    line_items: list[dict[str, Any]] | None = None,
    terms: dict[str, Any] | None = None,
    product_id: dict[str, Any] | None = None,
    completeness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "solicitation_number": solicitation_number,
        "agency": agency,
        "buyer": buyer,
        "source": source,
        "deal_type": deal_type,
        "requirement_confidence": (
            "HIGH"
            if (completeness or {}).get("document_completeness") == COMPLETE_ENOUGH_FOR_PRODUCT_ID
            else "MEDIUM"
            if (completeness or {}).get("document_completeness") in {LIKELY_COMPLETE, PARTIAL}
            else "LOW"
        ),
        "document_completeness": (completeness or {}).get("document_completeness"),
        "line_items": line_items or [],
        "terms": terms or {},
        "product_identification": product_id or {},
        "completeness": completeness or {},
    }
