"""Extract + normalize execution requirements from text and existing engines."""

from __future__ import annotations

import re
from typing import Any

from execution_requirements.constants import (
    ACTOR_OWNER,
    ACTOR_SUPPLIER,
    ACTOR_VA,
    CATEGORY_ALIASES,
    CLAUSE_ELIGIBILITY,
    CLAUSE_EXECUTION,
    CLAUSE_FINANCIAL,
    CLAUSE_INFO,
    CLAUSE_UNKNOWN,
    ST_REQUIRES_ACTION,
    ST_REQUIRES_OWNER,
    ST_REQUIRES_SUPPLIER,
    ST_UNKNOWN,
)
from execution_requirements.models import make_requirement

# --- Phase D supplemental patterns (fill gaps not covered by bid_requirement / DLA) ---
_EXTRA: list[tuple[str, str, str, re.Pattern[str], bool, str]] = [
    # category, subtype, label, pattern, mandatory, actor_hint
    ("INVOICE", "WAWF", "WAWF / Wide Area Workflow invoicing", re.compile(r"\b(?:WAWF|Wide\s+Area\s+Work\s*flow)\b", re.I), True, ACTOR_VA),
    ("INVOICE", "PIEE", "PIEE invoice / document system", re.compile(r"\bPIEE\b", re.I), True, ACTOR_VA),
    ("INVOICE", "SYSTEM", "Invoice submission system", re.compile(r"\binvoice\s+(?:via|through|in|using)\s+([^\n.]{3,60})", re.I), True, ACTOR_VA),
    ("INVOICE", "RECEIVING_REPORT", "Receiving report required", re.compile(r"\breceiving\s+report\b", re.I), True, ACTOR_VA),
    ("PAYMENT", "OFFICE", "Payment office", re.compile(r"\bpayment\s+office[:\s]+([^\n.]{3,80})", re.I), False, ACTOR_VA),
    ("PAYMENT", "EFT", "EFT / electronic payment", re.compile(r"\b(?:EFT|electronic\s+funds?\s+transfer)\b", re.I), False, ACTOR_VA),
    ("PAYMENT", "NET_TERMS", "Payment timing", re.compile(r"\bnet\s+(\d+)\b", re.I), False, ACTOR_OWNER),
    ("FOB", "TERMS", "FOB terms", re.compile(r"\bFOB\s+(ORIGIN|DESTINATION)\b", re.I), True, ACTOR_VA),
    ("QUANTITY_UOM", "PACK", "Pack / pieces-per-pack", re.compile(r"\b(\d+)\s+(?:per\s+)?(?:pack|box|carton|unit\s+pack)\b", re.I), True, ACTOR_VA),
    ("QUANTITY_UOM", "ESTIMATE", "Estimated quantity (not guaranteed)", re.compile(r"\bestimated\s+(?:annual\s+)?quantity[:\s]+([0-9,]+)", re.I), False, ACTOR_OWNER),
    ("QUANTITY_UOM", "MIN_MAX", "Min/max quantity", re.compile(r"\b(?:minimum|maximum)\s+quantity[:\s]+([0-9,]+)", re.I), False, ACTOR_VA),
    # HD = hundred — never treat as EA without converting (Phase F R04 / REGRESSION_QUANTITY)
    ("QUANTITY_UOM", "HD_HUNDRED", "Quantity in HD (hundred) — convert to pieces (×100)", re.compile(r"\b(?:quantity[:\s]+)?([0-9,]+)\s*HD\b|\bHD\s*\(\s*hundred\s*\)", re.I), True, ACTOR_VA),
    ("QUANTITY_UOM", "TOTAL_PIECES", "Total pieces after UOM normalization", re.compile(r"\btotal\s+pieces\s+(?:required\s+)?=\s*([0-9,]+)", re.I), True, ACTOR_VA),
    ("PRODUCT", "NSN", "NSN / NIIN", re.compile(r"\b(?:NSN|NIIN)[:\s#]*([0-9]{4}[\s\-]?[0-9]{2}[\s\-]?[0-9]{3}[\s\-]?[0-9]{4})\b", re.I), True, ACTOR_VA),
    ("PRODUCT", "PART", "Part number", re.compile(r"\b(?:P/?N|part\s+(?:number|no\.?))[:\s#]*([A-Z0-9][A-Z0-9\-/.]{2,40})", re.I), True, ACTOR_VA),
    ("PRODUCT", "BRAND_OR_EQUAL", "Brand name or equal", re.compile(r"\bbrand[\s-]*name\s+or\s+equal\b|\bor\s+equal\b", re.I), True, ACTOR_OWNER),
    ("PRODUCT", "BRAND_ONLY", "Brand name only / no substitutes", re.compile(r"\bbrand[\s-]*name\s+only\b|\bno\s+substitut", re.I), True, ACTOR_OWNER),
    ("DELIVERY", "DODAAC", "Ship-to DODAAC", re.compile(r"\bDODAAC[:\s#]*([A-Z0-9]{6})\b", re.I), True, ACTOR_VA),
    ("DELIVERY", "ARO", "Days after receipt of order", re.compile(r"\b(\d+)\s+days?\s+(?:after\s+)?(?:receipt\s+of\s+order|ARO|award)\b", re.I), True, ACTOR_VA),
    ("SHIPPING", "PARTIAL", "Partial shipment rules", re.compile(r"\bpartial\s+shipment", re.I), False, ACTOR_VA),
    ("SHIPPING", "APPOINTMENT", "Delivery appointment required", re.compile(r"\bappointment\s+required\b", re.I), True, ACTOR_VA),
    ("PACKAGING", "SPECIALIST", "Specialist packaging house may be required", re.compile(r"\b(?:packaging\s+house|specialist\s+packag|SPI\s+required)\b", re.I), True, ACTOR_SUPPLIER),
    ("INSPECTION", "SOURCE", "Source inspection", re.compile(r"\bsource\s+inspection\b", re.I), True, ACTOR_VA),
    ("INSPECTION", "DESTINATION", "Destination inspection", re.compile(r"\b(?:inspection\s+at\s+destination|destination\s+inspection)\b", re.I), True, ACTOR_VA),
    ("ACCEPTANCE", "DESTINATION", "Destination acceptance", re.compile(r"\bdestination\s+acceptance\b|\bacceptance\s+at\s+destination\b", re.I), True, ACTOR_VA),
    ("ACCEPTANCE", "COC", "Certificate of conformance", re.compile(r"\bcertificate\s+of\s+conformance\b|\bC\s*of\s*C\b|\bCoC\b", re.I), True, ACTOR_SUPPLIER),
    ("SUBMISSION", "FAX", "Fax submission", re.compile(r"\bfax\s+(?:to|number)[:\s]+([^\n]{5,40})", re.I), True, ACTOR_VA),
    ("SUBMISSION", "SUBJECT", "Email subject-line requirement", re.compile(r"\bsubject\s+line[:\s]+([^\n]{5,80})", re.I), True, ACTOR_VA),
    ("SUBMISSION", "VALIDITY", "Quote validity period", re.compile(r"\bquote\s+(?:valid|validity)[:\s]+([^\n.]{3,40})", re.I), True, ACTOR_VA),
]


def _plain_action(category: str, subtype: str | None, label: str, captured: str | None) -> str:
    detail = f" ({captured})" if captured else ""
    templates = {
        "FOB": f"Confirm FOB terms{detail} with supplier and include freight correctly in pricing.",
        "PACKAGING": f"Ask supplier whether {label}{detail} is included in the quote.",
        "INSPECTION": f"Confirm how {label}{detail} will be handled before shipment.",
        "ACCEPTANCE": f"Plan for {label}{detail} before invoicing.",
        "INVOICE": f"Prepare to submit invoice via {label}{detail} after acceptance.",
        "PAYMENT": f"Note payment requirement: {label}{detail}.",
        "PRODUCT": f"Confirm exact product identity{detail} with supplier — do not substitute without approval.",
        "QUANTITY_UOM": f"Confirm quantity and unit of measure{detail}; do not assume pack size.",
        "DELIVERY": f"Confirm delivery requirement{detail} is achievable.",
        "SHIPPING": f"Confirm shipping requirement: {label}{detail}.",
        "SUBMISSION": f"Complete submission item: {label}{detail}.",
        "SUPPLIER_CONFIRMATION": f"Get supplier confirmation: {label}{detail}.",
        "COUNTRY_OF_ORIGIN": f"Obtain country-of-origin representation{detail} from supplier.",
    }
    return templates.get(category, f"Resolve requirement: {label}{detail}.")


def _clause_relevance(category: str) -> str:
    if category in {"PACKAGING", "MARKING", "LABELING", "DELIVERY", "SHIPPING", "FOB", "INSPECTION", "ACCEPTANCE", "QUALITY", "TRACEABILITY", "WARRANTY", "POST_AWARD"}:
        return CLAUSE_EXECUTION
    if category in {"INVOICE", "PAYMENT"}:
        return CLAUSE_FINANCIAL
    if category in {"COUNTRY_OF_ORIGIN", "CERTIFICATION", "SUBMISSION"}:
        return CLAUSE_ELIGIBILITY
    if category == "OTHER":
        return CLAUSE_UNKNOWN
    return CLAUSE_INFO


def _what_requires(category: str, label: str, captured: str | None) -> str:
    c = f" specifically '{captured}'" if captured else ""
    return f"This requires us to satisfy: {label}{c}. Do not treat as optional if marked mandatory. Flag for review if meaning is unclear."


def _from_extra_patterns(text: str, *, source_document: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for category, subtype, label, pat, mandatory, actor in _EXTRA:
        for m in pat.finditer(text):
            raw = m.group(0).strip()
            key = f"{category}|{subtype}|{raw[:60].lower()}"
            if key in seen:
                continue
            seen.add(key)
            captured = m.group(1).strip() if m.lastindex and m.group(1) else None
            # Estimated quantity is never guaranteed purchase
            notes = None
            if subtype == "ESTIMATE":
                notes = "Estimated quantity is not a guaranteed purchase."
            status = ST_REQUIRES_ACTION
            if actor == ACTOR_SUPPLIER:
                status = ST_REQUIRES_SUPPLIER
            elif actor == ACTOR_OWNER and subtype in {"BRAND_OR_EQUAL", "BRAND_ONLY", "ESTIMATE"}:
                status = ST_REQUIRES_OWNER
            if category == "PACKAGING" and re.search(r"MIL[\s-]?STD", raw, re.I):
                status = ST_REQUIRES_SUPPLIER
            out.append(
                make_requirement(
                    category=category,
                    subtype=subtype,
                    normalized=f"{label}" + (f": {captured}" if captured else ""),
                    raw_text=raw,
                    source_document=source_document,
                    mandatory=mandatory,
                    blocking=mandatory,
                    status=status,
                    assigned_actor=actor,
                    confidence="EXTRACTED",
                    captured_value=captured,
                    notes=notes,
                    plain_english_action=_plain_action(category, subtype, label, captured),
                    clause_relevance=_clause_relevance(category),
                    what_this_requires=_what_requires(category, label, captured),
                )
            )
            # FOB / multi-destination: keep scanning for conflicting terms
            if category not in {"FOB", "DELIVERY", "QUANTITY_UOM"}:
                break
    return out


def _from_bid_requirements(reqs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in reqs or []:
        if not isinstance(r, dict):
            continue
        cat = CATEGORY_ALIASES.get(str(r.get("category") or ""), str(r.get("category") or "OTHER"))
        if cat not in {
            "SUBMISSION",
            "PRODUCT",
            "QUANTITY_UOM",
            "PACKAGING",
            "MARKING",
            "LABELING",
            "DELIVERY",
            "SHIPPING",
            "FOB",
            "INSPECTION",
            "ACCEPTANCE",
            "QUALITY",
            "TRACEABILITY",
            "WARRANTY",
            "COUNTRY_OF_ORIGIN",
            "CERTIFICATION",
            "TECHNICAL_DATA",
            "SUPPLIER_CONFIRMATION",
            "POST_AWARD",
            "INVOICE",
            "PAYMENT",
            "REPORTING",
            "OTHER",
        }:
            cat = "OTHER"
        mandatory = bool(r.get("mandatory"))
        raw = r.get("source_snippet") or r.get("normalized_requirement")
        out.append(
            make_requirement(
                category=cat,
                subtype=str(r.get("category") or ""),
                normalized=str(r.get("normalized_requirement") or "UNKNOWN"),
                raw_text=str(raw) if raw else None,
                source_document=r.get("source_document") or "UNKNOWN",
                source_page_or_section=r.get("page_or_section") or "UNKNOWN",
                mandatory=mandatory,
                blocking=mandatory,
                status=ST_REQUIRES_ACTION if mandatory else ST_UNKNOWN,
                confidence=str(r.get("confidence") or "EXTRACTED"),
                captured_value=r.get("captured_value"),
                plain_english_action=r.get("operator_action")
                or _plain_action(cat, str(r.get("category") or ""), str(r.get("normalized_requirement") or ""), str(r.get("captured_value") or "") or None),
                clause_relevance=_clause_relevance(cat),
                what_this_requires=_what_requires(cat, str(r.get("normalized_requirement") or ""), str(r.get("captured_value") or "") or None),
                evidence=list(r.get("evidence") or []),
            )
        )
    return out


def _from_dla_clauses(bundle: dict[str, Any] | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(bundle, dict):
        return out
    for c in bundle.get("clauses") or []:
        if not isinstance(c, dict):
            continue
        cat = str(c.get("category") or "OTHER").upper()
        if cat == "FREIGHT":
            cat = "SHIPPING"
        if cat not in {
            "PACKAGING",
            "MARKING",
            "LABELING",
            "DELIVERY",
            "SHIPPING",
            "FOB",
            "INSPECTION",
            "ACCEPTANCE",
            "CERTIFICATION",
            "OTHER",
        }:
            # Map unknown DLA cats
            if cat == "CERTIFICATIONS":
                cat = "CERTIFICATION"
            else:
                cat = "OTHER"
        ev = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
        raw = ev.get("extracted_text") or c.get("label") or c.get("field_key")
        status = ST_REQUIRES_SUPPLIER if cat in {"PACKAGING", "MARKING", "CERTIFICATION"} else ST_REQUIRES_ACTION
        out.append(
            make_requirement(
                category=cat,
                subtype=str(c.get("field_key") or ""),
                normalized=str(c.get("label") or c.get("field_key") or "UNKNOWN"),
                raw_text=str(raw) if raw else None,
                source_document=str(ev.get("source_document") or c.get("source_document") or "UNKNOWN"),
                source_page_or_section=str(ev.get("page") or ev.get("section") or "UNKNOWN"),
                mandatory=True,
                blocking=True,
                status=status,
                assigned_actor=ACTOR_SUPPLIER if status == ST_REQUIRES_SUPPLIER else ACTOR_VA,
                confidence=str(ev.get("confidence") or c.get("confidence") or "EXTRACTED"),
                plain_english_action=_plain_action(cat, str(c.get("field_key") or ""), str(c.get("label") or ""), None),
                clause_relevance=CLAUSE_EXECUTION,
                what_this_requires=_what_requires(cat, str(c.get("label") or ""), None),
            )
        )
    return out


def _from_clins(row: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    clins: list[dict[str, Any]] = []
    try:
        from micro_purchase_lab_integrity import extract_clins

        extracted = extract_clins(row) or {}
        if isinstance(extracted, dict):
            clins = list(extracted.get("clins") or extracted.get("line_items") or [])
        elif isinstance(extracted, list):
            clins = extracted
    except Exception:
        clins = []
    if not clins:
        clins = list(row.get("clins") or row.get("line_items") or [])
    for li in clins or []:
        if not isinstance(li, dict):
            continue
        clin = str(li.get("clin") or li.get("clin_number") or li.get("line") or "")
        qty_raw = li.get("quantity")
        qty = qty_raw
        uom = li.get("uom") or li.get("unit")
        if isinstance(qty_raw, dict):
            qty = qty_raw.get("raw_quantity") or qty_raw.get("quantity") or qty_raw.get("total_piece_equivalent")
            uom = uom or qty_raw.get("raw_uom") or qty_raw.get("uom")
        dest = li.get("destination")
        fob = li.get("fob")
        if qty is not None or uom:
            out.append(
                make_requirement(
                    category="QUANTITY_UOM",
                    subtype="CLIN",
                    normalized=f"CLIN {clin or 'UNKNOWN'}: qty={qty} UOM={uom}",
                    raw_text=str(li.get("raw") or li.get("description") or li.get("product_identity") or ""),
                    mandatory=True,
                    blocking=True,
                    status=ST_REQUIRES_ACTION if qty is not None else ST_UNKNOWN,
                    clin=clin or None,
                    captured_value={"quantity": qty, "uom": uom},
                    confidence="EXTRACTED" if qty is not None else "UNKNOWN",
                    notes="Do not treat estimated quantities as guaranteed. Do not assume qty 1 means one piece.",
                    plain_english_action=f"Confirm CLIN {clin or ''} quantity {qty} {uom or ''} with supplier.",
                    source_document=str(row.get("source") or "line_items"),
                )
            )
        if dest:
            out.append(
                make_requirement(
                    category="DELIVERY",
                    subtype="CLIN_DESTINATION",
                    normalized=f"CLIN {clin}: destination {dest}",
                    raw_text=str(dest),
                    mandatory=True,
                    blocking=True,
                    status=ST_REQUIRES_ACTION,
                    clin=clin or None,
                    captured_value=dest,
                    plain_english_action=f"Confirm delivery to {dest} for CLIN {clin}.",
                    source_document=str(row.get("source") or "line_items"),
                )
            )
        if fob:
            out.append(
                make_requirement(
                    category="FOB",
                    subtype="CLIN_FOB",
                    normalized=f"CLIN {clin}: FOB {fob}",
                    raw_text=str(fob),
                    mandatory=True,
                    blocking=True,
                    status=ST_REQUIRES_ACTION,
                    clin=clin or None,
                    captured_value=fob,
                    plain_english_action=f"Confirm FOB {fob} pricing includes correct freight responsibility.",
                    source_document=str(row.get("source") or "line_items"),
                )
            )
    return out


def _supplier_confirmation_seeds(requirements: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert execution needs into explicit supplier confirmation requirements."""
    seeds = [
        ("exact_part", "Confirm exact part number / NSN matches the solicitation"),
        ("qty_available", "Confirm quantity available to ship"),
        ("lead_time", "Confirm lead time meets delivery requirement"),
        ("coo", "Confirm country of origin"),
        ("warranty", "Confirm warranty terms"),
        ("packaging", "Confirm packaging compliance"),
        ("drop_ship", "Confirm drop-ship capability if required"),
        ("freight", "Confirm freight / FOB terms in quote"),
        ("quote_validity", "Confirm quote validity period"),
        ("gov_labeling", "Confirm government labeling / marking capability"),
    ]
    # Only emit if related evidence exists or always as REQUIRES_SUPPLIER for executable deals
    cats = {r.get("category") for r in requirements}
    out: list[dict[str, Any]] = []
    for key, label in seeds:
        relevant = True
        if key == "packaging" and "PACKAGING" not in cats and "MARKING" not in cats:
            # still ask if product deal — keep as recommended
            relevant = True
        if key == "coo" and "COUNTRY_OF_ORIGIN" not in cats:
            relevant = "COUNTRY_OF_ORIGIN" in cats or True
        out.append(
            make_requirement(
                category="SUPPLIER_CONFIRMATION",
                subtype=key,
                normalized=label,
                mandatory=key in {"exact_part", "qty_available", "lead_time", "packaging", "freight"},
                blocking=key in {"exact_part", "qty_available", "lead_time", "packaging"},
                status=ST_REQUIRES_SUPPLIER,
                assigned_actor=ACTOR_SUPPLIER,
                confidence="DERIVED",
                plain_english_action=label + ".",
                what_this_requires=label,
                clause_relevance=CLAUSE_EXECUTION,
                source_document="derived:supplier_confirmation_engine",
                source_page_or_section="N/A",
            )
        )
    return out


def _row_text(row: dict[str, Any]) -> str:
    parts = [
        row.get("solicitation_text"),
        row.get("description"),
        row.get("full_text"),
        row.get("raw_text"),
        row.get("title"),
        row.get("product"),
        row.get("Opportunity"),
    ]
    # Document snippets
    for d in row.get("documents") or []:
        if isinstance(d, dict):
            parts.append(d.get("text") or d.get("text_snippet"))
    return "\n".join(str(p) for p in parts if p)


def build_execution_requirements(
    row: dict[str, Any] | None = None,
    *,
    text: str | None = None,
    source_document: str = "solicitation",
    include_supplier_seeds: bool = True,
    use_existing_extractors: bool = True,
) -> list[dict[str, Any]]:
    """
    Build normalized ExecutionRequirement list.
    Never invents requirements without text/evidence match.
    Unknown stays UNKNOWN — never auto-CONFIRMED.
    """
    row = row if isinstance(row, dict) else {}
    body = text if text is not None else _row_text(row)
    body = str(body or "")
    out: list[dict[str, Any]] = []

    if use_existing_extractors and body.strip():
        try:
            from bid_requirement_extraction import extract_requirements_from_text

            bid_reqs = extract_requirements_from_text(
                body,
                solicitation_id=str(row.get("canonical_id") or row.get("solicitation_id") or "unknown"),
                source_document=source_document,
            )
            out.extend(_from_bid_requirements(bid_reqs))
        except Exception:
            pass
        try:
            from m3_dla_clause_extraction import extract_dla_clauses

            dla = extract_dla_clauses(row, texts=[{"text": body, "source_document": source_document, "document_type": "SOLICITATION"}])
            out.extend(_from_dla_clauses(dla))
        except Exception:
            pass

    if body.strip():
        out.extend(_from_extra_patterns(body, source_document=source_document))

    out.extend(_from_clins(row))

    if include_supplier_seeds and (body.strip() or out):
        out.extend(_supplier_confirmation_seeds(out))

    # Deduplicate by requirement_id / category+normalized
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for r in out:
        key = r.get("requirement_id") or f"{r.get('category')}|{r.get('normalized_requirement')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return deduped
