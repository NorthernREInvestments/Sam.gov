"""Historical government price benchmark — never invents unit prices from mixed awards."""

from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any

from application_clock import now_utc
from bid_pricing_constants import (
    HIST_BASKET,
    HIST_COMPARABLE,
    HIST_EXACT,
    HIST_FAMILY,
    HIST_WEAK,
)


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_date(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        s = str(v).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        try:
            return datetime.fromisoformat(str(v)[:10] + "T00:00:00+00:00")
        except ValueError:
            return None


def classify_observation(obs: dict[str, Any]) -> str:
    """Prevent mixed-order totals from becoming fake unit prices."""
    match = str(obs.get("match_quality") or obs.get("history_class") or "").upper()
    if match in {HIST_EXACT, HIST_FAMILY, HIST_COMPARABLE, HIST_BASKET, HIST_WEAK}:
        return match
    if obs.get("mixed_order") or obs.get("mixed_award_total"):
        return HIST_BASKET
    if obs.get("exact_product") or obs.get("part_number_match"):
        return HIST_EXACT
    if obs.get("same_family"):
        return HIST_FAMILY
    if obs.get("comparable"):
        return HIST_COMPARABLE
    if obs.get("unit_price") is None and obs.get("award_total") is not None:
        return HIST_BASKET
    return HIST_WEAK


def observation_unit_price(obs: dict[str, Any]) -> float | None:
    """Only return unit price when allocation is supported."""
    klass = classify_observation(obs)
    if klass == HIST_BASKET and not obs.get("unit_allocation_supported"):
        return None  # do not invent unit from mixed total
    if obs.get("unit_price") is not None:
        return _f(obs.get("unit_price"))
    qty = _f(obs.get("quantity"))
    total = _f(obs.get("extended_price") or obs.get("award_amount"))
    if qty and qty > 0 and total is not None and klass in {HIST_EXACT, HIST_FAMILY, HIST_COMPARABLE}:
        if obs.get("unit_allocation_supported") is False:
            return None
        return total / qty
    return None


def build_historical_government_price_benchmark(
    observations: list[dict[str, Any]] | None,
    *,
    as_of: str | None = None,
    exclude_outliers: bool = True,
    outlier_iqr_factor: float = 1.5,
) -> dict[str, Any]:
    obs_in = list(observations or [])
    usable: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []

    for raw in obs_in:
        klass = classify_observation(raw)
        unit = observation_unit_price(raw)
        if unit is None:
            exclusions.append(
                {
                    "reason": "mixed_award_or_no_supported_unit_allocation"
                    if klass == HIST_BASKET
                    else "missing_unit_price",
                    "match_quality": klass,
                    "award_total": raw.get("award_total") or raw.get("award_amount"),
                    "note": "Mixed-order totals are not exact unit pricing",
                }
            )
            continue
        if klass == HIST_WEAK and not raw.get("force_include_weak"):
            exclusions.append({"reason": "weak_comparable", "match_quality": klass, "unit_price": unit})
            continue
        qty = _f(raw.get("quantity")) or 1.0
        dt = _parse_date(raw.get("award_date") or raw.get("date"))
        usable.append(
            {
                **raw,
                "match_quality": klass,
                "unit_price_resolved": unit,
                "quantity_resolved": qty,
                "award_date_parsed": dt.isoformat() if dt else None,
            }
        )

    exact_n = sum(1 for u in usable if u["match_quality"] == HIST_EXACT)
    comparable_n = sum(1 for u in usable if u["match_quality"] in {HIST_COMPARABLE, HIST_FAMILY})
    prices = [u["unit_price_resolved"] for u in usable]

    if not prices:
        return {
            "kind": "HistoricalGovernmentPriceBenchmark",
            "low": None,
            "high": None,
            "median": None,
            "weighted_average": None,
            "recency_weighted_estimate": None,
            "number_of_observations": 0,
            "total_units_represented": 0,
            "exact_product_observation_count": 0,
            "comparable_observation_count": 0,
            "confidence": "UNKNOWN",
            "exclusions": exclusions,
            "anomalies": anomalies,
            "provenance": [],
            "false_precision_avoided": True,
            "mixed_totals_as_unit_prices": False,
            "weak_comparables_excluded": sum(1 for e in exclusions if e.get("reason") == "weak_comparable"),
        }

    # Outlier handling via IQR
    kept = list(usable)
    if exclude_outliers and len(prices) >= 4:
        sp = sorted(prices)
        def _quartile(sorted_vals: list[float], q: float) -> float:
            n = len(sorted_vals)
            pos = q * (n - 1)
            lo = int(pos)
            hi = min(lo + 1, n - 1)
            frac = pos - lo
            return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac

        q1 = _quartile(sp, 0.25)
        q3 = _quartile(sp, 0.75)
        iqr = q3 - q1
        lo_b, hi_b = q1 - outlier_iqr_factor * iqr, q3 + outlier_iqr_factor * iqr
        filtered = []
        for u in kept:
            p = u["unit_price_resolved"]
            if p < lo_b or p > hi_b:
                anomalies.append({"unit_price": p, "reason": "outlier_iqr", "match_quality": u["match_quality"]})
            else:
                filtered.append(u)
        if filtered:
            kept = filtered
            prices = [u["unit_price_resolved"] for u in kept]

    total_units = sum(u["quantity_resolved"] for u in kept)
    wavg = sum(u["unit_price_resolved"] * u["quantity_resolved"] for u in kept) / total_units if total_units else None
    med = float(median(prices))

    # Recency-weighted: newer awards weigh more (half-life ~365 days)
    as_of_dt = _parse_date(as_of) or now_utc()
    rw_num = 0.0
    rw_den = 0.0
    for u in kept:
        dt = _parse_date(u.get("award_date_parsed") or u.get("award_date"))
        age_days = max(0.0, (as_of_dt - dt).total_seconds() / 86400.0) if dt else 365.0
        weight = 0.5 ** (age_days / 365.0) * u["quantity_resolved"]
        rw_num += u["unit_price_resolved"] * weight
        rw_den += weight
    recency = rw_num / rw_den if rw_den else wavg

    if exact_n >= 3:
        confidence = "HIGH"
    elif exact_n >= 1 or comparable_n >= 3:
        confidence = "MEDIUM"
    elif prices:
        confidence = "LOW"
    else:
        confidence = "UNKNOWN"

    return {
        "kind": "HistoricalGovernmentPriceBenchmark",
        "low": round(min(prices), 4),
        "high": round(max(prices), 4),
        "median": round(med, 4),
        "weighted_average": round(wavg, 4) if wavg is not None else None,
        "recency_weighted_estimate": round(recency, 4) if recency is not None else None,
        "number_of_observations": len(kept),
        "total_units_represented": total_units,
        "exact_product_observation_count": exact_n,
        "comparable_observation_count": comparable_n,
        "weak_comparables_excluded": sum(1 for e in exclusions if e.get("reason") == "weak_comparable"),
        "confidence": confidence,
        "exclusions": exclusions,
        "anomalies": anomalies,
        "provenance": [
            {
                "award_date": u.get("award_date"),
                "unit_price": u["unit_price_resolved"],
                "quantity": u["quantity_resolved"],
                "match_quality": u["match_quality"],
                "agency": u.get("agency"),
                "vendor": u.get("vendor") or u.get("awardee"),
            }
            for u in kept[:20]
        ],
        "false_precision_avoided": True,
        "mixed_totals_as_unit_prices": False,
        "accounts_for_recency": True,
    }
