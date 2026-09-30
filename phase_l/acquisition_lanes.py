"""Phase L.3 — acquisition-path lanes, commercial score, reverse economics."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc

ROOT = Path(__file__).resolve().parents[1]
FAMILY_MEMORY_PATH = ROOT / "data" / "phase_l3_product_family_memory.json"
BUYER_MEMORY_PATH = ROOT / "data" / "phase_l3_buyer_commercial_memory.json"

# ---------------------------------------------------------------------------
# Lanes (ranking priority order)
# ---------------------------------------------------------------------------
COMMERCIAL_OPEN_CHANNEL = "COMMERCIAL_OPEN_CHANNEL"
COMMERCIAL_DISTRIBUTOR_CHANNEL = "COMMERCIAL_DISTRIBUTOR_CHANNEL"
QUOTE_REQUIRED_COMMERCIAL = "QUOTE_REQUIRED_COMMERCIAL"
MILSPEC_OPEN_CHANNEL = "MILSPEC_OPEN_CHANNEL"
UNKNOWN_ACQUISITION_CHANNEL = "UNKNOWN_ACQUISITION_CHANNEL"
MILSPEC_SPECIALTY = "MILSPEC_SPECIALTY"
SOURCE_APPROVAL_REQUIRED = "SOURCE_APPROVAL_REQUIRED"
SOLE_SOURCE_RESTRICTED = "SOLE_SOURCE_RESTRICTED"

LANE_PRIORITY = {
    COMMERCIAL_OPEN_CHANNEL: 1,
    COMMERCIAL_DISTRIBUTOR_CHANNEL: 2,
    QUOTE_REQUIRED_COMMERCIAL: 3,
    MILSPEC_OPEN_CHANNEL: 4,
    UNKNOWN_ACQUISITION_CHANNEL: 5,
    MILSPEC_SPECIALTY: 6,
    SOURCE_APPROVAL_REQUIRED: 7,
    SOLE_SOURCE_RESTRICTED: 8,
}

SPECIALTY_ACQUISITION_RESEARCH = "SPECIALTY_ACQUISITION_RESEARCH"
SPECIALTY_PRODUCT_PIPELINE = "SPECIALTY_PRODUCT_PIPELINE"
KNOWN_COMMERCIAL_ACQUISITION_PATH = "KNOWN_COMMERCIAL_ACQUISITION_PATH"
HISTORICALLY_DIFFICULT_ACQUISITION = "HISTORICALLY_DIFFICULT_ACQUISITION"
KNOWN_REPEAT_COMMERCIAL_PRODUCT = "KNOWN_REPEAT_COMMERCIAL_PRODUCT"

# Economic states (do not flatten missing retail into one failure)
VERIFIED_POSITIVE = "VERIFIED_POSITIVE"
APPARENT_POSITIVE = "APPARENT_POSITIVE"
PROMISING_REQUIRES_PRICE_VERIFICATION = "PROMISING_REQUIRES_PRICE_VERIFICATION"
PROMISING_REQUIRES_SUPPLIER_QUOTE = "PROMISING_REQUIRES_SUPPLIER_QUOTE"
MAX_BUY_PRICE_CALCULATED = "MAX_BUY_PRICE_CALCULATED"
CURRENT_PRICE_FOUND_HISTORY_MISSING = "CURRENT_PRICE_FOUND_HISTORY_MISSING"
HISTORY_FOUND_PRICE_MISSING = "HISTORY_FOUND_PRICE_MISSING"
SPECIALTY_CHANNEL = "SPECIALTY_CHANNEL"
SOURCE_APPROVAL_BLOCKED = "SOURCE_APPROVAL_BLOCKED"
ECONOMICALLY_NEGATIVE = "ECONOMICALLY_NEGATIVE"
INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
SUPPLIER_QUOTE_REQUIRED = "SUPPLIER_QUOTE_REQUIRED"

STAGE3_NO_ROW_CAP = True
DEEP_RESEARCH_NO_FIXED_COUNT = True
MANUAL_QUEUE_NO_FIXED_CAP = True

_COMMERCIAL_BRAND = re.compile(
    r"\b("
    r"Dell|HP|Hewlett[\-\s]?Packard|Lenovo|Cisco|HPE|ASUS|Acer|Apple|"
    r"Canon|Epson|Brother|Xerox|Samsung|LG|"
    r"Ford|Chevrolet|Chevy|GMC|Ram|Dodge|Stellantis|"
    r"Bobcat|Caterpillar|\bCAT\b|John\s+Deere|Kubota|JCB|Komatsu|Volvo|"
    r"Case\s+IH|CASE\s+(?:Construction|skid|loader|excavator)|"
    r"Milwaukee|DeWalt|Makita|Bosch|Hilti|Snap[\-\s]?on|RIDGID|Fluke|"
    r"Keysight|Tektronix|Agilent|Thermo\s+Fisher|"
    r"Grainger|Zoro|Mouser|Digi[\-\s]?Key"
    r")\b",
    re.I,
)
_COMMERCIAL_CATEGORY = re.compile(
    r"\b("
    r"laptop|desktop|server|monitor|printer|scanner|UPS|router|switch|"
    r"pickup|SUV|sport\s+utility|mid[\-\s]?size|van|vehicles?|"
    r"police\s+responder|fleet|utility\s+vehicle|trailer|shuttle\s+bus|"
    r"skid\s+steer|toolcat|tractor|mower|forklift|excavator|loader|crane|"
    r"drill|grinder|welder|compressor|generator|"
    r"pumps?|motors?|bearing|valves?|filter|hose|fitting|"
    r"helmet|ppe|fall\s+protection|"
    r"furniture|shelving|appliance|workstations?|"
    r"multimeter|oscilloscope|analyzer|calibration"
    r")\b",
    re.I,
)
_FLEET_OR_EQUIPMENT = re.compile(
    r"\b("
    r"vehicles?|SUV|pickup|van|fleet|police|SSV|PPV|responder|"
    r"excavator|loader|crane|tractor|forklift|mower|trailer|"
    r"toolcat|skid\s+steer|utility\s+work|shuttle\s+bus|"
    r"heavy\s+equipment|grounds\s+equipment|compact\s+tractor"
    r")\b",
    re.I,
)
_QUOTE_ONLY = re.compile(
    r"\b("
    r"call\s+for\s+price|request\s+(a\s+)?quote|contact\s+(your\s+)?dealer|"
    r"dealer\s+pricing|configure\s+and\s+quote|distributor\s+quote|"
    r"price\s+unavailable\s+online|account\s+pricing|government\s+quote|"
    r"RFQ\s+only|quote\s+required|see\s+dealer"
    r")\b",
    re.I,
)
_MILSPEC = re.compile(
    r"\b("
    r"NSN|NIIN|CAGE|QPL|QML|MIL[\-\s]?SPEC|DODICS|WSDC|"
    r"end[\s_]+item|aircraft|F[\-\s]?1[056]|AH[\-\s]?64|KC[\-\s]?135|"
    r"SPRTA|DLA|DIBBS|munition|ordnance"
    r")\b"
    r"|_NSN_|_End_Item_|End_Item_"
    r"|\b\d{4}-\d{2}-\d{3}-\d{4}\b",  # NIIN / NSN dash form
    re.I,
)
_SOURCE_APPROVAL = re.compile(
    r"\b("
    r"source\s+approval|approved\s+source\s+list|QPL|QML|BOAST|BOA\b|"
    r"manufacturer\s+authorization\s+required|OEM\s+authorized\s+only|"
    r"must\s+be\s+oem\s+authorized"
    r")\b",
    re.I,
)
_SOLE = re.compile(
    r"\b(sole[\-\s]?source|single[\-\s]?source|brand[\-\s]?name\s+only(?!\s+or\s+equal))\b",
    re.I,
)
_DISTRIBUTOR = re.compile(
    r"\b(authorized\s+distributor|industrial\s+distributor|VAR|value[\-\s]?added\s+reseller|"
    r"wholesale|dealer\s+network|manufacturer\s+dealer)\b",
    re.I,
)
_MODELISH = re.compile(
    r"\b([A-Z]{1,5}[\-\s]?\d{2,5}[A-Z0-9\-]*|"
    r"PowerEdge|Latitude|ToolCat|F[\-\s]?150|Expedition|Police\s+Responder)\b",
    re.I,
)


def _utc() -> str:
    return now_utc().isoformat()


def _blob(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> str:
    commercial = commercial or {}
    return " ".join(
        str(x or "")
        for x in (
            row.get("title"),
            row.get("description"),
            row.get("solicitation_id"),
            commercial.get("manufacturer"),
            commercial.get("model"),
            commercial.get("mpn"),
            commercial.get("commercial_identity_state"),
            row.get("nsn"),
            row.get("agency"),
        )
    )


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_json(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def commercial_acquisition_score(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    family_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """0–100 commercial acquisition path score (cheap signals only)."""
    commercial = commercial or {}
    blob = _blob(row, commercial)
    score = 0
    factors: list[str] = []

    if commercial.get("manufacturer") and commercial.get("model"):
        score += 25
        factors.append("manufacturer_model")
    elif commercial.get("model") or commercial.get("mpn"):
        score += 12
        factors.append("model_or_mpn")
    if _COMMERCIAL_BRAND.search(blob):
        score += 20
        factors.append("known_brand")
    if _COMMERCIAL_CATEGORY.search(blob):
        score += 15
        factors.append("commercial_category")
    if _MODELISH.search(blob) and not _MILSPEC.search(blob):
        score += 10
        factors.append("commercial_modelish")
    if _DISTRIBUTOR.search(blob):
        score += 8
        factors.append("distributor_language")
    if _QUOTE_ONLY.search(blob):
        score += 5
        factors.append("quote_channel_signal")
    if _MILSPEC.search(blob):
        score -= 25
        factors.append("milspec_language")
    if _SOURCE_APPROVAL.search(blob):
        score -= 35
        factors.append("source_approval")
    if _SOLE.search(blob):
        score -= 40
        factors.append("sole_source")
    # Family memory
    fam_key = _family_key(commercial, row)
    if family_memory and fam_key:
        rec = (family_memory.get("families") or {}).get(fam_key) or {}
        if rec.get("status") == KNOWN_COMMERCIAL_ACQUISITION_PATH:
            score += 20
            factors.append("known_commercial_family")
        if rec.get("status") == HISTORICALLY_DIFFICULT_ACQUISITION:
            score -= 15
            factors.append("historically_difficult_family")

    score = max(0, min(100, score))
    if score >= 80:
        band = "very_strong"
    elif score >= 60:
        band = "strong"
    elif score >= 40:
        band = "researchable"
    elif score >= 20:
        band = "difficult"
    else:
        band = "specialty_restricted"
    return {"score": score, "band": band, "factors": factors, "family_key": fam_key}


def _family_key(commercial: dict[str, Any], row: dict[str, Any]) -> str | None:
    mfr = str(commercial.get("manufacturer") or "").strip().upper()
    model = str(commercial.get("model") or commercial.get("mpn") or "").strip().upper()
    if mfr and model:
        return f"{mfr}|{model}"
    brand = _COMMERCIAL_BRAND.search(_blob(row, commercial))
    if brand and model:
        return f"{brand.group(1).upper()}|{model}"
    if model and len(model) >= 4:
        return f"|{model}"
    return None


def classify_acquisition_lane(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    access: dict[str, Any] | None = None,
    family_memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Cheap lane classification — NOT an eligibility rejection."""
    commercial = commercial or {}
    access = access or {}
    blob = _blob(row, commercial)
    cas = commercial_acquisition_score(row, commercial=commercial, family_memory=family_memory)
    score = cas["score"]

    blockers = list(access.get("access_blockers") or [])
    our = str(row.get("our_bid_access") or access.get("our_bid_access") or "").upper()

    lane = UNKNOWN_ACQUISITION_CHANNEL
    queue = None
    reason = "insufficient_channel_signals"

    if _SOLE.search(blob) or our == "NO" and "sole" in " ".join(blockers).lower():
        lane = SOLE_SOURCE_RESTRICTED
        queue = SPECIALTY_PRODUCT_PIPELINE
        reason = "sole_source_restricted"
    elif _SOURCE_APPROVAL.search(blob) or any("source_approval" in str(b).lower() for b in blockers):
        lane = SOURCE_APPROVAL_REQUIRED
        queue = SPECIALTY_PRODUCT_PIPELINE
        reason = "source_approval_required"
    elif _QUOTE_ONLY.search(blob) and (_COMMERCIAL_BRAND.search(blob) or _COMMERCIAL_CATEGORY.search(blob) or score >= 40):
        lane = QUOTE_REQUIRED_COMMERCIAL
        reason = "quote_driven_commercial_channel"
    elif _MILSPEC.search(blob) and score >= 45:
        lane = MILSPEC_OPEN_CHANNEL
        reason = "milspec_with_commercial_path"
    elif _MILSPEC.search(blob) and (
        _COMMERCIAL_BRAND.search(blob)
        or commercial.get("manufacturer")
        or commercial.get("model")
        or commercial.get("mpn")
    ):
        # NSN alone ≠ MILSPEC_SPECIALTY when commercial brand/model/MPN signals exist
        lane = MILSPEC_OPEN_CHANNEL
        reason = "nsn_with_commercial_channel_signals"
    elif _MILSPEC.search(blob) and score < 40:
        lane = MILSPEC_SPECIALTY
        queue = SPECIALTY_ACQUISITION_RESEARCH
        reason = "milspec_specialty_weak_commercial"
    # Fleet / heavy equipment with commercial brand → quote-driven commercial (not UNKNOWN)
    elif (
        (_COMMERCIAL_BRAND.search(blob) or commercial.get("manufacturer") or commercial.get("model"))
        and _FLEET_OR_EQUIPMENT.search(blob)
        and not _MILSPEC.search(blob)
    ):
        lane = QUOTE_REQUIRED_COMMERCIAL
        reason = "fleet_equipment_commercial_quote_typical"
    elif _FLEET_OR_EQUIPMENT.search(blob) and not _MILSPEC.search(blob):
        lane = QUOTE_REQUIRED_COMMERCIAL
        reason = "commercial_category_quote_typical"
    elif score >= 60 and (_COMMERCIAL_BRAND.search(blob) or _COMMERCIAL_CATEGORY.search(blob)):
        if _DISTRIBUTOR.search(blob) and not _FLEET_OR_EQUIPMENT.search(blob):
            lane = COMMERCIAL_DISTRIBUTOR_CHANNEL
            reason = "distributor_channel"
        elif _FLEET_OR_EQUIPMENT.search(blob):
            lane = QUOTE_REQUIRED_COMMERCIAL
            reason = "high_score_fleet_quote"
        else:
            lane = COMMERCIAL_OPEN_CHANNEL
            reason = "commercial_open_channel"
    elif score >= 45:
        if _FLEET_OR_EQUIPMENT.search(blob):
            lane = QUOTE_REQUIRED_COMMERCIAL
            reason = "likely_quote_fleet_channel"
        else:
            lane = COMMERCIAL_DISTRIBUTOR_CHANNEL
            reason = "likely_distributor_channel"
    elif score >= 30 and (_COMMERCIAL_CATEGORY.search(blob) or commercial.get("model")):
        if _FLEET_OR_EQUIPMENT.search(blob) or re.search(r"\b(equipment|bobcat|toolcat)\b", blob, re.I):
            lane = QUOTE_REQUIRED_COMMERCIAL
            reason = "equipment_fleet_quote_typical"
        else:
            lane = COMMERCIAL_DISTRIBUTOR_CHANNEL
            reason = "researchable_commercial"
    elif _MILSPEC.search(blob):
        lane = MILSPEC_SPECIALTY
        queue = SPECIALTY_ACQUISITION_RESEARCH
        reason = "milspec_default_specialty"
    else:
        lane = UNKNOWN_ACQUISITION_CHANNEL
        reason = "unknown_needs_limited_discovery"

    # Hard rule: difficult lane ≠ rejected opportunity
    rejected = False
    return {
        "kind": "AcquisitionLaneClassification",
        "acquisition_lane": lane,
        "lane_priority": LANE_PRIORITY.get(lane, 99),
        "commercial_acquisition_score": cas,
        "queue": queue,
        "reason": reason,
        "rejected": rejected,
        "specialty_pipeline": lane
        in {MILSPEC_SPECIALTY, SOURCE_APPROVAL_REQUIRED, SOLE_SOURCE_RESTRICTED},
        "primary_research_priority": lane
        in {
            COMMERCIAL_OPEN_CHANNEL,
            COMMERCIAL_DISTRIBUTOR_CHANNEL,
            QUOTE_REQUIRED_COMMERCIAL,
            MILSPEC_OPEN_CHANNEL,
        },
        "quote_required": lane == QUOTE_REQUIRED_COMMERCIAL,
        "classified_at": _utc(),
    }


def buyer_commercial_fit_score(row: dict[str, Any], *, buyer_memory: dict[str, Any] | None = None) -> dict[str, Any]:
    buyer = str(row.get("agency") or row.get("buyer") or row.get("department") or "").strip()
    blob = _blob(row)
    score = 0
    factors = []
    if re.search(
        r"\b(city|county|school|university|utility|transit|airport|public\s+works|"
        r"police|sheriff|fire|fleet|facilities|municipal)\b",
        buyer + " " + blob,
        re.I,
    ):
        score += 40
        factors.append("commercial_buyer_type")
    if re.search(r"\b(DLA|DoD|Army|Navy|Air\s+Force|USAF|SPRTA)\b", buyer + " " + blob, re.I):
        score -= 15
        factors.append("defense_buyer")
    if buyer_memory and buyer:
        rec = (buyer_memory.get("buyers") or {}).get(buyer.upper()) or {}
        score += int(rec.get("commercial_fit_bonus") or 0)
        if rec.get("publishes_bid_tabs"):
            score += 15
            factors.append("publishes_bid_tabs")
    score = max(0, min(100, score))
    return {"buyer": buyer or None, "score": score, "factors": factors}


def calculate_maximum_buy_price(
    *,
    government_unit: float | None,
    quantity: float | None = 1.0,
    financing_rate: float = 0.10,
    freight_reserve: float | None = None,
    other_fees: float = 0.0,
    desired_profits: tuple[float, ...] = (0.0, 5000.0, 10000.0, 25000.0),
) -> dict[str, Any] | None:
    """Reverse economics: government value → maximum supplier acquisition price."""
    gov = _f(government_unit)
    if gov is None or gov <= 0:
        return None
    qty = _f(quantity) or 1.0
    revenue = gov * qty
    freight = float(freight_reserve or 0.0)
    fees = float(other_fees or 0.0)
    # financing approximated as rate * acquisition; solve roughly:
    # revenue - acq - financing*acq - freight - fees - profit >= 0
    # acq * (1+rate) <= revenue - freight - fees - profit
    ceilings = {}
    for profit in desired_profits:
        numer = revenue - freight - fees - profit
        denom = 1.0 + float(financing_rate or 0.0)
        if denom <= 0:
            continue
        unit_ceil = numer / denom / qty
        label = "break_even" if profit == 0 else f"profit_{int(profit)}"
        ceilings[label] = round(unit_ceil, 2) if unit_ceil > 0 else None
    target = ceilings.get("profit_10000") or ceilings.get("break_even")
    return {
        "kind": "MAXIMUM_BUY_PRICE",
        "government_unit": gov,
        "quantity": qty,
        "financing_rate": financing_rate,
        "freight_reserve": freight,
        "other_fees": fees,
        "unit_ceilings": ceilings,
        "maximum_acquisition_unit": target,
        "status": MAX_BUY_PRICE_CALCULATED if target else INSUFFICIENT_EVIDENCE,
        "label": f"SUPPLIER_QUOTE_REQUIRED — TARGET ≤ ${target:,.0f}" if target else None,
    }


def detect_quote_channel_signals(text: str) -> bool:
    return bool(_QUOTE_ONLY.search(text or ""))


def generate_supplier_candidates(
    *,
    row: dict[str, Any],
    commercial: dict[str, Any] | None = None,
    family: str | None = None,
    limit: int = 6,
) -> list[dict[str, Any]]:
    """Public supplier candidates for quote-required commercial — NO outreach."""
    from phase_l.acquisition_pricing import FAMILY_SOURCES, infer_product_family
    from phase_l.market_price import _domain_search_url
    from urllib.parse import quote_plus

    commercial = commercial or {}
    fam = family or infer_product_family(row, commercial)
    key = commercial.get("primary_mpn") or commercial.get("mpn") or commercial.get("model")
    if not key:
        m = _MODELISH.search(_blob(row, commercial))
        key = m.group(1) if m else None
    if not key:
        return []
    out = []
    for domain, stype in (FAMILY_SOURCES.get(fam) or FAMILY_SOURCES["MRO"])[:limit]:
        url = _domain_search_url(domain, str(key)) or f"https://www.{domain}/search?q={quote_plus(str(key))}"
        out.append(
            {
                "supplier_domain": domain,
                "source_type": stype,
                "authorized_status": "UNKNOWN",
                "product_fit": fam,
                "url": url,
                "contact_path": "public_web_only",
                "government_sales_mentioned": None,
                "outreach_authorized": False,
            }
        )
    return out


def prepare_quote_packet_l3(
    *,
    row: dict[str, Any],
    commercial: dict[str, Any] | None = None,
    max_buy: dict[str, Any] | None = None,
    suppliers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Reuse quote-packet pattern — CONTENT ONLY, do not send."""
    commercial = commercial or {}
    title = (row.get("title") or "Opportunity")[:80]
    target = (max_buy or {}).get("maximum_acquisition_unit")
    body_lines = [
        f"Supplier quote request — {title}",
        "",
        f"Manufacturer: {commercial.get('manufacturer') or 'TBD'}",
        f"Model: {commercial.get('model') or 'TBD'}",
        f"MPN: {commercial.get('mpn') or commercial.get('primary_mpn') or 'TBD'}",
        f"NSN: {row.get('nsn') or commercial.get('nsn') or 'N/A'}",
        f"Quantity: {row.get('quantity') or 'TBD'}",
        f"Condition: New unless otherwise specified",
        f"Solicitation: {row.get('solicitation_id') or row.get('notice_id') or 'TBD'}",
        f"Quote deadline: {row.get('response_deadline') or row.get('due_date') or 'TBD'}",
        f"Target buy price (unit): ≤ ${target:,.2f}" if target else "Target buy price: TBD",
        "",
        "Please include: unit price, freight, lead time, quote validity.",
        "",
        "(Prepared by M3 — not transmitted. Operator must send manually if authorized.)",
    ]
    return {
        "kind": "SupplierQuotePacket",
        "transmission": "CONTENT_ONLY_NO_SEND",
        "subject": f"Quote request — {title}",
        "body": "\n".join(body_lines),
        "supplier_candidates": suppliers or [],
        "maximum_buy_price": max_buy,
        "outreach_authorized": False,
    }


def remember_family_outcome(
    memory: dict[str, Any],
    *,
    family_key: str | None,
    commercial_success: bool | None = None,
    difficult: bool | None = None,
) -> None:
    if not family_key:
        return
    fams = memory.setdefault("families", {})
    rec = fams.setdefault(family_key, {"attempts": 0, "successes": 0, "difficulties": 0})
    rec["attempts"] += 1
    if commercial_success:
        rec["successes"] += 1
    if difficult:
        rec["difficulties"] += 1
    if rec["successes"] >= 2:
        rec["status"] = KNOWN_COMMERCIAL_ACQUISITION_PATH
    elif rec["difficulties"] >= 3 and rec["successes"] == 0:
        rec["status"] = HISTORICALLY_DIFFICULT_ACQUISITION
    rec["updated_at"] = _utc()


def classify_economic_state(
    *,
    lane: str,
    hist_unit: float | None,
    verified_acq: float | None,
    lead_price: float | None,
    quote_required: bool,
    max_buy: dict[str, Any] | None,
    apparent_positive: bool,
    verified_positive: bool,
) -> str:
    if lane in {SOURCE_APPROVAL_REQUIRED, SOLE_SOURCE_RESTRICTED}:
        return SOURCE_APPROVAL_BLOCKED if "APPROVAL" in lane else SPECIALTY_CHANNEL
    if lane == MILSPEC_SPECIALTY and not verified_acq and not lead_price:
        return SPECIALTY_CHANNEL
    if verified_positive:
        return VERIFIED_POSITIVE
    if apparent_positive and verified_acq:
        return APPARENT_POSITIVE
    if apparent_positive and lead_price:
        return PROMISING_REQUIRES_PRICE_VERIFICATION
    if quote_required and (hist_unit or max_buy):
        return PROMISING_REQUIRES_SUPPLIER_QUOTE
    if max_buy and max_buy.get("maximum_acquisition_unit"):
        return MAX_BUY_PRICE_CALCULATED
    if verified_acq and not hist_unit:
        return CURRENT_PRICE_FOUND_HISTORY_MISSING
    if hist_unit and not verified_acq and not lead_price:
        return HISTORY_FOUND_PRICE_MISSING
    if hist_unit and lead_price and not apparent_positive:
        return ECONOMICALLY_NEGATIVE
    return INSUFFICIENT_EVIDENCE
