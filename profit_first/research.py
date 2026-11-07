"""Research prioritization — chase the missing fact that most changes the profit decision."""

from __future__ import annotations

from typing import Any


def _missing_set(econ: dict[str, Any]) -> set[str]:
    raw = list(econ.get("missing_facts") or []) + list(econ.get("missing_requirements") or [])
    return {str(x) for x in raw}


def research_next_priority(econ: dict[str, Any], *, identity_known: bool = True) -> dict[str, Any]:
    """What missing fact would most change the profit decision?"""
    missing = _missing_set(econ)
    rev = econ.get("proposed_bid_revenue")
    if rev is None:
        rev = econ.get("expected_revenue")
    cost = econ.get("product_cost")
    if cost is None:
        cost = (econ.get("costs") or {}).get("product_acquisition", {}).get("value")
    spread = econ.get("gross_spread")
    freight = econ.get("freight")
    if freight is None:
        freight = (econ.get("costs") or {}).get("outbound_freight", {}).get("value")
    status = econ.get("profit_status")
    readiness = econ.get("readiness")

    if not identity_known:
        return {
            "priority": "PRODUCT_IDENTITY",
            "reason": "Cannot price what we cannot identify",
            "urgency": "HIGH",
        }

    needs_acquisition = "ACQUISITION_COST" in missing or any(
        x.startswith("COST_PRODUCT_ACQUISITION") for x in missing
    )
    needs_revenue = (
        "GOVERNMENT_VALUE" in missing
        or "PROPOSED_BID_REVENUE" in missing
        or "HISTORICAL_AWARD_IS_NOT_PROPOSED_REVENUE" in missing
    )
    needs_freight = "FREIGHT" in missing or any("FREIGHT" in x for x in missing)
    needs_financing = "FINANCING" in missing or any("FINANCING" in x for x in missing)

    if needs_acquisition and rev is not None:
        return {
            "priority": "ACQUISITION_COST",
            "reason": f"Proposed bid known ({rev}); acquisition cost unknown — determines profit",
            "urgency": "HIGH",
        }
    if needs_revenue and cost is not None:
        return {
            "priority": "GOVERNMENT_VALUE",
            "reason": (
                f"Acquisition cost known ({cost}); need explicit company proposed bid "
                "(historical award is not revenue)"
            ),
            "urgency": "HIGH",
        }
    if rev is None and cost is None:
        return {
            "priority": "BOTH_SIDES",
            "reason": "Need proposed bid revenue and acquisition cost",
            "urgency": "HIGH",
        }
    if needs_freight and ((spread is not None and spread > 0) or (rev is not None and cost is not None)):
        return {
            "priority": "FREIGHT",
            "reason": f"Gross spread {spread}; freight unknown and may erase profit",
            "urgency": "HIGH" if (spread or 0) < 15000 else "MEDIUM",
        }
    if needs_financing and (econ.get("post_freight_profit") or 0) > 0:
        return {
            "priority": "FINANCING",
            "reason": "Post-freight positive; financing cost needed for final profit",
            "urgency": "MEDIUM",
        }
    if readiness == "VERIFY" or status in {"LIKELY_PROFITABLE", "POSSIBLE_PROFIT", "UNPROVEN"}:
        return {
            "priority": "SUPPLIER_QUOTE",
            "reason": "Improve cost basis / confirm executability / complete VERIFY gaps",
            "urgency": "MEDIUM",
        }
    return {
        "priority": "NONE",
        "reason": "Economics sufficient for current decision or unprofitable",
        "urgency": "LOW",
    }
