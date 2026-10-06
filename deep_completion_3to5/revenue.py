"""Revenue re-validation for deep-completion candidates."""

from __future__ import annotations

import json
from typing import Any

from deep_completion_3to5.models import PRIOR_REV, REVENUE_NOT_READY
from material_line_identity_price_recovery.models import (
    BOND_THRESHOLD,
    BUDGET,
    CEILING,
    CURRENT_CONTRACT_VALUE,
    ESTIMATE,
    HISTORICAL_REFERENCE,
    INSURANCE_THRESHOLD,
    OTHER_NON_REVENUE_AMOUNT,
    REVENUE_ALLOWED,
)
from material_line_identity_price_recovery.revenue_context import classify_money_context
from m3_data_root import data_path

_TYPE_MAP = {
    "CURRENT_VALUE_EXPLICIT": CURRENT_CONTRACT_VALUE,
    "EXACT_PRIOR_LINE_VALUE": HISTORICAL_REFERENCE,
    "COMPARABLE_PRIOR_BASKET_VALUE": HISTORICAL_REFERENCE,
    "BUYER_CATEGORY_REFERENCE": HISTORICAL_REFERENCE,
    "CHANNEL_ONLY_REFERENCE": OTHER_NON_REVENUE_AMOUNT,
}


def _load(name: str) -> dict[str, Any]:
    p = data_path(name)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def validate_revenue(opportunity_id: str) -> dict[str, Any]:
    rev = (_load(PRIOR_REV).get("by_opportunity") or {}).get(opportunity_id) or {}
    best = ((rev.get("revenue") or {}).get("best") or {})
    value = best.get("reference_value")
    etype = best.get("revenue_evidence_type")
    snippet = str(best.get("snippet") or "")
    source = best.get("source")

    # Semantic classify when snippet present
    if snippet and value is not None:
        classified = classify_money_context(float(value), text_window=snippet, document=source)
        role = classified["semantic_role"]
        usable = classified["usable_as_revenue"]
    else:
        role = _TYPE_MAP.get(str(etype or ""), OTHER_NON_REVENUE_AMOUNT)
        # Buyer-category mega totals are weak — allow as HISTORICAL_REFERENCE but mark low confidence
        usable = role in REVENUE_ALLOWED
        classified = {
            "semantic_role": role,
            "usable_as_revenue": usable,
            "reasons": ["mapped_from_evidence_type"],
            "text_window": "",
        }

    # Harden: BUYER_CATEGORY_REFERENCE alone is not strong enough for economics targeting
    confidence = "HIGH"
    if etype == "BUYER_CATEGORY_REFERENCE":
        confidence = "LOW"
        # Still historically referential but do not treat as contract value for ceilings
        role = HISTORICAL_REFERENCE
        usable = True  # allowed class, but weak
    if etype == "EXACT_PRIOR_LINE_VALUE":
        confidence = "MEDIUM"
        role = HISTORICAL_REFERENCE
    if etype == "COMPARABLE_PRIOR_BASKET_VALUE":
        confidence = "LOW"
        role = HISTORICAL_REFERENCE
    if etype == "CURRENT_VALUE_EXPLICIT" and usable:
        confidence = "HIGH"
        role = CURRENT_CONTRACT_VALUE if role not in {BOND_THRESHOLD, INSURANCE_THRESHOLD} else role

    if role in {BOND_THRESHOLD, INSURANCE_THRESHOLD, OTHER_NON_REVENUE_AMOUNT} or not usable:
        return {
            "opportunity_id": opportunity_id,
            "status": REVENUE_NOT_READY,
            "revenue_evidence": etype,
            "value": None,
            "reported_prior_value": value,
            "classification": role,
            "confidence": "NONE",
            "PASS_FAIL": "FAIL",
            "usable_as_revenue": False,
            "source": source,
            "snippet": snippet[:240],
            "classified": classified,
        }

    return {
        "opportunity_id": opportunity_id,
        "status": "REVENUE_READY" if confidence != "LOW" else "REVENUE_WEAK",
        "revenue_evidence": etype,
        "value": float(value) if value is not None else None,
        "classification": role,
        "confidence": confidence,
        "PASS_FAIL": "PASS" if usable else "FAIL",
        "usable_as_revenue": usable,
        "source": source,
        "snippet": snippet[:240],
        "exact_or_estimated": best.get("exact_or_estimated"),
        "classified": classified,
    }
