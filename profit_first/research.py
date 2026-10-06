"""Research prioritization — chase the missing fact that most changes the profit decision."""

from __future__ import annotations

from typing import Any


def research_next_priority(econ: dict[str, Any], *, identity_known: bool = True) -> dict[str, Any]:
    """What missing fact would most change the profit decision?"""
    missing = list(econ.get("missing_facts") or [])
    rev = econ.get("expected_revenue")
    cost = econ.get("product_cost")
    spread = econ.get("gross_spread")
    freight = econ.get("freight")
    status = econ.get("profit_status")

    if not identity_known:
        return {
            "priority": "PRODUCT_IDENTITY",
            "reason": "Cannot price what we cannot identify",
            "urgency": "HIGH",
        }

    if "ACQUISITION_COST" in missing and rev is not None:
        return {
            "priority": "ACQUISITION_COST",
            "reason": f"Government value known ({rev}); cost unknown — determines profit",
            "urgency": "HIGH",
        }
    if "GOVERNMENT_VALUE" in missing and cost is not None:
        return {
            "priority": "GOVERNMENT_VALUE",
            "reason": f"Acquisition cost known ({cost}); need defensible government value",
            "urgency": "HIGH",
        }
    if rev is None and cost is None:
        return {
            "priority": "BOTH_SIDES",
            "reason": "Need government value and acquisition cost",
            "urgency": "HIGH",
        }
    if "FREIGHT" in missing and spread is not None and spread > 0:
        # Freight could erase spread
        return {
            "priority": "FREIGHT",
            "reason": f"Gross spread {spread}; freight unknown and may erase profit",
            "urgency": "HIGH" if spread < 15000 else "MEDIUM",
        }
    if "FINANCING" in missing and (econ.get("post_freight_profit") or 0) > 0:
        return {
            "priority": "FINANCING",
            "reason": "Post-freight positive; financing cost needed for final profit",
            "urgency": "MEDIUM",
        }
    if status in {"LIKELY_PROFITABLE", "POSSIBLE_PROFIT"}:
        return {
            "priority": "SUPPLIER_QUOTE",
            "reason": "Improve cost basis / confirm executability",
            "urgency": "MEDIUM",
        }
    if status == "UNPROVEN":
        return {
            "priority": "EVIDENCE_GATHERING",
            "reason": "Insufficient economics evidence",
            "urgency": "MEDIUM",
        }
    return {
        "priority": "NONE",
        "reason": "Economics sufficient for current decision or unprofitable",
        "urgency": "LOW",
    }
