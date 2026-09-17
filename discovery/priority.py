"""Discovery research priority heuristic — NOT actual profit."""

from __future__ import annotations

from typing import Any

from discovery.constants import (
    CLASS_CORE_PRODUCT,
    CLASS_PRODUCT_PLUS_SERVICE,
    CLASS_SERVICE,
    CLASS_UNKNOWN,
    TIER_1,
)


def compute_research_priority(
    *,
    classification: str,
    estimated_value: str | None = None,
    estimated_value_status: str = "UNKNOWN",
    trust_tier: int = 3,
    deadline_days: int | None = None,
    document_count: int = 0,
    brand_name_required: bool = False,
    recurring_language: bool = False,
    set_aside_fit: bool | None = None,
    reject: bool = False,
) -> dict[str, Any]:
    """
    Deterministic PRE-RESEARCH priority.
    Label clearly as heuristic — never display as profit.
    """
    if reject:
        return {
            "research_priority": 0,
            "research_priority_label": "REJECTED",
            "is_actual_profit": False,
            "is_heuristic": True,
            "factors": ["rejected"],
            "LIVE_API_REQUESTS": 0,
        }

    score = 50
    factors: list[str] = []

    if classification == CLASS_CORE_PRODUCT:
        score += 40
        factors.append("core_product")
    elif classification == CLASS_PRODUCT_PLUS_SERVICE:
        score += 20
        factors.append("product_plus_service")
    elif classification == CLASS_UNKNOWN:
        score += 5
        factors.append("unknown_class")
    elif classification == CLASS_SERVICE:
        score -= 40
        factors.append("service")

    if estimated_value_status != "UNKNOWN" and estimated_value:
        score += 15
        factors.append("known_estimated_value")
        # Do not invent numbers — only boost if present
    else:
        factors.append("value_unknown_no_penalty")

    if trust_tier <= TIER_1:
        score += 15
        factors.append("tier1_official")
    elif trust_tier == 2:
        score += 8
        factors.append("tier2_coop")

    if document_count > 0:
        score += min(10, document_count * 2)
        factors.append("documents_available")

    if brand_name_required:
        score += 5
        factors.append("brand_name_requirement")

    if recurring_language:
        score += 8
        factors.append("recurring_purchase_language")

    if set_aside_fit is True:
        score += 10
        factors.append("set_aside_fit")
    elif set_aside_fit is False:
        score -= 20
        factors.append("set_aside_mismatch")

    if deadline_days is not None:
        if 3 <= deadline_days <= 21:
            score += 10
            factors.append("healthy_deadline_runway")
        elif deadline_days < 2:
            score -= 15
            factors.append("deadline_imminent")
        elif deadline_days > 90:
            score -= 5
            factors.append("deadline_far")

    score = max(0, min(100, score))
    if score >= 75:
        label = "HIGH"
    elif score >= 50:
        label = "MEDIUM"
    elif score >= 25:
        label = "LOW"
    else:
        label = "DEPRIORITIZE"

    return {
        "research_priority": score,
        "research_priority_label": label,
        "is_actual_profit": False,
        "is_heuristic": True,
        "display_note": "RESEARCH_PRIORITY heuristic — not actual profit",
        "factors": factors,
        "LIVE_API_REQUESTS": 0,
    }
