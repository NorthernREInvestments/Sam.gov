"""Phase L.2.3 — commercial identity recovery (brand/model), separate from MPN/NSN.

Does not replace Phase J. Overlays commercial exactness so manufacturer+model
products (Bobcat UW56, Ford F-150 Police Responder, Dell PowerEdge R670) enter
market research without weakening L.2.2 price verification.
"""

from __future__ import annotations

import re
from typing import Any

from application_clock import now_utc

# Commercial identity states
EXACT_MPN = "EXACT_MPN"
EXACT_NSN_MPN = "EXACT_NSN_MPN"
EXACT_COMMERCIAL_MODEL = "EXACT_COMMERCIAL_MODEL"
EXACT_OEM_SKU = "EXACT_OEM_SKU"
EXACT_BRAND_MODEL = "EXACT_BRAND_MODEL"
EXACT_VEHICLE_TRIM = "EXACT_VEHICLE_TRIM"
EXACT_EQUIPMENT_MODEL = "EXACT_EQUIPMENT_MODEL"
STRONG_COMMERCIAL_MODEL = "STRONG_COMMERCIAL_MODEL"
PARTIAL_COMMERCIAL_IDENTITY = "PARTIAL_COMMERCIAL_IDENTITY"
GENERIC_PRODUCT = "GENERIC_PRODUCT"
UNKNOWN_COMMERCIAL = "UNKNOWN"

# Configuration (separate from identity)
CFG_EXACT = "EXACT_CONFIGURATION"
CFG_PARTIAL = "PARTIAL_CONFIGURATION"
CFG_UNKNOWN = "UNKNOWN_CONFIGURATION"
CFG_CONFLICT = "CONFIGURATION_CONFLICT"

# Priceability
PRICEABILITY_HIGH = "HIGH"
PRICEABILITY_MEDIUM = "MEDIUM"
PRICEABILITY_LOW = "LOW"
PRICEABILITY_NONCOMMERCIAL = "NONCOMMERCIAL"
PRICEABILITY_UNKNOWN = "UNKNOWN"

# Profit research signals (not bid-ready)
UNIT_SPREAD_POSITIVE = "UNIT_SPREAD_POSITIVE"
UNIT_SPREAD_NEGATIVE = "UNIT_SPREAD_NEGATIVE"
TOTAL_SPREAD_POSITIVE = "TOTAL_SPREAD_POSITIVE"
EXPECTED_NET_POSITIVE = "EXPECTED_NET_POSITIVE"
EXPECTED_NET_GE_10K = "EXPECTED_NET_GE_10K"
PROMISING_UNIT_ECONOMICS = "PROMISING_UNIT_ECONOMICS"
QUANTITY_REQUIRED = "QUANTITY_REQUIRED"
TOTAL_PROFIT_UNKNOWN_QUANTITY = "TOTAL_PROFIT_UNKNOWN_QUANTITY"

_OEM_FROM_MODEL: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bToolCat\b|\bUW\d{2}\b", re.I), "Bobcat"),
    (re.compile(r"\bPowerEdge\b|\bLatitude\b|\bOptiPlex\b|\bPrecision\b", re.I), "Dell"),
    (re.compile(r"\bimageRUNNER\b|\bimageCLASS\b", re.I), "Canon"),
    (re.compile(r"\bThinkPad\b|\bThinkCentre\b|\bThinkStation\b", re.I), "Lenovo"),
    (re.compile(r"\bEliteBook\b|\bProBook\b|\bProLiant\b|\bLaserJet\b", re.I), "HP"),
    (re.compile(r"\bCatalyst\b|\bNexus\b|\bISR\d|\bASA\d|\bC9\d{3}\b", re.I), "Cisco"),
    (re.compile(r"\bPolice\s+Responder\b|\bExpedition\s+SSV\b", re.I), "Ford"),
]

_MODEL_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Equipment
    (
        re.compile(
            r"\b(?:Brand\s+Name\s+)?(?:Bobcat\s+)?(?:ToolCat\s+)?(UW\d{2})\b",
            re.I,
        ),
        EXACT_EQUIPMENT_MODEL,
    ),
    (
        re.compile(r"\b(?:Bobcat\s+)?ToolCat\s+(UW\d{2})\b", re.I),
        EXACT_EQUIPMENT_MODEL,
    ),
    # Vehicles
    (
        re.compile(
            r"\b(F[\-\s]?150\s+Police\s+Responder)\b",
            re.I,
        ),
        EXACT_VEHICLE_TRIM,
    ),
    (
        re.compile(r"\b(Expedition\s+SSV)\b", re.I),
        EXACT_VEHICLE_TRIM,
    ),
    (
        re.compile(
            r"\b(\d{1,2}[\-\s]?Passenger\s+Shuttle\s+Bus)\b",
            re.I,
        ),
        STRONG_COMMERCIAL_MODEL,
    ),
    (
        re.compile(
            r"\b((?:Mid[\-\s]?Size|Full[\-\s]?Size|Compact)\s+Sport\s+Utility\s+Vehicle)\b",
            re.I,
        ),
        STRONG_COMMERCIAL_MODEL,
    ),
    (
        re.compile(
            r"\b((?:Long\s+Reach\s+)?Tracked\s+Excavator(?:\s+\d+K\s*lbs)?)\b",
            re.I,
        ),
        STRONG_COMMERCIAL_MODEL,
    ),
    (
        re.compile(r"\b(Bridge\s+Inspection\s+Crane\s+Truck)\b", re.I),
        STRONG_COMMERCIAL_MODEL,
    ),
    (
        re.compile(r"\b(Low\s+Speed\s+Vehicle)\b", re.I),
        STRONG_COMMERCIAL_MODEL,
    ),
    (
        re.compile(r"\b(F[\-\s]?150|F[\-\s]?250|F[\-\s]?350|Explorer|Tahoe|Silverado)\b", re.I),
        STRONG_COMMERCIAL_MODEL,
    ),
    # IT servers / laptops
    (
        re.compile(r"\bPowerEdge\s+(R\d{3,4}(?:xa|xs)?)\b", re.I),
        EXACT_COMMERCIAL_MODEL,
    ),
    (
        re.compile(r"\b(Latitude\s+\d{4,}(?:\s+[A-Z0-9]+)?)\b", re.I),
        EXACT_COMMERCIAL_MODEL,
    ),
    (
        re.compile(r"\b(EliteBook\s+\d{3,4}\s*G\d{1,2})\b", re.I),
        EXACT_COMMERCIAL_MODEL,
    ),
    (
        re.compile(r"\b(ThinkPad\s+[A-Z]\d{1,3}(?:\s+Gen\s*\d+)?)\b", re.I),
        EXACT_COMMERCIAL_MODEL,
    ),
    # Monitors / electronics
    (
        re.compile(r"\b(?:ASUS|Asus)\s+([A-Z]{1,4}\d{2,}[A-Z0-9\-]*)\b"),
        EXACT_BRAND_MODEL,
    ),
    (
        re.compile(r"\b(VG\d{2}AQ[A-Z0-9]*)\b", re.I),
        EXACT_COMMERCIAL_MODEL,
    ),
    (
        re.compile(r"\b(PA\d{2}UC(?:XR)?)\b", re.I),
        EXACT_COMMERCIAL_MODEL,
    ),
    # Printers
    (
        re.compile(
            r"\bimageRUNNER(?:\s+ADVANCE)?(?:\s+DX)?\s+([A-Z]?\d{4,}i?)\b",
            re.I,
        ),
        EXACT_COMMERCIAL_MODEL,
    ),
    (
        re.compile(r"\bimageCLASS\s+([A-Z]{1,3}\s*[A-Z0-9\-]+)\b", re.I),
        EXACT_COMMERCIAL_MODEL,
    ),
    # Network / tools / meters
    (
        re.compile(r"\b(C9\d{3}[\-][A-Z0-9]+)\b", re.I),
        EXACT_OEM_SKU,
    ),
    (
        re.compile(r"\bFluke\s+(\d{2,}[A-Z]?)\b", re.I),
        EXACT_BRAND_MODEL,
    ),
    (
        re.compile(r"\bMilwaukee\s+(\d{4}[\-]\d{2})\b", re.I),
        EXACT_OEM_SKU,
    ),
    (
        re.compile(r"\bFLIR\s+([A-Z]\d{3,4}[A-Z0-9]*)\b", re.I),
        EXACT_BRAND_MODEL,
    ),
    (
        re.compile(r"\bCaterpillar\s+([A-Z0-9]{2,}\d[A-Z0-9]*)\b", re.I),
        EXACT_EQUIPMENT_MODEL,
    ),
    (
        re.compile(r"\bGenie\s+([A-Z]{1,3}\d{2,}[A-Z0-9\-]*)\b", re.I),
        EXACT_EQUIPMENT_MODEL,
    ),
]

_BRAND_RE = re.compile(
    r"\b(Dell|ASUS|Asus|Lenovo|HP|Hewlett[\-\s]?Packard|Canon|FLIR|Bobcat|Genie|"
    r"Caterpillar|\bCAT\b|Ford|Cisco|Milwaukee|Fluke|Apple|Microsoft|Epson|"
    r"Brother|Xerox|Sony|Panasonic|Bosch|DeWalt|Makita|John\s+Deere|Kubota|"
    r"Toyota|Chevrolet|GMC|Ram|Honda|Yamaha)\b",
    re.I,
)

_GENERIC_ONLY = re.compile(
    r"^(?:laptop|computer|monitor|printer|server|vehicle|truck|tool|pump|"
    r"generator|equipment|supplies|commodit(?:y|ies)|miscellaneous)\b",
    re.I,
)

_MIL_LOW = re.compile(
    r"\b(source[\-\s]?controlled|drawing[\-\s]?controlled|depot\s+repair|"
    r"overhaul|F[\-\s]?16|AH[\-\s]?64|aircraft|turbine|afterburner|"
    r"sustaining\s+engineering|BOA\s+holders?)\b",
    re.I,
)

_COMMERCIAL_FAMILY = re.compile(
    r"\b(laptop|monitor|printer|server|switch|router|tablet|camera|tool|"
    r"generator|pump|tractor|forklift|utility\s+work\s+machine|vehicle|"
    r"police|responder|mower|excavator|skid[\-\s]?steer)\b",
    re.I,
)

_EXACT_STATES = {
    EXACT_MPN,
    EXACT_NSN_MPN,
    EXACT_COMMERCIAL_MODEL,
    EXACT_OEM_SKU,
    EXACT_BRAND_MODEL,
    EXACT_VEHICLE_TRIM,
    EXACT_EQUIPMENT_MODEL,
}

_RESEARCHABLE_STATES = _EXACT_STATES | {STRONG_COMMERCIAL_MODEL}


def _utc() -> str:
    return now_utc().isoformat()


def _blob(row: dict[str, Any], extra: str | None = None) -> str:
    parts = [
        row.get("title"),
        row.get("description"),
        row.get("description_text"),
        row.get("ai_summary"),
        row.get("clin_text"),
        row.get("item_description"),
        extra,
    ]
    if isinstance(row.get("product_identity"), dict):
        pi = row["product_identity"]
        parts.extend([pi.get("nomenclature"), pi.get("model"), pi.get("manufacturer"), pi.get("mpn")])
    return " ".join(str(p) for p in parts if p)


def normalize_commercial_model(model: str | None) -> str | None:
    """Normalize whitespace/hyphen variants without collapsing distinct models."""
    if not model:
        return None
    s = str(model).strip()
    if not s:
        return None
    # F-150 / F 150 → F-150 canonical
    s = re.sub(r"\bF[\s\-]?(\d{2,3})\b", r"F-\1", s, flags=re.I)
    # PowerEdge-R670 → PowerEdge R670
    s = re.sub(r"(PowerEdge)[\s\-]+(R\d{3,4})", r"\1 \2", s, flags=re.I)
    # UW-56 → UW56 (ToolCat family only-safe: digits immediately after UW)
    s = re.sub(r"\bUW[\s\-]?(\d{2})\b", r"UW\1", s, flags=re.I)
    # C9300 48P → C9300-48P
    s = re.sub(r"\b(C9\d{3})\s+(\d{2}[A-Z]?)\b", r"\1-\2", s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def models_equivalent(a: str | None, b: str | None) -> bool:
    """True only when normalized forms match — never R660≈R670."""
    na = normalize_commercial_model(a)
    nb = normalize_commercial_model(b)
    if not na or not nb:
        return False
    return re.sub(r"[^A-Z0-9]+", "", na.upper()) == re.sub(r"[^A-Z0-9]+", "", nb.upper())


def infer_manufacturer(text: str, *, model: str | None = None) -> dict[str, Any] | None:
    """Deterministic OEM recovery from strong commercial evidence only."""
    blob = text or ""
    m = _BRAND_RE.search(blob)
    if m:
        name = m.group(1)
        if re.search(r"hewlett", name, re.I) or name.lower() == "hp":
            name = "HP"
        elif name.lower() == "asus":
            name = "ASUS"
        elif name.lower() == "flir":
            name = "FLIR"
        elif name.lower() == "gmc":
            name = "GMC"
        elif re.search(r"john\s+deere", name, re.I):
            name = "John Deere"
        elif name.lower() == "cat":
            name = "Caterpillar"
        else:
            name = name.title()
        return {
            "manufacturer": name,
            "source": "explicit_brand_in_text",
            "confidence": 0.95,
            "inferred": False,
        }
    combined = f"{blob} {model or ''}"
    for pat, oem in _OEM_FROM_MODEL:
        if pat.search(combined):
            return {
                "manufacturer": oem,
                "source": "model_family_map",
                "confidence": 0.9,
                "inferred": True,
            }
    return None


def extract_commercial_model(text: str) -> dict[str, Any] | None:
    """Parse exact commercial model / vehicle trim / equipment model from text."""
    blob = text or ""
    for pat, state in _MODEL_PATTERNS:
        m = pat.search(blob)
        if not m:
            continue
        raw = m.group(1) if m.lastindex else m.group(0)
        # Prefer fuller ToolCat form when present
        if state == EXACT_EQUIPMENT_MODEL and re.search(r"ToolCat", blob, re.I):
            display = f"ToolCat {normalize_commercial_model(raw)}"
        elif state == EXACT_VEHICLE_TRIM:
            display = normalize_commercial_model(raw)
            if "police responder" in raw.lower() and not display.lower().startswith("f-"):
                display = normalize_commercial_model(f"F-150 {raw}")
        elif state == EXACT_COMMERCIAL_MODEL and "PowerEdge" in m.group(0):
            display = f"PowerEdge {normalize_commercial_model(raw)}"
        elif state == EXACT_COMMERCIAL_MODEL and re.search(r"imageRUNNER", m.group(0), re.I):
            display = f"imageRUNNER {normalize_commercial_model(raw)}"
        else:
            display = normalize_commercial_model(raw) or str(raw).strip()
        return {
            "model": display,
            "raw_match": m.group(0).strip(),
            "commercial_identity_state": state,
            "match_span": m.span(),
        }
    return None


def classify_configuration_completeness(row: dict[str, Any], text: str) -> str:
    """Identity ≠ configuration. Incomplete config does not block research."""
    blob = (text or "").lower()
    title = str(row.get("title") or "").lower()
    # Vehicle year/trim without options → still partial config OK
    if re.search(r"\b(ram|cpu|processor|gb\s*ram|ssd|hdd|storage|gpu)\b", blob):
        # If multiple key attrs present → closer to exact
        attrs = sum(
            1
            for p in (
                r"\b\d+\s*gb\s*(ram|memory)",
                r"\b\d+\s*(tb|gb)\s*(ssd|hdd|nvme)",
                r"\b(intel|amd|xeon|core\s*i\d)",
            )
            if re.search(p, blob)
        )
        if attrs >= 2:
            return CFG_EXACT
        if attrs == 1:
            return CFG_PARTIAL
        return CFG_PARTIAL
    if re.search(r"poweredge|server|laptop|elitebook|latitude", title):
        return CFG_PARTIAL
    if re.search(r"police\s+responder|toolcat|f[\-\s]?150", title):
        # base machine known; attachments/options may be incomplete
        return CFG_PARTIAL
    return CFG_UNKNOWN


def classify_commercial_priceability(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any],
) -> dict[str, Any]:
    """HIGH/MEDIUM/LOW — prioritizes research; does not set bid eligibility."""
    state = commercial.get("commercial_identity_state") or UNKNOWN_COMMERCIAL
    blob = _blob(row)
    score = 0
    reasons: list[str] = []

    if state in {
        EXACT_COMMERCIAL_MODEL,
        EXACT_BRAND_MODEL,
        EXACT_VEHICLE_TRIM,
        EXACT_EQUIPMENT_MODEL,
        EXACT_OEM_SKU,
        EXACT_MPN,
        EXACT_NSN_MPN,
    }:
        score += 50
        reasons.append("exact_commercial_or_mpn")
    elif state == STRONG_COMMERCIAL_MODEL:
        score += 30
        reasons.append("strong_commercial_model")
    elif state == PARTIAL_COMMERCIAL_IDENTITY:
        score += 10
        reasons.append("partial_commercial")

    if commercial.get("manufacturer"):
        score += 15
        reasons.append("manufacturer_known")
    if commercial.get("model"):
        score += 15
        reasons.append("model_known")
    if _COMMERCIAL_FAMILY.search(blob):
        score += 10
        reasons.append("commercial_family")
    if commercial.get("mpn") or commercial.get("sku"):
        score += 10
        reasons.append("has_mpn_or_sku")

    if _MIL_LOW.search(blob) and state not in {
        EXACT_COMMERCIAL_MODEL,
        EXACT_BRAND_MODEL,
        EXACT_VEHICLE_TRIM,
        EXACT_EQUIPMENT_MODEL,
    }:
        score -= 35
        reasons.append("military_obscure_penalty")
    if state == GENERIC_PRODUCT:
        score -= 20
        reasons.append("generic_product")

    if score >= 55:
        level = PRICEABILITY_HIGH
    elif score >= 30:
        level = PRICEABILITY_MEDIUM
    elif score >= 10:
        level = PRICEABILITY_LOW
    elif _MIL_LOW.search(blob) and not commercial.get("model"):
        level = PRICEABILITY_NONCOMMERCIAL
    else:
        level = PRICEABILITY_LOW if score > 0 else PRICEABILITY_UNKNOWN

    return {
        "commercial_priceability": level,
        "priceability_score": score,
        "priceability_reasons": reasons,
    }


def recover_commercial_identity(
    row: dict[str, Any],
    *,
    phase_j_identity: dict[str, Any] | None = None,
    attachment_text: str | None = None,
) -> dict[str, Any]:
    """Recover commercial brand/model identity; preserve Phase J MPN/NSN when present."""
    phase_j_identity = phase_j_identity or {}
    text = _blob(row, attachment_text)

    mpn = phase_j_identity.get("mpn") or phase_j_identity.get("normalized_mpn") or row.get("mpn")
    nsn = phase_j_identity.get("nsn") or phase_j_identity.get("normalized_nsn") or row.get("nsn")
    sku = phase_j_identity.get("sku") or row.get("sku")

    # Title MPN recovery (P/N patterns)
    if not mpn:
        m = re.search(
            r"(?:P/?N|PN|Part\s*(?:Number|No\.?)|MPN)\s*[:_\-]?\s*([A-Z0-9][A-Z0-9\-_/]{3,})",
            text,
            re.I,
        )
        if m:
            mpn = m.group(1).strip().rstrip(".,;")

    model_hit = extract_commercial_model(text)
    model = None
    state = UNKNOWN_COMMERCIAL
    if model_hit:
        model = model_hit["model"]
        state = model_hit["commercial_identity_state"]
    elif phase_j_identity.get("model"):
        model = normalize_commercial_model(str(phase_j_identity["model"]))
        state = STRONG_COMMERCIAL_MODEL

    mfr_info = infer_manufacturer(text, model=model)
    manufacturer = (
        phase_j_identity.get("manufacturer")
        or phase_j_identity.get("brand")
        or row.get("manufacturer")
        or (mfr_info or {}).get("manufacturer")
    )

    # Prefer MPN/NSN+MPN when present
    if mpn and nsn:
        state = EXACT_NSN_MPN
    elif mpn and state not in {EXACT_VEHICLE_TRIM, EXACT_EQUIPMENT_MODEL, EXACT_COMMERCIAL_MODEL}:
        # Keep vehicle/equipment commercial exactness even if a spurious short PN appears
        if not model_hit:
            state = EXACT_MPN
    elif sku and not model_hit:
        state = EXACT_OEM_SKU

    # Brand Name <Brand> without model still partial-commercial if brand known
    if state == UNKNOWN_COMMERCIAL:
        bn = re.search(r"\bBrand\s+Name\s+([A-Za-z][A-Za-z0-9\-\s]{2,40})", text, re.I)
        if bn and manufacturer:
            state = PARTIAL_COMMERCIAL_IDENTITY
            if not model:
                # capture trailing product words after brand
                tail = bn.group(1).strip()
                if manufacturer.lower() in tail.lower():
                    rest = re.sub(re.escape(manufacturer), "", tail, flags=re.I).strip()
                    if len(rest) >= 3:
                        model = normalize_commercial_model(rest) or rest
                        state = STRONG_COMMERCIAL_MODEL

    if state == UNKNOWN_COMMERCIAL:
        if manufacturer and model:
            state = EXACT_BRAND_MODEL
        elif manufacturer or model:
            state = PARTIAL_COMMERCIAL_IDENTITY
        elif _GENERIC_ONLY.search((row.get("title") or "").strip()):
            state = GENERIC_PRODUCT
        elif _COMMERCIAL_FAMILY.search(text) and not mpn and not nsn:
            state = GENERIC_PRODUCT

    # Promote Phase-J exact MPN rows already classified
    if mpn and state == UNKNOWN_COMMERCIAL:
        state = EXACT_NSN_MPN if nsn else EXACT_MPN

    # Recover OEM when model matched but manufacturer missing
    if model_hit and not manufacturer:
        recovered = infer_manufacturer(text, model=model)
        if recovered:
            manufacturer = recovered["manufacturer"]
            mfr_info = recovered

    # Vehicle year + brand without trim still strong when F-150/Expedition etc. matched earlier
    if state == UNKNOWN_COMMERCIAL and manufacturer and re.search(
        r"\b(vehicle|truck|suv|bus|excavator|crane|generator)\b", text, re.I
    ):
        state = PARTIAL_COMMERCIAL_IDENTITY

    # STRONG commercial equipment/vehicle without brand still researchable at MEDIUM
    if state == STRONG_COMMERCIAL_MODEL and not manufacturer:
        pass  # keep strong; priceability classifier handles score

    config = classify_configuration_completeness(row, text)

    out = {
        "kind": "PhaseL23CommercialIdentity",
        "build": "20260926-m3-phase-l23-commercial-identity-recovery",
        "commercial_identity_state": state,
        "manufacturer": manufacturer,
        "manufacturer_source": (mfr_info or {}).get("source")
        or ("phase_j" if phase_j_identity.get("manufacturer") else None),
        "manufacturer_inferred": bool((mfr_info or {}).get("inferred")),
        "manufacturer_confidence": (mfr_info or {}).get("confidence"),
        "model": model,
        "model_normalized": normalize_commercial_model(model),
        "brand": manufacturer,
        "mpn": mpn,
        "sku": sku,
        "nsn": nsn,
        "configuration_completeness": config,
        "raw_title": row.get("title"),
        "recovered_at": _utc(),
    }
    priceability = classify_commercial_priceability(row, commercial=out)
    out.update(priceability)

    research_eligible = market_research_eligible(row, commercial=out)
    out["market_research_eligible"] = research_eligible["market_research_eligible"]
    out["market_research_reason"] = research_eligible["reason"]
    out["drop_reason"] = research_eligible.get("drop_reason")
    return out


def market_research_eligible(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any],
) -> dict[str, Any]:
    """Separate from final identity confidence and from economics eligibility."""
    state = commercial.get("commercial_identity_state")
    priceability = commercial.get("commercial_priceability")

    if state in {GENERIC_PRODUCT, UNKNOWN_COMMERCIAL}:
        return {
            "market_research_eligible": False,
            "reason": "GENERIC_PRODUCT_ONLY" if state == GENERIC_PRODUCT else "NO_COMMERCIAL_IDENTITY",
            "drop_reason": "NO_COMMERCIAL_IDENTITY" if state == UNKNOWN_COMMERCIAL else "GENERIC_PRODUCT_ONLY",
        }
    if state == PARTIAL_COMMERCIAL_IDENTITY and priceability in {
        PRICEABILITY_LOW,
        PRICEABILITY_NONCOMMERCIAL,
        PRICEABILITY_UNKNOWN,
    }:
        return {
            "market_research_eligible": False,
            "reason": "PARTIAL_TOO_WEAK",
            "drop_reason": "NO_COMMERCIAL_IDENTITY",
        }
    if state in _RESEARCHABLE_STATES or (
        state == PARTIAL_COMMERCIAL_IDENTITY and priceability in {PRICEABILITY_HIGH, PRICEABILITY_MEDIUM}
    ):
        return {
            "market_research_eligible": True,
            "reason": f"state={state};priceability={priceability}",
            "drop_reason": None,
        }
    if commercial.get("mpn") or (commercial.get("model") and commercial.get("manufacturer")):
        return {
            "market_research_eligible": True,
            "reason": "mpn_or_brand_model",
            "drop_reason": None,
        }
    return {
        "market_research_eligible": False,
        "reason": "NO_COMMERCIAL_IDENTITY",
        "drop_reason": "NO_COMMERCIAL_IDENTITY",
    }


def apply_commercial_overlay_to_screen(
    screen: dict[str, Any],
    row: dict[str, Any],
    *,
    attachment_text: str | None = None,
) -> dict[str, Any]:
    """Merge commercial recovery onto Phase L identity screen without rebuilding Phase J."""
    commercial = recover_commercial_identity(
        row,
        phase_j_identity=screen.get("identity") or {},
        attachment_text=attachment_text,
    )
    out = dict(screen)
    out["commercial_identity"] = commercial
    out["commercial_identity_state"] = commercial.get("commercial_identity_state")
    out["commercial_priceability"] = commercial.get("commercial_priceability")
    out["market_research_eligible"] = bool(commercial.get("market_research_eligible"))
    out["configuration_completeness"] = commercial.get("configuration_completeness")

    # Promote researchable when commercial exactness exists (even if Phase J said PARTIAL)
    if commercial.get("market_research_eligible"):
        out["researchable"] = True
        out["l23_researchable"] = True
        # Do not rewrite Phase J identity_research_state — add parallel commercial flag
        if screen.get("identity_research_state") in {
            "IDENTITY_PARTIAL",
            "IDENTITY_INSUFFICIENT",
            "OR_EQUAL_RESEARCHABLE",
        } and commercial.get("commercial_identity_state") in _EXACT_STATES:
            out["identity_research_state_commercial"] = "IDENTITY_EXACT_COMMERCIAL"
        elif commercial.get("commercial_identity_state") == STRONG_COMMERCIAL_MODEL:
            out["identity_research_state_commercial"] = "IDENTITY_STRONG_COMMERCIAL"

    # Merge recovered fields into identity dict for downstream search builders
    identity = dict(screen.get("identity") or {})
    if commercial.get("manufacturer") and not identity.get("manufacturer"):
        identity["manufacturer"] = commercial["manufacturer"]
    if commercial.get("model") and not identity.get("model"):
        identity["model"] = commercial["model"]
    if commercial.get("mpn") and not identity.get("mpn"):
        identity["mpn"] = commercial["mpn"]
        identity["normalized_mpn"] = commercial["mpn"]
    if commercial.get("brand"):
        identity["brand"] = commercial["brand"]
    identity["commercial_identity_state"] = commercial.get("commercial_identity_state")
    identity["configuration_completeness"] = commercial.get("configuration_completeness")
    out["identity"] = identity
    return out


def compute_unit_spread(
    *,
    historical_unit_price: float | None,
    public_retail_unit_price: float | None,
    quantity: float | None = None,
    financing_rate: float = 0.05,
) -> dict[str, Any]:
    """Unit economics without requiring quantity. Total profit stays unknown if qty missing."""
    hist = float(historical_unit_price) if historical_unit_price is not None else None
    retail = float(public_retail_unit_price) if public_retail_unit_price is not None else None
    if hist is None or retail is None:
        return {
            "unit_raw_spread": None,
            "unit_financing_estimate": None,
            "unit_net_spread_est": None,
            "signals": [],
            "total_status": None,
            "research_signal": None,
        }
    unit_spread = round(hist - retail, 2)
    unit_fin = round(retail * financing_rate, 2)
    unit_net = round(unit_spread - unit_fin, 2)
    signals: list[str] = []
    if unit_spread > 0:
        signals.append(UNIT_SPREAD_POSITIVE)
    elif unit_spread < 0:
        signals.append(UNIT_SPREAD_NEGATIVE)

    total_status = None
    research_signal = None
    total_spread = None
    if quantity is not None:
        total_spread = round(unit_spread * float(quantity), 2)
        if total_spread > 0:
            signals.append(TOTAL_SPREAD_POSITIVE)
        total_status = "TOTAL_KNOWN"
    else:
        total_status = TOTAL_PROFIT_UNKNOWN_QUANTITY
        if unit_spread > 0:
            research_signal = f"{PROMISING_UNIT_ECONOMICS} / {QUANTITY_REQUIRED}"
        else:
            research_signal = QUANTITY_REQUIRED

    return {
        "historical_unit_price": hist,
        "public_retail_unit_price": retail,
        "unit_raw_spread": unit_spread,
        "unit_financing_estimate": unit_fin,
        "unit_net_spread_est": unit_net,
        "quantity": quantity,
        "total_raw_spread": total_spread,
        "total_status": total_status,
        "signals": signals,
        "research_signal": research_signal,
        "economics_eligible_total": quantity is not None,
    }


def commercial_bucket(commercial: dict[str, Any]) -> str:
    """Validation bucket for L.2.3 sampling."""
    state = commercial.get("commercial_identity_state")
    if state in {
        EXACT_COMMERCIAL_MODEL,
        EXACT_BRAND_MODEL,
        EXACT_VEHICLE_TRIM,
        EXACT_EQUIPMENT_MODEL,
    }:
        return "A_EXACT_COMMERCIAL_MODEL"
    if state in {EXACT_MPN, EXACT_NSN_MPN, EXACT_OEM_SKU} or commercial.get("mpn"):
        return "B_EXACT_MPN_SKU"
    if state == STRONG_COMMERCIAL_MODEL or (
        commercial.get("market_research_eligible")
        and state == PARTIAL_COMMERCIAL_IDENTITY
    ):
        return "C_STRONG_OR_RECOVERY"
    return "OTHER"
