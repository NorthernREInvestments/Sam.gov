"""DLA / Federal product structure extraction — feeds existing M3 systems.

Deterministic-first extraction with evidence snippets. UNKNOWN is valid.
"""

from __future__ import annotations

import re
from typing import Any

from federal_dla_product_constants import (
    ID_AMBIGUOUS,
    ID_CATEGORY_IDENTIFIED,
    ID_EXACT_APPROVED_SOURCE_PART,
    ID_EXACT_NSN,
    ID_EXACT_OEM_PART,
    ID_UNKNOWN,
    READY_BLOCKED_CONTROLLED,
    READY_BLOCKED_MISSING_PACKAGE,
    READY_COMMERCIAL_RESEARCH,
    READY_DESCRIPTION_RECOVERED,
    READY_DISCOVERED_ONLY,
    READY_ECONOMIC_POTENTIAL_DOC_ACCESS,
    READY_HISTORICAL_RESEARCH,
    READY_HUMAN_DOCUMENT_ACCESS,
    READY_PACKAGE_PARTIAL,
    READY_PACKAGE_RECOVERED,
    READY_PRODUCT_IDENTITY_EXACT,
    READY_PRODUCT_IDENTITY_PARTIAL,
    READY_TRANSACTION_STRUCTURE_COMPLETE,
    READY_TRANSACTION_STRUCTURE_PARTIAL,
    STRUCT_IDC,
    STRUCT_MULTI_LINE_RFQ,
    STRUCT_SIDC,
    STRUCT_SINGLE_ITEM_RFQ,
    STRUCT_UNKNOWN,
)

NSN_RE = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")
NSN_COMPACT_RE = re.compile(r"\bNSN\s*[:#]?\s*(\d{13})\b", re.I)
NIIN_RE = re.compile(r"\bNIIN\s*[:#]?\s*(\d{9})\b", re.I)
PN_RE = re.compile(
    r"(?:(?:P/?N|PART\s*(?:NUMBER|NO\.?)|MFG\s*P/?N|MANUFACTURER\s*PART)\s*[:#]?\s*)([A-Z0-9][A-Z0-9\-/.]{2,32})",
    re.I,
)
CAGE_RE = re.compile(r"\bCAGE(?:\s*CODE)?\s*[:#]?\s*([A-Z0-9]{5})\b", re.I)
OEM_RE = re.compile(
    r"(?:(?:OEM|MANUFACTURER|MFR|MFG)\s*[:#]?\s*)([A-Z][A-Z0-9 &.,\-]{2,60})",
    re.I,
)
QTY_RE = re.compile(
    r"\b(?:QTY|QUANTITY|QTY\.|Qty)\s*[:#]?\s*([0-9]{1,7}(?:,[0-9]{3})*)\b|"
    r"\bLine\s+\d+\s+Qty\s+([0-9]{1,7})\b",
    re.I,
)
UOI_RE = re.compile(
    r"\b(?:UNIT\s*OF\s*(?:ISSUE|MEASURE)|UOI|UOM|UI)\s*[:#]?\s*([A-Z]{2,3})\b",
    re.I,
)
CLIN_RE = re.compile(r"\bCLIN\s*[:#]?\s*([0-9A-Z]{4,6})\b", re.I)
LINE_ITEM_RE = re.compile(r"\bLine\s+(\d{4})\b", re.I)
DELIVERY_RE = re.compile(r"\b(?:DELIVERY|DAYS\s*ARO|ARO)\s*[:#]?\s*([0-9]{1,4})\s*DAYS?\b", re.I)
FOB_RE = re.compile(r"\bFOB\s*[:#]?\s*(ORIGIN|DESTINATION)\b", re.I)
APPROVED_SRC_RE = re.compile(
    r"approved\s+source|source\s+controlled|qualified\s+source|qpl|qml|qsl|"
    r"source\s+approval\s+required|exact\s+part|brand[\s-]name[\s-]only|"
    r"authorized\s+distributor|traceability\s+required",
    re.I,
)
ALTERNATE_RE = re.compile(r"alternate\s+offer|brand[\s-]name[\s-]or[\s-]equal|or\s+equal", re.I)
FAT_RE = re.compile(r"\bFAT\b|first\s+article\s+test", re.I)
FAT_WAIVER_RE = re.compile(r"FAT\s+waiver|waiver\s+of\s+FAT|first\s+article\s+waiver", re.I)
IDC_RE = re.compile(r"\b(?:IDC|INDEFINITE\s+DELIVERY|LONG[\s-]TERM\s+CONTRACT)\b", re.I)
SIDC_RE = re.compile(r"\bSIDC\b|single\s+award\s+IDC", re.I)
SET_ASIDE_RE = re.compile(r"set[\s-]?aside|small\s+business|hubzone|8\(a\)|sdvosb|wosb|nmr", re.I)
PACK_RE = re.compile(r"MIL-STD-2073|MIL-STD-129|preservation|packaging|packing|marking|pallet", re.I)
INSPECT_RE = re.compile(r"inspection\s+(?:at|point)|acceptance\s+(?:at|point)|source\s+inspection", re.I)
SUB_DIBBS_RE = re.compile(r"dibbs|quote\s+via\s+dibbs", re.I)
SUB_EMAIL_RE = re.compile(r"submit(?:tal)?\s+by\s+e-?mail|email\s+quote|quotes?\s+to\s+\S+@", re.I)
SUB_SAM_RE = re.compile(r"submit\s+via\s+sam|sam\.gov\s+submission", re.I)
AUTO_ACQ_RE = re.compile(r"automated\s+acquisition|auto[\s-]solicitation", re.I)
MANUAL_EVAL_RE = re.compile(r"manual\s+evaluation|best\s+value|trade[\s-]?off", re.I)
PRICE_ONLY_RE = re.compile(r"price\s+only|lowest\s+price|award\s+based\s+on\s+price", re.I)
REV_AUCTION_RE = re.compile(r"reverse\s+auction", re.I)
GUAR_MIN_RE = re.compile(r"guaranteed\s+minimum|minimum\s+guarantee", re.I)
MAX_QTY_RE = re.compile(r"maximum\s+quantity|not\s+to\s+exceed\s+quantity", re.I)
JCP_RE = re.compile(r"\bJCP\b|export\s+control|controlled\s+technical\s+data", re.I)


def _ev(value: Any, *, conf: str, source: str, snippet: str, method: str = "regex") -> dict[str, Any]:
    return {
        "value": value,
        "confidence": conf,
        "evidence_source": source,
        "evidence_snippet": (snippet or "")[:160],
        "extraction_method": method,
    }


def _blob(row: dict[str, Any]) -> str:
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    recovered = str(row.get("recovered_description") or row.get("description_text") or "")
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    return f"{title}\n{desc}\n{recovered}\n{meta.get('nomenclature') or ''}"


def extract_dla_product_structure(row: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible structure + expanded fields."""
    title = str(row.get("title") or "")
    blob = _blob(row)
    source = "title+description"
    nsn_m = NSN_RE.search(blob)
    if not nsn_m:
        compact = NSN_COMPACT_RE.search(blob)
        if compact:
            raw = compact.group(1)
            # Format 13-digit NSN as ####-##-###-####
            dashed = f"{raw[0:4]}-{raw[4:6]}-{raw[6:9]}-{raw[9:13]}"
            class _M:
                def group(self, i=0):
                    return dashed if i == 1 else compact.group(0)
            nsn_m = _M()
    niin_m = NIIN_RE.search(blob)
    pn_m = PN_RE.search(blob)
    if not pn_m:
        m = re.search(r"\b([A-Z]{1,3}[-/]?\d{2,}[A-Z0-9\-/]{0,20})\b", title)
        if m and "SPE" not in m.group(1).upper() and "SPR" not in m.group(1).upper():
            pn_m = m
            source = "title_token"
    cage_m = CAGE_RE.search(blob)
    oem_m = OEM_RE.search(blob)
    qty_m = QTY_RE.search(blob)
    uoi_m = UOI_RE.search(blob)
    qty_val = None
    if qty_m:
        raw_q = qty_m.group(1) or qty_m.group(2)
        if raw_q:
            qty_val = int(str(raw_q).replace(",", ""))
    delivery_m = DELIVERY_RE.search(blob)
    clin_ms = list(CLIN_RE.finditer(blob))
    if not clin_ms:
        clin_ms = list(LINE_ITEM_RE.finditer(blob))
    approved = bool(APPROVED_SRC_RE.search(blob))
    alternate = bool(ALTERNATE_RE.search(blob))
    fat = bool(FAT_RE.search(blob))
    fat_waiver = bool(FAT_WAIVER_RE.search(blob))
    idc = bool(IDC_RE.search(blob))
    sidc = bool(SIDC_RE.search(blob))
    set_aside = bool(SET_ASIDE_RE.search(blob) or row.get("set_aside"))
    packaging = bool(PACK_RE.search(blob))
    inspection = bool(INSPECT_RE.search(blob))
    fob_m = FOB_RE.search(blob)
    package = str(row.get("package_access") or (row.get("raw_metadata") or {}).get("document_access") or "PUBLIC_METADATA_ONLY")
    controlled = package.upper() in {
        "CONTROLLED_ATTACHMENT",
        "JCP_REQUIRED",
        "EJCP_REQUIRED",
        "CAGE_REQUIRED",
        "PIEE_REQUIRED",
        "DIBBS_REGISTRATION_REQUIRED",
        "AUTH_GATED",
        "AUTH_REQUIRED",
    } or bool(JCP_RE.search(blob))

    fields = {
        "nsn": _ev(nsn_m.group(1), conf="HIGH", source=source, snippet=nsn_m.group(0)) if nsn_m else None,
        "niin": _ev(niin_m.group(1), conf="HIGH", source=source, snippet=niin_m.group(0)) if niin_m else None,
        "part_number": _ev(pn_m.group(1), conf="HIGH" if "PART" in (pn_m.group(0).upper()) else "MEDIUM", source=source, snippet=pn_m.group(0))
        if pn_m
        else None,
        "cage": _ev(cage_m.group(1).upper(), conf="HIGH", source=source, snippet=cage_m.group(0)) if cage_m else None,
        "oem": _ev(oem_m.group(1).strip()[:80], conf="MEDIUM", source=source, snippet=oem_m.group(0)) if oem_m else None,
        "quantity": _ev(qty_val, conf="HIGH", source=source, snippet=qty_m.group(0)) if qty_m and qty_val is not None else None,
        "unit_of_issue": _ev(uoi_m.group(1).upper(), conf="HIGH", source=source, snippet=uoi_m.group(0)) if uoi_m else None,
        "delivery_days": _ev(int(delivery_m.group(1)), conf="MEDIUM", source=source, snippet=delivery_m.group(0)) if delivery_m else None,
        "fob": _ev(fob_m.group(1).upper(), conf="MEDIUM", source=source, snippet=fob_m.group(0)) if fob_m else None,
    }
    clins = [_ev(m.group(1), conf="HIGH", source=source, snippet=m.group(0)) for m in clin_ms[:40]]

    if sidc:
        structure = STRUCT_SIDC
    elif idc:
        structure = STRUCT_IDC
    elif len(clins) > 1:
        structure = STRUCT_MULTI_LINE_RFQ
    elif nsn_m or pn_m or qty_m:
        structure = STRUCT_SINGLE_ITEM_RFQ
    else:
        structure = STRUCT_UNKNOWN

    # Flat convenience (backward compatible)
    nsn = fields["nsn"]["value"] if fields["nsn"] else None
    pn = fields["part_number"]["value"] if fields["part_number"] else None
    cage = fields["cage"]["value"] if fields["cage"] else None
    qty = fields["quantity"]["value"] if fields["quantity"] else None
    uoi = fields["unit_of_issue"]["value"] if fields["unit_of_issue"] else None

    return {
        "nsn": nsn,
        "part_number": pn,
        "cage": cage,
        "quantity": qty,
        "unit_of_issue": uoi,
        "delivery_days": fields["delivery_days"]["value"] if fields["delivery_days"] else None,
        "approved_source_signal": approved,
        "alternate_offer_signal": alternate,
        "fat_required_signal": fat,
        "fat_waiver_signal": fat_waiver,
        "idc_or_ltc_signal": idc,
        "sidc_signal": sidc,
        "set_aside_signal": set_aside,
        "packaging_signal": packaging,
        "inspection_signal": inspection,
        "jcp_or_export_signal": bool(JCP_RE.search(blob)),
        "nomenclature": title[:240] if title else None,
        "package_access": package,
        "public_package_available": package.upper()
        in {"PUBLIC_DIRECT", "PUBLIC_DETAIL_PAGE", "PUBLIC_PACKAGE_AVAILABLE"},
        "controlled_or_auth_package": controlled,
        "has_exact_nsn": bool(nsn),
        "has_exact_pn": bool(pn),
        "has_quantity": qty is not None,
        "has_uoi": bool(uoi),
        "has_cage": bool(cage),
        "fields": {k: v for k, v in fields.items() if v},
        "clins": clins,
        "structure_type": structure,
        "guaranteed_minimum_signal": bool(GUAR_MIN_RE.search(blob)),
        "maximum_quantity_signal": bool(MAX_QTY_RE.search(blob)),
    }


def resolve_product_identity(struct: dict[str, Any]) -> dict[str, Any]:
    """Deterministic identity priority — no invented parts."""
    candidates: list[dict[str, Any]] = []
    if struct.get("has_exact_nsn"):
        candidates.append({"identity_state": ID_EXACT_NSN, "key": struct.get("nsn"), "confidence": "HIGH"})
    if struct.get("has_exact_pn") and struct.get("has_cage") and struct.get("approved_source_signal"):
        candidates.append(
            {
                "identity_state": ID_EXACT_APPROVED_SOURCE_PART,
                "key": f"{struct.get('cage')}|{struct.get('part_number')}",
                "confidence": "HIGH",
            }
        )
    if struct.get("has_exact_pn"):
        candidates.append(
            {"identity_state": ID_EXACT_OEM_PART, "key": struct.get("part_number"), "confidence": "HIGH"}
        )
    if len(candidates) > 1 and candidates[0]["identity_state"] != ID_EXACT_NSN:
        primary = ID_AMBIGUOUS
    elif candidates:
        primary = candidates[0]["identity_state"]
    elif struct.get("nomenclature"):
        primary = ID_CATEGORY_IDENTIFIED
        candidates.append({"identity_state": ID_CATEGORY_IDENTIFIED, "key": struct.get("nomenclature"), "confidence": "LOW"})
    else:
        primary = ID_UNKNOWN
    return {"identity_state": primary, "candidates": candidates}


def extract_dla_research_signals(row: dict[str, Any], struct: dict[str, Any] | None = None) -> list[str]:
    blob = _blob(row)
    struct = struct or extract_dla_product_structure(row)
    signals: list[str] = []
    if AUTO_ACQ_RE.search(blob):
        signals.append("DLA_AUTOMATED_ACQUISITION")
    if MANUAL_EVAL_RE.search(blob):
        signals.append("DLA_MANUAL_EVALUATION")
    if PRICE_ONLY_RE.search(blob):
        signals.append("PRICE_ONLY_EVALUATION")
    if REV_AUCTION_RE.search(blob):
        signals.append("REVERSE_AUCTION_POSSIBLE")
    if struct.get("approved_source_signal"):
        signals.append("APPROVED_SOURCE_REQUIRED")
        signals.append("SOURCE_CONTROLLED_ITEM")
    if struct.get("alternate_offer_signal"):
        signals.append("ALTERNATE_OFFER_ALLOWED")
    if struct.get("fat_required_signal"):
        signals.append("FAT_REQUIRED")
    if struct.get("fat_waiver_signal"):
        signals.append("FAT_WAIVER_POSSIBLE")
    if struct.get("jcp_or_export_signal"):
        signals.append("JCP_REQUIRED")
        signals.append("CONTROLLED_TECHNICAL_DATA")
    if struct.get("has_exact_pn") and not struct.get("alternate_offer_signal"):
        signals.append("EXACT_PART_REQUIRED")
    if struct.get("alternate_offer_signal"):
        signals.append("MULTIPLE_APPROVED_SOURCES")
        signals.append("RESELLER_POSSIBLE")
    if SUB_DIBBS_RE.search(blob):
        signals.append("SUBMISSION_PATH_DIBBS")
    if SUB_EMAIL_RE.search(blob):
        signals.append("SUBMISSION_PATH_EMAIL")
    if SUB_SAM_RE.search(blob):
        signals.append("SUBMISSION_PATH_EXTERNAL")
    if struct.get("structure_type") == STRUCT_IDC:
        signals.append("IDC_STRUCTURE")
    if struct.get("structure_type") == STRUCT_SIDC:
        signals.append("SIDC_STRUCTURE")
    if struct.get("guaranteed_minimum_signal"):
        signals.append("GUARANTEED_MINIMUM_PRESENT")
    if struct.get("packaging_signal"):
        signals.append("PACKAGING_COMPLEXITY_SIGNAL")
    if struct.get("inspection_signal"):
        signals.append("INSPECTION_COMPLEXITY_SIGNAL")
    # de-dupe preserve order
    return list(dict.fromkeys(signals))


def compute_product_transaction_readiness(
    row: dict[str, Any],
    *,
    struct: dict[str, Any] | None = None,
    description_state: str | None = None,
    tech_state: str | None = None,
    refs_count: int = 0,
    docs_recovered: int = 0,
) -> dict[str, Any]:
    struct = struct or extract_dla_product_structure(row)
    identity = resolve_product_identity(struct)
    desc_state = description_state or row.get("description_state")
    notice_sem = str(row.get("notice_semantic_class") or "")
    state = READY_DISCOVERED_ONLY
    if desc_state in {"DESCRIPTION_INLINE", "DESCRIPTION_PUBLIC_RECOVERED"}:
        state = READY_DESCRIPTION_RECOVERED
    if refs_count > 0:
        state = READY_PACKAGE_PARTIAL
    if docs_recovered > 0:
        state = READY_PACKAGE_RECOVERED
    if identity["identity_state"] in {ID_EXACT_NSN, ID_EXACT_OEM_PART, ID_EXACT_APPROVED_SOURCE_PART}:
        state = READY_PRODUCT_IDENTITY_EXACT
    elif identity["identity_state"] not in {ID_UNKNOWN}:
        state = READY_PRODUCT_IDENTITY_PARTIAL
    has_qty = bool(struct.get("has_quantity"))
    has_id = identity["identity_state"] in {ID_EXACT_NSN, ID_EXACT_OEM_PART, ID_EXACT_APPROVED_SOURCE_PART}
    if has_id and has_qty:
        state = READY_TRANSACTION_STRUCTURE_COMPLETE
    elif has_id or has_qty:
        state = READY_TRANSACTION_STRUCTURE_PARTIAL
    commercial = has_id and has_qty
    historical = has_id  # NSN/PN alone enables historical award research
    if commercial:
        state = READY_COMMERCIAL_RESEARCH
    elif historical and state not in {READY_COMMERCIAL_RESEARCH}:
        # keep stronger structure states; else mark historical-ready
        if state in {
            READY_DISCOVERED_ONLY,
            READY_DESCRIPTION_RECOVERED,
            READY_PACKAGE_PARTIAL,
            READY_PACKAGE_RECOVERED,
            READY_PRODUCT_IDENTITY_PARTIAL,
            READY_PRODUCT_IDENTITY_EXACT,
        }:
            state = READY_HISTORICAL_RESEARCH
    if struct.get("controlled_or_auth_package") or (tech_state or "").startswith("CONTROLLED") or tech_state == "JCP_OR_ELIGIBILITY_REQUIRED":
        if not commercial:
            state = READY_BLOCKED_CONTROLLED if docs_recovered == 0 else READY_ECONOMIC_POTENTIAL_DOC_ACCESS
    if docs_recovered == 0 and refs_count == 0 and not (row.get("description") and len(str(row.get("description"))) > 80):
        if not has_id:
            if state == READY_DISCOVERED_ONLY:
                state = READY_BLOCKED_MISSING_PACKAGE
    if state in {READY_BLOCKED_CONTROLLED, READY_BLOCKED_MISSING_PACKAGE} and has_id:
        state = READY_ECONOMIC_POTENTIAL_DOC_ACCESS
    if notice_sem in {"AWARD_OR_HISTORY", "MARKET_RESEARCH"} and historical:
        state = READY_HISTORICAL_RESEARCH
    return {
        "readiness_state": state,
        "commercial_research_ready": commercial or state == READY_COMMERCIAL_RESEARCH,
        "historical_research_ready": historical or state == READY_HISTORICAL_RESEARCH,
        "knows_what": has_id,
        "knows_how_many": has_qty,
        "knows_uoi": bool(struct.get("has_uoi")),
        "knows_source_restrictions": bool(struct.get("approved_source_signal")),
        "knows_delivery": struct.get("delivery_days") is not None,
        "knows_submission": bool(
            set(extract_dla_research_signals(row, struct))
            & {"SUBMISSION_PATH_DIBBS", "SUBMISSION_PATH_EMAIL", "SUBMISSION_PATH_EXTERNAL"}
        ),
        "identity": identity,
        "human_document_access_eventually_required": state
        in {READY_HUMAN_DOCUMENT_ACCESS, READY_ECONOMIC_POTENTIAL_DOC_ACCESS, READY_BLOCKED_CONTROLLED},
    }


def enrich_with_dla_structure(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    struct = extract_dla_product_structure(out)
    identity = resolve_product_identity(struct)
    signals = extract_dla_research_signals(out, struct)
    out["dla_product_structure"] = struct
    out["product_identity"] = identity
    out["dla_research_signals"] = signals
    out["exact_nsn"] = struct.get("nsn")
    out["exact_part_number"] = struct.get("part_number")
    out["quantity"] = struct.get("quantity")
    out["unit_of_issue"] = struct.get("unit_of_issue")
    out["structure_type"] = struct.get("structure_type")
    meta = dict(out.get("raw_metadata") or {})
    meta["dla_product_structure"] = struct
    meta["product_identity"] = identity
    meta["dla_research_signals"] = signals
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
    strong = (
        bool(struct.get("has_exact_nsn"))
        or bool(struct.get("has_exact_pn"))
        or bool(struct.get("has_quantity") and struct.get("unit_of_issue"))
        or bool(struct.get("approved_source_signal"))
        or bool(
            re.search(
                r"proposed\s+procurement\s+for\s+nsn|supplies|equipment|hardware|parts?\b",
                f"{title} {desc}",
                re.I,
            )
        )
    )
    if strong and cls in {"UNKNOWN", "SERVICE", "CLEARLY_IRRELEVANT"}:
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
