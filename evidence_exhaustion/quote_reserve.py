"""Quote outreach reserve — last resort after automated exhaustion.

Build: 20261004-m3-evidence-exhaustion-v1
"""

from __future__ import annotations

from typing import Any

from evidence_exhaustion.models import (
    CHANNEL_CONTACT_REQUIRED,
    DISTRIBUTOR_QUOTE_REQUIRED,
    INSUFFICIENT_EVIDENCE,
    MIN_EXHAUSTION_SCORE_FOR_TERMINAL,
    OEM_QUOTE_REQUIRED,
    QUOTE_OUTREACH_RESERVE,
    REQUIRED_EXHAUSTION_FLAGS,
    RESEARCH_RETRYABLE,
    SUPPLIER_QUOTE_REQUIRED,
    UNPROVEN,
)


def _f(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def missing_exhaustion_flags(flags: dict[str, Any], *, routing: str | None = None) -> list[str]:
    missing = [k for k in REQUIRED_EXHAUSTION_FLAGS if not flags.get(k)]
    if routing == "STRONG_GENERIC_SPEC" and not flags.get("generic_spec_search_exhausted"):
        missing.append("generic_spec_search_exhausted")
    if routing == "BRAND_OR_EQUAL" and not flags.get("brand_equal_search_exhausted"):
        missing.append("brand_equal_search_exhausted")
    if routing == "SPECIALTY_OEM_NARROW_CHANNEL" and not flags.get("channel_research_exhausted"):
        missing.append("channel_research_exhausted")
    return missing


def sensitivity_table(
    *,
    expected_gov_revenue: float | None,
    thresholds: list[float] | None = None,
    target_margin_pct: float = 0.15,
) -> dict[str, Any]:
    """What supplier quote is required — does NOT invent the quote."""
    rev = _f(expected_gov_revenue)
    if rev is None or rev <= 0:
        return {"ok": False, "reason": "NO_REVENUE_REFERENCE"}
    th = thresholds or [0.0, 1000.0, 2500.0, 5000.0, 10000.0]
    quote_points = []
    for frac in (0.70, 0.75, 0.80, 0.85, 0.90):
        cost = round(rev * frac, 2)
        quote_points.append(
            {
                "assumed_quote_cost": cost,
                "implied_profit": round(rev - cost, 2),
                "implied_margin_pct": round(100.0 * (rev - cost) / rev, 1),
            }
        )
    max_cost = {
        "break_even": round(rev, 2),
        **{f"profit_{int(t) if t >= 1 else '0'}": round(rev - t, 2) for t in th},
        "target_margin": round(rev * (1.0 - target_margin_pct), 2),
    }
    return {
        "ok": True,
        "expected_government_revenue": rev,
        "quote_cost_scenarios": quote_points,
        "maximum_acceptable_acquisition_cost": max_cost,
        "note": "Sensitivity only — actual quote not invented",
    }


def admit_to_quote_reserve(
    *,
    eligibility_ok: bool,
    has_package: bool,
    usable_identity: bool,
    fatal_execution_blocker: bool,
    revenue_or_value: bool,
    time_remaining_ok: bool,
    sourcing_path: str | None,
    exhaustion_flags: dict[str, Any],
    exhaustion_score: float,
    routing: str | None = None,
    plausible_profit_if_quote_ok: bool,
    channel: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Strict admission — premature reserve forbidden."""
    missing = missing_exhaustion_flags(exhaustion_flags, routing=routing)
    if missing:
        return {
            "admitted": False,
            "status": RESEARCH_RETRYABLE,
            "reason": "EXHAUSTION_INCOMPLETE",
            "next_routes": missing,
            "premature": True,
        }
    if exhaustion_score < MIN_EXHAUSTION_SCORE_FOR_TERMINAL:
        return {
            "admitted": False,
            "status": RESEARCH_RETRYABLE,
            "reason": "EXHAUSTION_SCORE_TOO_LOW",
            "score": exhaustion_score,
            "next_routes": ["deepen_remaining_routes"],
            "premature": True,
        }
    if not eligibility_ok:
        return {"admitted": False, "status": "ELIGIBILITY_BLOCKED", "premature": False}
    if fatal_execution_blocker:
        return {"admitted": False, "status": "EXECUTION_BLOCKED", "premature": False}
    if not has_package or not usable_identity:
        return {"admitted": False, "status": INSUFFICIENT_EVIDENCE, "premature": False}
    if not revenue_or_value:
        return {"admitted": False, "status": UNPROVEN, "premature": False}
    if not time_remaining_ok:
        return {"admitted": False, "status": "INSUFFICIENT_TIME", "premature": False}
    if not sourcing_path:
        return {"admitted": False, "status": INSUFFICIENT_EVIDENCE, "reason": "NO_SOURCING_PATH"}
    if not plausible_profit_if_quote_ok:
        return {"admitted": False, "status": "UNPROFITABLE", "premature": False}

    ch = channel or {}
    sub = (
        ch.get("suggested_quote_substatus")
        or (
            OEM_QUOTE_REQUIRED
            if sourcing_path == "OEM"
            else DISTRIBUTOR_QUOTE_REQUIRED
            if sourcing_path == "DISTRIBUTOR"
            else SUPPLIER_QUOTE_REQUIRED
            if sourcing_path == "SUPPLIER"
            else CHANNEL_CONTACT_REQUIRED
        )
    )
    return {
        "admitted": True,
        "status": QUOTE_OUTREACH_RESERVE,
        "substatus": sub,
        "premature": False,
        "note": "Public/automated evidence exhausted; opportunity remains potentially viable pending real quote",
    }
