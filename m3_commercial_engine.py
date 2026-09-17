"""Commercial Intelligence Engine — evaluate profit candidacy without fabricating margins.

Government award price alone is NOT profit.
Research only: no supplier contact, accounts, quotes requests, financing outreach, or bids.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from application_clock import now_utc
from m3_commercial_intelligence import (  # noqa: F401 — re-export existing helpers
    assess_commercial_economics,
    assess_competition_history,
    build_commercial_research_panel,
)
from m3_evidence_constants import (
    COMPETITION_HISTORY_UNKNOWN,
    PUBLIC_RESEARCH_INCOMPLETE,
    WHOLESALE_PRICE_VERIFICATION_REQUIRED,
)

# Pricing confidence levels (Phase 4)
PRICE_LEVEL_1_ACTUAL = "LEVEL_1_ACTUAL_SUPPLIER_PRICE"
PRICE_LEVEL_2_PUBLIC = "LEVEL_2_PUBLIC_COMMERCIAL"
PRICE_LEVEL_3_COMPARABLE = "LEVEL_3_COMPARABLE"
PRICE_LEVEL_4_UNKNOWN = "LEVEL_4_UNKNOWN"

SCORE_HIGH = "HIGH"
SCORE_MEDIUM = "MEDIUM"
SCORE_LOW = "LOW"

FIN_EASY = "EASY"
FIN_MODERATE = "MODERATE"
FIN_DIFFICULT = "DIFFICULT"

WINNER_MANUFACTURER = "MANUFACTURER"
WINNER_DISTRIBUTOR = "DISTRIBUTOR"
WINNER_RESELLER = "RESELLER"
WINNER_UNKNOWN = "UNKNOWN"

NSN_RE = re.compile(r"\b(\d{4}-\d{2}-\d{3}-\d{4})\b")
PART_RE = re.compile(
    r"\b(?:P/?N|PART\s*(?:NO|NUMBER|#)|MFG\s*P/?N)[:\s#]*([A-Z0-9][A-Z0-9\-/\.]{2,30})\b",
    re.I,
)
CAGE_RE = re.compile(r"\bCAGE[:\s#]*([A-Z0-9]{5})\b", re.I)
QTY_RE = re.compile(r"\b(?:QTY|QUANTITY)[:\s]*(\d{1,6})\b", re.I)

# Well-known manufacturers often named in product solicitations (signal only)
# Prefer longer / more distinctive names first; skip ultra-short ambiguous tokens
KNOWN_MANUFACTURERS = [
    "Hewlett Packard",
    "John Deere",
    "Snap-on",
    "Plantronics",
    "Schneider",
    "Honeywell",
    "Caterpillar",
    "Microsoft",
    "Motorola",
    "Logitech",
    "Samsung",
    "Lenovo",
    "Brother",
    "Grainger",
    "Fastenal",
    "Insight",
    "Canon",
    "Epson",
    "Xerox",
    "Cisco",
    "Apple",
    "Dell",
    "Sony",
    "Bosch",
    "Fluke",
    "Eaton",
    "Oracle",
    "Adobe",
    "Garmin",
    "Makita",
    "DeWalt",
    "Milwaukee",
    "Toyota",
    "Ford",
    "IBM",
    "HP",
    "3M",
    "APC",
    "CDW",
    "SHI",
    "Poly",
]

# Tokens too ambiguous to match from free text alone
AMBIGUOUS_MFR_TOKENS = {"IBM", "HP", "APC", "3M", "Poly", "Ford"}

DISTRIBUTOR_HINTS = (
    "distributor",
    "wholesale",
    "authorized dealer",
    "value added reseller",
    "var ",
    "reseller",
    "supply co",
    "supplies",
    "trading",
    "import",
)
OEM_HINTS = (
    "inc.",
    "incorporated",
    "corp",
    "corporation",
    "manufactur",
    "mfg",
    "oem",
    "factory",
)


def _utc() -> str:
    return now_utc().isoformat()


def _num(v: Any) -> float | None:
    if v is None or v == "" or v == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _text_blob(row: dict[str, Any]) -> str:
    parts = [
        str(row.get("title") or ""),
        str(row.get("description") or ""),
        str(row.get("evidence_text_excerpt") or ""),
        str(row.get("solicitation_number") or ""),
    ]
    for li in row.get("line_items") or row.get("bom") or []:
        if isinstance(li, dict):
            parts.append(str(li.get("description") or ""))
            parts.append(str(li.get("manufacturer") or ""))
            parts.append(str(li.get("part_number") or ""))
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    parts.append(str(meta.get("nsn") or ""))
    return " ".join(parts)


def extract_identification(row: dict[str, Any]) -> dict[str, Any]:
    """IDENTIFICATION block — exact product signals when present; else UNKNOWN."""
    # Prefer title + BOM for manufacturer identity; full evidence blob causes portal chrome false hits
    title = str(row.get("title") or "")
    desc = str(row.get("description") or "")[:2000]
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    bom = row.get("line_items") or row.get("bom") or []
    first = bom[0] if isinstance(bom, list) and bom and isinstance(bom[0], dict) else {}
    bom_text = " ".join(
        f"{li.get('description') or ''} {li.get('manufacturer') or ''} {li.get('part_number') or ''}"
        for li in (bom if isinstance(bom, list) else [])
        if isinstance(li, dict)
    )
    identity_blob = " ".join([title, desc, bom_text, str(meta.get("nsn") or ""), str(row.get("solicitation_number") or "")])
    full_blob = _text_blob(row)

    nsn = meta.get("nsn") or (NSN_RE.search(identity_blob).group(1) if NSN_RE.search(identity_blob) else None)
    if not nsn and NSN_RE.search(full_blob):
        nsn = NSN_RE.search(full_blob).group(1)
    part = first.get("part_number") or (PART_RE.search(identity_blob).group(1) if PART_RE.search(identity_blob) else None)
    cage = first.get("cage") or (CAGE_RE.search(identity_blob).group(1) if CAGE_RE.search(identity_blob) else None)
    qty = _num(first.get("quantity"))
    if qty is None:
        qm = QTY_RE.search(identity_blob)
        qty = float(qm.group(1)) if qm else None

    mfr = first.get("manufacturer") or row.get("manufacturer")
    if not mfr:
        for name in KNOWN_MANUFACTURERS:
            if name in AMBIGUOUS_MFR_TOKENS:
                if not re.search(
                    rf"\b{re.escape(name)}\b",
                    title + " " + bom_text,
                    re.I,
                ):
                    continue
            if re.search(rf"\b{re.escape(name)}\b", title + " " + bom_text, re.I):
                mfr = name
                break

    return {
        "solicitation_number": row.get("solicitation_number") or row.get("external_id") or "UNKNOWN",
        "agency": row.get("agency") or row.get("buyer") or "UNKNOWN",
        "buyer": row.get("buyer") or row.get("agency") or "UNKNOWN",
        "title": row.get("title") or "UNKNOWN",
        "product_category": row.get("product_category") or row.get("product_classification") or "UNKNOWN",
        "NSN": nsn or "UNKNOWN",
        "part_number": part or "UNKNOWN",
        "manufacturer": mfr or "UNKNOWN",
        "CAGE": cage or "UNKNOWN",
        "quantity": qty if qty is not None else "UNKNOWN",
        "delivery_requirements": row.get("delivery_requirements")
        or meta.get("place_of_performance")
        or "UNKNOWN",
    }


def classify_winner_type(name: str, hints: dict[str, Any] | None = None) -> str:
    """Classify historical winner as MANUFACTURER / DISTRIBUTOR / RESELLER / UNKNOWN."""
    hints = hints or {}
    explicit = str(hints.get("vendor_type") or hints.get("winner_type") or "").upper()
    if explicit in {WINNER_MANUFACTURER, "OEM", "MFG"}:
        return WINNER_MANUFACTURER
    if explicit in {WINNER_DISTRIBUTOR, "DEALER"}:
        return WINNER_DISTRIBUTOR
    if explicit in {WINNER_RESELLER, "VAR"}:
        return WINNER_RESELLER

    n = (name or "").lower()
    if any(h in n for h in ("distribution", "distributor", "wholesale", "supply")):
        return WINNER_DISTRIBUTOR
    if any(h in n for h in ("reseller", "trading", "solutions", "technologies", "services llc", "enterprises")):
        return WINNER_RESELLER
    if any(h in n for h in OEM_HINTS) and not any(h in n for h in DISTRIBUTOR_HINTS):
        # weak OEM signal — still often UNKNOWN without confirmation
        return WINNER_UNKNOWN
    return WINNER_UNKNOWN


def build_winner_intelligence(row: dict[str, Any]) -> dict[str, Any]:
    """Historical winner analysis + WINNER_PATTERN_SCORE. Never invents profit."""
    awards = row.get("historical_awards") or row.get("award_history") or []
    if not isinstance(awards, list):
        awards = []

    # Also accept competition panel winners
    panel = row.get("commercial_research") or {}
    hist_panel = panel.get("historical_winners") if isinstance(panel, dict) else {}

    winners: list[dict[str, Any]] = []
    name_counts: Counter[str] = Counter()
    total_dollars = 0.0
    unit_prices: list[float] = []
    nsn_hits: list[str] = []

    for a in awards:
        if not isinstance(a, dict):
            continue
        name = str(a.get("winner") or a.get("vendor") or a.get("awardee") or "").strip()
        if not name:
            continue
        wtype = classify_winner_type(name, a)
        amt = _num(a.get("amount") or a.get("award_amount") or a.get("dollars"))
        unit = _num(a.get("unit_price") or a.get("unit_amount"))
        if amt:
            total_dollars += amt
        if unit:
            unit_prices.append(unit)
        nsn = a.get("nsn") or a.get("NSN")
        if nsn:
            nsn_hits.append(str(nsn))
        key = name.lower()
        name_counts[key] += 1
        winners.append(
            {
                "name": name,
                "type": wtype,
                "amount": amt if amt is not None else "UNKNOWN",
                "unit_price": unit if unit is not None else "UNKNOWN",
                "nsn": nsn or "UNKNOWN",
                "date": a.get("date") or a.get("award_date") or "UNKNOWN",
            }
        )

    repeats = [{"name": n, "wins": c} for n, c in name_counts.most_common(8) if c >= 2]
    pattern_score = 0
    if repeats:
        pattern_score += min(40, sum(c for _, c in ((r["name"], r["wins"]) for r in repeats)) * 8)
    if len(name_counts) >= 3:
        pattern_score += 15  # open competition among multiple winners
    if any(w["type"] == WINNER_RESELLER for w in winners):
        pattern_score += 20
    if any(w["type"] == WINNER_DISTRIBUTOR for w in winners):
        pattern_score += 15
    if total_dollars > 0:
        pattern_score += 10
    pattern_score = min(100, pattern_score)

    competition = assess_competition_history(awards) if awards else {
        "signal": (hist_panel or {}).get("signal") or COMPETITION_HISTORY_UNKNOWN,
        "auto_reject": False,
        "winners_sample": (hist_panel or {}).get("winners_sample") or [],
    }

    return {
        "kind": "M3WinnerIntelligence",
        "previous_winners": winners[:20],
        "award_frequency": len(awards),
        "total_dollars": round(total_dollars, 2) if total_dollars else "UNKNOWN",
        "unit_pricing_samples": unit_prices[:10] or "UNKNOWN",
        "repeat_NSNs": sorted(set(nsn_hits))[:10] or "UNKNOWN",
        "repeat_winners": repeats,
        "WINNER_PATTERN_SCORE": pattern_score,
        "competition_signal": competition.get("signal"),
        "notes": [
            "historical_award_is_not_guaranteed_current_revenue",
            "winner_type_classification_is_heuristic_not_verified",
            *(competition.get("notes") or []),
        ],
        "auto_reject": False,
    }


def discover_suppliers_public(row: dict[str, Any], ident: dict[str, Any] | None = None) -> dict[str, Any]:
    """Public-signal supplier discovery only — no contact, no accounts."""
    ident = ident or extract_identification(row)
    blob = _text_blob(row).lower()
    manufacturers: list[dict[str, Any]] = []
    distributors: list[dict[str, Any]] = []
    dealers: list[dict[str, Any]] = []
    gov_suppliers: list[dict[str, Any]] = []

    mfr = ident.get("manufacturer")
    if mfr and mfr != "UNKNOWN":
        manufacturers.append(
            {
                "name": mfr,
                "role": "OEM_CANDIDATE",
                "source": "solicitation_text_or_line_item",
                "confidence": "MEDIUM" if mfr in KNOWN_MANUFACTURERS else "LOW",
            }
        )

    # Known channel brands appearing in text
    for brand, role in (
        ("CDW", "DISTRIBUTOR"),
        ("SHI", "DISTRIBUTOR"),
        ("Insight", "DISTRIBUTOR"),
        ("Grainger", "DISTRIBUTOR"),
        ("Fastenal", "DISTRIBUTOR"),
        ("Amazon Business", "PUBLIC_DEALER"),
        ("GovX", "GOVERNMENT_SUPPLIER"),
    ):
        if brand.lower() in blob:
            entry = {"name": brand, "role": role, "source": "text_mention", "confidence": "LOW"}
            if role == "DISTRIBUTOR":
                distributors.append(entry)
            elif role == "PUBLIC_DEALER":
                dealers.append(entry)
            else:
                gov_suppliers.append(entry)

    # Historical winners as government-supplier candidates
    for w in (row.get("historical_awards") or [])[:8]:
        if not isinstance(w, dict):
            continue
        name = str(w.get("winner") or w.get("vendor") or w.get("awardee") or "").strip()
        if not name:
            continue
        gov_suppliers.append(
            {
                "name": name,
                "role": classify_winner_type(name, w),
                "source": "historical_award",
                "confidence": "MEDIUM",
            }
        )

    # Existing supplier_candidates on row
    for s in row.get("supplier_candidates") or []:
        if not isinstance(s, dict):
            continue
        name = s.get("name") or s.get("supplier")
        if not name:
            continue
        dealers.append(
            {
                "name": name,
                "role": s.get("role") or "CANDIDATE",
                "source": s.get("source") or "row_supplier_candidates",
                "confidence": "LOW",
            }
        )

    supply_confidence = SCORE_LOW
    if manufacturers and (distributors or dealers or gov_suppliers):
        supply_confidence = SCORE_HIGH
    elif manufacturers or distributors or len(gov_suppliers) >= 2:
        supply_confidence = SCORE_MEDIUM

    return {
        "kind": "M3SupplierDiscovery",
        "OEM_manufacturer": manufacturers[:5],
        "authorized_distributors": distributors[:8],
        "public_dealer_channels": dealers[:8],
        "government_suppliers": gov_suppliers[:10],
        "wholesale_sources": [],  # never invent wholesale contacts
        "supply_confidence": supply_confidence,
        "notes": [
            "research_only_no_outreach",
            "wholesale_sources_require_operator_verification",
            "authorized_status_not_confirmed_without_public_listing_evidence",
        ],
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def assess_pricing_intelligence(row: dict[str, Any]) -> dict[str, Any]:
    """Pricing confidence LEVEL 1–4. Never invents prices."""
    pricing = row.get("commercial_pricing") or row.get("public_pricing") or {}
    verified = _num(pricing.get("verified_wholesale_unit") or row.get("supplier_unit_cost"))
    public = _num(
        pricing.get("lowest_public_new_unit")
        or pricing.get("public_unit_price")
        or row.get("public_price_total")
    )
    comparable = _num(pricing.get("comparable_unit") or pricing.get("msrp"))
    gov = _num(
        pricing.get("government_historical_unit")
        or pricing.get("latest_government_unit")
        or row.get("government_revenue")
    )
    # Contract/solicitation value signals (revenue potential — NOT cost)
    meta = row.get("raw_metadata") if isinstance(row.get("raw_metadata"), dict) else {}
    contract_value = _num(
        row.get("estimated_value")
        or row.get("solicitation_value")
        or row.get("award_amount")
        or meta.get("award_amount")
    )

    if verified is not None:
        level = PRICE_LEVEL_1_ACTUAL
        confidence = SCORE_HIGH
        source = pricing.get("verified_source") or "row_verified_wholesale"
    elif public is not None:
        level = PRICE_LEVEL_2_PUBLIC
        confidence = SCORE_MEDIUM
        source = pricing.get("public_source") or "public_commercial_listing"
    elif comparable is not None:
        level = PRICE_LEVEL_3_COMPARABLE
        confidence = SCORE_LOW
        source = pricing.get("comparable_source") or "comparable_product"
    else:
        level = PRICE_LEVEL_4_UNKNOWN
        confidence = SCORE_LOW
        source = "none"

    acq_low = verified if verified is not None else ("UNKNOWN" if public is None else "UNKNOWN")
    acq_high = public if public is not None else "UNKNOWN"
    # Only compute margin range when BOTH government revenue signal AND acquisition evidence exist
    margin_estimate = "UNKNOWN"
    margin_confidence = SCORE_LOW
    if isinstance(gov, (int, float)) and isinstance(verified, (int, float)) and verified > 0:
        margin_estimate = round(((gov - verified) / gov) * 100, 1)
        margin_confidence = SCORE_MEDIUM
        # Still not HIGH without freight/financing
    elif isinstance(gov, (int, float)) and isinstance(public, (int, float)) and public > 0:
        # Public vs gov is NOT margin — flag verification required
        margin_estimate = "COMMERCIAL_VERIFICATION_REQUIRED"
        margin_confidence = SCORE_LOW

    status = "COMMERCIAL_VERIFICATION_REQUIRED"
    if level == PRICE_LEVEL_1_ACTUAL and isinstance(margin_estimate, (int, float)) and margin_estimate > 0:
        status = "PRELIMINARY_MARGIN_SUPPORTED"
    elif level == PRICE_LEVEL_4_UNKNOWN:
        status = "ACQUISITION_COST_UNKNOWN"

    return {
        "kind": "M3PricingIntelligence",
        "pricing_level": level,
        "government_paid_or_value": gov if gov is not None else (contract_value if contract_value is not None else "UNKNOWN"),
        "public_retail": public if public is not None else "UNKNOWN",
        "wholesale_estimate": verified if verified is not None else "UNKNOWN",
        "estimated_acquisition_cost_low": acq_low,
        "estimated_acquisition_cost_high": acq_high,
        "estimated_gross_margin": margin_estimate,
        "margin_confidence": margin_confidence,
        "source": source,
        "date": pricing.get("as_of") or row.get("last_seen_at") or _utc(),
        "confidence": confidence,
        "status": status,
        "notes": [
            "government_price_is_not_profit",
            "do_not_assume_cost_from_award_value",
            WHOLESALE_PRICE_VERIFICATION_REQUIRED
            if level in {PRICE_LEVEL_2_PUBLIC, PRICE_LEVEL_3_COMPARABLE, PRICE_LEVEL_4_UNKNOWN}
            else PUBLIC_RESEARCH_INCOMPLETE,
        ],
    }


def score_reseller_fit(row: dict[str, Any], ident: dict[str, Any], winners: dict[str, Any]) -> dict[str, Any]:
    """RESELLER_FIT_SCORE — tangible goods / part numbers / repeat buys / no install."""
    score = 0
    reasons: list[str] = []
    blob = _text_blob(row).lower()
    classification = str(row.get("product_classification") or "").upper()

    if classification in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE"}:
        score += 20
        reasons.append("product_resale_classification")
    elif classification not in {"SERVICE", "LIKELY_SERVICE_FALSE_POSITIVE"} and ident.get("manufacturer") not in {
        None,
        "UNKNOWN",
    }:
        score += 12
        reasons.append("named_manufacturer_product_title")
    cat = str(row.get("product_category") or "").upper()
    if any(x in cat for x in ("IT_", "ELECTRONIC", "COMPUTER", "PARTS", "VEHICLE", "INDUSTRIAL", "HVAC", "OFFICE")):
        score += 8
        reasons.append("product_category_signal")
    if ident.get("part_number") not in {None, "UNKNOWN"}:
        score += 15
        reasons.append("exact_part_number")
    if ident.get("NSN") not in {None, "UNKNOWN"}:
        score += 15
        reasons.append("NSN_present")
    if ident.get("manufacturer") not in {None, "UNKNOWN"}:
        score += 12
        reasons.append("known_manufacturer_signal")
    if (winners.get("award_frequency") or 0) >= 2:
        score += 12
        reasons.append("repeat_government_purchases")
    if (winners.get("WINNER_PATTERN_SCORE") or 0) >= 30:
        score += 10
        reasons.append("multiple_or_repeat_historical_vendors")
    if not any(x in blob for x in ("install", "installation", "labor", "services only", "professional services")):
        score += 10
        reasons.append("no_installation_signal")
    else:
        score -= 15
        reasons.append("installation_or_services_language")
    qty = ident.get("quantity")
    if isinstance(qty, (int, float)) and 1 <= qty <= 500:
        score += 8
        reasons.append("manageable_quantity")
    elif isinstance(qty, (int, float)) and qty > 5000:
        score -= 5
        reasons.append("very_large_quantity")

    score = max(0, min(100, score))
    band = SCORE_HIGH if score >= 65 else (SCORE_MEDIUM if score >= 40 else SCORE_LOW)
    return {
        "RESELLER_FIT_SCORE": score,
        "band": band,
        "reasons": reasons,
    }


def score_financing_complexity(
    row: dict[str, Any],
    *,
    pricing: dict[str, Any],
    ident: dict[str, Any],
) -> dict[str, Any]:
    """FINANCING_COMPLEXITY_SCORE — EASY / MODERATE / DIFFICULT. No financier contact."""
    value = _num(pricing.get("government_paid_or_value"))
    qty = ident.get("quantity")
    blob = _text_blob(row).lower()
    custom = any(x in blob for x in ("custom", "fabricat", "made to order", "build to print", "sole source"))

    if custom or (isinstance(value, (int, float)) and value >= 500_000):
        level = FIN_DIFFICULT
        why = "large_upfront_or_custom_manufacturing_signal"
    elif isinstance(value, (int, float)) and value >= 75_000:
        level = FIN_MODERATE
        why = "larger_order_supplier_terms_likely_needed"
    elif isinstance(qty, (int, float)) and qty <= 50 and (value is None or value < 75_000):
        level = FIN_EASY
        why = "small_inventory_or_common_product_profile"
    elif value is None:
        level = FIN_MODERATE
        why = "contract_size_unknown_default_moderate"
    else:
        level = FIN_EASY
        why = "modest_size_common_goods_profile"

    return {
        "FINANCING_COMPLEXITY_SCORE": level,
        "reason": why,
        "required_working_capital": value if value is not None else "UNKNOWN",
        "notes": ["no_financier_contact", "classification_is_heuristic"],
    }


def classification_ok(row: dict[str, Any]) -> bool:
    c = str(row.get("product_classification") or "").upper()
    cat = str(row.get("product_category") or "").upper()
    title = str(row.get("title") or "").lower()
    if c in {"SERVICE"} or "LIKELY_SERVICE" in cat:
        return False
    if any(x in title for x in ("restoration", "construction", "elevator replacement", "wetland", "install")):
        return False
    if any(x in title for x in ("switch", "laptop", "computer", "printer", "monitor", "server", "nsn", "equipment only")):
        return True
    if c in {"CORE_PRODUCT", "PRODUCT_PLUS_SERVICE", "UNKNOWN", ""}:
        return True
    return cat not in {"", "UNKNOWN"}


def score_commercial_opportunity(
    *,
    ident: dict[str, Any],
    winners: dict[str, Any],
    suppliers: dict[str, Any],
    pricing: dict[str, Any],
    reseller: dict[str, Any],
    financing: dict[str, Any],
    row: dict[str, Any],
) -> dict[str, Any]:
    """COMMERCIAL_OPPORTUNITY_SCORE as HIGH/MEDIUM/LOW with explanations — no fake precision."""
    points = 0
    explanations: list[str] = []

    # Revenue potential (signal only)
    gov_val = _num(pricing.get("government_paid_or_value"))
    if isinstance(gov_val, (int, float)) and gov_val >= 10_000:
        points += 15
        explanations.append("material_revenue_signal")
    elif gov_val is None:
        explanations.append("revenue_unknown")

    # Margin potential — only when acquisition evidence exists
    if pricing.get("pricing_level") == PRICE_LEVEL_1_ACTUAL:
        points += 25
        explanations.append("actual_acquisition_price_found")
    elif pricing.get("pricing_level") == PRICE_LEVEL_2_PUBLIC:
        points += 10
        explanations.append("public_price_found_wholesale_unverified")
    else:
        explanations.append("acquisition_cost_unknown_no_assumed_margin")

    # Supply confidence
    sc = suppliers.get("supply_confidence")
    if sc == SCORE_HIGH:
        points += 20
        explanations.append("manufacturer_and_channel_signals")
    elif sc == SCORE_MEDIUM:
        points += 12
        explanations.append("partial_supply_signals")
    else:
        explanations.append("unknown_supply_chain")

    # Competition / winners
    wps = int(winners.get("WINNER_PATTERN_SCORE") or 0)
    if wps >= 40:
        points += 15
        explanations.append("strong_historical_winner_pattern")
    elif wps >= 15:
        points += 8
        explanations.append("some_historical_winner_signal")

    # Reseller fit
    rfit = int(reseller.get("RESELLER_FIT_SCORE") or 0)
    if rfit >= 65:
        points += 15
        explanations.append("strong_reseller_fit")
    elif rfit >= 40:
        points += 8
        explanations.append("moderate_reseller_fit")

    # Financing ease
    if financing.get("FINANCING_COMPLEXITY_SCORE") == FIN_EASY:
        points += 8
        explanations.append("financing_looks_easy")
    elif financing.get("FINANCING_COMPLEXITY_SCORE") == FIN_DIFFICULT:
        points -= 10
        explanations.append("financing_difficult")

    # Compliance / sole source risk
    blob = _text_blob(row).lower()
    if "sole source" in blob or "brand name only" in blob:
        points -= 12
        explanations.append("sole_source_or_brand_lock_risk")
    if any(x in blob for x in ("clearance", "secret", "classified", "facility clearance")):
        points -= 20
        explanations.append("compliance_clearance_risk")

    points = max(0, min(100, points))

    # Band rules — never invent margin; still elevate identifiable resale candidates
    reseller_high = rfit >= 65
    reseller_med = rfit >= 40
    has_mfr = ident.get("manufacturer") not in {None, "UNKNOWN"}
    has_part = ident.get("part_number") not in {None, "UNKNOWN"} or ident.get("NSN") not in {None, "UNKNOWN"}
    clearance = any(x in blob for x in ("clearance", "secret", "classified", "facility clearance"))
    sole = "sole source" in blob or "brand name only" in blob

    if clearance or (sole and sc == SCORE_LOW):
        band = SCORE_LOW
    elif points >= 60 and sc != SCORE_LOW:
        band = SCORE_HIGH
    elif pricing.get("pricing_level") == PRICE_LEVEL_1_ACTUAL and isinstance(
        pricing.get("estimated_gross_margin"), (int, float)
    ):
        band = SCORE_HIGH if float(pricing["estimated_gross_margin"]) > 10 else SCORE_MEDIUM
    elif reseller_high and sc != SCORE_LOW and (has_mfr or has_part):
        band = SCORE_MEDIUM
        explanations.append("reseller_fit_strong_pending_acquisition_verification")
    elif has_mfr and sc != SCORE_LOW and not clearance and classification_ok(row):
        band = SCORE_MEDIUM
        explanations.append("named_manufacturer_with_channel_pending_cost_verification")
    elif points >= 35 or (reseller_med and has_mfr and sc != SCORE_LOW):
        band = SCORE_MEDIUM
        if points < 35:
            explanations.append("identifiable_product_channel_pending_cost_verification")
    else:
        band = SCORE_LOW

    next_action = "Low priority"
    if band in {SCORE_HIGH, SCORE_MEDIUM} and pricing.get("pricing_level") == PRICE_LEVEL_4_UNKNOWN:
        next_action = "Find distributor pricing"
    elif band == SCORE_HIGH:
        next_action = "Verify approved source"
    elif sc == SCORE_LOW:
        next_action = "Need supplier quote"
    elif ident.get("manufacturer") == "UNKNOWN":
        next_action = "Identify manufacturer"
    elif band == SCORE_MEDIUM:
        next_action = "Find distributor pricing"

    return {
        "COMMERCIAL_OPPORTUNITY_SCORE": band,
        "score_points": points,
        "explanations": explanations,
        "next_action": next_action,
    }


def build_commercial_intelligence(row: dict[str, Any]) -> dict[str, Any]:
    """Full commercial intelligence package for one opportunity."""
    ident = extract_identification(row)
    winners = build_winner_intelligence(row)
    suppliers = discover_suppliers_public(row, ident)
    pricing = assess_pricing_intelligence(row)
    reseller = score_reseller_fit(row, ident, winners)
    financing = score_financing_complexity(row, pricing=pricing, ident=ident)
    opportunity_score = score_commercial_opportunity(
        ident=ident,
        winners=winners,
        suppliers=suppliers,
        pricing=pricing,
        reseller=reseller,
        financing=financing,
        row=row,
    )
    # Keep legacy panel for compatibility
    try:
        panel = build_commercial_research_panel(row)
    except Exception:
        panel = {}

    return {
        "kind": "M3CommercialIntelligence",
        "generated_at": _utc(),
        "IDENTIFICATION": ident,
        "GOVERNMENT_SIDE": {
            "historical_award_price": pricing.get("government_paid_or_value"),
            "current_solicitation_value": pricing.get("government_paid_or_value"),
            "historical_winners": [w.get("name") for w in winners.get("previous_winners") or []][:8],
            "number_of_bidders": winners.get("award_frequency")
            if winners.get("award_frequency")
            else "UNKNOWN",
            "award_frequency": winners.get("award_frequency") or 0,
            "repeat_purchase_history": winners.get("repeat_winners") or [],
        },
        "SUPPLY_SIDE": suppliers,
        "ECONOMICS": {
            "estimated_acquisition_cost_low": pricing.get("estimated_acquisition_cost_low"),
            "estimated_acquisition_cost_high": pricing.get("estimated_acquisition_cost_high"),
            "estimated_gross_margin": pricing.get("estimated_gross_margin"),
            "margin_confidence": pricing.get("margin_confidence"),
            "required_working_capital": financing.get("required_working_capital"),
            "financing_difficulty": financing.get("FINANCING_COMPLEXITY_SCORE"),
            "pricing_level": pricing.get("pricing_level"),
            "pricing_status": pricing.get("status"),
        },
        "EXECUTION": {
            "product_availability": suppliers.get("supply_confidence"),
            "lead_time_risk": "UNKNOWN",
            "compliance_requirements": row.get("compliance_blockers") or "UNKNOWN",
            "approved_source_requirements": "UNKNOWN",
            "technical_complexity": (
                "HIGH"
                if any(x in _text_blob(row).lower() for x in ("custom", "fabricat", "engineering"))
                else "LOW"
            ),
        },
        "WINNER_INTELLIGENCE": winners,
        "PRICING_INTELLIGENCE": pricing,
        "RESELLER_FIT": reseller,
        "FINANCING": financing,
        "COMMERCIAL_OPPORTUNITY_SCORE": opportunity_score["COMMERCIAL_OPPORTUNITY_SCORE"],
        "score_detail": opportunity_score,
        "legacy_panel": panel,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def commercial_queue_rank_key(ci: dict[str, Any]) -> tuple:
    """Rank by profit candidacy — NOT raw contract value alone."""
    band = ci.get("COMMERCIAL_OPPORTUNITY_SCORE") or SCORE_LOW
    band_rank = {SCORE_HIGH: 0, SCORE_MEDIUM: 1, SCORE_LOW: 2}.get(band, 3)
    supply = (ci.get("SUPPLY_SIDE") or {}).get("supply_confidence") or SCORE_LOW
    supply_rank = {SCORE_HIGH: 0, SCORE_MEDIUM: 1, SCORE_LOW: 2}.get(supply, 3)
    fin = (ci.get("FINANCING") or {}).get("FINANCING_COMPLEXITY_SCORE") or FIN_MODERATE
    fin_rank = {FIN_EASY: 0, FIN_MODERATE: 1, FIN_DIFFICULT: 2}.get(fin, 1)
    reseller = int((ci.get("RESELLER_FIT") or {}).get("RESELLER_FIT_SCORE") or 0)
    points = int((ci.get("score_detail") or {}).get("score_points") or 0)
    return (band_rank, supply_rank, fin_rank, -reseller, -points)


def build_commercial_research_queue(
    opportunities: list[dict[str, Any]],
    *,
    limit: int = 25,
    persist: bool = False,
    store: Any = None,
) -> dict[str, Any]:
    """COMMERCIAL_RESEARCH_QUEUE — top opportunities by commercial candidacy."""
    analyzed: list[dict[str, Any]] = []
    for row in opportunities:
        if not isinstance(row, dict) or not row.get("canonical_id"):
            continue
        # Skip hard rejects and non-solicitation noise
        lc = str(row.get("lifecycle") or "").upper()
        if lc in {"REJECTED", "REJECTED_CHEAP_SCREEN", "CANCELLED", "CLOSED", "AWARDED", "ARCHIVED", "LOST"}:
            continue
        title_l = str(row.get("title") or "").lower()
        if any(
            x in title_l
            for x in (
                "how to protest",
                "how to register",
                "login",
                "vendor registration",
                "terms of use",
                "privacy policy",
            )
        ):
            continue
        ci = row.get("commercial_intelligence")
        if not isinstance(ci, dict) or ci.get("kind") != "M3CommercialIntelligence":
            ci = build_commercial_intelligence(row)
            if persist and store is not None:
                row = dict(row)
                row["commercial_intelligence"] = ci
                row["commercial_research"] = ci.get("legacy_panel") or row.get("commercial_research")
                store._rows[row["canonical_id"]] = {**(store.get(row["canonical_id"]) or row), **{
                    "commercial_intelligence": ci,
                    "commercial_research": ci.get("legacy_panel") or build_commercial_research_panel(row),
                    "commercial_opportunity_score": ci.get("COMMERCIAL_OPPORTUNITY_SCORE"),
                }}
        analyzed.append({"row": row, "ci": ci})

    analyzed.sort(key=lambda x: commercial_queue_rank_key(x["ci"]))
    top = analyzed[:limit]
    if persist and store is not None:
        try:
            store.save()
        except Exception:
            pass

    def _card(item: dict[str, Any]) -> dict[str, Any]:
        row, ci = item["row"], item["ci"]
        ident = ci.get("IDENTIFICATION") or {}
        econ = ci.get("ECONOMICS") or {}
        winners = ci.get("WINNER_INTELLIGENCE") or {}
        supply = ci.get("SUPPLY_SIDE") or {}
        return {
            "canonical_id": row.get("canonical_id"),
            "title": ident.get("title") or row.get("title"),
            "agency": ident.get("agency"),
            "product_category": ident.get("product_category"),
            "NSN": ident.get("NSN"),
            "manufacturer": ident.get("manufacturer"),
            "government_value": econ.get("pricing_status") and (ci.get("PRICING_INTELLIGENCE") or {}).get("government_paid_or_value"),
            "COMMERCIAL_OPPORTUNITY_SCORE": ci.get("COMMERCIAL_OPPORTUNITY_SCORE"),
            "supply_confidence": supply.get("supply_confidence"),
            "margin_confidence": econ.get("margin_confidence"),
            "financing_difficulty": econ.get("financing_difficulty"),
            "RESELLER_FIT_SCORE": (ci.get("RESELLER_FIT") or {}).get("RESELLER_FIT_SCORE"),
            "WINNER_PATTERN_SCORE": winners.get("WINNER_PATTERN_SCORE"),
            "historical_winners": [w.get("name") for w in (winners.get("previous_winners") or [])][:5],
            "next_action": (ci.get("score_detail") or {}).get("next_action"),
            "why_interesting": (ci.get("score_detail") or {}).get("explanations") or [],
            "pricing_level": econ.get("pricing_level"),
        }

    bands = Counter(i["ci"].get("COMMERCIAL_OPPORTUNITY_SCORE") for i in top)
    return {
        "kind": "COMMERCIAL_RESEARCH_QUEUE",
        "question": "Which opportunities are worth commercial pursuit research first?",
        "analyzed": len(analyzed),
        "queue_size": len(top),
        "HIGH": bands.get(SCORE_HIGH, 0),
        "MEDIUM": bands.get(SCORE_MEDIUM, 0),
        "LOW": bands.get(SCORE_LOW, 0),
        "queue": [_card(i) for i in top],
        "TOP_25": [_card(i) for i in top[:25]],
        "needs_supplier_verification": sum(
            1
            for i in top
            if (i["ci"].get("ECONOMICS") or {}).get("pricing_status")
            in {"COMMERCIAL_VERIFICATION_REQUIRED", "ACQUISITION_COST_UNKNOWN"}
        ),
        "with_manufacturer": sum(
            1
            for i in top
            if (i["ci"].get("IDENTIFICATION") or {}).get("manufacturer") not in {None, "UNKNOWN"}
        ),
        "with_repeat_winners": sum(
            1 for i in top if (i["ci"].get("WINNER_INTELLIGENCE") or {}).get("repeat_winners")
        ),
        "DEVELOPMENT_NO_OUTREACH": True,
    }


def analyze_top_commercial_opportunities(
    store: Any,
    *,
    limit: int = 25,
) -> dict[str, Any]:
    """Run commercial intelligence on top research candidates and persist."""
    rows = store.all() if hasattr(store, "all") else list(store)
    # Prefer research-queued / screened survivors
    def _prio(r: dict[str, Any]) -> tuple:
        lc = str(r.get("lifecycle") or "")
        queued = 0 if (r.get("research_queued") or lc in {"RESEARCH_QUEUED", "CHEAP_SCREENED", "RESEARCH_IN_PROGRESS"}) else 1
        return (queued, str(r.get("deadline") or "9999"))

    candidates = sorted(
        [
            r
            for r in rows
            if str(r.get("lifecycle") or "")
            not in {"REJECTED", "REJECTED_CHEAP_SCREEN", "CANCELLED", "CLOSED", "AWARDED", "ARCHIVED", "LOST"}
        ],
        key=_prio,
    )
    # Analyze up to limit*3 then rank to top limit
    pool = candidates[: max(limit * 3, limit)]
    for r in pool:
        ci = build_commercial_intelligence(r)
        full = {**(store.get(r["canonical_id"]) or r)}
        full["commercial_intelligence"] = ci
        full["commercial_opportunity_score"] = ci.get("COMMERCIAL_OPPORTUNITY_SCORE")
        full["commercial_research"] = ci.get("legacy_panel") or build_commercial_research_panel(full)
        store._rows[r["canonical_id"]] = full
    try:
        store.save()
    except Exception:
        pass
    # Rank using freshly built intelligence (ignore stale persisted scores for ranking)
    queue = build_commercial_research_queue(
        [
            {**(store.get(r["canonical_id"]) or r), "commercial_intelligence": (store.get(r["canonical_id"]) or {}).get("commercial_intelligence")}
            for r in store.all()
        ],
        limit=limit,
        persist=False,
    )
    return {
        "kind": "M3CommercialAnalysisRun",
        "generated_at": _utc(),
        "limit": limit,
        "pool_examined": len(pool),
        **queue,
        "DEVELOPMENT_NO_OUTREACH": True,
        "commercial_outreach": False,
    }


def deal_room_commercial_section(row: dict[str, Any]) -> dict[str, Any]:
    """Compact deal-room commercial intelligence display payload."""
    ci = row.get("commercial_intelligence")
    if not isinstance(ci, dict) or ci.get("kind") != "M3CommercialIntelligence":
        ci = build_commercial_intelligence(row)
    ident = ci.get("IDENTIFICATION") or {}
    econ = ci.get("ECONOMICS") or {}
    winners = ci.get("WINNER_INTELLIGENCE") or {}
    supply = ci.get("SUPPLY_SIDE") or {}
    pricing = ci.get("PRICING_INTELLIGENCE") or {}
    return {
        "kind": "M3DealRoomCommercial",
        "Opportunity": ident.get("title"),
        "Government_Value": pricing.get("government_paid_or_value"),
        "Historical_Winners": [w.get("name") for w in (winners.get("previous_winners") or [])][:6],
        "Known_Manufacturer": ident.get("manufacturer"),
        "Supply_Confidence": supply.get("supply_confidence"),
        "Estimated_Acquisition": {
            "low": econ.get("estimated_acquisition_cost_low"),
            "high": econ.get("estimated_acquisition_cost_high"),
            "level": econ.get("pricing_level"),
            "status": econ.get("pricing_status"),
        },
        "Margin_Potential": econ.get("estimated_gross_margin"),
        "Commercial_Confidence": ci.get("COMMERCIAL_OPPORTUNITY_SCORE"),
        "Financing_Difficulty": econ.get("financing_difficulty"),
        "Next_Action": (ci.get("score_detail") or {}).get("next_action"),
        "RESELLER_FIT_SCORE": (ci.get("RESELLER_FIT") or {}).get("RESELLER_FIT_SCORE"),
        "WINNER_PATTERN_SCORE": winners.get("WINNER_PATTERN_SCORE"),
        "full": ci,
    }
