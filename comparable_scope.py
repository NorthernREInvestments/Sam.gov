"""Pricing comparables: normalize awards to $/sq ft per visit; filter by clearance & geography only."""

from __future__ import annotations

import os
import re
import statistics
from typing import Any

# Facility buckets — janitorial contracts in unlike buildings are not comparable.
FACILITY_PATTERNS: list[tuple[str, str]] = [
    ("headquarters", r"\bheadquarters\b|\bhq\b"),
    ("office", r"\boffice\b|\badministrative\b|\brecruitment\b"),
    ("barracks", r"\bbarracks\b|\bdormitor"),
    ("warehouse", r"\bwarehouse\b|\bstorage\b"),
    ("medical", r"\bhospital\b|\bclinic\b|\bmedical\b|\bhealth\s+center\b"),
    ("hangar", r"\bhangar\b"),
    ("school", r"\bschool\b|\buniversity\b|\bcampus\b"),
    ("industrial", r"\bindustrial\b|\bplant\b|\bfactory\b"),
]

# Ordered most restrictive first for clearance level matching.
CLEARANCE_PATTERNS: list[tuple[str, str]] = [
    ("top_secret", r"top\s*secret|ts/sci|\bts\b.*clearance"),
    ("secret", r"\bsecret\s+clearance\b|active\s+secret|\bssbi\b"),
    ("public_trust", r"public\s+trust|moderate\s+risk\s+background|high\s+risk\s+background"),
    (
        "restricted_access",
        r"unescorted\s+access|restricted\s+area|cac\s+(?:holder|card|required)|"
        r"security\s+clearance|badge\s+access|controlled\s+access\s+area|"
        r"background\s+investigation\s+required|fingerprint",
    ),
]

CLEARANCE_RANK = {name: idx for idx, (name, _) in enumerate(CLEARANCE_PATTERNS)}

SQ_FT_PATTERN = re.compile(
    r"(\d{1,3}(?:,\d{3})+|\d+)\s*(?:"
    r"square\s*feet|square\s*foot|sq\.?\s*ft\.?|sqft|\bsf\b"
    r")",
    re.IGNORECASE,
)

# Service frequency — used to normalize award value to $/sq ft per site visit.
FREQUENCY_PATTERNS: list[tuple[str, str, float]] = [
    ("daily", r"\bdaily\b|7\s+days?\s+(?:a|per)\s+week|every\s+day", 365),
    ("weekday_daily", r"5\s+days?\s+(?:a|per)\s+week|monday\s+(?:through|thru|to)\s+friday", 260),
    ("twice_weekly", r"twice\s+(?:a|per)\s+week|2\s+times?\s+(?:a|per)\s+week", 104),
    ("weekly", r"(?:once\s+(?:a|per)\s+week|weekly|every\s+week|\bper\s+week\b)", 52),
    ("biweekly", r"bi-?weekly|every\s+(?:two|2)\s+weeks?", 26),
    ("monthly", r"(?:once\s+(?:a|per)\s+month|monthly|every\s+month|\bper\s+month\b)", 12),
    ("quarterly", r"quarterly|every\s+quarter|4\s+times?\s+(?:a|per)\s+year", 4),
    (
        "semiannual",
        r"twice\s+(?:a|per)\s+year|semi-?annual|two\s+times?\s+(?:a|per)\s+year|"
        r"2\s+times?\s+(?:a|per)\s+year|every\s+6\s+months?",
        2,
    ),
    ("annual", r"(?:once\s+(?:a|per)\s+year|annually|annual\s+(?:service|cleaning|inspection))", 1),
    ("one_time", r"one-?time|single\s+(?:occurrence|event)|move-?in/move-?out", 0.5),
]

TIMES_PER_YEAR_PATTERN = re.compile(
    r"(\d+)\s+times?\s+(?:per|a)\s+year",
    re.IGNORECASE,
)

FREQUENCY_LABELS = {
    "daily": "Daily",
    "weekday_daily": "Weekdays (M–F)",
    "twice_weekly": "Twice weekly",
    "weekly": "Weekly",
    "biweekly": "Biweekly",
    "monthly": "Monthly",
    "quarterly": "Quarterly",
    "semiannual": "Twice per year",
    "annual": "Once per year",
    "one_time": "One-time",
    "custom": "Custom schedule",
}


def detect_clearance_level(text: str) -> str | None:
    """Return most restrictive clearance/access level found in text, or None."""
    if not text:
        return None
    lowered = text.lower()
    for name, pattern in CLEARANCE_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return name
    return None


def clearance_label(level: str | None) -> str:
    if not level:
        return "No clearance required"
    labels = {
        "top_secret": "Top Secret clearance",
        "secret": "Secret clearance",
        "public_trust": "Public Trust / background check",
        "restricted_access": "Restricted access / badge required",
    }
    return labels.get(level, level.replace("_", " ").title())


def frequency_label(name: str | None, visits_per_year: float | None = None) -> str:
    if not name and visits_per_year:
        if visits_per_year >= 200:
            return "Daily / weekdays"
        if visits_per_year >= 40:
            return "Weekly"
        if visits_per_year >= 20:
            return "Biweekly"
        if visits_per_year >= 6:
            return "Monthly"
        if visits_per_year >= 3:
            return "Quarterly"
        if visits_per_year >= 1.5:
            return "Twice per year"
        return "Once per year or less"
    if not name:
        return "Unknown frequency"
    label = FREQUENCY_LABELS.get(name, name.replace("_", " ").title())
    if visits_per_year and name == "custom":
        return f"{int(visits_per_year)}× per year"
    return label


def detect_service_frequency(text: str) -> dict[str, Any] | None:
    """Return {name, visits_per_year, label} from solicitation/award text."""
    if not text:
        return None
    lowered = text.lower()
    for name, pattern, visits in FREQUENCY_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return {
                "name": name,
                "visits_per_year": visits,
                "label": frequency_label(name, visits),
            }
    match = TIMES_PER_YEAR_PATTERN.search(text)
    if match:
        try:
            visits = float(match.group(1))
        except ValueError:
            visits = 0
        if visits >= 1:
            return {
                "name": "custom",
                "visits_per_year": visits,
                "label": frequency_label("custom", visits),
            }
    return None


def extract_square_feet(text: str) -> int | None:
    """Largest square-footage figure mentioned in text (500+ sq ft)."""
    if not text:
        return None
    square_feet: int | None = None
    for match in SQ_FT_PATTERN.finditer(text):
        raw = match.group(1).replace(",", "")
        try:
            value = int(raw)
        except ValueError:
            continue
        if value < 500:
            continue
        if square_feet is None or value > square_feet:
            square_feet = value
    return square_feet


def award_text_blob(award: dict[str, Any]) -> str:
    description = str(award.get("description") or award.get("award_description") or "")
    title_bits = " ".join(
        str(award.get(key) or "")
        for key in ("recipient_name", "awarding_agency", "performance_location")
    )
    return f"{description} {title_bits}".strip()


def annotate_award_unit_rates(award: dict[str, Any]) -> dict[str, Any]:
    """
    Add normalized pricing fields: award amount ÷ sq ft ÷ visits per year = $/sq ft per visit.
    Assumes USAspending award_amount is an annual contract value when frequency is known.
    """
    row = dict(award)
    text = award_text_blob(row)
    amount = row.get("award_amount")
    sqft = extract_square_feet(text)
    freq = detect_service_frequency(text)
    visits = freq["visits_per_year"] if freq else None

    row["award_square_feet"] = sqft
    row["award_visits_per_year"] = visits
    row["award_frequency_label"] = freq["label"] if freq else None
    row["price_per_sqft"] = None
    row["price_per_sqft_per_visit"] = None

    if sqft and amount and float(amount) > 0:
        row["price_per_sqft"] = round(float(amount) / sqft, 4)
    if sqft and visits and visits > 0 and amount and float(amount) > 0:
        row["price_per_sqft_per_visit"] = round(float(amount) / (sqft * visits), 6)

    return row


def filter_clearance_compatible_awards(
    awards: list[dict[str, Any]],
    profile: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop awards with incompatible clearance; do not filter by square footage."""
    target_clearance = profile.get("clearance_level")
    kept: list[dict[str, Any]] = []
    dropped = 0
    for award in awards:
        row = annotate_award_unit_rates(award)
        text = award_text_blob(row)
        award_clearance = detect_clearance_level(text)
        ok, _ = _clearance_compatible(target_clearance, award_clearance)
        if ok:
            row["clearance_level"] = award_clearance
            kept.append(row)
        else:
            dropped += 1

    meta: dict[str, Any] = {
        "candidates": len(awards),
        "kept_count": len(kept),
        "excluded_clearance": dropped,
        "target_clearance_level": profile.get("clearance_level"),
        "target_clearance_label": clearance_label(profile.get("clearance_level")),
        "target_square_feet": profile.get("square_feet"),
        "target_visits_per_year": profile.get("visits_per_year"),
        "target_frequency_label": profile.get("service_frequency_label"),
        "unit_rate_matching": True,
        "scope_matching": True,
    }
    notes: list[str] = []
    if dropped:
        notes.append(f"Excluded {dropped} award(s) with incompatible clearance/access.")
    if profile.get("clearance_level"):
        notes.append(f"Your contract: {clearance_label(profile['clearance_level'])}.")
    elif dropped:
        notes.append("Your contract: no clearance required — restricted-access awards excluded.")
    rated = sum(1 for a in kept if a.get("price_per_sqft_per_visit"))
    if rated:
        notes.append(
            f"{rated} award(s) normalized to $/sq ft per visit "
            f"(contract value ÷ square footage ÷ visits per year)."
        )
    else:
        notes.append(
            "Few awards had both square footage and frequency in the description — "
            "showing total contract amounts where unit rates are unavailable."
        )
    if notes:
        meta["scope_note"] = " ".join(notes)
    return kept, meta


def summarize_unit_rates(
    awards: list[dict[str, Any]],
    profile: dict[str, Any],
) -> dict[str, Any]:
    """
    Regional unit-rate summary and recommended annual bid.

    Formula (arm's-length initial interest):
      regional avg $/sq ft per visit × contract sq ft × annual visits = recommended annual bid
    """
    rated = [
        a
        for a in awards
        if a.get("price_per_sqft_per_visit") and a.get("recency_weight")
    ]
    summary: dict[str, Any] = {
        "rated_awards_count": len(rated),
        "average_price_per_sqft_per_visit": None,
        "weighted_average_price_per_sqft_per_visit": None,
        "regional_avg_price_per_sqft_per_visit": None,
        "lowest_price_per_sqft_per_visit": None,
        "highest_price_per_sqft_per_visit": None,
        "recommended_annual_bid": None,
        "recommended_bid_formula": None,
        "recommended_bid_low": None,
        "recommended_bid_high": None,
        "recommended_bid_note": None,
        "contract_square_feet": profile.get("square_feet"),
        "contract_visits_per_year": profile.get("visits_per_year"),
        "contract_frequency_label": profile.get("service_frequency_label"),
    }
    if not rated:
        summary["recommended_bid_note"] = (
            "Need local awards with square footage and frequency in the description "
            "to calculate $/sq ft per visit."
        )
        return summary

    rates = [float(a["price_per_sqft_per_visit"]) for a in rated]
    pairs = [(float(a["price_per_sqft_per_visit"]), float(a["recency_weight"])) for a in rated]
    total_weight = sum(w for _, w in pairs)
    regional_avg = sum(r * w for r, w in pairs) / total_weight if total_weight else statistics.mean(rates)

    summary["average_price_per_sqft_per_visit"] = round(statistics.mean(rates), 6)
    summary["weighted_average_price_per_sqft_per_visit"] = round(regional_avg, 6)
    summary["regional_avg_price_per_sqft_per_visit"] = round(regional_avg, 6)
    summary["lowest_price_per_sqft_per_visit"] = round(min(rates), 6)
    summary["highest_price_per_sqft_per_visit"] = round(max(rates), 6)

    target_sqft = profile.get("square_feet")
    target_visits = profile.get("visits_per_year")
    if not target_sqft or not target_visits or target_visits <= 0:
        summary["recommended_bid_note"] = (
            "Square footage or service frequency not found on this contract — "
            "cannot project recommended annual bid yet."
        )
        return summary

    recommended = regional_avg * target_sqft * target_visits
    freq_label = profile.get("service_frequency_label") or frequency_label(None, target_visits)
    visits_int = int(target_visits) if target_visits == int(target_visits) else target_visits

    summary["recommended_annual_bid"] = round(recommended, 2)
    summary["recommended_bid_low"] = round(min(rates) * target_sqft * target_visits, 2)
    summary["recommended_bid_high"] = round(max(rates) * target_sqft * target_visits, 2)
    summary["recommended_bid_formula"] = (
        f"${regional_avg:.4f}/sq ft/visit × {target_sqft:,} sq ft × "
        f"{freq_label} ({visits_int} visits/yr) = ${recommended:,.0f}"
    )
    summary["recommended_bid_note"] = (
        "Arm's-length initial interest: regional average $/sq ft per visit "
        f"× {target_sqft:,} sq ft × {visits_int} visits/year."
    )
    if max(rates) / max(min(rates), 1e-9) > 5:
        summary["recommended_bid_note"] += (
            " Comparable unit rates vary widely — treat as a rough starting point."
        )

    return summary


def _clearance_compatible(target_level: str | None, award_level: str | None) -> tuple[bool, str]:
    """Comparable awards must match clearance posture (open vs restricted)."""
    if not target_level and not award_level:
        return True, "no clearance on either side"
    if not target_level and award_level:
        return False, f"award requires {clearance_label(award_level)} — yours does not"
    if target_level and not award_level:
        return False, "your contract requires clearance — award does not mention it"
    assert target_level and award_level
    target_rank = CLEARANCE_RANK.get(target_level, 99)
    award_rank = CLEARANCE_RANK.get(award_level, 99)
    if abs(target_rank - award_rank) <= 1:
        return True, f"similar access ({clearance_label(award_level)})"
    return False, f"clearance mismatch ({clearance_label(award_level)} vs {clearance_label(target_level)})"


def extract_scope_profile(
    *texts: str | None,
    clearance_required: bool | None = None,
    service_frequency: str | None = None,
    visits_per_year: float | None = None,
) -> dict[str, Any]:
    """Pull square footage, facility type, and clearance signals from solicitation text."""
    blob = " ".join(t for t in texts if t).strip()
    if not blob and clearance_required is None:
        return {
            "square_feet": None,
            "facility_types": [],
            "clearance_level": None,
            "clearance_required": False,
            "service_frequency": None,
            "visits_per_year": None,
            "service_frequency_label": None,
            "text_sample": "",
        }

    square_feet = extract_square_feet(blob) if blob else None

    facility_types: list[str] = []
    lowered = blob.lower()
    for label, pattern in FACILITY_PATTERNS:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            facility_types.append(label)

    clearance_level = detect_clearance_level(blob) if blob else None
    if clearance_required is True and not clearance_level:
        clearance_level = "restricted_access"

    freq = detect_service_frequency(blob) if blob else None
    if visits_per_year and visits_per_year >= 1:
        freq = {
            "name": service_frequency or "custom",
            "visits_per_year": float(visits_per_year),
            "label": frequency_label(service_frequency or "custom", float(visits_per_year)),
        }
    elif service_frequency and not freq:
        for name, _, visits in FREQUENCY_PATTERNS:
            if name == service_frequency or service_frequency.lower() in frequency_label(name).lower():
                freq = {"name": name, "visits_per_year": visits, "label": frequency_label(name, visits)}
                break

    return {
        "square_feet": square_feet,
        "facility_types": facility_types,
        "clearance_level": clearance_level,
        "clearance_required": bool(clearance_level),
        "service_frequency": freq["name"] if freq else None,
        "visits_per_year": freq["visits_per_year"] if freq else None,
        "service_frequency_label": freq["label"] if freq else None,
        "text_sample": blob[:500],
    }


def pricing_max_distance_miles() -> int:
    raw = os.getenv("PRICING_MAX_DISTANCE_MILES", "120").strip()
    try:
        return max(10, int(raw))
    except ValueError:
        return 120


def pricing_allow_neighbor_states() -> bool:
    return os.getenv("PRICING_ALLOW_NEIGHBOR_STATES", "false").strip().lower() in ("1", "true", "yes")
