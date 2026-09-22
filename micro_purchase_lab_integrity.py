"""µLab product-transaction integrity — quantity/CLIN, variants, actionability, economics targets.

History-first: historical gov price → max allowable acquisition cost → commercial validation.
Does not fabricate prices. UNKNOWN ≠ acceptable for COMPLETE.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

BUILD_TAG = "20260922-m3-micro-lab-integrity-1"

# Quantity confidence
QTY_EXACT = "EXACT"
QTY_STRONG = "STRONG"
QTY_AMBIGUOUS = "AMBIGUOUS"
QTY_UNRESOLVED = "UNRESOLVED"

# Quantity basis
BASIS_FIRM = "FIRM_QUANTITY"
BASIS_ESTIMATED = "ESTIMATED_QUANTITY"
BASIS_ANNUAL_EST = "ANNUAL_ESTIMATED_QUANTITY"
BASIS_MIN_ORDER = "MINIMUM_ORDER_QUANTITY"
BASIS_MAX_ORDER = "MAXIMUM_ORDER_QUANTITY"
BASIS_MIN_CONTRACT = "MINIMUM_CONTRACT_QUANTITY"
BASIS_MAX_CONTRACT = "MAXIMUM_CONTRACT_QUANTITY"
BASIS_PER_DO = "PER_DELIVERY_ORDER_QUANTITY"
BASIS_UNKNOWN = "UNKNOWN_QUANTITY_BASIS"

# Identity states
ID_EXACT_NSN = "EXACT_NSN"
ID_EXACT_PART = "EXACT_PART_NUMBER"
ID_EXACT_MPN = "EXACT_MANUFACTURER_PART"
ID_EXACT_MODEL = "EXACT_MODEL"
ID_STRONG_SPEC = "STRONG_SPEC_MATCH"
ID_APPROVED_SOURCE = "APPROVED_SOURCE_MATCH"
ID_VARIANT_CONFLICT = "VARIANT_CONFLICT"
ID_AMBIGUOUS = "AMBIGUOUS"
ID_UNRESOLVED = "UNRESOLVED"

# Actionability
ACT_TRANSACTIONAL = "AUTHORITATIVE_TRANSACTIONAL"
ACT_PRE_SOLICITATION = "AUTHORITATIVE_PRE_SOLICITATION"
ACT_BLOCKED = "SOURCE_ACCESS_BLOCKED"
ACT_AGGREGATOR = "AGGREGATOR_LEAD_ONLY"
ACT_NOT_RESOLVED = "UNDERLYING_SOLICITATION_NOT_RESOLVED"
ACT_EVERGREEN = "NON_TRANSACTIONAL_EVERGREEN"
ACT_VENDOR_REG = "VENDOR_REGISTRATION_ONLY"
ACT_QUAL_POOL = "QUALIFICATION_POOL_ONLY"
ACT_FORECAST = "FORECAST_ONLY"
ACT_SOURCES_SOUGHT = "SOURCES_SOUGHT_ONLY"
ACT_RFI = "RFI_ONLY"
ACT_DEADLINE_SUSPICIOUS = "DEADLINE_SUSPICIOUS"
ACT_EXPIRED = "EXPIRED"
ACT_CANCELLED = "CANCELLED"

# Approved source
SRC_OPEN = "OPEN_SOURCE"
SRC_APPROVED_REQUIRED = "APPROVED_SOURCE_REQUIRED"
SRC_MULTIPLE = "MULTIPLE_APPROVED_SOURCES"
SRC_SOLE = "SOLE_APPROVED_MANUFACTURER"
SRC_APPROVAL_REQ = "SOURCE_APPROVAL_REQUIRED"
SRC_TRACEABILITY = "TRACEABILITY_REQUIRED"
SRC_TRACE_UNRESOLVED = "TRACEABILITY_UNRESOLVED"

# Procurement paths
PATH_DIBBS = "DIBBS_RFQ"
PATH_SAM_RFQ = "SAM_OPEN_MARKET_RFQ"
PATH_SAM_CSS = "SAM_COMBINED_SYNOPSIS_SOLICITATION"
PATH_GSA_MAS = "GSA_MAS"
PATH_GSA_ADV = "GSA_ADVANTAGE"
PATH_GSA_EBUY = "GSA_EBUY"
PATH_COMM_PLAT = "COMMERCIAL_PLATFORMS_PROGRAM"
PATH_BPA = "BPA_CALL"
PATH_IDIQ = "IDIQ_DELIVERY_ORDER"
PATH_UNISON = "UNISON"
PATH_STATE = "STATE_LOCAL_PORTAL"
PATH_DIRECT = "DIRECT_SMALL_BUY"
PATH_UNKNOWN = "UNKNOWN"

ACCESS_DIRECT = "DIRECT_ACCESS"
ACCESS_REG = "REGISTRATION_REQUIRED"
ACCESS_SCHEDULE = "SCHEDULE_REQUIRED"
ACCESS_VEHICLE = "VEHICLE_REQUIRED"
ACCESS_PARTNER = "PARTNER_REQUIRED"
ACCESS_BLOCKED = "SOURCE_ACCESS_BLOCKED"
ACCESS_UNKNOWN = "UNKNOWN"

# Competitor
COMP_NONE = "NO_COMPETITOR_SIGNAL"
COMP_BIDDER = "HISTORICAL_GOVERNMENT_BIDDER"
COMP_AWARDEE = "HISTORICAL_AWARDEE"
COMP_INCUMBENT = "INCUMBENT_SUPPLIER"
COMP_MFR_DIRECT = "MANUFACTURER_DIRECT_COMPETITOR"
COMP_UNKNOWN = "UNKNOWN"

# Executability
EXEC_OK = "EXECUTABLE_FOR_STARTUP"
EXEC_SUPPLIER = "EXECUTABLE_WITH_SUPPLIER_SUPPORT"
EXEC_PARTNER = "EXECUTABLE_WITH_PARTNER"
EXEC_PAST_PERF = "PAST_PERFORMANCE_REQUIRED"
EXEC_VEHICLE = "VEHICLE_ACCESS_REQUIRED"
EXEC_APPROVED = "APPROVED_SOURCE_BARRIER"
EXEC_CERT = "SPECIAL_CERTIFICATION_BARRIER"
EXEC_INSTALL = "INSTALLATION_HEAVY"
EXEC_CUSTOM = "CUSTOMIZATION_COMPLEX"
EXEC_DELIVERY = "DELIVERY_RISK"
EXEC_FUNDING = "FUNDING_RISK"
EXEC_UNKNOWN = "UNKNOWN_EXECUTABILITY"

# Signal vs transaction
SIGNAL_TRANSACTIONAL = "TRANSACTIONAL_PRODUCT_OPPORTUNITY"
SIGNAL_SMALL_BUY = "BUYER_SMALL_BUY_SIGNAL"

_CASE_PACK = re.compile(
    r"\b(?:case|box|carton|pack|pkg|package)\s*(?:of|/)?\s*(\d{1,5})\b|"
    r"\b(\d{1,5})\s*(?:per|/)\s*(?:case|box|pack|pkg|package)\b|"
    r"\((\d{1,5})\s*(?:ea|each|pc|pcs|pieces?)\s*(?:per|/)?\s*(?:case|box|pack)?\)",
    re.I,
)
_CLIN_LINE = re.compile(
    r"\bCLIN\s*[:#]?\s*([0-9A-Z]{3,6})\b.{0,120}?(?:qty|quantity|qnty)?\s*[:#]?\s*(\d[\d,]*)\s*(EA|EACH|CS|CASE|BX|BOX|PK|PACK|KT|KIT|PR|SET)?",
    re.I | re.S,
)
_IDIQ = re.compile(
    r"\b(idiq|indefinite\s+quantity|estimated\s+annual\s+quantity|minimum\s+(?:delivery\s+)?order|"
    r"maximum\s+(?:delivery\s+)?order|min(?:imum)?\s+contract\s+qty|max(?:imum)?\s+contract\s+qty)\b",
    re.I,
)
_SOURCES_SOUGHT = re.compile(r"\b(sources?\s+sought|request\s+for\s+information|\bRFI\b|forecast\s+only|special\s+notice)\b", re.I)
_APPROVED_SRC = re.compile(
    r"\b(approved\s+source|source\s+controlled|qpl|qml|cage\s*[:#]?\s*[0-9A-Z]{5}|only\s+the\s+following\s+source|"
    r"manufacturer\s+traceability|traceability\s+required)\b",
    re.I,
)
_VARIANT_SUFFIX = re.compile(r"([A-Z0-9]+?)([A-Z]\d*|[P]\d+|KIT|B|P2|P1)$", re.I)
_MIL_PACK = re.compile(r"\bMIL[- ]?STD[- ]?2073\b", re.I)
_FOB = re.compile(r"\bFOB\s*[:#]?\s*(ORIGIN|DESTINATION)\b", re.I)
_SUSPICIOUS_YEARS = re.compile(r"\b(20[3-9]\d|21\d{2})\b")  # absurd far-future years


def _d(v: Any) -> Decimal | None:
    if v is None or v == "" or str(v).upper() in {"UNKNOWN", "NONE", "NULL", "N/A"}:
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _blob(row: dict[str, Any]) -> str:
    parts = [row.get("title"), row.get("description"), row.get("product")]
    for li in row.get("line_items") or row.get("bom") or []:
        if isinstance(li, dict):
            parts.append(li.get("description"))
            parts.append(str(li.get("raw_text") or ""))
    return "\n".join(str(p or "") for p in parts)


# ---------------------------------------------------------------------------
# Quantity / CLIN / pack
# ---------------------------------------------------------------------------


def normalize_part_number(raw: Any) -> str | None:
    if raw is None or raw == "":
        return None
    s = str(raw).strip().upper()
    s = re.sub(r"\s+", "", s)
    # Cosmetic punctuation only — keep hyphens that may be material if alphanumeric both sides
    # Remove only spaces already done; strip surrounding punctuation
    s = s.strip("-_./")
    return s or None


def parts_materially_different(a: Any, b: Any) -> bool:
    """True if variants differ beyond cosmetic normalization."""
    na, nb = normalize_part_number(a), normalize_part_number(b)
    if not na or not nb:
        return False
    if na == nb:
        return False
    # Hyphen/underscore only differences that leave same alnum stream
    ca, cb = re.sub(r"[^A-Z0-9]", "", na), re.sub(r"[^A-Z0-9]", "", nb)
    if ca == cb:
        return False
    # Known kit/bare suffixes
    if ca.rstrip("B") == cb or cb.rstrip("B") == ca:
        if ca.endswith("B") != cb.endswith("B"):
            return True
    if "P2" in (ca, cb) or "P1" in (ca, cb) or "KIT" in (ca + cb):
        if ca != cb:
            return True
    # Trailing digit/revision differences on otherwise similar roots
    if ca[:-1] == cb[:-1] and ca[-1] != cb[-1] and ca[-1].isdigit() and cb[-1].isdigit():
        return True
    if ca != cb:
        return True
    return False


def parse_pack_text(text: str) -> dict[str, Any]:
    m = _CASE_PACK.search(text or "")
    if not m:
        return {"pieces_per_pack": None, "raw_pack_text": None}
    n = next((g for g in m.groups() if g), None)
    return {"pieces_per_pack": int(n) if n else None, "raw_pack_text": m.group(0)}


def normalize_quantity(
    *,
    raw_quantity: Any,
    raw_uom: Any = "EA",
    raw_pack_text: str | None = None,
    pieces_per_pack: Any = None,
    source: str = "row",
    source_location: str | None = None,
) -> dict[str, Any]:
    qty = _d(raw_quantity)
    uom = str(raw_uom or "EA").upper().strip() or "EA"
    pack = _d(pieces_per_pack)
    pack_info = parse_pack_text(raw_pack_text or "")
    if pack is None and pack_info.get("pieces_per_pack"):
        pack = Decimal(str(pack_info["pieces_per_pack"]))
        raw_pack_text = pack_info.get("raw_pack_text") or raw_pack_text

    notes = []
    confidence = QTY_UNRESOLVED
    normalized_base_uom = "EA"
    normalized_base_qty = None
    total_piece = None
    pack_count = None

    if qty is None:
        return {
            "raw_quantity": raw_quantity,
            "raw_uom": uom,
            "raw_pack_text": raw_pack_text,
            "normalized_base_quantity": None,
            "normalized_base_uom": None,
            "pack_count": None,
            "pieces_per_pack": str(pack) if pack else None,
            "total_piece_equivalent": None,
            "source": source,
            "source_location": source_location,
            "confidence": QTY_UNRESOLVED,
            "notes": ["QUANTITY_UNRESOLVED"],
            "conflicts": ["QUANTITY_UNRESOLVED"],
        }

    # Case/box without pack size → ambiguous
    if uom in {"CS", "CASE", "BX", "BOX", "PK", "PACK", "PKG", "CARTON"} and pack is None:
        confidence = QTY_AMBIGUOUS
        notes.append("PACK_SIZE_CONFLICT")
        pack_count = qty
        return {
            "raw_quantity": str(qty),
            "raw_uom": uom,
            "raw_pack_text": raw_pack_text,
            "normalized_base_quantity": None,
            "normalized_base_uom": "EA",
            "pack_count": str(pack_count),
            "pieces_per_pack": None,
            "total_piece_equivalent": None,
            "source": source,
            "source_location": source_location,
            "confidence": confidence,
            "notes": notes,
            "conflicts": ["PACK_SIZE_CONFLICT", "UOM_CONFLICT"],
        }

    if uom in {"CS", "CASE", "BX", "BOX", "PK", "PACK", "PKG", "CARTON"} and pack is not None:
        pack_count = qty
        total_piece = qty * pack
        normalized_base_qty = total_piece
        confidence = QTY_EXACT
        notes.append(f"converted_{uom}_x_{pack}_pieces")
    elif uom in {"EA", "EACH", "PC", "PCS", "PIECE", "UNIT"}:
        if pack is not None and pack > 1 and "case" in str(raw_pack_text or "").lower():
            # "1 EA" but pack text says case of 10 → ambiguous unless clarified
            confidence = QTY_AMBIGUOUS
            notes.append("PACK_SIZE_CONFLICT")
            conflicts = ["PACK_SIZE_CONFLICT"]
            return {
                "raw_quantity": str(qty),
                "raw_uom": uom,
                "raw_pack_text": raw_pack_text,
                "normalized_base_quantity": None,
                "normalized_base_uom": "EA",
                "pack_count": None,
                "pieces_per_pack": str(pack),
                "total_piece_equivalent": None,
                "source": source,
                "source_location": source_location,
                "confidence": confidence,
                "notes": notes,
                "conflicts": conflicts,
            }
        normalized_base_qty = qty
        total_piece = qty
        confidence = QTY_EXACT if source_location else QTY_STRONG
    else:
        normalized_base_qty = qty
        total_piece = qty
        confidence = QTY_STRONG
        notes.append(f"uom_{uom}_treated_as_base")

    return {
        "raw_quantity": str(qty),
        "raw_uom": uom,
        "raw_pack_text": raw_pack_text,
        "normalized_base_quantity": str(normalized_base_qty) if normalized_base_qty is not None else None,
        "normalized_base_uom": normalized_base_uom,
        "pack_count": str(pack_count) if pack_count is not None else None,
        "pieces_per_pack": str(pack) if pack is not None else None,
        "total_piece_equivalent": str(total_piece) if total_piece is not None else None,
        "source": source,
        "source_location": source_location,
        "confidence": confidence,
        "notes": notes,
        "conflicts": [],
    }


def extract_clins(row: dict[str, Any]) -> dict[str, Any]:
    clins: list[dict[str, Any]] = []
    for li in row.get("line_items") or row.get("bom") or row.get("clins") or []:
        if not isinstance(li, dict):
            continue
        dest = li.get("destination") or li.get("ship_to") or li.get("place_of_performance")
        qty_n = normalize_quantity(
            raw_quantity=li.get("quantity") or li.get("qty"),
            raw_uom=li.get("unit") or li.get("uom") or li.get("unit_of_issue"),
            raw_pack_text=str(li.get("packaging") or li.get("description") or ""),
            pieces_per_pack=li.get("pieces_per_pack"),
            source="line_item",
            source_location=str(li.get("line") or li.get("clin") or li.get("line_number") or ""),
        )
        clins.append(
            {
                "clin_number": li.get("clin") or li.get("line") or li.get("line_number") or li.get("item_number"),
                "product_identity": li.get("description") or li.get("item_name") or li.get("part_number"),
                "quantity": qty_n,
                "destination": dest,
                "delivery_date": li.get("delivery_date") or li.get("days_after_award"),
                "fob": li.get("fob"),
                "inspection_point": li.get("inspection"),
                "acceptance_point": li.get("acceptance"),
                "packaging": li.get("packaging"),
                "option_base_status": li.get("option_status") or "BASE",
                "price_basis": li.get("unit_price"),
            }
        )

    blob = _blob(row)
    if not clins:
        for m in _CLIN_LINE.finditer(blob):
            qty_n = normalize_quantity(
                raw_quantity=m.group(2).replace(",", ""),
                raw_uom=m.group(3) or "EA",
                source="text_clin",
                source_location=f"CLIN {m.group(1)}",
            )
            clins.append(
                {
                    "clin_number": m.group(1),
                    "product_identity": None,
                    "quantity": qty_n,
                    "destination": None,
                    "delivery_date": None,
                    "fob": None,
                    "inspection_point": None,
                    "acceptance_point": None,
                    "packaging": None,
                    "option_base_status": "BASE",
                    "price_basis": None,
                }
            )

    # Header-level quantity if no CLINs
    if not clins and (row.get("quantity") is not None or (row.get("dla_product_structure") or {}).get("quantity") is not None):
        qty_n = normalize_quantity(
            raw_quantity=row.get("quantity") or (row.get("dla_product_structure") or {}).get("quantity"),
            raw_uom=row.get("unit_of_issue") or (row.get("dla_product_structure") or {}).get("unit_of_issue") or "EA",
            raw_pack_text=blob[:500],
            pieces_per_pack=row.get("pieces_per_pack") or row.get("commercial_units_per_gov_unit"),
            source="header",
            source_location="solicitation_header",
        )
        clins.append(
            {
                "clin_number": "HEADER",
                "product_identity": row.get("title"),
                "quantity": qty_n,
                "destination": row.get("delivery_location") or row.get("place_of_performance"),
                "delivery_date": None,
                "fob": None,
                "inspection_point": None,
                "acceptance_point": None,
                "packaging": None,
                "option_base_status": "BASE",
                "price_basis": None,
            }
        )

    destinations = {c.get("destination") for c in clins if c.get("destination")}
    multi_dest = len(destinations) > 1
    aggregate_ok = False
    aggregate_pieces = None
    if clins and all((c.get("quantity") or {}).get("confidence") in {QTY_EXACT, QTY_STRONG} for c in clins):
        pieces = [_d((c.get("quantity") or {}).get("total_piece_equivalent")) for c in clins]
        if all(p is not None for p in pieces):
            aggregate_pieces = sum(pieces)  # type: ignore[arg-type]
            aggregate_ok = not multi_dest  # destination-specific shipping may matter
            if multi_dest:
                aggregate_ok = True  # still report aggregate but flag
    confidences = [(c.get("quantity") or {}).get("confidence") for c in clins]
    if not clins:
        overall = QTY_UNRESOLVED
    elif QTY_AMBIGUOUS in confidences or QTY_UNRESOLVED in confidences:
        overall = QTY_AMBIGUOUS if QTY_AMBIGUOUS in confidences else QTY_UNRESOLVED
    elif all(c == QTY_EXACT for c in confidences):
        overall = QTY_EXACT
    else:
        overall = QTY_STRONG

    basis = detect_quantity_basis(row, blob)
    return {
        "clins": clins,
        "clin_count": len(clins),
        "multiple_destinations": multi_dest,
        "destinations": sorted(destinations) if destinations else [],
        "aggregate_piece_equivalent": str(aggregate_pieces) if aggregate_pieces is not None else None,
        "aggregate_valid_for_undifferentiated_total": aggregate_ok and not multi_dest,
        "keep_line_economics_separate": multi_dest,
        "overall_quantity_confidence": overall,
        "quantity_basis": basis,
        "conflicts": _qty_conflicts(clins, basis, overall),
    }


def detect_quantity_basis(row: dict[str, Any], blob: str | None = None) -> dict[str, Any]:
    text = blob or _blob(row)
    basis = BASIS_FIRM
    details: dict[str, Any] = {}
    if _IDIQ.search(text) or str(row.get("contract_type") or "").upper() in {"IDIQ", "INDEFINITE_QUANTITY"}:
        basis = BASIS_ESTIMATED
        # Try extract annual vs min order
        ann = re.search(r"estimated\s+annual\s+quantity\s*[:#]?\s*([\d,]+)", text, re.I)
        mno = re.search(r"minimum\s+(?:delivery\s+)?order\s*[:#]?\s*([\d,]+)", text, re.I)
        if ann:
            details["annual_estimated_quantity"] = ann.group(1).replace(",", "")
            basis = BASIS_ANNUAL_EST
        if mno:
            details["minimum_delivery_order"] = mno.group(1).replace(",", "")
            details["note"] = "ANNUAL_ESTIMATED_QUANTITY != MINIMUM_ORDER_QUANTITY for sourcing volume"
        if ann and mno:
            basis = BASIS_UNKNOWN  # unresolved which applies to first buy
            details["status"] = "QUANTITY_BASIS_UNRESOLVED"
    if row.get("quantity_basis"):
        basis = str(row.get("quantity_basis"))
    return {"basis": basis, "details": details, "unresolved": basis in {BASIS_UNKNOWN} or details.get("status") == "QUANTITY_BASIS_UNRESOLVED"}


def _qty_conflicts(clins: list[dict[str, Any]], basis: dict[str, Any], overall: str) -> list[str]:
    c = []
    if overall in {QTY_AMBIGUOUS, QTY_UNRESOLVED}:
        c.append("QUANTITY_UNRESOLVED" if overall == QTY_UNRESOLVED else "PACK_SIZE_CONFLICT")
    if basis.get("unresolved"):
        c.append("IDIQ_QUANTITY_BASIS_UNRESOLVED")
    for clin in clins:
        c.extend((clin.get("quantity") or {}).get("conflicts") or [])
    return list(dict.fromkeys(c))


# ---------------------------------------------------------------------------
# Identity / approved source / actionability
# ---------------------------------------------------------------------------


def assess_variant_identity(row: dict[str, Any], identity: dict[str, Any] | None = None) -> dict[str, Any]:
    ident = (identity or {}).get("identity") or identity or {}
    raw_pn = row.get("part_number") or row.get("exact_part_number") or ident.get("manufacturer_part_number")
    norm = normalize_part_number(raw_pn)
    nsn = row.get("exact_nsn") or row.get("nsn") or ident.get("nsn")
    mfr = row.get("manufacturer") or ident.get("manufacturer")
    model = ident.get("model")
    candidates = row.get("identity_candidates") or ident.get("candidates") or []

    state = ID_UNRESOLVED
    conflicts = []
    if nsn:
        state = ID_EXACT_NSN
    elif norm and mfr:
        state = ID_EXACT_MPN
    elif norm:
        state = ID_EXACT_PART
    elif model:
        state = ID_EXACT_MODEL
    elif (identity or {}).get("confidence") in {"STRONG", "MEDIUM", "EXACT"}:
        state = ID_STRONG_SPEC if (identity or {}).get("confidence") != "EXACT" else ID_EXACT_MODEL
    else:
        state = ID_AMBIGUOUS if row.get("title") else ID_UNRESOLVED

    # Candidate variant conflicts
    parts = set()
    for c in candidates:
        if isinstance(c, dict):
            p = normalize_part_number(c.get("part_number") or c.get("mpn") or c.get("Part_number"))
            if p:
                parts.add(p)
    if norm:
        parts.add(norm)
    parts_list = sorted(parts)
    for i, a in enumerate(parts_list):
        for b in parts_list[i + 1 :]:
            if parts_materially_different(a, b):
                conflicts.append({"a": a, "b": b, "reason": "VARIANT_CONFLICT"})
                state = ID_VARIANT_CONFLICT

    # Explicit comparison fields
    if row.get("compared_part_number") and parts_materially_different(raw_pn, row.get("compared_part_number")):
        conflicts.append({"a": normalize_part_number(raw_pn), "b": normalize_part_number(row.get("compared_part_number")), "reason": "VARIANT_CONFLICT"})
        state = ID_VARIANT_CONFLICT

    return {
        "solicitation_raw_part_number": raw_pn,
        "normalized_part_number": norm,
        "manufacturer": mfr,
        "manufacturer_cage": row.get("cage") or ident.get("cage"),
        "model": model,
        "nsn": nsn,
        "niin": row.get("niin") or (str(nsn).split("-", 1)[-1] if nsn and "-" in str(nsn) else None),
        "fsc": row.get("fsc") or (str(nsn).split("-")[0] if nsn else None),
        "upc": ident.get("upc"),
        "sku": ident.get("sku"),
        "brand": ident.get("brand") or mfr,
        "salient_characteristics": ident.get("salient_characteristics"),
        "identity_state": state,
        "variant_conflicts": conflicts,
        "ok_for_complete": state
        in {ID_EXACT_NSN, ID_EXACT_PART, ID_EXACT_MPN, ID_EXACT_MODEL, ID_STRONG_SPEC, ID_APPROVED_SOURCE}
        and not conflicts,
    }


def assess_approved_source(row: dict[str, Any]) -> dict[str, Any]:
    blob = _blob(row)
    cages = list(row.get("approved_cages") or [])
    parts = list(row.get("approved_part_numbers") or [])
    struct = row.get("dla_product_structure") or {}
    if isinstance(struct, dict):
        cages.extend(struct.get("approved_cages") or [])
        parts.extend(struct.get("approved_part_numbers") or [])
    for m in re.finditer(r"\bCAGE\s*[:#]?\s*([0-9A-Z]{5})\b", blob, re.I):
        cages.append(m.group(1).upper())
    cages = sorted({str(c).upper() for c in cages if c})
    parts = sorted({str(p) for p in parts if p})

    required = bool(_APPROVED_SRC.search(blob) or row.get("approved_source_required") or cages or parts)
    trace = bool(re.search(r"traceability", blob, re.I) or row.get("traceability_required"))
    if not required and not trace:
        return {"state": SRC_OPEN, "approved_cages": cages, "approved_part_numbers": parts, "ok_for_complete": True, "resolved": True}
    if cages and len(cages) == 1:
        state = SRC_SOLE
    elif cages and len(cages) > 1:
        state = SRC_MULTIPLE
    elif required:
        state = SRC_APPROVED_REQUIRED
    else:
        state = SRC_OPEN
    if trace and not row.get("traceability_resolved"):
        return {
            "state": SRC_TRACE_UNRESOLVED,
            "approved_cages": cages,
            "approved_part_numbers": parts,
            "ok_for_complete": False,
            "resolved": False,
            "reason": "TRACEABILITY_UNRESOLVED",
        }
    # Source-controlled without resolved supplier path
    if state in {SRC_SOLE, SRC_APPROVED_REQUIRED, SRC_MULTIPLE} and not row.get("approved_source_path_resolved"):
        # Still allow COMPLETE only if cages/parts listed (resolved requirement) — path may be WILL_SOURCE later
        return {
            "state": state,
            "approved_cages": cages,
            "approved_part_numbers": parts,
            "ok_for_complete": True,  # requirement known
            "resolved": True,
            "special_flags": ["APPROVED_SOURCE_REQUIRED"] if required else [],
        }
    return {"state": state, "approved_cages": cages, "approved_part_numbers": parts, "ok_for_complete": True, "resolved": True}


def assess_actionability(row: dict[str, Any]) -> dict[str, Any]:
    blob = _blob(row)
    status = str(row.get("status") or row.get("opportunity_status") or "OPEN").upper()
    notice = str(row.get("notice_type") or row.get("type") or "").upper()
    sid = str(row.get("source_id") or "").lower()
    family = str(row.get("source_family") or row.get("platform_family") or "").upper()

    if status in {"EXPIRED", "CLOSED", "AWARDED"}:
        return {"state": ACT_EXPIRED, "transactional": False, "reason": "EXPIRED"}
    if status in {"CANCELLED", "CANCELED"}:
        return {"state": ACT_CANCELLED, "transactional": False, "reason": "CANCELLED"}
    if _SOURCES_SOUGHT.search(blob) or "SOURCES SOUGHT" in notice or notice in {"RFI", "SPECIAL NOTICE"}:
        if "RFI" in notice or re.search(r"\bRFI\b", blob):
            return {"state": ACT_RFI, "transactional": False, "reason": "NON_TRANSACTIONAL"}
        if "FORECAST" in notice or re.search(r"\bforecast\b", blob, re.I):
            return {"state": ACT_FORECAST, "transactional": False, "reason": "NON_TRANSACTIONAL"}
        return {"state": ACT_SOURCES_SOUGHT, "transactional": False, "reason": "NON_TRANSACTIONAL"}
    if re.search(r"\bforecast\b", blob, re.I) and "RFQ" not in notice:
        return {"state": ACT_FORECAST, "transactional": False, "reason": "NON_TRANSACTIONAL"}
    if "vendor registration" in blob.lower() and "rfq" not in blob.lower():
        return {"state": ACT_VENDOR_REG, "transactional": False, "reason": "NON_TRANSACTIONAL"}

    # Suspicious deadline (multi-year absurd)
    deadline = str(row.get("deadline") or row.get("response_deadline") or "")
    runway = None
    de = row.get("deadline_evaluation") or {}
    try:
        runway = int(de.get("calendar_days_remaining")) if de.get("calendar_days_remaining") is not None else None
    except (TypeError, ValueError):
        pass
    if runway is not None and runway > 800:
        return {"state": ACT_DEADLINE_SUSPICIOUS, "transactional": False, "reason": "DEADLINE_SUSPICIOUS", "runway_days": runway}
    if _SUSPICIOUS_YEARS.search(deadline) and runway and runway > 400:
        return {"state": ACT_DEADLINE_SUSPICIOUS, "transactional": False, "reason": "DEADLINE_SUSPICIOUS", "runway_days": runway}

    if family in {"AGGREGATOR", "BIDNET", "SOVRA"} or "bidnet" in sid or "sovra" in sid:
        if not row.get("authoritative_solicitation_resolved") and not row.get("detail_url_authoritative"):
            return {"state": ACT_AGGREGATOR, "transactional": False, "reason": "AGGREGATOR_LEAD_ONLY"}
        if row.get("authoritative_solicitation_resolved"):
            return {"state": ACT_TRANSACTIONAL, "transactional": True, "upgraded_from": "aggregator"}

    if str(row.get("source_access_state") or "").upper() in {"AUTH_REQUIRED", "BOT_PROTECTED", "BLOCKED"}:
        return {"state": ACT_BLOCKED, "transactional": False, "reason": "SOURCE_ACCESS_BLOCKED"}

    if notice in {"PRESOLICITATION", "PRE-SOLICITATION"}:
        return {"state": ACT_PRE_SOLICITATION, "transactional": False, "reason": "NON_TRANSACTIONAL"}

    return {
        "state": ACT_TRANSACTIONAL,
        "transactional": True,
        "accessible_source": True,
        "realistic_deadline": runway is None or 2 <= runway <= 800,
        "response_method_known": bool(row.get("response_method") or row.get("detail_url") or True),
    }


def classify_procurement_path(row: dict[str, Any]) -> dict[str, Any]:
    sid = str(row.get("source_id") or "").lower()
    sol = str(row.get("solicitation_number") or "").upper()
    blob = _blob(row).lower()
    path = PATH_UNKNOWN
    access = ACCESS_UNKNOWN
    if "dibbs" in sid or sol.startswith(("SPE", "SPR")) or row.get("is_dla"):
        path = PATH_DIBBS
        access = ACCESS_REG if "registration" in blob else ACCESS_DIRECT
    elif "ebuy" in blob or "e-buy" in blob:
        path = PATH_GSA_EBUY
        access = ACCESS_VEHICLE
    elif "gsa advantage" in blob or "gsa_advantage" in sid:
        path = PATH_GSA_ADV
        access = ACCESS_SCHEDULE
    elif "commercial platform" in blob:
        path = PATH_COMM_PLAT
        access = ACCESS_DIRECT
    elif "bpa" in blob and "call" in blob:
        path = PATH_BPA
        access = ACCESS_VEHICLE
    elif "idiq" in blob or "delivery order" in blob:
        path = PATH_IDIQ
        access = ACCESS_VEHICLE
    elif "unison" in sid or "unison" in blob:
        path = PATH_UNISON
        access = ACCESS_REG
    elif "sam" in sid or row.get("notice_id"):
        path = PATH_SAM_RFQ
        access = ACCESS_DIRECT
    elif sid or row.get("detail_url"):
        path = PATH_STATE
        access = ACCESS_DIRECT
    if path == PATH_UNKNOWN:
        return {"path": path, "access": access, "ok_for_complete_direct": False, "reason": "PROCUREMENT_PATH_UNKNOWN"}
    if access in {ACCESS_VEHICLE, ACCESS_SCHEDULE} and not row.get("vehicle_held") and not row.get("partner_vehicle_available"):
        return {
            "path": path,
            "access": ACCESS_PARTNER if row.get("partner_available") else access,
            "ok_for_complete_direct": False,
            "reason": "PARTNER_REQUIRED" if row.get("partner_available") else "VEHICLE_ACCESS_REQUIRED",
        }
    return {
        "path": path,
        "access": access,
        "ok_for_complete_direct": access in {ACCESS_DIRECT, ACCESS_REG},
        "reason": None if access in {ACCESS_DIRECT, ACCESS_REG} else "PROCUREMENT_PATH_UNKNOWN",
    }


# ---------------------------------------------------------------------------
# History-first economics target + commercial validation helpers
# ---------------------------------------------------------------------------


def historical_price_range(evidence: list[dict[str, Any]]) -> dict[str, Any]:
    prices = []
    for e in evidence or []:
        if e.get("unit_price_status") == "TOTAL_ONLY_NOT_UNIT":
            continue
        p = _d(e.get("unit_price"))
        if p is not None:
            prices.append(p)
    if not prices:
        return {"sample_count": 0, "recent_low": None, "recent_median": None, "recent_high": None, "fabricated": False}
    if len(prices) == 1:
        return {
            "sample_count": 1,
            "recent_low": str(prices[0]),
            "recent_median": str(prices[0]),
            "recent_high": str(prices[0]),
            "fabricated": False,
            "note": "single_observation_not_a_statistical_range",
        }
    sp = sorted(prices)
    return {
        "sample_count": len(sp),
        "recent_low": str(min(sp)),
        "recent_median": str(sp[len(sp) // 2]),
        "recent_high": str(max(sp)),
        "fabricated": False,
    }


def compute_max_acquisition_cost(
    *,
    historical_unit_price: Any,
    quantity: Any,
    required_gross_margin_pct: Any = 25,
    known_freight: Any = 0,
    known_packaging: Any = 0,
    known_fees: Any = 0,
    known_financing: Any = 0,
    target_bid_unit: Any = None,
) -> dict[str, Any]:
    """History-first sourcing target. Not guaranteed profit. Margin ≠ markup."""
    hist = _d(historical_unit_price)
    qty = _d(quantity) or Decimal("1")
    margin = _d(required_gross_margin_pct) or Decimal("25")
    freight = _d(known_freight) or Decimal("0")
    packaging = _d(known_packaging) or Decimal("0")
    fees = _d(known_fees) or Decimal("0")
    financing = _d(known_financing) or Decimal("0")
    if hist is None:
        return {"ok": False, "reason": "HISTORY_NOT_FOUND", "preliminary": True}
    bid_unit = _d(target_bid_unit) or hist
    # revenue at target bid
    revenue = (bid_unit * qty).quantize(Decimal("0.01"))
    # max product cost such that (revenue - product - other) / revenue >= margin/100
    # product <= revenue * (1 - margin/100) - other
    other = freight + packaging + fees + financing
    max_product = (revenue * (Decimal("1") - margin / Decimal("100")) - other).quantize(Decimal("0.01"))
    max_landed_unit = (max_product / qty).quantize(Decimal("0.01")) if qty else None
    if max_product < 0:
        max_product = Decimal("0.00")
        max_landed_unit = Decimal("0.00")
    return {
        "ok": True,
        "preliminary": True,
        "label": "MAXIMUM_ALLOWABLE_ACQUISITION_COST",
        "reference_government_unit_price": str(hist),
        "target_unit_bid": str(bid_unit),
        "target_revenue": str(revenue),
        "required_gross_margin_pct": str(margin),
        "known_other_costs": str(other),
        "max_total_acquisition_cost": str(max_product),
        "max_landed_unit_cost": str(max_landed_unit) if max_landed_unit is not None else None,
        "note": "Sourcing target — not guaranteed profit; UNKNOWN costs keep this PRELIMINARY",
    }


def normalize_commercial_to_base(
    *,
    unit_price: Any,
    pack_quantity: Any = 1,
    seller_uom: str = "EA",
    government_base_uom: str = "EA",
) -> dict[str, Any]:
    price = _d(unit_price)
    pack = _d(pack_quantity) or Decimal("1")
    if price is None:
        return {"ok": False, "normalized_unit_price": None, "reason": "CURRENT_PRICE_NOT_FOUND"}
    su = str(seller_uom or "EA").upper()
    gu = str(government_base_uom or "EA").upper()
    if su in {"CS", "CASE", "BX", "BOX", "PK", "PACK"} and pack > 1:
        norm = (price / pack).quantize(Decimal("0.0001"))
        return {"ok": True, "normalized_unit_price": str(norm), "basis": f"{su}/{pack}→EA", "raw_price": str(price)}
    if su != gu and su not in {"EA", "EACH"} and gu in {"EA", "EACH"}:
        return {"ok": False, "normalized_unit_price": None, "reason": "UOM_CONFLICT", "seller_uom": su, "gov_uom": gu}
    return {"ok": True, "normalized_unit_price": str(price), "basis": "EA", "raw_price": str(price)}


def assess_supplier_competitor_risk(row: dict[str, Any], market_seller: str | None = None) -> dict[str, Any]:
    seller = (market_seller or "").strip().lower()
    signals = []
    state = COMP_NONE
    for a in row.get("historical_awards") or []:
        if not isinstance(a, dict):
            continue
        vendor = str(a.get("awarded_vendor") or a.get("vendor") or a.get("awardee") or a.get("winner") or "").strip().lower()
        if not vendor:
            continue
        signals.append(vendor)
        if seller and (seller in vendor or vendor in seller):
            state = COMP_INCUMBENT
        elif state == COMP_NONE:
            state = COMP_AWARDEE
    mfr = str(row.get("manufacturer") or "").strip().lower()
    if mfr and seller and (mfr in seller or seller in mfr):
        state = COMP_MFR_DIRECT
    if row.get("supplier_bids_same_nsn"):
        state = COMP_BIDDER
    return {
        "state": state if signals or state != COMP_NONE else COMP_UNKNOWN if not signals else state,
        "historical_vendors": sorted(set(signals))[:12],
        "fatal": False,
        "note": "Competitor signal surfaces risk — does not automatic-reject",
    }


def assess_execution_and_funding(row: dict[str, Any], blob: str | None = None) -> dict[str, Any]:
    text = blob or _blob(row)
    fob_m = _FOB.search(text)
    fob = fob_m.group(1).upper() if fob_m else (row.get("fob") or "FOB_UNKNOWN")
    if fob in {"ORIGIN", "DESTINATION"}:
        fob = f"FOB_{fob}"
    flags = []
    if _MIL_PACK.search(text):
        flags.append("SPECIAL_PACKAGING_REQUIRED")
    if re.search(r"\binstall(?:ation)?\b", text, re.I):
        flags.append("INSTALLATION_REQUIRED")
    if re.search(r"\bcustom(?:ization)?|made[- ]to[- ]order\b", text, re.I):
        flags.append("CUSTOMIZATION_REQUIRED")
    if re.search(r"\bpast\s+performance\b", text, re.I):
        flags.append("PAST_PERFORMANCE_REQUIRED")
    if re.search(r"\btechnical\s+data|drawing\s+required|tdmt\b", text, re.I):
        flags.append("TECH_DATA_REQUIRED")

    exec_state = EXEC_OK
    if "INSTALLATION_REQUIRED" in flags and not re.search(r"\bequipment\s+only|supply\s+only\b", text, re.I):
        exec_state = EXEC_INSTALL
    if "PAST_PERFORMANCE_REQUIRED" in flags:
        exec_state = EXEC_PAST_PERF
    if "CUSTOMIZATION_REQUIRED" in flags:
        exec_state = EXEC_CUSTOM
    if row.get("personal_guarantee_required") or row.get("personal_credit_required"):
        exec_state = EXEC_FUNDING

    funding = {
        "pre_delivery": {
            "routes": ["SUPPLIER_TERMS", "PO_FINANCE", "DIRECT_SUPPLIER_TO_GOVERNMENT", "DEPOSIT_REQUIRED", "COMPANY_CASH", "UNKNOWN"],
            "selected": row.get("pre_delivery_funding") or "UNKNOWN",
            "pg_required": "PG_REQUIRED" if row.get("personal_guarantee_required") else (
                "NO_PG_CONFIRMED" if row.get("no_pg_confirmed") else "UNKNOWN"
            ),
            "personal_credit_required": "PERSONAL_CREDIT_REQUIRED"
            if row.get("personal_credit_required")
            else ("NO_PERSONAL_CREDIT_CONFIRMED" if row.get("no_personal_credit_confirmed") else "UNKNOWN"),
            "bank_line_available": False,  # never default TRUE
            "note": "Unknown ≠ acceptable; bank/SBA not assumed for first contract",
        },
        "post_delivery": {
            "routes": ["AR_FACTORING", "ASSIGNMENT_OF_CLAIMS", "RECEIVABLE_FINANCE", "NORMAL_GOVERNMENT_PAYMENT", "UNKNOWN"],
            "selected": row.get("post_delivery_funding") or "UNKNOWN",
        },
    }
    fatal = exec_state in {EXEC_INSTALL, EXEC_PAST_PERF, EXEC_FUNDING, EXEC_APPROVED, EXEC_CERT}
    # SPECIAL packaging alone is not fatal
    return {
        "fob": fob,
        "inspection": row.get("inspection") or "UNKNOWN",
        "acceptance": row.get("acceptance") or "UNKNOWN",
        "delivery_days_after_award": row.get("delivery_days"),
        "flags": flags,
        "executability": exec_state,
        "fatal_for_startup_complete": fatal,
        "funding": funding,
        "mil_std_packaging": bool(_MIL_PACK.search(text)),
        "mil_std_automatic_reject": False,
    }


# ---------------------------------------------------------------------------
# Integrity evaluation — strengthens COMPLETE
# ---------------------------------------------------------------------------


def evaluate_integrity(row: dict[str, Any], *, pipeline_partial: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run integrity layers. Returns blockers preventing COMPLETE."""
    from micro_purchase_lab_source_router import route_plan_for_opportunity

    identity_in = None
    if pipeline_partial:
        identity_in = {"identity": pipeline_partial.get("identity"), "confidence": pipeline_partial.get("identity_confidence")}
    qty = extract_clins(row)
    variant = assess_variant_identity(row, identity_in)
    approved = assess_approved_source(row)
    action = assess_actionability(row)
    path = classify_procurement_path(row)
    execf = assess_execution_and_funding(row)
    routes = route_plan_for_opportunity(row)

    hist_evidence = (pipeline_partial or {}).get("historical_evidence") or []
    hist_range = historical_price_range(hist_evidence)
    hist_unit = (pipeline_partial or {}).get("last_government_unit_price")
    qty_base = None
    if qty.get("clins"):
        qty_base = (qty["clins"][0].get("quantity") or {}).get("normalized_base_quantity") or (
            qty["clins"][0].get("quantity") or {}
        ).get("raw_quantity")
    max_acq = compute_max_acquisition_cost(
        historical_unit_price=hist_unit,
        quantity=qty_base or row.get("quantity") or 1,
        required_gross_margin_pct=row.get("min_gross_margin_pct_target") or 25,
        known_freight=row.get("freight") or 0,
        known_packaging=row.get("packaging_cost") or 0,
    )
    seller = (pipeline_partial or {}).get("current_market_seller")
    competitor = assess_supplier_competitor_risk(row, seller)

    # Commercial UOM alignment
    market_price = (pipeline_partial or {}).get("current_public_price")
    commercial_norm = normalize_commercial_to_base(
        unit_price=market_price,
        pack_quantity=(pipeline_partial or {}).get("current_market_pack") or 1,
        seller_uom=(pipeline_partial or {}).get("current_market_uom") or "EA",
    )

    blockers: list[str] = []
    if not action.get("transactional"):
        blockers.append(action.get("reason") or action.get("state") or "NON_TRANSACTIONAL")
    if not variant.get("ok_for_complete"):
        blockers.append("VARIANT_CONFLICT" if variant.get("identity_state") == ID_VARIANT_CONFLICT else "IDENTITY_UNRESOLVED")
    if qty.get("overall_quantity_confidence") not in {QTY_EXACT, QTY_STRONG}:
        blockers.append("QUANTITY_UNRESOLVED" if qty.get("overall_quantity_confidence") == QTY_UNRESOLVED else "PACK_SIZE_CONFLICT")
    if qty.get("quantity_basis", {}).get("unresolved"):
        blockers.append("IDIQ_QUANTITY_BASIS_UNRESOLVED")
    if not approved.get("ok_for_complete"):
        blockers.append(approved.get("reason") or "TRACEABILITY_UNRESOLVED")
    if not path.get("ok_for_complete_direct") and path.get("reason"):
        blockers.append(path["reason"])
    if execf.get("fatal_for_startup_complete"):
        if execf.get("executability") == EXEC_PAST_PERF:
            blockers.append("PAST_PERFORMANCE_REQUIRED")
        elif execf.get("executability") == EXEC_INSTALL:
            blockers.append("INSTALLATION_HEAVY")
        elif execf.get("executability") == EXEC_FUNDING:
            blockers.append("FUNDING_RISK")
        else:
            blockers.append(str(execf.get("executability")))
    if "TECH_DATA_REQUIRED" in (execf.get("flags") or []) and row.get("tech_data_available") is False:
        blockers.append("TECH_DATA_REQUIRED_BUT_UNAVAILABLE")
    if not max_acq.get("ok"):
        blockers.append(max_acq.get("reason") or "HISTORY_NOT_FOUND")
    if market_price and not commercial_norm.get("ok"):
        blockers.append(commercial_norm.get("reason") or "UOM_CONFLICT")

    # History/market still required (pipeline) — integrity adds gates
    if not hist_unit:
        blockers.append("HISTORY_NOT_FOUND")
    if not market_price:
        blockers.append("CURRENT_PRICE_NOT_FOUND")

    blockers = list(dict.fromkeys(blockers))
    return {
        "build": BUILD_TAG,
        "signal_class": SIGNAL_TRANSACTIONAL,
        "quantity": qty,
        "variant_identity": variant,
        "approved_source": approved,
        "actionability": action,
        "procurement_path": path,
        "execution": execf,
        "historical_range": hist_range,
        "max_acquisition_cost": max_acq,
        "commercial_normalization": commercial_norm,
        "supplier_competitor_risk": competitor,
        "source_route_plan": routes,
        "complete_blockers": blockers,
        "integrity_allows_complete": len(blockers) == 0,
    }
