"""DLA / Federal product structure extraction — feeds existing M3 systems."""

from __future__ import annotations

import re
from typing import Any

NSN_RE = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")
PN_RE = re.compile(
    r"(?:(?:P/?N|PART\s*(?:NUMBER|NO\.?)|MFG\s*P/?N|MANUFACTURER\s*PART)\s*[:#]?\s*)([A-Z0-9][A-Z0-9\-/.]{2,32})",
    re.I,
)
CAGE_RE = re.compile(r"\bCAGE(?:\s*CODE)?\s*[:#]?\s*([A-Z0-9]{5})\b", re.I)
QTY_RE = re.compile(r"\b(?:QTY|QUANTITY|QTY\.)\s*[:#]?\s*([0-9]{1,7}(?:,[0-9]{3})*)\b", re.I)
UOI_RE = re.compile(r"\b(?:UNIT\s*OF\s*(?:ISSUE|MEASURE)|UOI|UOM)\s*[:#]?\s*([A-Z]{2})\b", re.I)
DELIVERY_RE = re.compile(r"\b(?:DELIVERY|DAYS\s*ARO|ARO)\s*[:#]?\s*([0-9]{1,4})\s*DAYS?\b", re.I)
APPROVED_SRC_RE = re.compile(r"approved\s+source|qualified\s+source|qpl|source\s+controlled", re.I)
IDC_RE = re.compile(r"\b(?:IDC|INDEFINITE\s+DELIVERY|LONG[\s-]TERM\s+CONTRACT)\b", re.I)
SET_ASIDE_RE = re.compile(r"set[\s-]?aside|small\s+business|hubzone|8\(a\)|sdvosb|wosb", re.I)


def extract_dla_product_structure(row: dict[str, Any]) -> dict[str, Any]:
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    blob = f"{title}\n{desc}\n{meta.get('nomenclature') or ''}"
    nsn = NSN_RE.search(blob)
    pn = PN_RE.search(blob)
    # Also bare P/N-like tokens near NSN titles
    if not pn:
        m = re.search(r"\b([A-Z]{1,3}[-/]?\d{2,}[A-Z0-9\-/]{0,20})\b", title)
        if m and "SPE" not in m.group(1).upper() and "SPR" not in m.group(1).upper():
            pn = m
    cage = CAGE_RE.search(blob)
    qty = QTY_RE.search(blob)
    uoi = UOI_RE.search(blob)
    delivery = DELIVERY_RE.search(blob)
    approved = bool(APPROVED_SRC_RE.search(blob))
    idc = bool(IDC_RE.search(blob))
    set_aside = bool(SET_ASIDE_RE.search(blob) or row.get("set_aside"))
    package = str(row.get("package_access") or meta.get("document_access") or "PUBLIC_METADATA_ONLY")
    controlled = package.upper() in {
        "CONTROLLED_ATTACHMENT",
        "JCP_REQUIRED",
        "EJCP_REQUIRED",
        "CAGE_REQUIRED",
        "PIEE_REQUIRED",
        "DIBBS_REGISTRATION_REQUIRED",
        "AUTH_GATED",
        "AUTH_REQUIRED",
    }
    return {
        "nsn": nsn.group(1) if nsn else None,
        "part_number": pn.group(1) if pn else None,
        "cage": cage.group(1).upper() if cage else None,
        "quantity": int(qty.group(1).replace(",", "")) if qty else None,
        "unit_of_issue": uoi.group(1).upper() if uoi else None,
        "delivery_days": int(delivery.group(1)) if delivery else None,
        "approved_source_signal": approved,
        "idc_or_ltc_signal": idc,
        "set_aside_signal": set_aside,
        "nomenclature": title[:240] if title else None,
        "package_access": package,
        "public_package_available": package.upper() in {"PUBLIC_DIRECT", "PUBLIC_DETAIL_PAGE", "PUBLIC_PACKAGE_AVAILABLE"},
        "controlled_or_auth_package": controlled,
        "has_exact_nsn": bool(nsn),
        "has_exact_pn": bool(pn),
        "has_quantity": bool(qty),
    }


def enrich_with_dla_structure(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    struct = extract_dla_product_structure(out)
    out["dla_product_structure"] = struct
    out["exact_nsn"] = struct.get("nsn")
    out["exact_part_number"] = struct.get("part_number")
    out["quantity"] = struct.get("quantity")
    # Preserve into raw_metadata for downstream document/economics
    meta = dict(out.get("raw_metadata") or {})
    meta["dla_product_structure"] = struct
    out["raw_metadata"] = meta
    return out


def classify_federal_product_cheap(row: dict[str, Any]) -> dict[str, Any]:
    """Deterministic Federal product cheap screen — UNKNOWN preserved."""
    from discovery.classify import classify_discovery_opportunity

    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    struct = row.get("dla_product_structure") or extract_dla_product_structure(row)
    base = classify_discovery_opportunity(title=title, description=desc, status=row.get("status"))
    cls = str(base.get("classification") or "UNKNOWN")
    # Strengthen with DLA/Federal structure signals
    strong = (
        bool(struct.get("has_exact_nsn"))
        or bool(struct.get("has_exact_pn"))
        or bool(struct.get("has_quantity") and struct.get("unit_of_issue"))
        or bool(struct.get("approved_source_signal"))
        or bool(re.search(r"proposed\s+procurement\s+for\s+nsn|supplies|equipment|hardware|parts?\b", f"{title} {desc}", re.I))
    )
    if strong and cls in {"UNKNOWN", "SERVICE", "CLEARLY_IRRELEVANT"}:
        # Don't force SERVICE into product; UNKNOWN/irrelevant with strong NSN → product-likely
        if cls == "SERVICE" and not struct.get("has_exact_nsn"):
            pass
        else:
            cls = "CORE_PRODUCT"
    if cls == "CORE_PRODUCT":
        bucket = "FEDERAL_PRODUCT_LIKELY"
    elif cls == "PRODUCT_PLUS_SERVICE":
        bucket = "FEDERAL_PRODUCT_PLUS_MINOR_SERVICE"
    elif cls == "SERVICE":
        bucket = "FEDERAL_SERVICE"
    elif "CONSTRUCTION" in cls:
        bucket = "FEDERAL_CONSTRUCTION"
    elif cls in {"MIXED"}:
        bucket = "FEDERAL_MIXED"
    else:
        bucket = "FEDERAL_UNKNOWN"
    return {
        "product_classification": cls,
        "federal_product_class": bucket,
        "cheap_screen_class": {
            "CORE_PRODUCT": "LIKELY_PRODUCT_RESALE",
            "PRODUCT_PLUS_SERVICE": "PRODUCT_PLUS_MINOR_SERVICE",
            "SERVICE": "LIKELY_SERVICE",
            "UNKNOWN": "UNKNOWN",
        }.get(cls, "UNKNOWN"),
        "structure_signals": {
            "nsn": struct.get("has_exact_nsn"),
            "pn": struct.get("has_exact_pn"),
            "qty": struct.get("has_quantity"),
            "approved_source": struct.get("approved_source_signal"),
        },
        "unknown_preserved": bucket == "FEDERAL_UNKNOWN",
        "reject_unknown": False,
    }
