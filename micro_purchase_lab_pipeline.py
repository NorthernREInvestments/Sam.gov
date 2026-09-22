"""µLab large-pool funnel — product-resale gating + research completeness.

UNKNOWN is never PASS. UNKNOWN earns no positive ranking points.
COMPLETE requires meaningful history + market evidence (or explicit incomplete buckets).

Reuses: discovery.classify, national_discovery_funnel.stage1, product_category_yield,
m3_evidence_acquisition.classify_deal_type, ai_funnel.classify_opportunity,
m3_supplier_intelligence identity/pricing collectors, existing µLab research helpers.
"""

from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from micro_purchase_lab_config import (
    CLASS_ABOVE,
    CLASS_MICRO,
    CLASS_NEAR,
    CLASS_SMALL_SA,
    CLASS_UNKNOWN,
    classify_opportunity_size,
    funnel_config,
)

BUILD_TAG = "20260922-m3-micro-lab-pipeline-1"

# Operator-facing research states
STATE_RAW = "RAW_UNRESOLVED"
STATE_RESEARCHING = "RESEARCH_IN_PROGRESS"
STATE_RESEARCHABLE = "RESEARCHABLE_PRODUCT_CANDIDATE"
STATE_COMPLETE = "COMPLETE"
STATE_REJECTED = "REJECTED"
STATE_NO_HISTORY = "RESEARCH_INCOMPLETE_NO_HISTORY"
STATE_NO_MARKET = "RESEARCH_INCOMPLETE_NO_MARKET_PRICE"
STATE_WEAK_IDENTITY = "RESEARCH_INCOMPLETE_WEAK_IDENTITY"
STATE_VALUE_UNKNOWN = "RESEARCH_INCOMPLETE_VALUE_UNKNOWN"

# Product-resale classes
CLS_PRODUCT_RESALE = "PRODUCT_RESALE"
CLS_PRODUCT_MINOR_SERVICE = "PRODUCT_WITH_MINOR_INCIDENTAL_SERVICE"
CLS_SERVICE = "SERVICE"
CLS_CONSTRUCTION = "CONSTRUCTION"
CLS_LABOR_HEAVY = "LABOR_HEAVY"
CLS_PERISHABLE = "PERISHABLE"
CLS_UNKNOWN = "UNKNOWN"

PROCEED_CLASSES = frozenset({CLS_PRODUCT_RESALE, CLS_PRODUCT_MINOR_SERVICE})

# Reject / unresolved reason codes
REJECT_CONSTRUCTION = "REJECT_CONSTRUCTION"
REJECT_INSTALLATION_HEAVY = "REJECT_INSTALLATION_HEAVY"
REJECT_SERVICE = "REJECT_SERVICE"
REJECT_LABOR_HEAVY = "REJECT_LABOR_HEAVY"
REJECT_PERISHABLE = "REJECT_PERISHABLE"
REJECT_EXPIRED = "REJECT_EXPIRED"
REJECT_DEADLINE = "REJECT_DEADLINE"
REJECT_PRODUCT_IDENTITY_TOO_WEAK = "REJECT_PRODUCT_IDENTITY_TOO_WEAK"
REJECT_NOT_RESELLER_COMPATIBLE = "REJECT_NOT_RESELLER_COMPATIBLE"
UNRESOLVED_VALUE = "UNRESOLVED_VALUE"
UNRESOLVED_PRODUCT_IDENTITY = "UNRESOLVED_PRODUCT_IDENTITY"
NO_HISTORY_FOUND = "NO_HISTORY_FOUND"
NO_CURRENT_PRICE_FOUND = "NO_CURRENT_PRICE_FOUND"

HISTORY_EXACT = "HISTORY_EXACT"
HISTORY_STRONG = "HISTORY_STRONG_COMPARABLE"
HISTORY_WEAK = "HISTORY_WEAK_COMPARABLE"
HISTORY_NOT_FOUND = "HISTORY_NOT_FOUND"
HISTORY_UNRESOLVED = "HISTORY_UNRESOLVED"

ID_STRONG = "STRONG"
ID_MEDIUM = "MEDIUM"
ID_WEAK = "WEAK"
ID_UNKNOWN = "UNKNOWN"

PRICE_PUBLIC_RETAIL = "PUBLIC_RETAIL"
PRICE_PUBLIC_DISTRIBUTOR = "PUBLIC_DISTRIBUTOR"
PRICE_PUBLIC_WHOLESALE = "PUBLIC_WHOLESALE"
PRICE_CONTRACT = "CONTRACT_PRICE"
PRICE_QUOTE_REQUIRED = "QUOTE_REQUIRED"
PRICE_UNVERIFIED = "UNVERIFIED"
PRICE_UNKNOWN = "UNKNOWN"

QUOTE_NONE = "NONE"
QUOTE_PUBLIC_ONLY = "PUBLIC_PRICE_ONLY"
QUOTE_PREPARABLE = "QUOTE_PREPARABLE"
QUOTE_REQUESTED = "REQUESTED"
QUOTE_RECEIVED = "RECEIVED"
QUOTE_STALE = "STALE"

_PERISHABLE = re.compile(
    r"\b(food|foods|beverage|beverages|perishable|produce\b|dairy|meat\b|seafood|"
    r"fresh\s+fruit|fresh\s+vegetable|meal\s+component|grocer(?:y|ies)|spoilage|"
    r"refrigerated\s+food|frozen\s+food)\b",
    re.I,
)
_CONSTRUCTION_HARD = re.compile(
    r"\b(construction|remodel(?:ing)?|renovation|building\s+alteration|glazing\s+installation|"
    r"roofing|paving|excavation|demolition|design[- ]?bid[- ]?build|cmar|"
    r"construction\s+manager|build[- ]?out)\b",
    re.I,
)
_INSTALL_HEAVY = re.compile(
    r"\b((?:security\s+)?glazing.{0,40}install|laminate\s+installation|"
    r"furnish\s+and\s+install|supply\s+and\s+install|installation\s+of\b|"
    r"install(?:ation)?\s+(?:only|services?))\b",
    re.I,
)
_SERVICE_HARD = re.compile(
    r"\b(professional\s+services?|consulting|staffing|training\s+services?|"
    r"engineering\s+services?|architectural\s+services?|inspection\s+services?|"
    r"janitorial\s+services?|custodial\s+services?|grounds(?:keeping)?|"
    r"landscap(?:e|ing)\s+services?|maintenance\s+services?|repair\s+services?|"
    r"hauling|transportation\s+services?|software\s+development|saas)\b",
    re.I,
)
_LABOR_HARD = re.compile(
    r"\b(labor[- ]heavy|temporary\s+labor|mowing|custodial|janitorial(?!\s+supplies)|"
    r"grounds?\s+maintenance)\b",
    re.I,
)
_BRAND_MODEL = re.compile(
    r"\b((?:DEWALT|Milwaukee|Makita|Bosch|Cisco|Dell|Lenovo|HP|Apple|Brother|Epson|"
    r"Schneider|APC|Grainger|3M|Caterpillar|Honda|John\s+Deere)[\s\-]*[A-Z0-9][A-Z0-9\-/\.]{2,24})\b",
    re.I,
)
_MPN = re.compile(
    r"\b(?:P/?N|MPN|MODEL|PART\s*(?:NO|NUMBER|#)|SKU)[:\s#]*([A-Z0-9][A-Z0-9\-/\.]{2,32})\b",
    re.I,
)
_NSN = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")
_WEAK_DESC = re.compile(
    r"^(equipment|supplies|materials|goods|commodities|items?|products?|misc(?:ellaneous)?|"
    r"various|assorted|general\s+supplies)\b",
    re.I,
)
_PRODUCT_SUPPLY = re.compile(
    r"\b(purchase|procurement|supply|furnish|delivery|equipment|supplies|materials|"
    r"hardware|parts?|commodit|NSN|NIIN|laptop|server|drill|printer|monitor|"
    r"seal\s+kit|hydraulic|gasket|bearing|fastener|valve|hose|filter|kit)\b",
    re.I,
)


def _d(v: Any) -> Decimal | None:
    if v is None or v == "" or str(v).upper() in {"UNKNOWN", "NONE", "NULL", "N/A"}:
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("$", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _blob(row: dict[str, Any]) -> str:
    parts = [
        row.get("title"),
        row.get("description"),
        row.get("product"),
        row.get("solicitation_number"),
        (row.get("dla_product_structure") or {}).get("item_name") if isinstance(row.get("dla_product_structure"), dict) else None,
    ]
    li = row.get("line_items") or row.get("bom") or []
    if isinstance(li, list):
        for item in li[:12]:
            if isinstance(item, dict):
                parts.append(item.get("description") or item.get("item_name"))
    return "\n".join(str(p or "") for p in parts)


def _days_remaining(row: dict[str, Any]) -> int | None:
    de = row.get("deadline_evaluation") or {}
    if de.get("calendar_days_remaining") is not None:
        try:
            return int(de["calendar_days_remaining"])
        except (TypeError, ValueError):
            pass
    raw = row.get("deadline") or row.get("response_deadline") or de.get("deadline")
    if not raw:
        return None
    try:
        from micro_purchase_lab_economics import _parse_date

        d = _parse_date(raw)
        if d is None:
            return None
        return (d - datetime.now(timezone.utc).date()).days
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Phase 2 — hard early exclusion
# ---------------------------------------------------------------------------


def classify_product_resale(row: dict[str, Any]) -> dict[str, Any]:
    """Deterministic product-resale class using best local evidence."""
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    blob = _blob(row)
    cheap = str(
        row.get("cheap_screen_class")
        or row.get("product_classification")
        or row.get("federal_product_class")
        or ""
    ).upper()

    # Structured cheap-screen first
    if cheap in {"SERVICE", "CLEARLY_IRRELEVANT"}:
        return {"class": CLS_SERVICE, "reason": REJECT_SERVICE, "confidence": "HIGH", "source": "cheap_screen"}
    if "CONSTRUCTION" in cheap:
        return {"class": CLS_CONSTRUCTION, "reason": REJECT_CONSTRUCTION, "confidence": "HIGH", "source": "cheap_screen"}

    if _PERISHABLE.search(blob) and not re.search(r"\b(equipment|appliance|refrigerator|freezer)\b", title, re.I):
        return {"class": CLS_PERISHABLE, "reason": REJECT_PERISHABLE, "confidence": "HIGH", "source": "perishable_phrase"}

    if _CONSTRUCTION_HARD.search(title) or _CONSTRUCTION_HARD.search(desc[:800]):
        # Allow "equipment purchase" overrides
        if not (_PRODUCT_SUPPLY.search(title) and re.search(r"\b(purchase|procurement|supply of|furnish)\b", title, re.I)):
            return {"class": CLS_CONSTRUCTION, "reason": REJECT_CONSTRUCTION, "confidence": "HIGH", "source": "construction_phrase"}

    if _INSTALL_HEAVY.search(blob) and not re.search(r"\b(equipment\s+only|supply\s+only|no\s+installation)\b", blob, re.I):
        # Product+install may be minor incidental if strong product identity
        if _BRAND_MODEL.search(blob) or row.get("exact_nsn") or row.get("exact_part_number"):
            return {
                "class": CLS_PRODUCT_MINOR_SERVICE,
                "reason": None,
                "confidence": "MEDIUM",
                "source": "install_with_identifiable_product",
            }
        return {"class": CLS_LABOR_HEAVY, "reason": REJECT_INSTALLATION_HEAVY, "confidence": "HIGH", "source": "installation_heavy"}

    if _SERVICE_HARD.search(title) and not _PRODUCT_SUPPLY.search(title):
        return {"class": CLS_SERVICE, "reason": REJECT_SERVICE, "confidence": "HIGH", "source": "service_phrase"}
    if _LABOR_HARD.search(title) and not re.search(r"\bsupplies\b", title, re.I):
        return {"class": CLS_LABOR_HEAVY, "reason": REJECT_LABOR_HEAVY, "confidence": "HIGH", "source": "labor_phrase"}

    # Reuse M3 deal-type classifier
    try:
        from m3_evidence_acquisition import classify_deal_type

        deal = classify_deal_type(row)
        dt = str(deal.get("deal_type") or "").upper()
        if dt == "CONSTRUCTION":
            return {"class": CLS_CONSTRUCTION, "reason": REJECT_CONSTRUCTION, "confidence": deal.get("confidence") or "MEDIUM", "source": "classify_deal_type", "deal": deal}
        if dt == "SERVICE":
            return {"class": CLS_SERVICE, "reason": REJECT_SERVICE, "confidence": deal.get("confidence") or "MEDIUM", "source": "classify_deal_type", "deal": deal}
        if dt in {"PRODUCT", "PRODUCT_RESALE"}:
            return {"class": CLS_PRODUCT_RESALE, "reason": None, "confidence": deal.get("confidence") or "MEDIUM", "source": "classify_deal_type", "deal": deal}
        if dt == "MIXED":
            return {"class": CLS_PRODUCT_MINOR_SERVICE, "reason": None, "confidence": "MEDIUM", "source": "classify_deal_type_mixed", "deal": deal}
    except Exception:
        pass

    # discovery classify + stage1
    try:
        from discovery.classify import classify_discovery_opportunity
        from national_discovery_funnel import stage1_ultra_cheap

        s1 = stage1_ultra_cheap(row)
        if not s1.get("survive"):
            reason = str(s1.get("reason") or "")
            if "status_" in reason:
                return {"class": CLS_UNKNOWN, "reason": REJECT_EXPIRED, "confidence": "HIGH", "source": "stage1", "stage1": s1}
            return {"class": CLS_SERVICE, "reason": REJECT_SERVICE, "confidence": "MEDIUM", "source": "stage1", "stage1": s1}
        dcls = classify_discovery_opportunity(title=title, description=desc)
        klass = dcls.get("classification")
        if klass == "CORE_PRODUCT":
            return {"class": CLS_PRODUCT_RESALE, "reason": None, "confidence": "HIGH", "source": "discovery_classify", "discovery": dcls}
        if klass == "PRODUCT_PLUS_SERVICE":
            return {"class": CLS_PRODUCT_MINOR_SERVICE, "reason": None, "confidence": "MEDIUM", "source": "discovery_classify", "discovery": dcls}
        if klass == "SERVICE":
            return {"class": CLS_SERVICE, "reason": REJECT_SERVICE, "confidence": "HIGH", "source": "discovery_classify", "discovery": dcls}
    except Exception:
        pass

    try:
        from ai_funnel import classify_opportunity

        af_cls, af_conf = classify_opportunity(row)
        if af_cls == "CONSTRUCTION":
            return {"class": CLS_CONSTRUCTION, "reason": REJECT_CONSTRUCTION, "confidence": af_conf, "source": "ai_funnel"}
        if af_cls in {"LABOR_HEAVY", "SPECIALIST_SERVICE"}:
            return {"class": CLS_LABOR_HEAVY, "reason": REJECT_LABOR_HEAVY, "confidence": af_conf, "source": "ai_funnel"}
        if af_cls == "PRODUCT_RESELL":
            return {"class": CLS_PRODUCT_RESALE, "reason": None, "confidence": af_conf, "source": "ai_funnel"}
        if af_cls == "PRODUCT_PLUS_SERVICE":
            return {"class": CLS_PRODUCT_MINOR_SERVICE, "reason": None, "confidence": af_conf, "source": "ai_funnel"}
        if af_cls in {"SUBCONTRACTABLE_SERVICE"}:
            return {"class": CLS_SERVICE, "reason": REJECT_SERVICE, "confidence": af_conf, "source": "ai_funnel"}
    except Exception:
        pass

    # Category yield
    try:
        from product_category_yield import classify_product_category

        cat = classify_product_category(title, desc)
        if cat.get("category") == "FOOD_COMMODITIES":
            return {"class": CLS_PERISHABLE, "reason": REJECT_PERISHABLE, "confidence": "HIGH", "source": "product_category", "category": cat}
        if cat.get("category") == "LIKELY_SERVICE_FALSE_POSITIVE":
            return {"class": CLS_SERVICE, "reason": REJECT_SERVICE, "confidence": "HIGH", "source": "product_category", "category": cat}
        if cat.get("category") not in {"UNKNOWN", "MIXED_GOODS_SERVICES"}:
            return {"class": CLS_PRODUCT_RESALE, "reason": None, "confidence": cat.get("confidence") or "MEDIUM", "source": "product_category", "category": cat}
        if cat.get("category") == "MIXED_GOODS_SERVICES":
            return {"class": CLS_PRODUCT_MINOR_SERVICE, "reason": None, "confidence": "MEDIUM", "source": "product_category", "category": cat}
    except Exception:
        pass

    # DLA / NSN structured product
    if row.get("exact_nsn") or row.get("nsn") or (row.get("dla_product_structure") or {}).get("has_exact_nsn"):
        return {"class": CLS_PRODUCT_RESALE, "reason": None, "confidence": "HIGH", "source": "nsn_structure"}

    # Clear tangible-goods language without service/construction dominance
    if _PRODUCT_SUPPLY.search(title) and not _SERVICE_HARD.search(title) and not _CONSTRUCTION_HARD.search(title):
        return {"class": CLS_PRODUCT_RESALE, "reason": None, "confidence": "MEDIUM", "source": "product_supply_language"}

    return {"class": CLS_UNKNOWN, "reason": UNRESOLVED_PRODUCT_IDENTITY, "confidence": "LOW", "source": "insufficient"}


def evaluate_deadline_gate(row: dict[str, Any], *, min_runway_days: int = 2) -> dict[str, Any]:
    status = str(row.get("status") or row.get("opportunity_status") or "OPEN").upper()
    if status in {"EXPIRED", "CANCELLED", "CANCELED", "CLOSED", "AWARDED"}:
        return {"ok": False, "reason": REJECT_EXPIRED, "status": status, "runway_days": None}
    runway = _days_remaining(row)
    if runway is not None and runway < min_runway_days:
        return {"ok": False, "reason": REJECT_DEADLINE, "status": status, "runway_days": runway}
    return {"ok": True, "reason": None, "status": status, "runway_days": runway}


# ---------------------------------------------------------------------------
# Phase 3 — product identity
# ---------------------------------------------------------------------------


def assess_product_identity(row: dict[str, Any]) -> dict[str, Any]:
    """STRONG/MEDIUM/WEAK — NSN/P/N not required if other evidence is sufficient."""
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")
    blob = _blob(row)
    mfr = row.get("manufacturer") or (row.get("product_identity") or {}).get("manufacturer")
    part = (
        row.get("exact_part_number")
        or row.get("part_number")
        or (row.get("product_identity") or {}).get("part_number")
    )
    nsn = row.get("exact_nsn") or row.get("nsn") or (row.get("dla_product_structure") or {}).get("nsn")
    qty = row.get("quantity") or (row.get("dla_product_structure") or {}).get("quantity")
    uom = row.get("unit_of_issue") or (row.get("dla_product_structure") or {}).get("unit_of_issue") or "EA"

    brand_model = None
    bm = _BRAND_MODEL.search(blob)
    if bm:
        brand_model = bm.group(1).strip()
    mpn = part
    if not mpn:
        mm = _MPN.search(blob)
        if mm:
            mpn = mm.group(1).strip()
    if not nsn:
        nm = _NSN.search(blob)
        if nm:
            nsn = nm.group(1)

    line_items = []
    for li in row.get("line_items") or row.get("bom") or []:
        if isinstance(li, dict):
            line_items.append(
                {
                    "line_item_number": li.get("line") or li.get("line_number") or li.get("item_number"),
                    "product_description": li.get("description") or li.get("item_name"),
                    "manufacturer": li.get("manufacturer"),
                    "part_number": li.get("part_number") or li.get("mpn"),
                    "nsn": li.get("nsn"),
                    "quantity": li.get("quantity"),
                    "unit_of_measure": li.get("unit") or li.get("uom"),
                }
            )

    # Try existing identity builders (no fabrication)
    try:
        from m3_supplier_intelligence import build_product_identity

        product = build_product_identity(row)
        if not mfr and product.get("Manufacturer") not in {None, "UNKNOWN"}:
            mfr = product.get("Manufacturer")
        if not mpn and product.get("Part_number") not in {None, "UNKNOWN"}:
            mpn = product.get("Part_number")
        if not nsn and product.get("NSN") not in {None, "UNKNOWN"}:
            nsn = product.get("NSN")
        if not brand_model and product.get("Model") not in {None, "UNKNOWN"}:
            brand_model = product.get("Model")
    except Exception:
        product = {}

    salient = None
    if len(desc) >= 80 and not _WEAK_DESC.match(desc.strip()):
        salient = desc[:240]
    elif len(title) >= 24 and not _WEAK_DESC.match(title.strip()):
        salient = title

    identity = {
        "product_description": title or (line_items[0].get("product_description") if line_items else None),
        "manufacturer": mfr,
        "brand": (str(brand_model).split()[0] if brand_model else mfr),
        "model": brand_model,
        "manufacturer_part_number": mpn,
        "government_part_number": row.get("government_part_number"),
        "nsn": nsn,
        "sku": (product or {}).get("SKU") if (product or {}).get("SKU") not in {None, "UNKNOWN"} else None,
        "upc": (product or {}).get("UPC") if (product or {}).get("UPC") not in {None, "UNKNOWN"} else None,
        "salient_characteristics": salient,
        "quantity": qty,
        "unit_of_measure": uom,
        "packaging": row.get("packaging"),
        "delivery_location": row.get("delivery_location") or row.get("place_of_performance"),
        "brand_name_or_equal": bool(re.search(r"brand\s*name\s*or\s*equal|or\s*equal", blob, re.I)),
        "substitutions_allowed": row.get("substitutions_allowed"),
        "line_items": line_items[:20],
    }

    if nsn and (mfr or mpn or brand_model):
        confidence = ID_STRONG
    elif nsn or (mfr and mpn) or brand_model:
        confidence = ID_STRONG
    elif mpn or (mfr and salient) or (salient and qty is not None and len(str(salient)) >= 40):
        confidence = ID_MEDIUM
    elif salient and not _WEAK_DESC.match(str(salient)):
        confidence = ID_MEDIUM if len(str(salient)) >= 40 else ID_WEAK
    elif _WEAK_DESC.match(title.strip()) or not title.strip():
        confidence = ID_WEAK
    else:
        confidence = ID_WEAK if len(title) < 20 else ID_MEDIUM

    # Explicit weak titles
    if re.search(r"\b(equipment|supplies|materials)\b", title, re.I) and not (nsn or mpn or brand_model or mfr):
        if len(title.split()) <= 4:
            confidence = ID_WEAK

    return {
        "confidence": confidence,
        "identity": identity,
        "sufficient_for_research": confidence in {ID_STRONG, ID_MEDIUM},
        "needs": None if confidence in {ID_STRONG, ID_MEDIUM} else UNRESOLVED_PRODUCT_IDENTITY,
    }


# ---------------------------------------------------------------------------
# Phase 4 — dollar band
# ---------------------------------------------------------------------------


def resolve_estimated_value(row: dict[str, Any]) -> dict[str, Any]:
    """Cheap deterministic value resolution. Preserve UNKNOWN — never invent."""
    candidates: list[tuple[str, Decimal]] = []
    for key, label in (
        ("estimated_value", "solicitation_estimated_value"),
        ("government_revenue", "government_revenue_field"),
        ("budget", "budget_field"),
        ("stated_budget", "stated_budget"),
        ("historical_award_amount", "historical_award_total"),
    ):
        v = _d(row.get(key))
        if v is not None and v > 0:
            candidates.append((label, v))

    # Line qty × historical unit (only if unit is known)
    qty = _d(row.get("quantity") or (row.get("dla_product_structure") or {}).get("quantity"))
    hist_unit = None
    gph = row.get("government_price_history")
    if isinstance(gph, dict):
        for a in gph.get("awards") or gph.get("observations") or []:
            if isinstance(a, dict) and a.get("unit_price") is not None:
                hist_unit = _d(a.get("unit_price"))
                if hist_unit:
                    break
    # Do NOT treat historical_award_amount alone as unit price
    if hist_unit and qty:
        candidates.append(("qty_x_historical_unit", hist_unit * qty))

    if not candidates:
        return {
            "value": None,
            "status": "UNKNOWN",
            "source": None,
            "classification": CLASS_UNKNOWN,
            "jurisdiction": row.get("jurisdiction") or row.get("source_type") or "UNKNOWN",
        }

    # Prefer solicitation estimate
    preferred = next((c for c in candidates if c[0].startswith("solicitation")), candidates[0])
    label, amount = preferred
    return {
        "value": str(amount),
        "status": "RESOLVED",
        "source": label,
        "classification": classify_opportunity_size(amount),
        "jurisdiction": row.get("jurisdiction") or row.get("source_type") or "UNKNOWN",
        "all_candidates": [{"source": s, "value": str(v)} for s, v in candidates],
    }


# ---------------------------------------------------------------------------
# Phase 5 — historical government price
# ---------------------------------------------------------------------------


def research_historical_prices(row: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    """Collect comparable prior purchases. Never present total as unit without qty."""
    evidence: list[dict[str, Any]] = []
    ident = identity.get("identity") or {}
    nsn = ident.get("nsn") or row.get("exact_nsn") or row.get("nsn")
    mpn = ident.get("manufacturer_part_number") or row.get("exact_part_number") or row.get("part_number")
    mfr = ident.get("manufacturer") or row.get("manufacturer")
    model = ident.get("model")

    def _add(raw: dict[str, Any], match: str) -> None:
        qty = _d(raw.get("quantity"))
        unit = _d(raw.get("unit_price") or raw.get("price"))
        total = _d(raw.get("total_award") or raw.get("amount") or raw.get("total"))
        # Guard: do not treat total-as-unit
        if unit is None and total is not None and qty and qty > 0:
            unit = (total / qty).quantize(Decimal("0.01"))
            inferred = True
        else:
            inferred = False
        if unit is None and total is not None and not qty:
            # Keep as total-only evidence — not usable as unit price
            evidence.append(
                {
                    "prior_buyer": raw.get("agency") or raw.get("buyer"),
                    "award_date": raw.get("award_date") or raw.get("date"),
                    "quantity": None,
                    "unit_of_measure": raw.get("unit") or raw.get("unit_of_issue"),
                    "total_award_value": str(total),
                    "unit_price": None,
                    "unit_price_status": "TOTAL_ONLY_NOT_UNIT",
                    "vendor": raw.get("vendor") or raw.get("awardee") or raw.get("awarded_vendor") or raw.get("winner"),
                    "contract_number": raw.get("contract_number") or raw.get("award_id"),
                    "source_url": raw.get("source_url") or raw.get("url"),
                    "identity_match_strength": match,
                    "comparability_notes": "total_award_without_quantity_cannot_be_unit_price",
                    "inferred_unit_from_total": False,
                }
            )
            return
        if unit is None:
            return
        evidence.append(
            {
                "prior_buyer": raw.get("agency") or raw.get("buyer"),
                "award_date": raw.get("award_date") or raw.get("date"),
                "quantity": str(qty) if qty is not None else raw.get("quantity"),
                "unit_of_measure": raw.get("unit") or raw.get("unit_of_issue") or "EA",
                "total_award_value": str(total) if total is not None else None,
                "unit_price": str(unit),
                "unit_price_status": "INFERRED_FROM_TOTAL_QTY" if inferred else "EXPLICIT_UNIT",
                "vendor": raw.get("vendor") or raw.get("awardee") or raw.get("awarded_vendor") or raw.get("winner"),
                "contract_number": raw.get("contract_number") or raw.get("award_id"),
                "source_url": raw.get("source_url") or raw.get("url"),
                "identity_match_strength": match,
                "comparability_notes": raw.get("notes"),
                "inferred_unit_from_total": inferred,
            }
        )

    gph = row.get("government_price_history")
    if isinstance(gph, dict):
        for a in gph.get("awards") or gph.get("observations") or []:
            if not isinstance(a, dict):
                continue
            match = HISTORY_WEAK
            a_nsn = str(a.get("nsn") or "")
            a_pn = str(a.get("part_number") or a.get("mpn") or "")
            if nsn and a_nsn and a_nsn == str(nsn):
                match = HISTORY_EXACT
            elif mpn and a_pn and a_pn.upper() == str(mpn).upper():
                match = HISTORY_EXACT
            elif mfr and model and str(a.get("model") or "").upper() == str(model).upper():
                match = HISTORY_STRONG
            elif mfr and str(a.get("manufacturer") or "").lower() == str(mfr).lower():
                match = HISTORY_STRONG
            else:
                match = HISTORY_STRONG if nsn or mpn else HISTORY_WEAK
            _add(a, match)

    for a in row.get("historical_awards") or []:
        if isinstance(a, dict):
            match = HISTORY_EXACT if (nsn or mpn) else HISTORY_STRONG
            _add(a, match)

    # Scalar historical_award_amount: only as TOTAL evidence, never as unit alone
    ham = _d(row.get("historical_award_amount"))
    if ham is not None and not evidence:
        qty = _d(row.get("quantity"))
        if qty and qty > 0:
            _add(
                {
                    "award_date": row.get("historical_award_date"),
                    "quantity": qty,
                    "total_award": ham,
                    "unit_price": ham / qty,
                    "vendor": row.get("historical_vendor"),
                    "source_url": row.get("detail_url"),
                    "notes": "derived_from_historical_award_amount_and_quantity",
                },
                HISTORY_WEAK,
            )
        else:
            _add(
                {
                    "award_date": row.get("historical_award_date"),
                    "total_award": ham,
                    "source_url": row.get("detail_url"),
                    "notes": "historical_award_amount_total_only",
                },
                HISTORY_UNRESOLVED,
            )

    usable = [e for e in evidence if e.get("unit_price") is not None]
    if not evidence:
        status = HISTORY_NOT_FOUND
    elif not usable:
        status = HISTORY_UNRESOLVED
    else:
        strengths = [e.get("identity_match_strength") for e in usable]
        if HISTORY_EXACT in strengths:
            status = HISTORY_EXACT
        elif HISTORY_STRONG in strengths:
            status = HISTORY_STRONG
        else:
            status = HISTORY_WEAK

    best_unit = None
    if usable:
        # Prefer exact, then newest
        ranked = sorted(
            usable,
            key=lambda e: (
                0 if e.get("identity_match_strength") == HISTORY_EXACT else 1 if e.get("identity_match_strength") == HISTORY_STRONG else 2,
                str(e.get("award_date") or ""),
            ),
        )
        # newest within best tier
        best_tier = ranked[0].get("identity_match_strength")
        tier = [e for e in usable if e.get("identity_match_strength") == best_tier]
        tier.sort(key=lambda e: str(e.get("award_date") or ""), reverse=True)
        best_unit = tier[0]

    return {
        "status": status,
        "evidence": evidence,
        "usable_unit_observations": len(usable),
        "best": best_unit,
        "comparable_count": len(usable),
    }


# ---------------------------------------------------------------------------
# Phase 6 — current market
# ---------------------------------------------------------------------------


def research_current_market(row: dict[str, Any], identity: dict[str, Any]) -> dict[str, Any]:
    """Find current public/acquisition price evidence — never fabricate."""
    observations: list[dict[str, Any]] = []

    def _add(raw: dict[str, Any], price_type: str) -> None:
        price = _d(raw.get("unit_price") or raw.get("price") or raw.get("amount") or raw.get("normalized_unit_price"))
        if price is None:
            return
        observations.append(
            {
                "seller": raw.get("seller") or raw.get("vendor") or raw.get("Source") or raw.get("source_name"),
                "exact_product": raw.get("product_matched") or raw.get("exact_product") or (identity.get("identity") or {}).get("product_description"),
                "manufacturer": raw.get("manufacturer") or (identity.get("identity") or {}).get("manufacturer"),
                "model_part_number": raw.get("part_number") or (identity.get("identity") or {}).get("manufacturer_part_number"),
                "unit_price": str(price),
                "pack_quantity": raw.get("quantity") or raw.get("pack_quantity") or 1,
                "shipping": raw.get("freight") or raw.get("shipping"),
                "availability": raw.get("stock") or raw.get("availability"),
                "source_url": raw.get("url") or raw.get("source_url"),
                "price_date": raw.get("date") or raw.get("evidence_date") or raw.get("as_of") or raw.get("Date"),
                "price_type": price_type,
            }
        )

    # Existing row commercial pricing
    pricing = row.get("commercial_pricing") or row.get("public_pricing") or {}
    if isinstance(pricing, dict):
        if pricing.get("verified_wholesale_unit") is not None:
            _add({"unit_price": pricing.get("verified_wholesale_unit"), "seller": pricing.get("verified_source"), "as_of": pricing.get("as_of")}, PRICE_PUBLIC_WHOLESALE)
        if pricing.get("lowest_public_new_unit") is not None or pricing.get("public_unit_price") is not None:
            _add(
                {
                    "unit_price": pricing.get("lowest_public_new_unit") or pricing.get("public_unit_price"),
                    "seller": pricing.get("public_source"),
                    "as_of": pricing.get("as_of"),
                },
                PRICE_PUBLIC_DISTRIBUTOR if "distrib" in str(pricing.get("public_source") or "").lower() else PRICE_PUBLIC_RETAIL,
            )
        if pricing.get("comparable_unit") is not None or pricing.get("msrp") is not None:
            _add({"unit_price": pricing.get("comparable_unit") or pricing.get("msrp"), "seller": pricing.get("comparable_source")}, PRICE_UNVERIFIED)

    for m in row.get("current_market_prices") or []:
        if isinstance(m, dict):
            pt = str(m.get("price_type") or m.get("evidence_type") or PRICE_PUBLIC_RETAIL).upper()
            if "WHOLESALE" in pt:
                pt = PRICE_PUBLIC_WHOLESALE
            elif "DISTRIB" in pt or "AUTHORIZED" in pt:
                pt = PRICE_PUBLIC_DISTRIBUTOR
            elif "RETAIL" in pt or "MARKETPLACE" in pt:
                pt = PRICE_PUBLIC_RETAIL
            elif pt not in {PRICE_PUBLIC_RETAIL, PRICE_PUBLIC_DISTRIBUTOR, PRICE_PUBLIC_WHOLESALE, PRICE_CONTRACT, PRICE_QUOTE_REQUIRED, PRICE_UNVERIFIED}:
                pt = PRICE_UNVERIFIED
            _add(m, pt)

    # Existing supplier intel collector (local only — no paid calls in funnel scan)
    try:
        from m3_supplier_intelligence import build_product_identity, collect_existing_price_evidence

        product = build_product_identity(row)
        for e in collect_existing_price_evidence(row, product):
            level = str(e.get("level") or "")
            if "LEVEL_1" in level:
                pt = PRICE_PUBLIC_WHOLESALE
            elif "LEVEL_2" in level:
                pt = PRICE_PUBLIC_DISTRIBUTOR
            else:
                pt = PRICE_UNVERIFIED
            _add({"unit_price": e.get("amount"), "seller": e.get("Source"), "as_of": e.get("Date")}, pt)
    except Exception:
        pass

    if not observations:
        return {
            "status": PRICE_UNKNOWN,
            "observations": [],
            "best": None,
            "label": "NO_CURRENT_PRICE_FOUND",
            "note": "CURRENT PUBLIC MARKET PRICE is not a supplier quote",
        }

    # Prefer wholesale/distributor over retail
    rank = {
        PRICE_PUBLIC_WHOLESALE: 0,
        PRICE_PUBLIC_DISTRIBUTOR: 1,
        PRICE_CONTRACT: 2,
        PRICE_PUBLIC_RETAIL: 3,
        PRICE_UNVERIFIED: 4,
        PRICE_QUOTE_REQUIRED: 5,
    }
    best = sorted(observations, key=lambda o: (rank.get(o.get("price_type"), 9), _d(o.get("unit_price")) or Decimal("999999")))[0]
    return {
        "status": best.get("price_type") or PRICE_UNVERIFIED,
        "observations": observations,
        "best": best,
        "verified_count": len(observations),
        "label": "CURRENT_PUBLIC_MARKET_PRICE",
        "note": "Public market price ≠ supplier acquisition quote",
    }


# ---------------------------------------------------------------------------
# Phase 7–8 — quote status + preliminary economics
# ---------------------------------------------------------------------------


def derive_quote_status(row: dict[str, Any], market: dict[str, Any]) -> str:
    quotes = [q for q in (row.get("supplier_quotes") or []) if not q.get("archived")]
    if quotes:
        return QUOTE_RECEIVED
    if str(row.get("quote_status") or "").upper() in {"REQUESTED", QUOTE_REQUESTED}:
        return QUOTE_REQUESTED
    if market.get("best"):
        return QUOTE_PUBLIC_ONLY
    if row.get("supplier_candidates") or row.get("recommended_quote_targets"):
        return QUOTE_PREPARABLE
    return QUOTE_NONE


def preliminary_economics(
    *,
    hist: dict[str, Any],
    market: dict[str, Any],
    qty: Any,
) -> dict[str, Any]:
    """PRELIMINARY only — unknown freight/fees/etc. Margin ≠ markup."""
    q = _d(qty) or Decimal("1")
    gov_unit = _d((hist.get("best") or {}).get("unit_price"))
    acq_unit = _d((market.get("best") or {}).get("unit_price"))
    if gov_unit is None or acq_unit is None:
        return {
            "ok": False,
            "preliminary": True,
            "label": "PRELIMINARY_ECONOMICS_INCOMPLETE",
            "note": "Need both government history unit price and current acquisition unit price",
        }
    revenue = (gov_unit * q).quantize(Decimal("0.01"))
    cost = (acq_unit * q).quantize(Decimal("0.01"))
    spread = (revenue - cost).quantize(Decimal("0.01"))
    margin = ((spread / revenue) * Decimal("100")).quantize(Decimal("0.01")) if revenue else None
    markup = ((spread / cost) * Decimal("100")).quantize(Decimal("0.01")) if cost else None
    return {
        "ok": True,
        "preliminary": True,
        "label": "PRELIMINARY_ECONOMICS",
        "estimated_government_unit_price": str(gov_unit),
        "estimated_acquisition_unit_price": str(acq_unit),
        "quantity": str(q),
        "estimated_revenue": str(revenue),
        "estimated_product_cost": str(cost),
        "gross_spread": str(spread),
        "gross_margin_pct": str(margin) if margin is not None else None,
        "markup_pct": str(markup) if markup is not None else None,
        "estimated_gross_dollars": str(spread),
        "note": "Preliminary gross spread ≠ final profit (freight/fees/financing/tax unknown)",
    }


# ---------------------------------------------------------------------------
# Phase 9–10 — evaluate one row + funnel
# ---------------------------------------------------------------------------


def evaluate_opportunity(row: dict[str, Any], *, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Full cheap→complete evaluation for one opportunity. No fabricated prices."""
    cfg = cfg or funnel_config()
    min_runway = int(cfg.get("min_deadline_runway_days") or 2)

    deadline = evaluate_deadline_gate(row, min_runway_days=min_runway)
    if not deadline["ok"]:
        return _result(
            row,
            state=STATE_REJECTED,
            reason=deadline["reason"],
            product_class=CLS_UNKNOWN,
            deadline=deadline,
        )

    pr = classify_product_resale(row)
    product_class = pr["class"]
    if product_class not in PROCEED_CLASSES:
        if product_class == CLS_UNKNOWN:
            # UNKNOWN may stay unresolved — not COMPLETE, not automatic reject of value
            identity = assess_product_identity(row)
            value = resolve_estimated_value(row)
            return _result(
                row,
                state=STATE_RAW,
                reason=pr.get("reason") or UNRESOLVED_PRODUCT_IDENTITY,
                product_class=product_class,
                deadline=deadline,
                identity=identity,
                value=value,
                product_resale=pr,
            )
        return _result(
            row,
            state=STATE_REJECTED,
            reason=pr.get("reason") or REJECT_NOT_RESELLER_COMPATIBLE,
            product_class=product_class,
            deadline=deadline,
            product_resale=pr,
        )

    identity = assess_product_identity(row)
    if not identity.get("sufficient_for_research"):
        return _result(
            row,
            state=STATE_WEAK_IDENTITY,
            reason=REJECT_PRODUCT_IDENTITY_TOO_WEAK,
            product_class=product_class,
            deadline=deadline,
            identity=identity,
            product_resale=pr,
        )

    value = resolve_estimated_value(row)
    # Value UNKNOWN → separate bucket, not COMPLETE
    value_ok = value.get("status") == "RESOLVED"
    klass = value.get("classification") or CLASS_UNKNOWN
    # Prefer micro/near/small; ABOVE can remain researchable but not default complete target
    hist = research_historical_prices(row, identity)
    market = research_current_market(row, identity)
    econ = preliminary_economics(
        hist=hist,
        market=market,
        qty=(identity.get("identity") or {}).get("quantity") or row.get("quantity"),
    )
    quote_status = derive_quote_status(row, market)

    hist_ok = hist.get("status") in {HISTORY_EXACT, HISTORY_STRONG}
    market_ok = bool(market.get("best"))

    if not value_ok:
        state = STATE_VALUE_UNKNOWN
        reason = UNRESOLVED_VALUE
    elif not hist_ok:
        state = STATE_NO_HISTORY
        reason = NO_HISTORY_FOUND
    elif not market_ok:
        state = STATE_NO_MARKET
        reason = NO_CURRENT_PRICE_FOUND
    elif not econ.get("ok"):
        state = STATE_RESEARCHABLE
        reason = None
    else:
        # Size band: COMPLETE prefers micro/near/small; above-micro goes researchable
        if klass in {CLASS_MICRO, CLASS_NEAR, CLASS_SMALL_SA} or (
            klass == CLASS_UNKNOWN and value_ok is False
        ):
            state = STATE_COMPLETE
            reason = None
        elif klass == CLASS_ABOVE:
            state = STATE_RESEARCHABLE
            reason = None
        else:
            # value resolved into micro bands already covered; UNKNOWN value handled above
            state = STATE_COMPLETE if klass != CLASS_ABOVE else STATE_RESEARCHABLE
            reason = None

    # Extra: if history is WEAK only, do not call COMPLETE
    if state == STATE_COMPLETE and hist.get("status") == HISTORY_WEAK:
        state = STATE_NO_HISTORY
        reason = NO_HISTORY_FOUND

    return _result(
        row,
        state=state,
        reason=reason,
        product_class=product_class,
        deadline=deadline,
        identity=identity,
        value=value,
        hist=hist,
        market=market,
        economics=econ,
        quote_status=quote_status,
        product_resale=pr,
    )


def _result(
    row: dict[str, Any],
    *,
    state: str,
    reason: str | None,
    product_class: str,
    deadline: dict[str, Any] | None = None,
    identity: dict[str, Any] | None = None,
    value: dict[str, Any] | None = None,
    hist: dict[str, Any] | None = None,
    market: dict[str, Any] | None = None,
    economics: dict[str, Any] | None = None,
    quote_status: str | None = None,
    product_resale: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ident = (identity or {}).get("identity") or {}
    best_hist = (hist or {}).get("best") or {}
    best_mkt = (market or {}).get("best") or {}
    econ = economics or {}
    title = str(row.get("title") or "")
    return {
        "build": BUILD_TAG,
        "canonical_id": row.get("canonical_id"),
        "product_title": title,
        "source": row.get("source_id") or row.get("preferred_source_id"),
        "solicitation": row.get("solicitation_number") or row.get("external_id"),
        "agency": row.get("agency"),
        "detail_url": row.get("detail_url") or row.get("source_url"),
        "deadline": row.get("deadline") or row.get("response_deadline") or (deadline or {}).get("deadline"),
        "deadline_runway": (deadline or {}).get("runway_days"),
        "research_state": state,
        "reject_reason": reason if state == STATE_REJECTED else None,
        "unresolved_reason": reason if state != STATE_REJECTED else None,
        "product_resale_class": product_class,
        "product_resale": product_resale,
        "identity_confidence": (identity or {}).get("confidence"),
        "identity": ident,
        "estimated_opportunity_size": (value or {}).get("value") or "UNKNOWN",
        "value_status": (value or {}).get("status") or "UNKNOWN",
        "value_source": (value or {}).get("source"),
        "classification": (value or {}).get("classification") or CLASS_UNKNOWN,
        "jurisdiction": (value or {}).get("jurisdiction"),
        "historical_status": (hist or {}).get("status") or HISTORY_NOT_FOUND,
        "historical_comparable_count": (hist or {}).get("comparable_count") or 0,
        "last_government_unit_price": best_hist.get("unit_price"),
        "historical_match": best_hist.get("identity_match_strength"),
        "historical_evidence": (hist or {}).get("evidence") or [],
        "current_market_status": (market or {}).get("status") or PRICE_UNKNOWN,
        "current_public_price": best_mkt.get("unit_price"),
        "current_market_seller": best_mkt.get("seller"),
        "current_market_price_type": best_mkt.get("price_type"),
        "current_market_observations": (market or {}).get("observations") or [],
        "quote_status": quote_status or QUOTE_NONE,
        "preliminary_economics": econ,
        "manufacturer": ident.get("manufacturer") or row.get("manufacturer"),
        "part_number": ident.get("manufacturer_part_number") or row.get("exact_part_number") or row.get("part_number"),
        "nsn": ident.get("nsn") or row.get("exact_nsn") or row.get("nsn"),
        "quantity": ident.get("quantity") or row.get("quantity"),
        "unit_of_issue": ident.get("unit_of_measure") or row.get("unit_of_issue") or "EA",
        "queue_score": score_candidate(
            state=state,
            identity_confidence=(identity or {}).get("confidence"),
            hist_status=(hist or {}).get("status"),
            market_ok=bool(best_mkt),
            econ_ok=bool(econ.get("ok")),
            runway=(deadline or {}).get("runway_days"),
            classification=(value or {}).get("classification"),
            product_class=product_class,
        ),
        "badge": _badge(state),
        "next_action": _next_action(state),
    }


def _badge(state: str) -> str:
    return {
        STATE_COMPLETE: "COMPLETE",
        STATE_RESEARCHING: "RESEARCHING",
        STATE_NO_HISTORY: "NO HISTORY",
        STATE_NO_MARKET: "NO MARKET PRICE",
        STATE_WEAK_IDENTITY: "WEAK IDENTITY",
        STATE_VALUE_UNKNOWN: "VALUE UNKNOWN",
        STATE_REJECTED: "REJECTED",
        STATE_RAW: "UNRESOLVED",
        STATE_RESEARCHABLE: "RESEARCHABLE",
    }.get(state, state)


def _next_action(state: str) -> str:
    return {
        STATE_COMPLETE: "Load into Lab",
        STATE_NO_HISTORY: "Research historical prices",
        STATE_NO_MARKET: "Research current market price",
        STATE_WEAK_IDENTITY: "Resolve product identity",
        STATE_VALUE_UNKNOWN: "Resolve estimated value",
        STATE_RESEARCHABLE: "Continue research",
        STATE_RAW: "Cheap resolution research",
        STATE_REJECTED: "Inspect rejection",
        STATE_RESEARCHING: "Wait / continue research",
    }.get(state, "Review")


def score_candidate(
    *,
    state: str,
    identity_confidence: str | None,
    hist_status: str | None,
    market_ok: bool,
    econ_ok: bool,
    runway: int | None,
    classification: str | None,
    product_class: str | None,
) -> int:
    """Deterministic ranking. UNKNOWN never receives positive points."""
    if state == STATE_REJECTED:
        return -1000
    score = 0
    if state == STATE_COMPLETE:
        score += 100
    elif state == STATE_RESEARCHABLE:
        score += 40
    elif state in {STATE_NO_HISTORY, STATE_NO_MARKET}:
        score += 15
    # WEAK / RAW / VALUE_UNKNOWN: no bonus

    if identity_confidence == ID_STRONG:
        score += 30
    elif identity_confidence == ID_MEDIUM:
        score += 15
    # WEAK/UNKNOWN: 0

    if hist_status == HISTORY_EXACT:
        score += 25
    elif hist_status == HISTORY_STRONG:
        score += 18
    elif hist_status == HISTORY_WEAK:
        score += 5
    # NOT_FOUND/UNRESOLVED: 0

    if market_ok:
        score += 20
    if econ_ok:
        score += 15

    if classification == CLASS_MICRO:
        score += 20
    elif classification == CLASS_NEAR:
        score += 14
    elif classification == CLASS_SMALL_SA:
        score += 8
    # UNKNOWN / ABOVE: 0 positive for unknown

    if product_class == CLS_PRODUCT_RESALE:
        score += 10
    elif product_class == CLS_PRODUCT_MINOR_SERVICE:
        score += 5

    if runway is not None:
        if runway >= 7:
            score += 10
        elif runway >= 3:
            score += 5
        # insufficient already rejected

    return score


def empty_funnel_counts() -> dict[str, int]:
    return {
        "raw_examined": 0,
        "expired_deadline_rejected": 0,
        "service_construction_rejected": 0,
        "perishable_rejected": 0,
        "product_identity_too_weak": 0,
        "dollar_value_unresolved": 0,
        "product_candidates_researched": 0,
        "historical_pricing_found": 0,
        "current_market_pricing_found": 0,
        "both_price_sides_found": 0,
        "complete_candidates": 0,
        "researchable_candidates": 0,
        "research_incomplete": 0,
        "raw_unresolved": 0,
    }


def run_micro_lab_funnel(
    rows: list[dict[str, Any]],
    *,
    raw_search_target: int | None = None,
    complete_target: int | None = None,
    filter_state: str = "COMPLETE",
) -> dict[str, Any]:
    """
    Search a large pool; return ranked candidates + reconciled funnel counts.

    Stops when complete_target COMPLETE candidates found OR raw_search_target exhausted.
    filter_state: COMPLETE | RESEARCH_INCOMPLETE | REJECTED | ALL | RESEARCHABLE
    """
    cfg = funnel_config()
    raw_target = int(raw_search_target or cfg["raw_search_target"])
    complete_tgt = int(complete_target or cfg["complete_target"])
    counts = empty_funnel_counts()
    evaluated: list[dict[str, Any]] = []
    complete_n = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        if counts["raw_examined"] >= raw_target:
            break
        if complete_n >= complete_tgt and filter_state == "COMPLETE":
            # Still allow exhausting? Spec: stop when 10-15 COMPLETE found OR budget exhausted.
            break

        counts["raw_examined"] += 1
        result = evaluate_opportunity(row, cfg=cfg)
        evaluated.append(result)
        _accumulate(counts, result)
        if result["research_state"] == STATE_COMPLETE:
            complete_n += 1

    filtered = _apply_filter(evaluated, filter_state)
    filtered.sort(key=lambda r: (-int(r.get("queue_score") or 0), str(r.get("deadline") or "9999")))

    # Default operator list: top complete_target COMPLETE (or filtered set)
    if filter_state == "COMPLETE":
        items = filtered[:complete_tgt]
    else:
        items = filtered[: max(complete_tgt * 3, 40)]

    return {
        "build": BUILD_TAG,
        "count": len(items),
        "filter_state": filter_state,
        "items": items,
        "funnel": counts,
        "thresholds": cfg,
        "note": "COMPLETE requires tangible product + MEDIUM/STRONG identity + EXACT/STRONG history + current market price + preliminary economics",
        "raw_search_target": raw_target,
        "complete_target": complete_tgt,
        "stopped_reason": (
            "complete_target_met"
            if complete_n >= complete_tgt
            else "raw_search_exhausted"
            if counts["raw_examined"] >= raw_target or counts["raw_examined"] >= len([r for r in rows if isinstance(r, dict)])
            else "pool_exhausted"
        ),
    }


def _accumulate(counts: dict[str, int], result: dict[str, Any]) -> None:
    state = result.get("research_state")
    reason = result.get("reject_reason") or result.get("unresolved_reason")
    if state == STATE_REJECTED:
        if reason in {REJECT_EXPIRED, REJECT_DEADLINE}:
            counts["expired_deadline_rejected"] += 1
        elif reason == REJECT_PERISHABLE:
            counts["perishable_rejected"] += 1
        elif reason in {
            REJECT_CONSTRUCTION,
            REJECT_SERVICE,
            REJECT_LABOR_HEAVY,
            REJECT_INSTALLATION_HEAVY,
            REJECT_NOT_RESELLER_COMPATIBLE,
        }:
            counts["service_construction_rejected"] += 1
        else:
            counts["service_construction_rejected"] += 1
        return

    if state == STATE_WEAK_IDENTITY:
        counts["product_identity_too_weak"] += 1
        return
    if state == STATE_VALUE_UNKNOWN:
        counts["dollar_value_unresolved"] += 1
        counts["product_candidates_researched"] += 1
        return
    if state == STATE_RAW:
        counts["raw_unresolved"] += 1
        return

    # Product path researched
    counts["product_candidates_researched"] += 1
    if (result.get("historical_comparable_count") or 0) > 0 and result.get("last_government_unit_price"):
        counts["historical_pricing_found"] += 1
    if result.get("current_public_price"):
        counts["current_market_pricing_found"] += 1
    if result.get("last_government_unit_price") and result.get("current_public_price"):
        counts["both_price_sides_found"] += 1

    if state == STATE_COMPLETE:
        counts["complete_candidates"] += 1
    elif state == STATE_RESEARCHABLE:
        counts["researchable_candidates"] += 1
    elif state in {STATE_NO_HISTORY, STATE_NO_MARKET}:
        counts["research_incomplete"] += 1


def _apply_filter(rows: list[dict[str, Any]], filter_state: str) -> list[dict[str, Any]]:
    fs = (filter_state or "COMPLETE").upper()
    if fs in {"ALL", "*"}:
        return list(rows)
    if fs == "COMPLETE":
        return [r for r in rows if r.get("research_state") == STATE_COMPLETE]
    if fs in {"RESEARCH_INCOMPLETE", "INCOMPLETE"}:
        return [
            r
            for r in rows
            if r.get("research_state")
            in {STATE_NO_HISTORY, STATE_NO_MARKET, STATE_WEAK_IDENTITY, STATE_VALUE_UNKNOWN, STATE_RESEARCHING}
        ]
    if fs == "REJECTED":
        return [r for r in rows if r.get("research_state") == STATE_REJECTED]
    if fs == "RESEARCHABLE":
        return [r for r in rows if r.get("research_state") in {STATE_RESEARCHABLE, STATE_COMPLETE}]
    if fs == "UNRESOLVED":
        return [r for r in rows if r.get("research_state") == STATE_RAW]
    return [r for r in rows if r.get("research_state") == STATE_COMPLETE]
