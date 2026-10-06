"""Ranking by probability of reaching executable profit — categories are signals only."""

from __future__ import annotations

from typing import Any


def profit_probability_score(
    *,
    econ: dict[str, Any],
    signals: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Higher = more likely to become an executable profitable deal. Not a category score."""
    signals = signals or {}
    score = 40
    factors: list[str] = []
    status = str(econ.get("profit_status") or "")

    # Economics dominate
    if status == "PROVEN_PROFITABLE":
        score += 40
        factors.append("+proven_profitable")
    elif status == "LIKELY_PROFITABLE":
        score += 28
        factors.append("+likely_profitable")
    elif status == "POSSIBLE_PROFIT":
        score += 14
        factors.append("+possible_profit")
    elif status == "UNPROFITABLE":
        score -= 35
        factors.append("-unprofitable")
    elif status == "EXECUTION_BLOCKED":
        score -= 25
        factors.append("-execution_blocked")

    profit = econ.get("expected_profit") or econ.get("post_financing_profit") or econ.get("gross_spread")
    if profit is not None:
        if profit >= 30000:
            score += 15
            factors.append("+large_dollar_profit")
        elif profit >= 10000:
            score += 10
            factors.append("+strong_dollar_profit")
        elif profit >= 5000:
            score += 6
            factors.append("+meets_dollar_target")
        elif profit > 0:
            score += 2
            factors.append("+small_positive")

    if "PROFITABLE_AT_PUBLIC_RETAIL" in (econ.get("proof_signals") or []):
        score += 12
        factors.append("+profitable_at_public_retail")

    # Positive routing signals (not economic truth)
    pos = [
        ("exact_identity", 6),
        ("qty_uom_known", 4),
        ("pricing_schedule", 5),
        ("prior_award_or_bid_tab", 8),
        ("public_retail_available", 7),
        ("commercially_common", 3),  # ease signal only
        ("multiple_supplier_paths", 5),
        ("brand_or_equal", 4),
        ("recurring_buyer", 4),
        ("prior_similar_profitable", 6),
        ("multiline_priceable", 5),
        ("low_freight_risk", 4),
        ("weak_competition", 3),
        ("financing_compatible", 3),
    ]
    for key, pts in pos:
        if signals.get(key):
            score += pts
            factors.append(f"+{key}")

    neg = [
        ("impossible_eligibility", 20),
        ("unsupported_set_aside", 15),
        ("bonding_cash_unavailable", 12),
        ("mandatory_install_labor", 12),
        ("source_approval_inaccessible", 15),
        ("proprietary_no_channel", 18),
        ("custom_fabrication", 10),
        ("unknown_quantity", 8),
        ("extreme_freight_risk", 12),
        ("obvious_negative_economics", 20),
    ]
    for key, pts in neg:
        if signals.get(key):
            score -= pts
            factors.append(f"-{key}")

    # Explicit: do NOT penalize specialty/OEM/electronics/single-line merely as categories
    # (no negative points for those labels)

    score = max(0, min(100, score))
    return {
        "profit_probability_score": score,
        "factors": factors,
        "band": "HIGH" if score >= 70 else ("MEDIUM" if score >= 45 else "LOW"),
    }
