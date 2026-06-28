"""Plain-English labels, tiers, and search terms for NAICS codes used by GovTracker."""

from __future__ import annotations

from datetime import date

NAICS_LABELS: dict[str, str] = {
    # Tier 1 — daily
    "561720": "Janitorial Services",
    "561210": "Facilities Support Services",
    "561730": "Landscaping Services",
    "561710": "Pest Control Services",
    "562111": "Solid Waste Collection",
    "561790": "Other Services to Buildings and Dwellings",
    "561740": "Carpet and Upholstery Cleaning",
    "562119": "Other Waste Collection",
    # Tier 2 — Mon/Wed/Fri
    "561439": "Document Shredding Services",
    "541930": "Translation and Interpretation Services",
    "811192": "Car Wash and Vehicle Cleaning Services",
    "238220": "Plumbing and HVAC Maintenance Services",
    "562910": "Remediation Services",
    "484210": "Moving and Relocation Services",
    "484110": "General Freight Trucking Local",
    "492110": "Couriers and Messengers",
    # Tier 3 — Sunday full search
    "711320": "Photography and Videography Services",
    "532490": "Equipment Rental and Leasing",
    "561422": "Telephone Answering Services",
}

TIER_1_CODES: list[str] = [
    "561720",
    "561210",
    "561730",
    "561710",
    "562111",
    "561790",
    "561740",
    "562119",
]

TIER_2_CODES: list[str] = [
    "561439",
    "541930",
    "811192",
    "238220",
    "562910",
    "484210",
    "484110",
    "492110",
]

TIER_3_CODES: list[str] = [
    "711320",
    "532490",
    "561422",
]

NAICS_TIER_BY_CODE: dict[str, int] = {
    **{code: 1 for code in TIER_1_CODES},
    **{code: 2 for code in TIER_2_CODES},
    **{code: 3 for code in TIER_3_CODES},
}

ALL_NAICS_CODES: list[str] = TIER_1_CODES + TIER_2_CODES + TIER_3_CODES

NAICS_TIER_GROUPS: list[dict[str, str | int | list[str]]] = [
    {
        "tier": 1,
        "name": "Tier 1 — Daily search, highest priority",
        "schedule": "Every day at scheduled sync time",
        "codes": TIER_1_CODES,
    },
    {
        "tier": 2,
        "name": "Tier 2 — Every other day, high value low competition",
        "schedule": "Monday, Wednesday, and Friday",
        "codes": TIER_2_CODES,
    },
    {
        "tier": 3,
        "name": "Tier 3 — Weekly, future expansion",
        "schedule": "Sunday (included in full weekly search)",
        "codes": TIER_3_CODES,
    },
]

# Backward-compatible alias for settings UI
NAICS_GROUPS = NAICS_TIER_GROUPS

NAICS_SEARCH_TERMS: dict[str, list[str]] = {
    "561720": ["commercial cleaning company", "janitorial services"],
    "561210": ["commercial cleaning company", "janitorial services"],
    "561790": ["commercial cleaning company", "janitorial services"],
    "561740": ["commercial cleaning company", "janitorial services"],
    "561730": ["commercial landscaping", "grounds maintenance"],
    "561710": ["commercial pest control", "exterminator"],
    "562111": ["waste removal company", "waste hauling"],
    "562119": ["waste removal company", "waste hauling"],
    "561439": ["document shredding service", "secure document destruction"],
    "541930": ["translation services", "interpretation services"],
    "811192": ["fleet vehicle washing", "mobile car wash"],
    "238220": ["commercial HVAC maintenance", "plumbing maintenance"],
    "562910": ["environmental remediation", "soil remediation company"],
    "484210": ["commercial moving company", "office relocation"],
    "484110": ["freight trucking", "local freight delivery"],
    "492110": ["courier service", "same day delivery service"],
    "711320": ["commercial photography", "event photographer"],
    "532490": ["equipment rental company", "commercial equipment leasing"],
    "561422": ["telephone answering service", "call center service"],
}

TIER_SCHEDULE_SUMMARY = (
    "Tier 1 daily · Tier 2 Mon/Wed/Fri · Tier 3 Sunday (full search)"
)


def naics_label(code: str | None) -> str:
    if not code:
        return "Unknown"
    return NAICS_LABELS.get(str(code).strip(), "Other Services")


def naics_display(code: str | None) -> str:
    if not code:
        return "Unknown"
    label = naics_label(code)
    return f"{code} — {label}"


def naics_tier(code: str | None) -> int | None:
    return NAICS_TIER_BY_CODE.get(str(code or "").strip())


def tier_label(tier: int | None) -> str:
    if tier == 1:
        return "Tier 1"
    if tier == 2:
        return "Tier 2"
    if tier == 3:
        return "Tier 3"
    return "Unknown"


def codes_in_tiers(tier_numbers: list[int]) -> list[str]:
    """Return NAICS codes for the given tiers in catalog order."""
    tier_set = set(tier_numbers)
    ordered: list[str] = []
    for tier, codes in ((1, TIER_1_CODES), (2, TIER_2_CODES), (3, TIER_3_CODES)):
        if tier in tier_set:
            ordered.extend(codes)
    return ordered


def tiers_for_scheduled_sync(day: date | None = None) -> list[int]:
    """Determine which tiers to search on a scheduled sync run."""
    day = day or date.today()
    tiers = [1]
    if day.weekday() in (0, 2, 4):  # Mon, Wed, Fri
        tiers.append(2)
    if day.weekday() == 6:  # Sunday — full search including tier 3
        if 2 not in tiers:
            tiers.append(2)
        tiers.append(3)
    return sorted(set(tiers))


def search_terms_for_naics(naics_code: str | None) -> list[str] | None:
    code = str(naics_code or "").strip()
    if not code:
        return None
    return NAICS_SEARCH_TERMS.get(code)
