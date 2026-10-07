"""Pre-quote decision + quote priority score. Orchestrates existing evidence only."""

from __future__ import annotations

from typing import Any

PRE_QUOTE_DECISIONS = (
    "CALL_TODAY",
    "QUOTE_IF_CAPACITY",
    "WATCH",
    "PASS",
    "INSUFFICIENT_EVIDENCE",
)


def quote_priority_score(
    *,
    headroom_pct: float | None,
    coverage_class: str,
    channel_score: int,
    days_remaining: float | None,
    supplier_breadth: int,
    competition_dist_pct: float,
    financing_plausible: bool | None,
) -> tuple[int, dict[str, float]]:
    """Weighted 0–100. Missing verified inputs contribute 0 for that factor (not invented)."""
    parts: dict[str, float] = {}

    # PUBLIC PRICE HEADROOM 30%
    if headroom_pct is None:
        parts["headroom"] = 0.0
    elif headroom_pct >= 25:
        parts["headroom"] = 30.0
    elif headroom_pct >= 15:
        parts["headroom"] = 24.0
    elif headroom_pct >= 8:
        parts["headroom"] = 18.0
    elif headroom_pct > 0:
        parts["headroom"] = 10.0
    else:
        parts["headroom"] = 0.0

    # PUBLIC PRICE COVERAGE 20%
    cov_map = {"COMPLETE": 20, "STRONG": 18, "USABLE": 14, "WEAK": 6, "INSUFFICIENT": 0}
    parts["coverage"] = float(cov_map.get(coverage_class, 0))

    # CHANNEL FIT 20% — lower dominance = higher fit
    if channel_score <= 20:
        parts["channel"] = 20.0
    elif channel_score <= 40:
        parts["channel"] = 14.0
    elif channel_score <= 60:
        parts["channel"] = 8.0
    elif channel_score <= 80:
        parts["channel"] = 3.0
    else:
        parts["channel"] = 0.0

    # RUNWAY 10%
    if days_remaining is None:
        parts["runway"] = 0.0
    elif days_remaining >= 10:
        parts["runway"] = 10.0
    elif days_remaining >= 5:
        parts["runway"] = 8.0
    elif days_remaining >= 3:
        parts["runway"] = 4.0
    else:
        parts["runway"] = 0.0

    # SUPPLIER BREADTH 10%
    if supplier_breadth >= 3:
        parts["supplier"] = 10.0
    elif supplier_breadth == 2:
        parts["supplier"] = 7.0
    elif supplier_breadth == 1:
        parts["supplier"] = 4.0
    else:
        parts["supplier"] = 0.0

    # COMPETITION 5% — lower distributor bidder % better
    if competition_dist_pct <= 20:
        parts["competition"] = 5.0
    elif competition_dist_pct <= 40:
        parts["competition"] = 3.0
    elif competition_dist_pct <= 60:
        parts["competition"] = 1.0
    else:
        parts["competition"] = 0.0

    # FINANCING 5%
    if financing_plausible is True:
        parts["financing"] = 5.0
    elif financing_plausible is False:
        parts["financing"] = 0.0
    else:
        parts["financing"] = 0.0

    return int(round(sum(parts.values()))), parts


def pre_quote_decision(
    *,
    channel_class: str,
    channel_score: int,
    coverage_class: str,
    headroom: float | None,
    headroom_pct: float | None,
    priority: int,
    days_remaining: float | None,
    eligibility: str | None,
    package_state: str | None,
    exception_reason: str | None = None,
) -> tuple[str, list[str]]:
    """Combine hard kills + MSRP-first screen into PRE_QUOTE_DECISION."""
    reasons: list[str] = []

    if coverage_class == "INSUFFICIENT":
        return "INSUFFICIENT_EVIDENCE", ["public_price_coverage_insufficient"]

    # Hard kill: channel dominated without exceptional headroom
    exceptional = headroom_pct is not None and headroom_pct >= 30 and coverage_class in {"STRONG", "COMPLETE"}
    if channel_class == "D_CHANNEL_DOMINATED" and not exceptional and not exception_reason:
        reasons.append("D_CHANNEL_DOMINATED_without_exceptional_headroom")
        return "PASS", reasons

    if headroom is not None and headroom < 0 and not exception_reason:
        reasons.append("public_basket_above_government_value")
        return "PASS", reasons

    # Need verified headroom for CALL/QUOTE
    if headroom_pct is None or headroom is None:
        if coverage_class in {"WEAK", "USABLE"}:
            return "WATCH", ["headroom_not_computable_or_coverage_weak"]
        return "INSUFFICIENT_EVIDENCE", ["missing_visible_headroom"]

    if headroom_pct <= 0:
        return "PASS", ["no_visible_headroom"]

    elig = str(eligibility or "")
    if "BLOCKED" in elig.upper():
        return "PASS", ["eligibility_blocked"]

    pkg = str(package_state or "")
    package_ok = "ACQUIRED" in pkg.upper() or "ACCESSIBLE" in pkg.upper() or pkg == ""

    days_ok = days_remaining is None or days_remaining >= 3
    if not days_ok:
        return "PASS", ["insufficient_runway"]

    if (
        channel_class in {"A_RESELLER_FRIENDLY", "B_MIXED_CHANNEL"}
        and coverage_class in {"USABLE", "STRONG", "COMPLETE"}
        and headroom_pct >= 12
        and priority >= 55
        and package_ok
    ):
        reasons.append("reseller_friendly_with_msrp_headroom")
        return "CALL_TODAY", reasons

    if (
        channel_class in {"A_RESELLER_FRIENDLY", "B_MIXED_CHANNEL", "C_DISTRIBUTOR_ADVANTAGED"}
        and coverage_class in {"USABLE", "STRONG", "COMPLETE"}
        and headroom_pct >= 8
        and priority >= 40
    ):
        reasons.append("enough_headroom_for_capacity_quote")
        return "QUOTE_IF_CAPACITY", reasons

    if exceptional and exception_reason:
        reasons.append(exception_reason)
        return "QUOTE_IF_CAPACITY", reasons

    if coverage_class == "WEAK" or (headroom_pct is not None and 0 < headroom_pct < 8):
        return "WATCH", ["weak_coverage_or_thin_headroom"]

    return "WATCH", ["does_not_clear_call_or_quote_gates"]
