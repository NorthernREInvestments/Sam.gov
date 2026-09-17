"""Complete Procurement Package Intelligence — research only.

Converts a solicitation into an acquisition package a government reseller
would review before deciding whether to pursue. Never invents accessories,
compatibility, prices, or profit. Incomplete data stays UNKNOWN with a next action.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from typing import Any

from application_clock import now_utc
from m3_commercial_engine import (
    CAGE_RE,
    KNOWN_MANUFACTURERS,
    NSN_RE,
    PART_RE,
    PRICE_LEVEL_1_ACTUAL,
    PRICE_LEVEL_2_PUBLIC,
    PRICE_LEVEL_3_COMPARABLE,
    PRICE_LEVEL_4_UNKNOWN,
    QTY_RE,
    SCORE_HIGH,
    SCORE_LOW,
    SCORE_MEDIUM,
    extract_identification,
)
from m3_product_pricing import (
    MATCH_HIGH,
    MATCH_LOW,
    MATCH_MEDIUM,
    MATCH_UNKNOWN,
    VAL_ESTIMATED,
    VAL_UNKNOWN,
    VAL_VERIFIED,
    build_contract_value_model,
    build_market_price_profile,
    build_product_identity_profile,
    product_match_confidence,
)
from m3_supplier_intelligence import MODEL_RE, SKU_RE, UPC_RE

log = logging.getLogger("govtracker.m3_procurement_package")

PACKAGE_INDEX_KEY = "m3_procurement_package_v1"

# Completeness / readiness
READY_FOR_ECONOMICS = "READY_FOR_ECONOMICS"
READY_FOR_PRICING = "READY_FOR_PRICING"
PARTIAL = "PARTIAL"
NEEDS_DOCUMENTS = "NEEDS_DOCUMENTS"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"

# Research queues
Q_NEEDS_PRODUCT_IDENTITY = "NEEDS_PRODUCT_IDENTITY"
Q_NEEDS_DOCUMENT_RECOVERY = "NEEDS_DOCUMENT_RECOVERY"
Q_NEEDS_CONFIGURATION = "NEEDS_CONFIGURATION"
Q_NEEDS_PRICING = "NEEDS_PRICING"
Q_READY = "READY_FOR_PACKAGE_PRICING"

# BOM / config statuses
BOM_CONFIRMED = "CONFIRMED"
BOM_LIKELY = "LIKELY"
BOM_UNKNOWN = "UNKNOWN"

CONFIG_TYPES = (
    "PRIMARY_EQUIPMENT",
    "REQUIRED_ACCESSORIES",
    "REQUIRED_OPTIONS",
    "COMPONENTS",
    "SUBASSEMBLIES",
    "CONSUMABLES",
    "LICENSES",
    "SOFTWARE",
    "WARRANTY",
    "SUPPORT_AGREEMENTS",
    "SERVICE_REQUIREMENTS",
    "INSTALLATION_MATERIALS",
    "TRAINING_REQUIREMENTS",
)

UOM_RE = re.compile(r"\b(EA|EACH|BOX|CS|CASE|SET|KIT|LB|KG|FT|GAL|PAIR|PK|PACK|LOT)\b", re.I)
CONTRACT_TYPE_RE = re.compile(
    r"\b(IFB|ITB|RFQ|RFP|RFI|IDIQ|BPA|PO|PURCHASE\s+ORDER|SEALED\s+BID|INVITATION\s+FOR\s+BID)\b",
    re.I,
)
DELIVERY_LOC_RE = re.compile(
    r"(?:deliver(?:y|ed)?\s+(?:to|at)|place\s+of\s+performance|ship\s+to)[:\s]+([A-Za-z0-9 ,\-/#]{5,80})",
    re.I,
)
DATE_REQ_RE = re.compile(
    r"(?:delivery\s+(?:date|by|required)|period\s+of\s+performance|POP)[:\s]+([A-Za-z0-9 ,/\-]{4,40})",
    re.I,
)
OPTION_RE = re.compile(r"\b(option\s+year|option\s+period|renewal|base\s+year)\b", re.I)
RENEWAL_RE = re.compile(r"\b(renewal|renewable|option\s+to\s+extend)\b", re.I)

ACCESSORY_HINT_RE = re.compile(
    r"\b(accessor(?:y|ies)|mount(?:ing)?|cable|adapter|bracket|power\s+cord|"
    r"rack\s*kit|rail\s*kit|antenna|battery|charger|case|cover|kit)\b",
    re.I,
)
OPTION_HINT_RE = re.compile(
    r"\b(option(?:al)?|configuration\s+option|config\s+option|add[- ]?on)\b", re.I
)
COMPONENT_HINT_RE = re.compile(
    r"\b(component|module|blade|transceiver|SFP|NIC|memory|DIMM|hard\s*drive|SSD|HDD)\b",
    re.I,
)
LICENSE_HINT_RE = re.compile(r"\b(license|licen[cs]e|subscription|software\s+entitlement)\b", re.I)
SOFTWARE_HINT_RE = re.compile(r"\b(software|firmware|OS\s+image|operating\s+system)\b", re.I)
WARRANTY_HINT_RE = re.compile(r"\b(warranty|extended\s+warranty|NBD|next[- ]business[- ]day)\b", re.I)
SUPPORT_HINT_RE = re.compile(r"\b(support\s+agreement|maintenance|SmartNet|ProSupport|care\s+pack)\b", re.I)
SERVICE_HINT_RE = re.compile(r"\b(installation\s+service|configuration\s+service|on[- ]?site\s+service)\b", re.I)
INSTALL_HINT_RE = re.compile(r"\b(installation\s+material|mounting\s+hardware|fastener|anchor)\b", re.I)
TRAINING_HINT_RE = re.compile(r"\b(training|operator\s+training|user\s+training)\b", re.I)
CONSUMABLE_HINT_RE = re.compile(r"\b(consumable|toner|ink|filter|cartridge|media)\b", re.I)

# False identifier rejection
FALSE_ID_BLOCKLIST = re.compile(
    r"(https?://|www\.|\.gov|\.com|\.pdf|\.html|/opportunity/|/documents?/|"
    r"sam\.gov|attachment|amendment|solicitation|notice\s*id|"
    r"\d{4}-\d{2}-\d{2}T|\d{10,}|javascript|cookie|session)",
    re.I,
)
DOC_ID_RE = re.compile(
    r"^(DOC|ATT|AMD|SOL|RFQ|RFP|IFB|NOTICE|BID)[-_]?[\d][\d\-_]{1,}$",
    re.I,
)
TIMESTAMPISH_RE = re.compile(r"^\d{8,}([T_]\d+)?$")
URL_FRAGMENT_RE = re.compile(r"[/\\]|^\w+\.\w+$")


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


def _evidence_blob(row: dict[str, Any], *, max_chars: int = 12000) -> str:
    parts = [
        str(row.get("title") or ""),
        str(row.get("description") or "")[:3000],
        str(row.get("solicitation_number") or ""),
    ]
    for key in ("solicitation_text", "attachment_text", "governing_text", "pws_text", "evidence_text_excerpt"):
        if row.get(key):
            parts.append(str(row.get(key))[:3500])
    for li in row.get("line_items") or row.get("bom") or []:
        if isinstance(li, dict):
            parts.append(str(li.get("description") or ""))
            parts.append(str(li.get("part_number") or ""))
            parts.append(str(li.get("manufacturer") or ""))
            parts.append(str(li.get("specification") or ""))
            parts.append(str(li.get("nsn") or ""))
    docs = row.get("documents") or []
    if isinstance(docs, list):
        for d in docs[:10]:
            if isinstance(d, dict):
                parts.append(str(d.get("name") or d.get("filename") or ""))
                parts.append(str(d.get("extracted_text") or d.get("text") or "")[:2500])
    return " ".join(parts)[:max_chars]


def _line_items(row: dict[str, Any]) -> list[dict[str, Any]]:
    items = row.get("line_items") or row.get("bom") or []
    return [li for li in items if isinstance(li, dict)] if isinstance(items, list) else []


def _source_label(row: dict[str, Any], default: str = "solicitation_text") -> str:
    if _line_items(row):
        return "line_items_or_bom"
    if row.get("documents"):
        return "attachments_or_documents"
    if row.get("governing_text") or row.get("solicitation_text"):
        return "solicitation_body"
    if row.get("award_amount") or row.get("estimated_value"):
        return "award_or_value_metadata"
    return default


# ---------------------------------------------------------------------------
# Phase 3 — identifier validation
# ---------------------------------------------------------------------------

def validate_identifier(
    identifier: Any,
    *,
    source: str = "UNKNOWN",
    kind: str = "part_number",
) -> dict[str, Any]:
    """Reject portal chrome, URLs, timestamps, document IDs."""
    raw = str(identifier or "").strip()
    if not raw or raw.upper() == "UNKNOWN":
        return {
            "Identifier": "UNKNOWN",
            "Source": source,
            "Confidence": MATCH_UNKNOWN,
            "Validation_status": "REJECTED_EMPTY",
            "kind": kind,
            "accepted": False,
        }

    reasons: list[str] = []
    if FALSE_ID_BLOCKLIST.search(raw):
        reasons.append("portal_or_url_metadata")
    if DOC_ID_RE.match(raw):
        reasons.append("document_id_pattern")
    if TIMESTAMPISH_RE.match(raw):
        reasons.append("timestamp_like")
    if URL_FRAGMENT_RE.search(raw) and kind != "nsn":
        reasons.append("url_fragment")
    if kind == "part_number" and len(raw) < 3:
        reasons.append("too_short")
    if kind == "part_number" and raw.lower() in {
        "contracts",
        "documents",
        "opportunities",
        "attachments",
        "amendments",
        "notice",
        "physical",
        "security",
        "systems",
        "general",
        "supplies",
        "equipment",
    }:
        reasons.append("generic_word")

    if reasons:
        return {
            "Identifier": raw,
            "Source": source,
            "Confidence": MATCH_UNKNOWN,
            "Validation_status": "REJECTED",
            "rejection_reasons": reasons,
            "kind": kind,
            "accepted": False,
        }

    conf = MATCH_HIGH if source in {
        "line_items_or_bom",
        "official_attachment",
        "manufacturer_document",
        "technical_specification",
        "bom_document",
        "official_catalog",
    } else MATCH_MEDIUM if source in {"solicitation_body", "title_description"} else MATCH_LOW

    return {
        "Identifier": raw,
        "Source": source,
        "Confidence": conf,
        "Validation_status": "ACCEPTED",
        "kind": kind,
        "accepted": True,
    }


def extract_validated_identifiers(row: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect identifiers only from trusted contexts, then validate."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(val: Any, source: str, kind: str) -> None:
        v = validate_identifier(val, source=source, kind=kind)
        key = f"{kind}:{v.get('Identifier')}"
        if key in seen:
            return
        seen.add(key)
        out.append(v)

    for li in _line_items(row):
        if li.get("part_number"):
            _add(li.get("part_number"), "line_items_or_bom", "part_number")
        if li.get("nsn"):
            _add(li.get("nsn"), "line_items_or_bom", "nsn")
        if li.get("model") or li.get("model_number"):
            _add(li.get("model") or li.get("model_number"), "line_items_or_bom", "model")
        if li.get("sku"):
            _add(li.get("sku"), "line_items_or_bom", "sku")
        if li.get("cage"):
            _add(li.get("cage"), "line_items_or_bom", "cage")

    # Trusted text only: title + description + BOM text (not portal HTML chrome)
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")[:2000]
    bom_text = " ".join(
        f"{li.get('description') or ''} {li.get('part_number') or ''} {li.get('manufacturer') or ''}"
        for li in _line_items(row)
    )
    trusted = f"{title} {desc} {bom_text}"
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}

    for m in NSN_RE.finditer(trusted):
        _add(m.group(1), "title_description", "nsn")
    if meta.get("nsn"):
        _add(meta.get("nsn"), "official_catalog", "nsn")

    for m in PART_RE.finditer(trusted):
        _add(m.group(1), "title_description", "part_number")

    for m in MODEL_RE.finditer(trusted):
        _add(m.group(1), "title_description", "model")

    for m in SKU_RE.finditer(trusted):
        _add(m.group(1), "title_description", "sku")

    for m in CAGE_RE.finditer(trusted):
        _add(m.group(1), "title_description", "cage")

    for m in UPC_RE.finditer(trusted):
        _add(m.group(1), "title_description", "upc")

    # Attachment excerpts — only labeled part/NSN patterns
    for key in ("attachment_text", "governing_text", "solicitation_text"):
        text = str(row.get(key) or "")[:4000]
        if not text:
            continue
        for m in PART_RE.finditer(text):
            _add(m.group(1), "official_attachment", "part_number")
        for m in NSN_RE.finditer(text):
            _add(m.group(1), "official_attachment", "nsn")
        for m in MODEL_RE.finditer(text):
            _add(m.group(1), "official_attachment", "model")

    return out


# ---------------------------------------------------------------------------
# Phase 1 — commercial requirement extraction
# ---------------------------------------------------------------------------

def build_commercial_requirement_profile(row: dict[str, Any]) -> dict[str, Any]:
    blob = _evidence_blob(row)
    ident = extract_identification(row)
    contract = build_contract_value_model(row)
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}

    ct = CONTRACT_TYPE_RE.search(blob)
    contract_type = (
        row.get("contract_type")
        or row.get("procurement_type")
        or meta.get("contract_type")
        or (ct.group(1).upper() if ct else "UNKNOWN")
    )

    uom_m = UOM_RE.search(blob)
    qty = contract.get("quantity")
    if qty == "UNKNOWN":
        qty = ident.get("quantity") if ident.get("quantity") != "UNKNOWN" else "UNKNOWN"

    loc_m = DELIVERY_LOC_RE.search(blob)
    date_m = DATE_REQ_RE.search(blob)
    options = bool(OPTION_RE.search(blob))
    renewals = bool(RENEWAL_RE.search(blob))

    line_items = []
    for i, li in enumerate(_line_items(row), start=1):
        line_items.append(
            {
                "line": i,
                "description": li.get("description") or "UNKNOWN",
                "part_number": li.get("part_number") or "UNKNOWN",
                "manufacturer": li.get("manufacturer") or "UNKNOWN",
                "quantity": _num(li.get("quantity")) if _num(li.get("quantity")) is not None else "UNKNOWN",
                "unit": li.get("unit") or li.get("uom") or "UNKNOWN",
                "nsn": li.get("nsn") or "UNKNOWN",
                "specification": li.get("specification") or "UNKNOWN",
            }
        )

    # Confidence: verified if award; estimated if value/qty present; else unknown
    has_value = contract.get("confidence") != VAL_UNKNOWN
    has_lines = bool(line_items)
    if contract.get("confidence") == VAL_VERIFIED and has_lines:
        conf = VAL_VERIFIED
    elif has_value or has_lines or _known(qty):
        conf = VAL_ESTIMATED
    else:
        conf = VAL_UNKNOWN

    special = []
    for token in ("Buy American", "TAA", "Berry", "small business", "set-aside", "FOB destination", "FOB origin"):
        if re.search(re.escape(token), blob, re.I):
            special.append(token)

    return {
        "kind": "COMMERCIAL_REQUIREMENT_PROFILE",
        "Solicitation_ID": ident.get("solicitation_number") or "UNKNOWN",
        "Agency": ident.get("agency") or "UNKNOWN",
        "Buyer": ident.get("buyer") or "UNKNOWN",
        "Contract_type": contract_type if contract_type else "UNKNOWN",
        "Estimated_contract_value": contract.get("estimated_value")
        if contract.get("estimated_value") != "UNKNOWN"
        else contract.get("contract_value"),
        "Award_value": contract.get("award_amount"),
        "Line_items": line_items[:40],
        "Quantity": qty if qty is not None else "UNKNOWN",
        "Unit_of_measure": (uom_m.group(1).upper() if uom_m else "UNKNOWN"),
        "Delivery_requirements": ident.get("delivery_requirements") or "UNKNOWN",
        "Delivery_location": loc_m.group(1).strip() if loc_m else (meta.get("place_of_performance") or "UNKNOWN"),
        "Required_dates": date_m.group(1).strip() if date_m else (row.get("deadline") or "UNKNOWN"),
        "Options": options,
        "Renewals": renewals,
        "Special_requirements": special or "UNKNOWN",
        "Evidence_source": _source_label(row),
        "Confidence": conf,
        "Contract_value_model": contract,
    }


# ---------------------------------------------------------------------------
# Phase 2 — exact product identity (validated)
# ---------------------------------------------------------------------------

def build_package_product_identity(row: dict[str, Any]) -> dict[str, Any]:
    base = build_product_identity_profile(row)
    ids = extract_validated_identifiers(row)
    accepted = [i for i in ids if i.get("accepted")]

    part = next((i["Identifier"] for i in accepted if i["kind"] == "part_number"), None)
    nsn = next((i["Identifier"] for i in accepted if i["kind"] == "nsn"), None)
    model = next((i["Identifier"] for i in accepted if i["kind"] == "model"), None)
    sku = next((i["Identifier"] for i in accepted if i["kind"] == "sku"), None)
    cage = next((i["Identifier"] for i in accepted if i["kind"] == "cage"), None)
    upc = next((i["Identifier"] for i in accepted if i["kind"] == "upc"), None)

    # Prefer validated over base (base may still have false positives)
    mfr = base.get("Manufacturer") or "UNKNOWN"
    if not _known(mfr):
        for name in KNOWN_MANUFACTURERS:
            if re.search(rf"\b{re.escape(name)}\b", str(row.get("title") or ""), re.I):
                mfr = name
                break

    # Identity confidence priority: exact PN/NSN > model+mfr > mfr > category
    if _known(part) or _known(nsn) or _known(sku):
        conf = MATCH_HIGH if _known(mfr) or _known(nsn) else MATCH_MEDIUM
    elif _known(mfr) and _known(model):
        conf = MATCH_MEDIUM
    elif _known(mfr) or _known(model):
        conf = MATCH_LOW
    else:
        conf = MATCH_UNKNOWN

    # Do not keep rejected identifiers from base
    rejected = [i for i in ids if not i.get("accepted") and i.get("Identifier") != "UNKNOWN"]
    if base.get("Manufacturer_part_number") not in {None, "UNKNOWN"}:
        chk = validate_identifier(base.get("Manufacturer_part_number"), source="base_profile", kind="part_number")
        if not chk.get("accepted"):
            part = part  # keep validated only
        elif not part:
            part = base.get("Manufacturer_part_number")

    return {
        "kind": "PRODUCT_IDENTITY_PROFILE",
        "Manufacturer": mfr if _known(mfr) else "UNKNOWN",
        "Manufacturer_part_number": part or "UNKNOWN",
        "OEM_part_number": part or "UNKNOWN",
        "Model_number": model or (base.get("Model_number") if _known(base.get("Model_number")) else "UNKNOWN"),
        "SKU": sku or "UNKNOWN",
        "NSN": nsn or (base.get("NSN") if _known(base.get("NSN")) and validate_identifier(base.get("NSN"), kind="nsn").get("accepted") else "UNKNOWN"),
        "CAGE": cage or base.get("CAGE") or "UNKNOWN",
        "UPC": upc or "UNKNOWN",
        "Product_family": base.get("Category") or row.get("product_category") or "UNKNOWN",
        "Description": base.get("Description") or row.get("title") or "UNKNOWN",
        "Specifications": base.get("Specification_requirements") or "UNKNOWN",
        "Configuration_requirements": base.get("Configuration") or "UNKNOWN",
        "Quantity": base.get("Quantity") if _known(base.get("Quantity")) else "UNKNOWN",
        "Identity_confidence": conf,
        "IDENTIFIER_CONFIDENCE": accepted[:20],
        "rejected_identifiers": [
            {"Identifier": r.get("Identifier"), "reasons": r.get("rejection_reasons"), "kind": r.get("kind")}
            for r in rejected[:15]
        ],
        "sufficient_for_pricing_research": conf in {MATCH_HIGH, MATCH_MEDIUM},
    }


# ---------------------------------------------------------------------------
# Phase 4 — configuration extraction (evidence only; never assume)
# ---------------------------------------------------------------------------

def _classify_line_role(text: str, index: int) -> str:
    t = text or ""
    if index == 0 and not any(
        r.search(t)
        for r in (
            ACCESSORY_HINT_RE,
            LICENSE_HINT_RE,
            WARRANTY_HINT_RE,
            SUPPORT_HINT_RE,
            TRAINING_HINT_RE,
            CONSUMABLE_HINT_RE,
        )
    ):
        return "PRIMARY_EQUIPMENT"
    if ACCESSORY_HINT_RE.search(t):
        return "REQUIRED_ACCESSORIES"
    if OPTION_HINT_RE.search(t):
        return "REQUIRED_OPTIONS"
    if COMPONENT_HINT_RE.search(t):
        return "COMPONENTS"
    if CONSUMABLE_HINT_RE.search(t):
        return "CONSUMABLES"
    if LICENSE_HINT_RE.search(t):
        return "LICENSES"
    if SOFTWARE_HINT_RE.search(t):
        return "SOFTWARE"
    if WARRANTY_HINT_RE.search(t):
        return "WARRANTY"
    if SUPPORT_HINT_RE.search(t):
        return "SUPPORT_AGREEMENTS"
    if SERVICE_HINT_RE.search(t):
        return "SERVICE_REQUIREMENTS"
    if INSTALL_HINT_RE.search(t):
        return "INSTALLATION_MATERIALS"
    if TRAINING_HINT_RE.search(t):
        return "TRAINING_REQUIREMENTS"
    return "PRIMARY_EQUIPMENT" if index == 0 else "COMPONENTS"


def build_product_configuration_profile(row: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    """Extract configuration only from explicit line items / labeled text — never invent."""
    items_by_type: dict[str, list[dict[str, Any]]] = {k: [] for k in CONFIG_TYPES}
    lines = _line_items(row)

    if lines:
        for i, li in enumerate(lines):
            desc = str(li.get("description") or "")
            role = _classify_line_role(desc, i)
            pn_val = validate_identifier(li.get("part_number"), source="line_items_or_bom", kind="part_number")
            entry = {
                "Item": desc or "UNKNOWN",
                "Manufacturer": li.get("manufacturer") or identity.get("Manufacturer") or "UNKNOWN",
                "Part_number": pn_val["Identifier"] if pn_val.get("accepted") else "UNKNOWN",
                "Quantity": _num(li.get("quantity")) if _num(li.get("quantity")) is not None else "UNKNOWN",
                "Required_or_optional": "REQUIRED" if role != "REQUIRED_OPTIONS" else "OPTIONAL",
                "Relationship_to_primary": "PRIMARY" if role == "PRIMARY_EQUIPMENT" else "SUPPORTS_PRIMARY",
                "Evidence_source": "line_items_or_bom",
                "Confidence": BOM_CONFIRMED if desc else BOM_UNKNOWN,
                "config_type": role,
            }
            items_by_type[role].append(entry)
    else:
        # Single primary from identity — accessories UNKNOWN (not assumed)
        items_by_type["PRIMARY_EQUIPMENT"].append(
            {
                "Item": identity.get("Description") or row.get("title") or "UNKNOWN",
                "Manufacturer": identity.get("Manufacturer") or "UNKNOWN",
                "Part_number": identity.get("Manufacturer_part_number") or "UNKNOWN",
                "Quantity": identity.get("Quantity") or "UNKNOWN",
                "Required_or_optional": "REQUIRED",
                "Relationship_to_primary": "PRIMARY",
                "Evidence_source": _source_label(row),
                "Confidence": BOM_LIKELY if identity.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM} else BOM_UNKNOWN,
                "config_type": "PRIMARY_EQUIPMENT",
            }
        )

    # Labeled accessory phrases in attachments (explicit only)
    blob = _evidence_blob(row, max_chars=8000)
    if ACCESSORY_HINT_RE.search(blob) and not items_by_type["REQUIRED_ACCESSORIES"]:
        # Mark that accessories are mentioned but not enumerated — do NOT invent SKUs
        items_by_type["REQUIRED_ACCESSORIES"].append(
            {
                "Item": "ACCESSORIES_REFERENCED_BUT_NOT_ENUMERATED",
                "Manufacturer": "UNKNOWN",
                "Part_number": "UNKNOWN",
                "Quantity": "UNKNOWN",
                "Required_or_optional": "REQUIRED",
                "Relationship_to_primary": "SUPPORTS_PRIMARY",
                "Evidence_source": "solicitation_body",
                "Confidence": BOM_UNKNOWN,
                "config_type": "REQUIRED_ACCESSORIES",
                "note": "Referenced in text; exact accessory list not extracted — do not assume",
            }
        )

    primary_count = len(items_by_type["PRIMARY_EQUIPMENT"])
    accessory_known = [
        a for a in items_by_type["REQUIRED_ACCESSORIES"] if a.get("Item") != "ACCESSORIES_REFERENCED_BUT_NOT_ENUMERATED"
    ]
    complete = (
        primary_count >= 1
        and identity.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM}
        and (
            not items_by_type["REQUIRED_ACCESSORIES"]
            or bool(accessory_known)
        )
        and all(
            a.get("Confidence") != BOM_UNKNOWN
            for a in items_by_type["REQUIRED_ACCESSORIES"]
            if a.get("Item") != "ACCESSORIES_REFERENCED_BUT_NOT_ENUMERATED"
        )
    )
    # Incomplete if accessories referenced but not listed
    if any(a.get("Item") == "ACCESSORIES_REFERENCED_BUT_NOT_ENUMERATED" for a in items_by_type["REQUIRED_ACCESSORIES"]):
        complete = False

    return {
        "kind": "PRODUCT_CONFIGURATION_PROFILE",
        "items_by_type": items_by_type,
        "PRIMARY_EQUIPMENT": items_by_type["PRIMARY_EQUIPMENT"],
        "REQUIRED_ACCESSORIES": items_by_type["REQUIRED_ACCESSORIES"],
        "configuration_complete": complete,
        "notes": [
            "never_assume_accessories",
            "never_assume_compatibility",
            "UNKNOWN_when_not_evidenced",
        ],
    }


# ---------------------------------------------------------------------------
# Phase 5 — procurement BOM
# ---------------------------------------------------------------------------

def build_procurement_bom(
    row: dict[str, Any],
    identity: dict[str, Any],
    configuration: dict[str, Any],
) -> dict[str, Any]:
    lines_out: list[dict[str, Any]] = []
    line_no = 0
    order = (
        "PRIMARY_EQUIPMENT",
        "REQUIRED_ACCESSORIES",
        "REQUIRED_OPTIONS",
        "COMPONENTS",
        "SUBASSEMBLIES",
        "CONSUMABLES",
        "LICENSES",
        "SOFTWARE",
        "WARRANTY",
        "SUPPORT_AGREEMENTS",
        "SERVICE_REQUIREMENTS",
        "INSTALLATION_MATERIALS",
        "TRAINING_REQUIREMENTS",
    )
    by_type = configuration.get("items_by_type") or {}
    for role in order:
        for entry in by_type.get(role) or []:
            line_no += 1
            status = entry.get("Confidence") or BOM_UNKNOWN
            lines_out.append(
                {
                    "Line_number": line_no,
                    "Description": entry.get("Item") or "UNKNOWN",
                    "Manufacturer": entry.get("Manufacturer") or "UNKNOWN",
                    "Part_number": entry.get("Part_number") or "UNKNOWN",
                    "NSN": identity.get("NSN") if role == "PRIMARY_EQUIPMENT" else "UNKNOWN",
                    "Quantity": entry.get("Quantity") if entry.get("Quantity") is not None else "UNKNOWN",
                    "Unit": "EA",
                    "Requirement_status": entry.get("Required_or_optional") or "UNKNOWN",
                    "config_type": role,
                    "Source_document": entry.get("Evidence_source") or _source_label(row),
                    "Confidence": status,
                    "include_in_cost": status in {BOM_CONFIRMED, BOM_LIKELY},
                }
            )

    if not lines_out:
        lines_out.append(
            {
                "Line_number": 1,
                "Description": identity.get("Description") or row.get("title") or "UNKNOWN",
                "Manufacturer": identity.get("Manufacturer") or "UNKNOWN",
                "Part_number": identity.get("Manufacturer_part_number") or "UNKNOWN",
                "NSN": identity.get("NSN") or "UNKNOWN",
                "Quantity": identity.get("Quantity") or "UNKNOWN",
                "Unit": "EA",
                "Requirement_status": "REQUIRED",
                "config_type": "PRIMARY_EQUIPMENT",
                "Source_document": _source_label(row),
                "Confidence": BOM_UNKNOWN,
                "include_in_cost": False,
            }
        )

    confirmed = sum(1 for L in lines_out if L.get("Confidence") == BOM_CONFIRMED)
    likely = sum(1 for L in lines_out if L.get("Confidence") == BOM_LIKELY)
    unknown = sum(1 for L in lines_out if L.get("Confidence") == BOM_UNKNOWN)
    completeness_pct = int(round(100 * (confirmed + 0.5 * likely) / max(1, len(lines_out))))

    return {
        "kind": "PROCUREMENT_BOM",
        "lines": lines_out,
        "line_count": len(lines_out),
        "confirmed_lines": confirmed,
        "likely_lines": likely,
        "unknown_lines": unknown,
        "BOM_completeness_pct": completeness_pct,
        "cost_eligible_lines": [L for L in lines_out if L.get("include_in_cost")],
        "notes": ["UNKNOWN_items_excluded_from_cost_unless_approved"],
    }


# ---------------------------------------------------------------------------
# Phase 6 — commercial completeness
# ---------------------------------------------------------------------------

def commercial_completeness_score(
    commercial: dict[str, Any],
    identity: dict[str, Any],
    configuration: dict[str, Any],
    bom: dict[str, Any],
    market: dict[str, Any] | None = None,
) -> dict[str, Any]:
    flags = {
        "has_contract_value": (commercial.get("Contract_value_model") or {}).get("confidence") != VAL_UNKNOWN,
        "has_quantity": _known(commercial.get("Quantity")) or _known(identity.get("Quantity")),
        "has_exact_product_identity": identity.get("Identity_confidence") == MATCH_HIGH,
        "has_part_or_model": _known(identity.get("Manufacturer_part_number"))
        or _known(identity.get("Model_number"))
        or _known(identity.get("NSN")),
        "has_configuration": bool(configuration.get("PRIMARY_EQUIPMENT")),
        "has_bom": int(bom.get("line_count") or 0) >= 1 and int(bom.get("confirmed_lines") or 0) + int(bom.get("likely_lines") or 0) >= 1,
        "has_pricing": bool(market and market.get("primary_level") not in {None, PRICE_LEVEL_4_UNKNOWN}),
        "has_delivery": _known(commercial.get("Delivery_requirements"))
        or _known(commercial.get("Delivery_location")),
    }
    score = sum(12 if v else 0 for v in flags.values())
    # slight boosts
    if configuration.get("configuration_complete"):
        score += 4
    if identity.get("Identity_confidence") == MATCH_MEDIUM:
        score += 4
    score = min(100, score)

    missing = [k.replace("has_", "") for k, v in flags.items() if not v]

    docs_thin = commercial.get("Evidence_source") in {"solicitation_text", "award_or_value_metadata"} and not _line_items(
        {"line_items": commercial.get("Line_items")}
    )

    if (
        flags["has_contract_value"]
        and flags["has_quantity"]
        and flags["has_part_or_model"]
        and flags["has_pricing"]
        and identity.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM}
    ):
        status = READY_FOR_ECONOMICS
    elif (
        flags["has_contract_value"]
        and flags["has_part_or_model"]
        and identity.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM}
    ):
        status = READY_FOR_PRICING
    elif docs_thin and identity.get("Identity_confidence") in {MATCH_UNKNOWN, MATCH_LOW}:
        status = NEEDS_DOCUMENTS if not flags["has_contract_value"] else PARTIAL
    elif score >= 36:
        status = PARTIAL
    elif flags["has_contract_value"] or flags["has_part_or_model"] or flags["has_bom"]:
        status = PARTIAL
    else:
        status = INSUFFICIENT_DATA

    if not flags["has_contract_value"] and not flags["has_part_or_model"] and docs_thin:
        status = NEEDS_DOCUMENTS

    return {
        "kind": "COMMERCIAL_COMPLETENESS_SCORE",
        "COMMERCIAL_COMPLETENESS": status,
        "score": score,
        "flags": flags,
        "missing": missing,
    }


# ---------------------------------------------------------------------------
# Phase 7–8 — pricing readiness + economics handoff
# ---------------------------------------------------------------------------

def build_market_pricing_for_package(
    row: dict[str, Any],
    identity: dict[str, Any],
    *,
    allow_paid_web: bool = False,
) -> dict[str, Any]:
    """Exact product/config only — never generic category pricing."""
    match = {
        "PRODUCT_MATCH_CONFIDENCE": identity.get("Identity_confidence") or MATCH_UNKNOWN,
        "pricing_research_allowed": identity.get("sufficient_for_pricing_research") is True
        and identity.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM},
    }
    # Reuse product pricing market builder with a synthetic identity shape
    synth_identity = {
        "Manufacturer": identity.get("Manufacturer"),
        "Model_number": identity.get("Model_number"),
        "Manufacturer_part_number": identity.get("Manufacturer_part_number"),
        "NSN": identity.get("NSN"),
        "SKU": identity.get("SKU"),
        "Description": identity.get("Description"),
        "Identity_confidence": identity.get("Identity_confidence"),
        "sufficient_for_pricing_research": identity.get("sufficient_for_pricing_research"),
    }
    market = build_market_price_profile(row, synth_identity, match, allow_paid_web=allow_paid_web)
    # Annotate exact-match requirement
    market["pricing_query_standard"] = "EXACT_PRODUCT_AND_CONFIGURATION"
    market["generic_category_pricing_forbidden"] = True
    if identity.get("Identity_confidence") in {MATCH_LOW, MATCH_UNKNOWN}:
        market["primary_level"] = PRICE_LEVEL_4_UNKNOWN
        market["items"] = []
        market["research_blocked_reason"] = "identity_insufficient_for_exact_pricing"
    return market


def economics_handoff(
    row: dict[str, Any],
    commercial: dict[str, Any],
    identity: dict[str, Any],
    bom: dict[str, Any],
    market: dict[str, Any],
) -> dict[str, Any]:
    missing: list[str] = []
    contract = commercial.get("Contract_value_model") or {}
    if contract.get("confidence") == VAL_UNKNOWN:
        missing.append("contract value")
    if not _known(identity.get("Quantity")) and not _known(commercial.get("Quantity")):
        missing.append("quantity")
    if not _known(identity.get("Manufacturer_part_number")) and not _known(identity.get("NSN")):
        missing.append("exact part number")
    if market.get("primary_level") == PRICE_LEVEL_4_UNKNOWN or not market.get("items"):
        missing.append("acquisition price")
    # UNKNOWN BOM lines must not silently enter cost
    if any(L.get("Confidence") == BOM_UNKNOWN and L.get("config_type") != "PRIMARY_EQUIPMENT" for L in (bom.get("lines") or [])):
        # informational — not always blocking
        pass

    can_calc = (
        contract.get("confidence") != VAL_UNKNOWN
        and bool(market.get("items"))
        and market.get("primary_level") != PRICE_LEVEL_4_UNKNOWN
        and identity.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM}
    )

    economics = None
    if can_calc:
        try:
            from m3_deal_economics import build_deal_economics

            best = min(
                (e for e in market["items"] if _num(e.get("Price")) is not None),
                key=lambda e: float(e["Price"]),
                default=None,
            )
            enriched = {
                **row,
                "estimated_value": contract.get("contract_value")
                if contract.get("contract_value") != "UNKNOWN"
                else row.get("estimated_value"),
                "supplier_intelligence": {
                    **(row.get("supplier_intelligence") or {}),
                    "Pricing_evidence": {
                        "primary_level": market.get("primary_level"),
                        "items": [
                            {
                                "level": best.get("level") if best else market.get("primary_level"),
                                "amount": best.get("Price") if best else None,
                                "Source": best.get("Supplier_source") if best else None,
                                "Date": best.get("Date") if best else _utc(),
                                "Product_match_confidence": best.get("Confidence") if best else SCORE_LOW,
                            }
                        ]
                        if best
                        else [],
                    },
                    "cost_detail": {"best_price": best.get("Price") if best else None},
                    "ACQUISITION_COST_CONFIDENCE": market.get("PRICE_CONFIDENCE"),
                },
            }
            economics = build_deal_economics(enriched)
        except Exception as exc:
            economics = {"error": str(exc)}

    if can_calc:
        msg = "Economics calculable from complete package evidence"
    else:
        msg = "Cannot calculate:\nMissing:\n- " + "\n- ".join(missing or ["insufficient inputs"])

    return {
        "can_calculate": can_calc,
        "ready_for_economics": can_calc,
        "missing": missing,
        "message": msg,
        "deal_economics": economics,
        "Government_revenue": contract.get("contract_value"),
        "Quantity": identity.get("Quantity") if _known(identity.get("Quantity")) else commercial.get("Quantity"),
        "Current_market_cost": (market.get("items") or [{}])[0].get("Price") if market.get("items") else "UNKNOWN",
        "Pricing_confidence": market.get("PRICE_CONFIDENCE") or MATCH_UNKNOWN,
    }


# ---------------------------------------------------------------------------
# Phase 9 — research readiness + queues
# ---------------------------------------------------------------------------

def research_readiness_and_queue(
    commercial: dict[str, Any],
    identity: dict[str, Any],
    configuration: dict[str, Any],
    completeness: dict[str, Any],
    market: dict[str, Any],
) -> dict[str, Any]:
    score = 0
    if identity.get("Identity_confidence") == MATCH_HIGH:
        score += 30
    elif identity.get("Identity_confidence") == MATCH_MEDIUM:
        score += 20
    elif identity.get("Identity_confidence") == MATCH_LOW:
        score += 8

    if (commercial.get("Contract_value_model") or {}).get("confidence") == VAL_VERIFIED:
        score += 25
    elif (commercial.get("Contract_value_model") or {}).get("confidence") == VAL_ESTIMATED:
        score += 15

    if _known(identity.get("Quantity")) or _known(commercial.get("Quantity")):
        score += 10
    if configuration.get("configuration_complete"):
        score += 15
    elif configuration.get("PRIMARY_EQUIPMENT"):
        score += 5
    if market.get("primary_level") != PRICE_LEVEL_4_UNKNOWN:
        score += 15
    if completeness.get("flags", {}).get("has_delivery"):
        score += 5

    score = min(100, score)
    status = completeness.get("COMMERCIAL_COMPLETENESS") or INSUFFICIENT_DATA

    # Queue placement (do not delete incomplete)
    if status == READY_FOR_ECONOMICS or (
        status == READY_FOR_PRICING and market.get("primary_level") == PRICE_LEVEL_4_UNKNOWN
    ):
        queue = Q_NEEDS_PRICING if market.get("primary_level") == PRICE_LEVEL_4_UNKNOWN else Q_READY
        if status == READY_FOR_ECONOMICS:
            queue = Q_READY
    elif identity.get("Identity_confidence") in {MATCH_UNKNOWN, MATCH_LOW}:
        queue = Q_NEEDS_PRODUCT_IDENTITY
    elif not configuration.get("configuration_complete") and any(
        (a.get("Item") == "ACCESSORIES_REFERENCED_BUT_NOT_ENUMERATED")
        for a in (configuration.get("REQUIRED_ACCESSORIES") or [])
    ):
        queue = Q_NEEDS_CONFIGURATION
    elif status == NEEDS_DOCUMENTS:
        queue = Q_NEEDS_DOCUMENT_RECOVERY
    elif not _known(identity.get("Manufacturer_part_number")) and not _known(identity.get("NSN")):
        queue = Q_NEEDS_PRODUCT_IDENTITY
    elif market.get("primary_level") == PRICE_LEVEL_4_UNKNOWN and identity.get("sufficient_for_pricing_research"):
        queue = Q_NEEDS_PRICING
    else:
        queue = Q_NEEDS_DOCUMENT_RECOVERY if status in {NEEDS_DOCUMENTS, INSUFFICIENT_DATA} else Q_NEEDS_CONFIGURATION

    return {
        "RESEARCH_READINESS_SCORE": score,
        "RESEARCH_QUEUE": queue,
        "COMMERCIAL_COMPLETENESS": status,
    }


def next_package_action(readiness: dict[str, Any], identity: dict[str, Any], economics: dict[str, Any]) -> str:
    q = readiness.get("RESEARCH_QUEUE")
    if q == Q_NEEDS_PRODUCT_IDENTITY:
        return "Recover solicitation attachments and extract exact manufacturer part / model / NSN"
    if q == Q_NEEDS_DOCUMENT_RECOVERY:
        return "Recover full solicitation package (PDF/BOM/pricing schedule)"
    if q == Q_NEEDS_CONFIGURATION:
        return "Extract required accessories/options from technical exhibits — do not assume"
    if q == Q_NEEDS_PRICING:
        return "Research exact-configuration distributor pricing (no generic category quotes)"
    if readiness.get("COMMERCIAL_COMPLETENESS") == READY_FOR_ECONOMICS and economics.get("can_calculate"):
        return "Review deal economics and profit target status"
    if economics.get("missing"):
        return "Fill missing: " + ", ".join(economics["missing"][:4])
    return "Continue package completion research"


# ---------------------------------------------------------------------------
# Full package builder
# ---------------------------------------------------------------------------

def build_procurement_package(row: dict[str, Any], *, allow_paid_web: bool = False) -> dict[str, Any]:
    commercial = build_commercial_requirement_profile(row)
    identity = build_package_product_identity(row)
    # Align quantity from commercial if identity missing
    if not _known(identity.get("Quantity")) and _known(commercial.get("Quantity")):
        identity["Quantity"] = commercial.get("Quantity")
    configuration = build_product_configuration_profile(row, identity)
    bom = build_procurement_bom(row, identity, configuration)
    market = build_market_pricing_for_package(row, identity, allow_paid_web=allow_paid_web)
    completeness = commercial_completeness_score(commercial, identity, configuration, bom, market)
    economics = economics_handoff(row, commercial, identity, bom, market)
    readiness = research_readiness_and_queue(commercial, identity, configuration, completeness, market)
    action = next_package_action(readiness, identity, economics)

    package_complete = (
        completeness.get("COMMERCIAL_COMPLETENESS") in {READY_FOR_ECONOMICS, READY_FOR_PRICING}
        and identity.get("Identity_confidence") in {MATCH_HIGH, MATCH_MEDIUM}
        and configuration.get("configuration_complete")
    )

    return {
        "kind": "M3ProcurementPackageIntelligence",
        "generated_at": _utc(),
        "COMMERCIAL_REQUIREMENT": commercial,
        "PRODUCT_IDENTITY": identity,
        "PRODUCT_CONFIGURATION": configuration,
        "PROCUREMENT_BOM": bom,
        "COMMERCIAL_COMPLETENESS": completeness,
        "MARKET_PRICING": market,
        "ECONOMICS_HANDOFF": economics,
        "RESEARCH_READINESS": readiness,
        "package_complete": package_complete,
        "Next_Action": action,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


# ---------------------------------------------------------------------------
# Durable index + analyze
# ---------------------------------------------------------------------------

def load_package_index() -> dict[str, Any]:
    try:
        from database import SessionLocal
        from models import AppSetting

        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == PACKAGE_INDEX_KEY).one_or_none()
            if not row or not row.value:
                return {}
            data = json.loads(row.value)
            return data if isinstance(data, dict) else {}
        finally:
            db.close()
    except Exception:
        log.exception("load package index failed")
        return {}


def save_package_index(by_id: dict[str, Any]) -> bool:
    try:
        from database import SessionLocal
        from models import AppSetting

        payload = {"kind": "M3ProcurementPackageIndex", "updated_at": _utc(), "by_id": by_id, "count": len(by_id)}
        raw = json.dumps(payload, default=str)
        db = SessionLocal()
        try:
            row = db.query(AppSetting).filter(AppSetting.key == PACKAGE_INDEX_KEY).one_or_none()
            if row:
                row.value = raw
            else:
                db.add(AppSetting(key=PACKAGE_INDEX_KEY, value=raw))
            db.commit()
            return True
        finally:
            db.close()
    except Exception:
        log.exception("save package index failed")
        return False


def get_persisted_package(canonical_id: str) -> dict[str, Any] | None:
    data = load_package_index()
    by_id = data.get("by_id") if isinstance(data.get("by_id"), dict) else {}
    v = by_id.get(canonical_id)
    return v if isinstance(v, dict) else None


def _priority_seed(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    scored = []
    for r in rows:
        if not r.get("canonical_id"):
            continue
        ci = r.get("commercial_intelligence") or {}
        ei = r.get("execution_intelligence") or {}
        commercial = 70 if (ci.get("COMMERCIAL_OPPORTUNITY_SCORE") or r.get("commercial_opportunity_score")) == SCORE_HIGH else (
            50 if (ci.get("COMMERCIAL_OPPORTUNITY_SCORE") or r.get("commercial_opportunity_score")) == SCORE_MEDIUM else 20
        )
        execution = int((ei.get("EXECUTION") or {}).get("EXECUTION_SCORE") or 0)
        # preview readiness without paid web
        try:
            ident = build_package_product_identity(r)
            comm = build_commercial_requirement_profile(r)
            cfg = build_product_configuration_profile(r, ident)
            bom = build_procurement_bom(r, ident, cfg)
            comp = commercial_completeness_score(comm, ident, cfg, bom, None)
            ready = research_readiness_and_queue(comm, ident, cfg, comp, {"primary_level": PRICE_LEVEL_4_UNKNOWN})
            rs = int(ready.get("RESEARCH_READINESS_SCORE") or 0)
        except Exception:
            rs = 0
        # potential profit proxy: known value only (not invented)
        value = _num((r.get("estimated_value") or r.get("award_amount")))
        value_boost = 10 if value and value >= 50000 else (5 if value else 0)
        scored.append((commercial + execution + rs + value_boost, r))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:limit]]


def analyze_procurement_packages_top(
    store: Any,
    *,
    limit: int = 25,
    allow_paid_web: bool = False,
    paid_limit: int = 3,
) -> dict[str, Any]:
    rows = store.all() if hasattr(store, "all") else list(store)

    try:
        from m3_supplier_intelligence import get_persisted_supplier_intelligence
        from m3_execution_intelligence import get_persisted_execution
        from m3_competitive_intelligence import get_persisted_competitive
        from m3_deal_economics import get_persisted_economics
        from m3_product_pricing import get_persisted_product
    except Exception:
        get_persisted_supplier_intelligence = get_persisted_execution = None  # type: ignore
        get_persisted_competitive = get_persisted_economics = get_persisted_product = None  # type: ignore

    enriched = []
    for r in rows:
        row = dict(r)
        cid = str(row.get("canonical_id") or "")
        for key, loader in (
            ("supplier_intelligence", get_persisted_supplier_intelligence),
            ("execution_intelligence", get_persisted_execution),
            ("competitive_intelligence", get_persisted_competitive),
            ("deal_economics", get_persisted_economics),
            ("product_pricing_intelligence", get_persisted_product),
        ):
            if not row.get(key) and callable(loader):
                try:
                    got = loader(cid)
                    if got:
                        row[key] = got
                except Exception:
                    pass
        enriched.append(row)

    targets = _priority_seed(enriched, limit)
    results = []
    index_updates: dict[str, Any] = {}
    identity_counts: Counter[str] = Counter()
    completeness_counts: Counter[str] = Counter()
    queue_counts: Counter[str] = Counter()
    level_counts = {"LEVEL_1": 0, "LEVEL_2": 0, "LEVEL_3": 0, "LEVEL_4": 0}
    complete_pkgs = 0
    partial_pkgs = 0
    missing_docs = 0
    config_complete = 0
    config_incomplete = 0
    boms_created = 0
    econ_ready = 0
    paid_total = 0
    openai_total = 0
    paid_used = 0

    for row in targets:
        use_paid = allow_paid_web and paid_used < paid_limit
        ident_preview = build_package_product_identity(row)
        if use_paid and ident_preview.get("sufficient_for_pricing_research"):
            pkg = build_procurement_package(row, allow_paid_web=True)
            wr = (pkg.get("MARKET_PRICING") or {}).get("web_research") or {}
            if wr.get("paid"):
                paid_used += 1
        else:
            pkg = build_procurement_package(row, allow_paid_web=False)

        index_updates[row["canonical_id"]] = pkg
        full = {**(store.get(row["canonical_id"]) or row)}
        full["procurement_package_intelligence"] = pkg
        store._rows[row["canonical_id"]] = full

        identity = pkg.get("PRODUCT_IDENTITY") or {}
        conf = identity.get("Identity_confidence") or MATCH_UNKNOWN
        identity_counts[conf] += 1
        comp_st = (pkg.get("COMMERCIAL_COMPLETENESS") or {}).get("COMMERCIAL_COMPLETENESS") or INSUFFICIENT_DATA
        completeness_counts[comp_st] += 1
        q = (pkg.get("RESEARCH_READINESS") or {}).get("RESEARCH_QUEUE") or "UNKNOWN"
        queue_counts[q] += 1

        if pkg.get("package_complete"):
            complete_pkgs += 1
        elif comp_st in {PARTIAL, READY_FOR_PRICING, NEEDS_DOCUMENTS}:
            partial_pkgs += 1
        if comp_st == NEEDS_DOCUMENTS:
            missing_docs += 1

        cfg = pkg.get("PRODUCT_CONFIGURATION") or {}
        if cfg.get("configuration_complete"):
            config_complete += 1
        else:
            config_incomplete += 1

        bom = pkg.get("PROCUREMENT_BOM") or {}
        if bom.get("lines"):
            boms_created += 1

        lvl = str((pkg.get("MARKET_PRICING") or {}).get("primary_level") or PRICE_LEVEL_4_UNKNOWN)
        if "LEVEL_1" in lvl:
            level_counts["LEVEL_1"] += 1
        elif "LEVEL_2" in lvl:
            level_counts["LEVEL_2"] += 1
        elif "LEVEL_3" in lvl:
            level_counts["LEVEL_3"] += 1
        else:
            level_counts["LEVEL_4"] += 1

        wr = (pkg.get("MARKET_PRICING") or {}).get("web_research") or {}
        paid_total += int(wr.get("paid") or 0)
        openai_total += int(wr.get("OpenAI") or 0)
        if (pkg.get("ECONOMICS_HANDOFF") or {}).get("can_calculate"):
            econ_ready += 1

        accessories = cfg.get("REQUIRED_ACCESSORIES") or []
        acc_summary = [
            a.get("Item") for a in accessories if a.get("Item") != "ACCESSORIES_REFERENCED_BUT_NOT_ENUMERATED"
        ][:5] or ("REFERENCED_UNKNOWN" if accessories else "NONE_EVIDENCED")

        results.append(
            {
                "canonical_id": row.get("canonical_id"),
                "Opportunity": row.get("title"),
                "Contract_value": (pkg.get("COMMERCIAL_REQUIREMENT") or {}).get("Estimated_contract_value"),
                "Exact_product": identity.get("Description"),
                "Manufacturer": identity.get("Manufacturer"),
                "Part_number": identity.get("Manufacturer_part_number"),
                "Model": identity.get("Model_number"),
                "Quantity": identity.get("Quantity"),
                "Accessories": acc_summary,
                "BOM_completeness": bom.get("BOM_completeness_pct"),
                "BOM_lines": bom.get("line_count"),
                "Identity_confidence": conf,
                "Configuration_complete": cfg.get("configuration_complete"),
                "Pricing_readiness": (pkg.get("COMMERCIAL_COMPLETENESS") or {}).get("COMMERCIAL_COMPLETENESS"),
                "Pricing_level": lvl,
                "Economic_readiness": READY_FOR_ECONOMICS
                if (pkg.get("ECONOMICS_HANDOFF") or {}).get("can_calculate")
                else (pkg.get("COMMERCIAL_COMPLETENESS") or {}).get("COMMERCIAL_COMPLETENESS"),
                "Research_queue": q,
                "Research_score": (pkg.get("RESEARCH_READINESS") or {}).get("RESEARCH_READINESS_SCORE"),
                "Missing": (pkg.get("ECONOMICS_HANDOFF") or {}).get("missing")
                or (pkg.get("COMMERCIAL_COMPLETENESS") or {}).get("missing"),
                "Next_action": pkg.get("Next_Action"),
                "package_complete": pkg.get("package_complete"),
            }
        )

    results.sort(
        key=lambda r: (
            0 if r.get("Economic_readiness") == READY_FOR_ECONOMICS else 1,
            0 if r.get("Pricing_readiness") == READY_FOR_PRICING else 1,
            -int(r.get("Research_score") or 0),
        )
    )

    try:
        existing = load_package_index()
        by = existing.get("by_id") if isinstance(existing.get("by_id"), dict) else {}
        by.update(index_updates)
        save_package_index(by)
    except Exception:
        log.exception("package index save failed")
    try:
        store.save()
    except Exception:
        log.exception("pipeline save failed")

    missing_inputs = Counter()
    for r in results:
        for m in r.get("Missing") or []:
            missing_inputs[str(m)] += 1

    return {
        "kind": "M3ProcurementPackageRun",
        "generated_at": _utc(),
        "analyzed": len(results),
        "complete_commercial_packages": complete_pkgs,
        "partial_packages": partial_pkgs,
        "missing_documents": missing_docs,
        "product_identity": {
            "exact_matches": identity_counts.get(MATCH_HIGH, 0),
            "partial_matches": identity_counts.get(MATCH_MEDIUM, 0) + identity_counts.get(MATCH_LOW, 0),
            "unknown": identity_counts.get(MATCH_UNKNOWN, 0),
            "breakdown": dict(identity_counts),
        },
        "configuration": {
            "complete_configurations": config_complete,
            "incomplete_configurations": config_incomplete,
            "boms_created": boms_created,
        },
        "pricing_levels": level_counts,
        "economics": {
            "ready_for_economics": econ_ready,
            "blocked": len(results) - econ_ready,
            "missing_inputs": dict(missing_inputs),
        },
        "completeness_breakdown": dict(completeness_counts),
        "research_queues": dict(queue_counts),
        "TOP_OPPORTUNITIES": results[:10],
        "ALL_SCORED": results,
        "OpenAI": openai_total,
        "paid": paid_total,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def deal_room_procurement_package_section(row: dict[str, Any]) -> dict[str, Any]:
    pkg = row.get("procurement_package_intelligence")
    if not isinstance(pkg, dict) or pkg.get("kind") != "M3ProcurementPackageIntelligence":
        persisted = get_persisted_package(str(row.get("canonical_id") or ""))
        if isinstance(persisted, dict) and persisted.get("kind") == "M3ProcurementPackageIntelligence":
            pkg = persisted
        else:
            pkg = build_procurement_package(row, allow_paid_web=False)

    commercial = pkg.get("COMMERCIAL_REQUIREMENT") or {}
    identity = pkg.get("PRODUCT_IDENTITY") or {}
    cfg = pkg.get("PRODUCT_CONFIGURATION") or {}
    bom = pkg.get("PROCUREMENT_BOM") or {}
    market = pkg.get("MARKET_PRICING") or {}
    completeness = pkg.get("COMMERCIAL_COMPLETENESS") or {}
    economics = pkg.get("ECONOMICS_HANDOFF") or {}
    readiness = pkg.get("RESEARCH_READINESS") or {}

    return {
        "kind": "M3DealRoomProcurementPackage",
        "Contract_information": {
            "Solicitation_ID": commercial.get("Solicitation_ID"),
            "Agency": commercial.get("Agency"),
            "Buyer": commercial.get("Buyer"),
            "Contract_type": commercial.get("Contract_type"),
            "Estimated_value": commercial.get("Estimated_contract_value"),
            "Award_value": commercial.get("Award_value"),
            "Delivery": commercial.get("Delivery_location"),
            "Confidence": commercial.get("Confidence"),
        },
        "Required_products": [p.get("Item") for p in (cfg.get("PRIMARY_EQUIPMENT") or [])][:5],
        "Exact_models": identity.get("Model_number"),
        "Part_numbers": identity.get("Manufacturer_part_number"),
        "NSN": identity.get("NSN"),
        "Manufacturer": identity.get("Manufacturer"),
        "Quantity": identity.get("Quantity"),
        "Accessories": [
            a.get("Item") for a in (cfg.get("REQUIRED_ACCESSORIES") or [])
        ][:8],
        "BOM": (bom.get("lines") or [])[:20],
        "BOM_completeness_pct": bom.get("BOM_completeness_pct"),
        "Configuration_completeness": "COMPLETE" if cfg.get("configuration_complete") else "INCOMPLETE",
        "Identity_confidence": identity.get("Identity_confidence"),
        "Pricing_readiness": completeness.get("COMMERCIAL_COMPLETENESS"),
        "Pricing_evidence": {
            "level": market.get("primary_level"),
            "items": (market.get("items") or [])[:5],
        },
        "Missing_information": economics.get("missing") or completeness.get("missing") or [],
        "Economics_readiness": READY_FOR_ECONOMICS if economics.get("can_calculate") else completeness.get("COMMERCIAL_COMPLETENESS"),
        "Economics_message": economics.get("message"),
        "Research_queue": readiness.get("RESEARCH_QUEUE"),
        "Research_score": readiness.get("RESEARCH_READINESS_SCORE"),
        "Next_Action": pkg.get("Next_Action"),
        "full": pkg,
    }
