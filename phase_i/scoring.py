"""Deal-hunt priority scoring — never overrides hard blockers."""

from __future__ import annotations

import re
from typing import Any

_NSN_RE = re.compile(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b")
_PN_RE = re.compile(r"\bP/?N[:\s]+([A-Z0-9][A-Z0-9._/-]{2,})\b", re.I)
_PRODUCT_NOUN = re.compile(
    r"\b(PARTS?\s+KIT|TEST\s+SET|ADAPTER|ASSEMBLY|PUMP|VALVE|BEARING|PANEL|"
    r"CIRCUIT\s+CARD|WHEEL|BLADE|DISPLAY|AMPLIFIER|FILTER|CONNECTOR|MOTOR)\b",
    re.I,
)
_SERVICEISH = re.compile(
    r"\b(janitorial|consulting|staffing|construction|rehabilitation|"
    r"design[\-\s]?build|sewer|services?\b|idiq\b|sacc\b|sources?\s+sought)\b",
    re.I,
)
_CUSTOM_HARD = re.compile(
    r"\b(FAT\b|first\s+article|source\s+approval|SAR\b|QPL\b|controlled\s+drawing|"
    r"TDP\b|technical\s+data\s+package|New\s+Manufactured\s+Material)\b",
    re.I,
)
_REPAIRISH = re.compile(r"\bRepair\s+of\b|\boverhaul\b", re.I)


def deal_hunt_score(row: dict[str, Any], *, eligibility_status: str | None = None) -> dict[str, Any]:
    """
    Deterministic priority score for deep-research ordering.
    Hard blockers (eligibility fail, service, expired) should exclude before scoring.
    """
    title = str(row.get("title") or "")
    blob = f"{title}\n{row.get('description') or ''}"
    reasons: list[str] = []
    score = 0

    if _NSN_RE.search(title) or row.get("has_nsn") or row.get("nsn"):
        score += 40
        reasons.append("exact_NSN")
    if _PN_RE.search(title):
        score += 15
        reasons.append("P/N_in_title")
    if row.get("is_dla") or "DLA" in str(row.get("buyer") or "").upper():
        score += 12
        reasons.append("DLA_path")
    if _PRODUCT_NOUN.search(title):
        score += 10
        reasons.append("product_noun")
    if row.get("ui_link") or row.get("listing_url") or row.get("source_opportunity_id"):
        score += 5
        reasons.append("listing_resolvable")
    if row.get("enriched"):
        score += 3
        reasons.append("prior_enrich")

    # Soft positives
    if eligibility_status == "ELIGIBILITY_NOT_APPLICABLE":
        score += 8
        reasons.append("no_special_vehicle")
    elif eligibility_status == "ELIGIBLE_CONFIRMED":
        score += 10
        reasons.append("eligibility_confirmed")

    # Soft negatives (not hard reject)
    if _CUSTOM_HARD.search(blob):
        score -= 15
        reasons.append("possible_TDP_FAT_SAR")
    if _REPAIRISH.search(title):
        score -= 40
        reasons.append("repair_overhaul_title")
    if _SERVICEISH.search(title):
        score -= 30
        reasons.append("serviceish_title")

    days = row.get("deadline_runway_days")
    try:
        d = float(days) if days is not None else None
    except (TypeError, ValueError):
        d = None
    if d is not None:
        if d < 3:
            score -= 25
            reasons.append("very_short_runway")
        elif d >= 14:
            score += 8
            reasons.append("runway_14d_plus")
        elif d >= 7:
            score += 4
            reasons.append("runway_7d_plus")

    return {
        "deal_hunt_score": score,
        "deal_hunt_reasons": reasons,
        "has_nsn": bool(_NSN_RE.search(title) or row.get("has_nsn") or row.get("nsn")),
        "has_pn": bool(_PN_RE.search(title)),
    }
