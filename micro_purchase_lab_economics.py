"""Deterministic micro-purchase reseller economics — Decimal only, no invented prices."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from statistics import median, pstdev
from typing import Any

from micro_purchase_lab_config import (
    CURRENT_MARKET_STALE_DAYS,
    HISTORICAL_MARKET_WINDOW_DAYS,
    classify_opportunity_size,
)

MONEY_Q = Decimal("0.01")
RATIO_Q = Decimal("0.0001")
PCT_Q = Decimal("0.01")

STATUSES = (
    "NOT_TESTED",
    "PRODUCT_IDENTITY_NEEDED",
    "UNIT_NORMALIZATION_REQUIRED",
    "HISTORICAL_DATA_NEEDED",
    "HISTORICAL_COST_NEEDED",
    "CURRENT_MARKET_DATA_NEEDED",
    "SUPPLIER_QUOTE_NEEDED",
    "ECONOMIC_FAIL",
    "ECONOMIC_PASS",
    "EXECUTION_FAIL",
    "BID_CANDIDATE",
)

IDENTITY_OK = {"EXACT", "STRONG"}


def D(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        s = str(value).replace(",", "").replace("$", "").replace("%", "").strip()
        if not s or s.upper() in {"UNKNOWN", "NONE", "NULL", "N/A"}:
            return None
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def money(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


def ratio(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(RATIO_Q, rounding=ROUND_HALF_UP)


def pct(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return value.quantize(PCT_Q, rounding=ROUND_HALF_UP)


def historical_spread(award: Any, market: Any) -> Decimal | None:
    a, m = D(award), D(market)
    if a is None or m is None:
        return None
    return money(a - m)


def historical_markup_on_cost(award: Any, market: Any) -> Decimal | None:
    a, m = D(award), D(market)
    if a is None or m is None or m == 0:
        return None
    return pct(((a / m) - Decimal("1")) * Decimal("100"))


def historical_gross_margin(award: Any, market: Any) -> Decimal | None:
    a, m = D(award), D(market)
    if a is None or m is None or a == 0:
        return None
    return pct(((a - m) / a) * Decimal("100"))


def government_market_ratio(award: Any, market: Any) -> Decimal | None:
    a, m = D(award), D(market)
    if a is None or m is None or m == 0:
        return None
    return ratio(a / m)


def market_change_ratio(current: Any, historical: Any) -> Decimal | None:
    c, h = D(current), D(historical)
    if c is None or h is None or h == 0:
        return None
    return ratio(c / h)


def market_change_pct(current: Any, historical: Any) -> Decimal | None:
    r = market_change_ratio(current, historical)
    if r is None:
        return None
    return pct((r - Decimal("1")) * Decimal("100"))


def historical_equivalent_bid(historical_award: Any, current_market: Any, historical_market: Any) -> Decimal | None:
    """historical_award * (current_market / historical_market)."""
    a, c, h = D(historical_award), D(current_market), D(historical_market)
    if a is None or c is None or h is None or h == 0:
        return None
    return money(a * (c / h))


def normalize_quantity(
    government_quantity: Any,
    *,
    commercial_units_per_gov_unit: Any = None,
) -> dict[str, Any]:
    qty = D(government_quantity)
    factor = D(commercial_units_per_gov_unit)
    if qty is None or qty <= 0:
        return {
            "ok": False,
            "status": "UNIT_NORMALIZATION_REQUIRED",
            "reason": "invalid_or_missing_government_quantity",
            "normalized_commercial_units": None,
        }
    if factor is None:
        return {
            "ok": False,
            "status": "UNIT_NORMALIZATION_REQUIRED",
            "reason": "commercial_units_per_gov_unit_unknown",
            "government_quantity": str(qty),
            "normalized_commercial_units": None,
        }
    if factor <= 0:
        return {
            "ok": False,
            "status": "UNIT_NORMALIZATION_REQUIRED",
            "reason": "invalid_commercial_units_per_gov_unit",
            "normalized_commercial_units": None,
        }
    return {
        "ok": True,
        "status": "OK",
        "government_quantity": str(qty),
        "commercial_units_per_gov_unit": str(factor),
        "normalized_commercial_units": str(qty * factor),
    }


def product_cost(quoted_unit_cost: Any, required_quantity: Any) -> Decimal | None:
    u, q = D(quoted_unit_cost), D(required_quantity)
    if u is None or q is None or q <= 0:
        return None
    if u < 0:
        return None
    return money(u * q)


def landed_cost(
    *,
    quoted_unit_cost: Any,
    required_quantity: Any,
    freight: Any = 0,
    financing: Any = 0,
    other: Any = 0,
) -> dict[str, Any]:
    prod = product_cost(quoted_unit_cost, required_quantity)
    fr = D(freight) if freight not in (None, "") else Decimal("0")
    fin = D(financing) if financing not in (None, "") else Decimal("0")
    oth = D(other) if other not in (None, "") else Decimal("0")
    if prod is None:
        return {
            "product_cost": None,
            "freight": money(fr) if fr is not None else None,
            "financing": money(fin) if fin is not None else None,
            "other": money(oth) if oth is not None else None,
            "total_landed_cost": None,
            "ok": False,
            "reason": "missing_product_cost",
        }
    if fr is None or fin is None or oth is None:
        return {
            "product_cost": prod,
            "freight": None if fr is None else money(fr),
            "financing": None if fin is None else money(fin),
            "other": None if oth is None else money(oth),
            "total_landed_cost": None,
            "ok": False,
            "reason": "missing_cost_component",
        }
    if fr < 0 or fin < 0 or oth < 0:
        return {
            "product_cost": prod,
            "freight": money(fr),
            "financing": money(fin),
            "other": money(oth),
            "total_landed_cost": None,
            "ok": False,
            "reason": "negative_cost_component",
        }
    total = money(prod + fr + fin + oth)
    return {
        "product_cost": prod,
        "freight": money(fr),
        "financing": money(fin),
        "other": money(oth),
        "total_landed_cost": total,
        "ok": True,
    }


def bid_economics(
    *,
    unit_bid: Any,
    quantity: Any,
    total_landed_cost: Any,
) -> dict[str, Any]:
    ub, qty, landed = D(unit_bid), D(quantity), D(total_landed_cost)
    if ub is None or qty is None or qty <= 0 or landed is None:
        return {
            "unit_bid": money(ub) if ub is not None else None,
            "total_revenue": None,
            "total_landed_cost": money(landed) if landed is not None else None,
            "profit_per_unit": None,
            "total_gross_profit": None,
            "gross_margin_pct": None,
            "markup_on_landed_pct": None,
            "ok": False,
        }
    revenue = money(ub * qty)
    profit = money(revenue - landed) if revenue is not None else None
    profit_unit = money(profit / qty) if profit is not None else None
    margin = pct((profit / revenue) * Decimal("100")) if profit is not None and revenue and revenue != 0 else None
    markup = pct((profit / landed) * Decimal("100")) if profit is not None and landed != 0 else None
    return {
        "unit_bid": money(ub),
        "total_revenue": revenue,
        "total_landed_cost": money(landed),
        "profit_per_unit": profit_unit,
        "total_gross_profit": profit,
        "gross_margin_pct": margin,
        "markup_on_landed_pct": markup,
        "ok": True,
    }


def minimum_viable_bid(
    *,
    total_landed_cost: Any,
    quantity: Any,
    min_gross_profit: Any = None,
    min_gross_margin_pct: Any = None,
) -> dict[str, Any]:
    landed, qty = D(total_landed_cost), D(quantity)
    if landed is None or qty is None or qty <= 0:
        return {"unit_bid": None, "total_bid": None, "ok": False, "reason": "missing_landed_or_qty"}
    floors: list[Decimal] = [landed]
    reasons = ["cover_landed_cost"]
    gp = D(min_gross_profit)
    if gp is not None:
        floors.append(landed + gp)
        reasons.append("min_gross_profit")
    gm = D(min_gross_margin_pct)
    if gm is not None:
        # margin = (rev - landed) / rev => rev = landed / (1 - margin)
        if gm >= Decimal("100"):
            return {"unit_bid": None, "total_bid": None, "ok": False, "reason": "invalid_margin_target"}
        denom = Decimal("1") - (gm / Decimal("100"))
        if denom <= 0:
            return {"unit_bid": None, "total_bid": None, "ok": False, "reason": "invalid_margin_target"}
        floors.append(landed / denom)
        reasons.append("min_gross_margin")
    total = money(max(floors))
    unit = money(total / qty) if total is not None else None
    return {
        "unit_bid": unit,
        "total_bid": total,
        "ok": True,
        "constraints_applied": reasons,
    }


def weighted_historical_ratios(
    pairs: list[dict[str, Any]],
    *,
    as_of: date | None = None,
) -> dict[str, Any]:
    """pairs: [{award, market, award_date}] → median/min/max/weighted ratio."""
    as_of = as_of or datetime.now(timezone.utc).date()
    ratios: list[Decimal] = []
    weights: list[Decimal] = []
    usable = 0
    for p in pairs or []:
        r = government_market_ratio(p.get("award") or p.get("award_unit_price"), p.get("market") or p.get("market_unit_cost"))
        if r is None:
            continue
        ad = _parse_date(p.get("award_date") or p.get("date"))
        w = Decimal("1")
        if ad is not None:
            age_months = (as_of.year - ad.year) * 12 + (as_of.month - ad.month)
            if age_months <= 12:
                w = Decimal("3")
            elif age_months <= 24:
                w = Decimal("2")
            else:
                w = Decimal("1")
        ratios.append(r)
        weights.append(w)
        usable += 1
    if not ratios:
        return {
            "observation_count": 0,
            "median_ratio": None,
            "min_ratio": None,
            "max_ratio": None,
            "recency_weighted_ratio": None,
            "std_dev": None,
        }
    wsum = sum(weights)
    weighted = sum(r * w for r, w in zip(ratios, weights)) / wsum if wsum else None
    med = Decimal(str(median([float(x) for x in ratios])))
    std = None
    if len(ratios) >= 2:
        std = ratio(Decimal(str(pstdev([float(x) for x in ratios]))))
    return {
        "observation_count": usable,
        "median_ratio": ratio(med),
        "min_ratio": ratio(min(ratios)),
        "max_ratio": ratio(max(ratios)),
        "recency_weighted_ratio": ratio(weighted) if weighted is not None else None,
        "std_dev": std,
    }


def historical_market_in_window(
    award_date: Any,
    evidence_date: Any,
    *,
    window_days: int = HISTORICAL_MARKET_WINDOW_DAYS,
) -> dict[str, Any]:
    ad, ed = _parse_date(award_date), _parse_date(evidence_date)
    if ad is None or ed is None:
        return {"in_window": False, "days_delta": None, "confidence_hint": "UNKNOWN"}
    delta = abs((ed - ad).days)
    return {
        "in_window": delta <= window_days,
        "days_delta": delta,
        "confidence_hint": "STRONG" if delta <= window_days else "WEAK",
    }


def is_stale_market_observation(obs_date: Any, *, stale_days: int = CURRENT_MARKET_STALE_DAYS) -> bool:
    d = _parse_date(obs_date)
    if d is None:
        return True
    return (datetime.now(timezone.utc).date() - d).days > stale_days


def is_quote_expired(expiration: Any, *, as_of: date | None = None) -> bool:
    d = _parse_date(expiration)
    if d is None:
        return False
    today = as_of or datetime.now(timezone.utc).date()
    return d < today


def execution_flags(quote: dict[str, Any] | None) -> dict[str, Any]:
    q = quote or {}
    terms = str(q.get("payment_terms") or "").upper()
    prepaid = bool(q.get("requires_prepayment")) or terms in {"PREPAID", "CREDIT_CARD"} and bool(
        q.get("requires_full_prepayment", True) if terms == "PREPAID" else q.get("requires_full_prepayment")
    )
    if terms == "PREPAID":
        prepaid = True
    if q.get("requires_full_prepayment") is True:
        prepaid = True
    pg = bool(q.get("personal_guarantee_required") or q.get("requires_personal_guarantee"))
    personal_credit = bool(q.get("personal_credit_required") or q.get("requires_personal_credit"))
    fail = prepaid or pg or personal_credit
    reasons = []
    if prepaid:
        reasons.append("cash_upfront_or_prepayment")
    if pg:
        reasons.append("personal_guarantee_required")
    if personal_credit:
        reasons.append("personal_credit_required")
    return {
        "execution_fail": fail,
        "reasons": reasons,
        "requires_prepayment": prepaid,
        "personal_guarantee_required": pg,
        "personal_credit_required": personal_credit,
    }


def derive_status(test: dict[str, Any]) -> str:
    identity = str(test.get("identity_confidence") or "UNKNOWN").upper()
    if identity not in IDENTITY_OK and identity != "POSSIBLE":
        if not (test.get("manufacturer") and (test.get("part_number") or test.get("nsn"))):
            return "PRODUCT_IDENTITY_NEEDED"
    if identity == "UNKNOWN" and not (test.get("part_number") or test.get("nsn")):
        return "PRODUCT_IDENTITY_NEEDED"

    norm = test.get("unit_normalization") or {}
    if not norm.get("ok"):
        # Default EA↔EA factor 1 is allowed when operator affirms
        if str(test.get("government_unit") or "EA").upper() == "EA" and D(test.get("commercial_units_per_gov_unit")) == Decimal("1"):
            pass
        elif D(test.get("commercial_units_per_gov_unit")) is None and str(test.get("government_unit") or "EA").upper() != "EA":
            return "UNIT_NORMALIZATION_REQUIRED"
        elif not norm.get("ok") and D(test.get("commercial_units_per_gov_unit")) is None:
            if str(test.get("government_unit") or "").upper() not in {"", "EA", "EACH"}:
                return "UNIT_NORMALIZATION_REQUIRED"

    awards = test.get("historical_awards") or []
    if not awards:
        return "HISTORICAL_DATA_NEEDED"
    hist_costs = test.get("historical_market_costs") or []
    if not hist_costs:
        return "HISTORICAL_COST_NEEDED"
    current = test.get("current_market_prices") or []
    if not current:
        return "CURRENT_MARKET_DATA_NEEDED"
    quotes = [q for q in (test.get("supplier_quotes") or []) if not q.get("archived")]
    if not quotes:
        return "SUPPLIER_QUOTE_NEEDED"

    best = _best_usable_quote(quotes)
    if not best:
        return "SUPPLIER_QUOTE_NEEDED"
    if is_quote_expired(best.get("quote_expiration")):
        return "SUPPLIER_QUOTE_NEEDED"

    flags = execution_flags(best)
    if flags["execution_fail"]:
        return "EXECUTION_FAIL"

    snap = test.get("economics_snapshot") or {}
    if not snap.get("ok"):
        return "SUPPLIER_QUOTE_NEEDED"
    profit = D(snap.get("estimated_gross_profit"))
    margin = D(snap.get("estimated_gross_margin_pct"))
    min_profit = D(test.get("min_gross_profit_target")) or Decimal("0")
    min_margin = D(test.get("min_gross_margin_pct_target"))
    if profit is None:
        return "ECONOMIC_FAIL"
    if profit < min_profit:
        return "ECONOMIC_FAIL"
    if min_margin is not None and (margin is None or margin < min_margin):
        return "ECONOMIC_FAIL"

    # BID_CANDIDATE gate
    if identity not in IDENTITY_OK:
        return "ECONOMIC_PASS"
    if flags["execution_fail"]:
        return "EXECUTION_FAIL"
    live = str(test.get("opportunity_status") or "OPEN").upper() not in {"EXPIRED", "CANCELLED", "CLOSED", "AWARDED"}
    if not live:
        return "ECONOMIC_PASS"
    if not (test.get("part_number") or test.get("nsn")):
        return "ECONOMIC_PASS"
    return "BID_CANDIDATE"


def compute_pricing_summary(test: dict[str, Any]) -> dict[str, Any]:
    awards = test.get("historical_awards") or []
    hist_markets = test.get("historical_market_costs") or []
    current_markets = test.get("current_market_prices") or []
    quotes = test.get("supplier_quotes") or []

    last_gov = _pick_latest_unit_price(awards, date_key="award_date", price_key="unit_price")
    hist_market = _pick_latest_unit_price(hist_markets, date_key="evidence_date", price_key="unit_price")
    current_market = _pick_reference_market(current_markets)
    ratio_gm = government_market_ratio(last_gov, hist_market) if last_gov and hist_market else None
    change_pct = market_change_pct(current_market, hist_market) if current_market and hist_market else None
    equiv = historical_equivalent_bid(last_gov, current_market, hist_market)

    qty = D(test.get("normalized_quantity")) or D(test.get("quantity"))
    factor = D(test.get("commercial_units_per_gov_unit"))
    if factor is None and str(test.get("government_unit") or "EA").upper() in {"", "EA", "EACH"}:
        factor = Decimal("1")
    norm = normalize_quantity(test.get("quantity"), commercial_units_per_gov_unit=factor)
    req_qty = D(norm.get("normalized_commercial_units")) or qty

    best_quote = _best_usable_quote(quotes)
    unit_cost = D((best_quote or {}).get("quoted_unit_cost"))
    freight = D((best_quote or {}).get("freight")) or D(test.get("freight")) or Decimal("0")
    financing = D(test.get("financing_cost")) or Decimal("0")
    other = D(test.get("other_execution_costs")) or Decimal("0")
    landed = landed_cost(
        quoted_unit_cost=unit_cost,
        required_quantity=req_qty,
        freight=freight,
        financing=financing,
        other=other,
    )

    mvb = minimum_viable_bid(
        total_landed_cost=landed.get("total_landed_cost"),
        quantity=req_qty,
        min_gross_profit=test.get("min_gross_profit_target"),
        min_gross_margin_pct=test.get("min_gross_margin_pct_target"),
    )

    candidate_unit = D(test.get("candidate_bid_unit")) or mvb.get("unit_bid") or equiv
    econ = bid_economics(
        unit_bid=candidate_unit,
        quantity=req_qty,
        total_landed_cost=landed.get("total_landed_cost"),
    )

    pairs = []
    for a in awards:
        # match nearest market by date if present
        ad = a.get("award_date")
        m_price = None
        for m in hist_markets:
            win = historical_market_in_window(ad, m.get("evidence_date"))
            if win.get("in_window"):
                m_price = m.get("unit_price")
                break
        if m_price is None and hist_markets:
            m_price = hist_markets[0].get("unit_price")
        if a.get("unit_price") is not None and m_price is not None:
            pairs.append({"award": a.get("unit_price"), "market": m_price, "award_date": ad})

    weighted = weighted_historical_ratios(pairs)
    flags = execution_flags(best_quote)

    range_low = mvb.get("unit_bid")
    range_high = equiv
    snapshot = {
        "ok": bool(landed.get("ok") and econ.get("ok") and unit_cost is not None),
        "last_government_price": _s(last_gov),
        "historical_market_cost": _s(hist_market),
        "historical_government_market_ratio": _s(ratio_gm),
        "current_market_price": _s(current_market),
        "market_price_change_pct": _s(change_pct),
        "historical_equivalent_bid": _s(equiv),
        "our_best_supplier_quote_unit": _s(unit_cost),
        "landed_cost": _s(landed.get("total_landed_cost")),
        "product_cost": _s(landed.get("product_cost")),
        "freight": _s(landed.get("freight")),
        "financing": _s(landed.get("financing")),
        "other_costs": _s(landed.get("other")),
        "minimum_viable_bid_unit": _s(mvb.get("unit_bid")),
        "minimum_viable_bid_total": _s(mvb.get("total_bid")),
        "estimated_gross_profit": _s(econ.get("total_gross_profit")),
        "estimated_gross_margin_pct": _s(econ.get("gross_margin_pct")),
        "estimated_markup_pct": _s(econ.get("markup_on_landed_pct")),
        "economic_bid_range": {
            "low_unit": _s(range_low),
            "high_unit": _s(range_high),
            "label": "HISTORICAL-EQUIVALENT BID ESTIMATE is not a guaranteed winning price",
        },
        "historical_spread": _s(historical_spread(last_gov, hist_market)),
        "historical_markup_pct": _s(historical_markup_on_cost(last_gov, hist_market)),
        "historical_gross_margin_pct": _s(historical_gross_margin(last_gov, hist_market)),
        "weighted_historical_ratios": {k: _s(v) if isinstance(v, Decimal) else v for k, v in weighted.items()},
        "unit_normalization": norm,
        "execution": flags,
        "bid_economics": {k: _s(v) if isinstance(v, Decimal) else v for k, v in econ.items()},
        "label_historical_equivalent": "HISTORICAL-EQUIVALENT BID ESTIMATE",
    }
    return snapshot


def enrich_test(test: dict[str, Any]) -> dict[str, Any]:
    row = dict(test)
    if not row.get("classification") or row.get("classification") == "UNKNOWN":
        row["classification"] = classify_opportunity_size(row.get("estimated_opportunity_size"))
    snap = compute_pricing_summary(row)
    row["economics_snapshot"] = snap
    row["unit_normalization"] = snap.get("unit_normalization") or {}
    row["status"] = derive_status(row)
    row["pricing_summary"] = {
        "Last Government Price": snap.get("last_government_price"),
        "Historical Market Cost": snap.get("historical_market_cost"),
        "Historical Government / Market Ratio": snap.get("historical_government_market_ratio"),
        "Current Market Price": snap.get("current_market_price"),
        "Market Price Change": snap.get("market_price_change_pct"),
        "Historical-Equivalent Bid": snap.get("historical_equivalent_bid"),
        "Our Best Actual Supplier Quote": snap.get("our_best_supplier_quote_unit"),
        "Landed Cost": snap.get("landed_cost"),
        "Minimum Viable Bid": snap.get("minimum_viable_bid_unit"),
        "Estimated Gross Profit": snap.get("estimated_gross_profit"),
        "Estimated Gross Margin": snap.get("estimated_gross_margin_pct"),
        "Estimated Markup": snap.get("estimated_markup_pct"),
    }
    return row


def build_quote_request_text(test: dict[str, Any]) -> str:
    return (
        "We are evaluating a government procurement requirement for:\n\n"
        f"Manufacturer: {test.get('manufacturer') or '[manufacturer]'}\n"
        f"Part Number: {test.get('part_number') or '[part]'}\n"
        f"NSN: {test.get('nsn') or 'N/A'}\n"
        f"Quantity: {test.get('quantity') or '[quantity]'} {test.get('government_unit') or ''}\n"
        f"Delivery Destination: {test.get('delivery_location') or '[location]'}\n"
        f"Required Delivery / Response Date: {test.get('deadline') or '[date]'}\n\n"
        "Please provide your best reseller/government-project pricing, including:\n"
        "- unit price;\n"
        "- extended price;\n"
        "- freight;\n"
        "- stock status;\n"
        "- lead time;\n"
        "- quote expiration;\n"
        "- payment terms;\n"
        "- direct-ship availability;\n"
        "- whether special government/project pricing is available;\n"
        "- whether deal registration or special-bid pricing applies.\n\n"
        "Note: This inquiry does not claim dealer authorization, government contract status, "
        "or past performance beyond what has been independently verified."
    )


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _s(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _pick_latest_unit_price(rows: list[dict[str, Any]], *, date_key: str, price_key: str) -> Decimal | None:
    best = None
    best_d = None
    for r in rows or []:
        p = D(r.get(price_key))
        if p is None:
            continue
        d = _parse_date(r.get(date_key))
        if best is None or (d and (best_d is None or d > best_d)):
            best = p
            best_d = d
    return best


def _pick_reference_market(rows: list[dict[str, Any]]) -> Decimal | None:
    usable = []
    for r in rows or []:
        p = D(r.get("unit_price") or r.get("price"))
        if p is None:
            continue
        if is_stale_market_observation(r.get("date") or r.get("evidence_date")):
            continue
        cond = str(r.get("condition") or "UNKNOWN").upper()
        if cond in {"USED", "REFURBISHED", "SURPLUS_NEW", "NEW_AFTERMARKET"}:
            # keep but deprioritize — still usable if only option
            usable.append((1, p))
        else:
            usable.append((0, p))
    if not usable:
        # fall back to any price including stale
        for r in rows or []:
            p = D(r.get("unit_price") or r.get("price"))
            if p is not None:
                return p
        return None
    usable.sort(key=lambda x: (x[0], x[1]))
    return usable[0][1]


def _best_usable_quote(quotes: list[dict[str, Any]]) -> dict[str, Any] | None:
    best = None
    best_cost = None
    for q in quotes or []:
        if q.get("archived"):
            continue
        if is_quote_expired(q.get("quote_expiration")):
            continue
        cost = D(q.get("quoted_unit_cost"))
        if cost is None or cost < 0:
            continue
        if best is None or (best_cost is not None and cost < best_cost):
            best = q
            best_cost = cost
    return best
