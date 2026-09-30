"""Phase L.6 — quote-required economics: gov value → max-buy → quote targets."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from application_clock import now_utc
from phase_l.acquisition_lanes import (
    MILSPEC_SPECIALTY,
    QUOTE_REQUIRED_COMMERCIAL,
    UNKNOWN_ACQUISITION_CHANNEL,
    calculate_maximum_buy_price,
    classify_acquisition_lane,
    generate_supplier_candidates,
    prepare_quote_packet_l3,
)

ROOT = Path(__file__).resolve().parents[1]
BUYER_VALUE_PATH = ROOT / "data" / "phase_l6_buyer_value_memory.json"
SUPPLIER_MEMORY_PATH = ROOT / "data" / "phase_l6_supplier_memory.json"
BUILD = "20260927-m3-phase-l6-quote-required-economics"

GOV_VALUE_EXACT = "GOV_VALUE_EXACT"
GOV_VALUE_STRONG = "GOV_VALUE_STRONG"
GOV_VALUE_COMPARABLE = "GOV_VALUE_COMPARABLE"
GOV_VALUE_RANGE = "GOV_VALUE_RANGE"
GOV_VALUE_UNKNOWN = "GOV_VALUE_UNKNOWN"

FREIGHT_LOW_CONFIDENCE_RESERVE = "FREIGHT_LOW_CONFIDENCE_RESERVE"
FREIGHT_ESTIMATE = "FREIGHT_ESTIMATE"
FREIGHT_QUOTE_REQUIRED = "FREIGHT_QUOTE_REQUIRED"

EXCELLENT_QUOTE = "EXCELLENT_QUOTE"
ACCEPTABLE_QUOTE = "ACCEPTABLE_QUOTE"
MARGINAL_QUOTE = "MARGINAL_QUOTE"
FAIL_QUOTE = "FAIL_QUOTE"

QUOTE_DEPENDENT_POSITIVE = "QUOTE_DEPENDENT_POSITIVE"
APPARENTLY_WITHIN_QUOTE_TARGET = "APPARENTLY_WITHIN_QUOTE_TARGET"
PUBLIC_PRICE_WITHIN_TARGET = "PUBLIC_PRICE_WITHIN_TARGET"
UOM_UNRESOLVED = "UOM_UNRESOLVED"
BUYER_PRICE_HISTORY_AVAILABLE = "BUYER_PRICE_HISTORY_AVAILABLE"
RECURRING_QUOTE_TARGET = "RECURRING_QUOTE_TARGET"
DEFAULT_FINANCING_RATE = 0.05


def _utc() -> str:
    return now_utc().isoformat()


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load_json(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


def _product_memory_key(row: dict[str, Any], commercial: dict[str, Any] | None = None) -> str | None:
    commercial = commercial or {}
    for k in (
        commercial.get("model"),
        commercial.get("mpn"),
        commercial.get("manufacturer"),
        row.get("nsn"),
        row.get("part_number"),
    ):
        s = str(k or "").strip().upper()
        if s and len(s) >= 3:
            return s[:80]
    title = str(row.get("title") or "").strip().upper()
    if len(title) >= 12:
        return title[:60]
    return None


def commercial_government_benchmark(
    row: dict[str, Any], *, commercial: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Tier-C / range government-side benchmarks for common commercial families.

    Reconnaissance only — never EXACT. Used so quote-required commercial rows
    can still produce max-buy ceilings before a supplier quote exists.
    """
    commercial = commercial or {}
    title = str(row.get("title") or "").lower()
    model = str(commercial.get("model") or "").lower()
    blob = f"{title} {model} {commercial.get('manufacturer') or ''}".lower()
    st = str(commercial.get("commercial_identity_state") or "")

    # (lo, mid, hi, label) — municipal/state award-ish ranges, not retail
    rules: list[tuple[str, float, float, float, str]] = [
        (r"f[\-\s]?150\s+police|police\s+responder", 48000, 58000, 72000, "police_pickup_award_band"),
        (r"expedition\s+ssv|police\s+suv|interceptor\s+utility|piu\s+explorer", 45000, 55000, 70000, "police_suv_award_band"),
        (r"\bf[\-\s]?150\b|\bf[\-\s]?250\b|\bf[\-\s]?350\b|fleet\s+truck|pickup\s+truck", 38000, 48000, 62000, "light_truck_award_band"),
        (r"toolcat|uw56|bobcat\s+l\d+|skid[\-\s]?steer|articulated\s+loader", 48000, 65000, 90000, "compact_equipment_award_band"),
        (r"forklift|warehouse\s+fork", 25000, 40000, 65000, "forklift_award_band"),
        (r"shuttle\s+bus|passenger\s+van|14[\-\s]?passenger", 55000, 75000, 110000, "shuttle_van_award_band"),
        (r"cisco\s+(catalyst|switch)|networking\s+switch", 8000, 18000, 45000, "network_switch_award_band"),
        (r"trailer|deckover|tilt\s+trailer|heavy\s+haul", 12000, 28000, 55000, "trailer_award_band"),
        (r"rugged\s+laptop|panasonic\s+cf|toughbook", 2500, 4500, 8000, "rugged_laptop_award_band"),
    ]
    for pat, lo, mid, hi, label in rules:
        if re.search(pat, blob, re.I):
            return {
                "tier": "C",
                "state": GOV_VALUE_RANGE,
                "source": f"GOVERNMENT_CATEGORY_BENCHMARK_RECON:{label}",
                "unit_value": mid,
                "unit_value_low": lo,
                "unit_value_high": hi,
                "confidence": "APPROXIMATE",
                "final_award_value": False,
            }
    if "VEHICLE" in st or re.search(r"\b(vehicle|suv|truck|automobile)\b", blob, re.I):
        return {
            "tier": "C",
            "state": GOV_VALUE_COMPARABLE,
            "source": "GOVERNMENT_CATEGORY_BENCHMARK_RECON:generic_vehicle",
            "unit_value": 45000.0,
            "unit_value_low": 30000.0,
            "unit_value_high": 65000.0,
            "confidence": "APPROXIMATE",
            "final_award_value": False,
        }
    return None


def assess_government_value(
    row: dict[str, Any],
    *,
    history: dict[str, Any] | None = None,
    stage3: dict[str, Any] | None = None,
    buyer_memory: dict[str, Any] | None = None,
    commercial: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tiered government-side value — never fabricate award values."""
    history = history or {}
    stage3 = stage3 or {}
    commercial = commercial or {}
    evidence: list[dict[str, Any]] = []

    hist_u = _f(history.get("historical_award_unit_price") or row.get("historical_award_unit_price"))
    hist_match = str(history.get("identity_match_type") or row.get("identity_match_type") or "").upper()
    # Reject agency-wide / weak joins as EXACT
    if hist_u and hist_u > 0 and hist_match not in {"AGENCY", "BUYER_ONLY", "CATEGORY", "WEAK"}:
        state = GOV_VALUE_EXACT if hist_match in {"EXACT", "NSN", "MPN", "MODEL", ""} or not hist_match else GOV_VALUE_STRONG
        if hist_match in {"FAMILY", "COMPARABLE", "NEAR"}:
            state = GOV_VALUE_COMPARABLE
        evidence.append(
            {
                "tier": "A" if state == GOV_VALUE_EXACT else "B" if state == GOV_VALUE_STRONG else "C",
                "state": state,
                "source": "historical_award_unit_price",
                "unit_value": hist_u,
                "confidence": "HIGH" if state == GOV_VALUE_EXACT else "MEDIUM",
                "identity_match_type": hist_match or None,
            }
        )

    for k, state, tier in (
        ("not_to_exceed", GOV_VALUE_EXACT, "A"),
        ("budget", GOV_VALUE_STRONG, "B"),
        ("estimated_value", GOV_VALUE_STRONG, "B"),
        ("ceiling", GOV_VALUE_STRONG, "B"),
        ("total_value", GOV_VALUE_STRONG, "B"),
        ("award_amount", GOV_VALUE_STRONG, "B"),
    ):
        v = _f(row.get(k))
        if v and v > 0:
            evidence.append(
                {
                    "tier": tier,
                    "state": state,
                    "source": k,
                    "total_value": v,
                    "confidence": "HIGH" if tier == "A" else "MEDIUM",
                }
            )

    hr = stage3.get("historical_range") or {}
    lo, hi = _f(hr.get("low")), _f(hr.get("high"))
    if lo and hi and lo > 0:
        conf = str(hr.get("confidence") or "")
        evidence.append(
            {
                "tier": "B" if conf in {"EXACT", "STRONG"} else "C",
                "state": GOV_VALUE_RANGE if lo != hi else GOV_VALUE_STRONG,
                "source": "stage3_historical_range",
                "unit_value_low": lo,
                "unit_value_high": hi,
                "unit_value": (lo + hi) / 2.0,
                "confidence": conf or "APPROXIMATE",
            }
        )

    buyer = str(row.get("agency") or row.get("buyer") or "").strip()
    pkey = _product_memory_key(row, commercial)
    if buyer_memory and buyer and pkey:
        rec = (buyer_memory.get("buyers") or {}).get(buyer.upper()) or {}
        by_product = rec.get("by_product") or {}
        # Exact product key only — never agency-wide median (contaminates DoD/etc.)
        prod = by_product.get(pkey) or {}
        med = _f(prod.get("median_unit_price"))
        if med and med > 0 and len(prod.get("samples") or []) >= 1:
            evidence.append(
                {
                    "tier": "B",
                    "state": GOV_VALUE_STRONG,
                    "source": BUYER_PRICE_HISTORY_AVAILABLE,
                    "unit_value": med,
                    "unit_value_low": _f(prod.get("min_unit_price")) or med * 0.9,
                    "unit_value_high": _f(prod.get("max_unit_price")) or med * 1.1,
                    "confidence": "MEDIUM",
                    "product_key": pkey,
                }
            )

    # Tier C category benchmarks when stronger evidence absent
    if not any(e.get("tier") in {"A", "B"} for e in evidence):
        bench = commercial_government_benchmark(row, commercial=commercial)
        if bench:
            evidence.append(bench)

    if not evidence:
        return {
            "state": GOV_VALUE_UNKNOWN,
            "tier": None,
            "unit_value": None,
            "unit_low": None,
            "unit_high": None,
            "total_value": None,
            "quantity": _f(row.get("quantity")),
            "evidence": [],
            "best": None,
        }

    best = None
    for pref in (GOV_VALUE_EXACT, GOV_VALUE_STRONG, GOV_VALUE_RANGE, GOV_VALUE_COMPARABLE):
        for e in evidence:
            if e.get("state") == pref and (e.get("unit_value") or e.get("total_value")):
                best = e
                break
        if best:
            break
    best = best or evidence[0]

    unit = _f(best.get("unit_value"))
    total = _f(best.get("total_value"))
    qty = _f(row.get("quantity")) or _f(stage3.get("quantity"))
    if unit is None and total and qty and qty > 0:
        unit = total / qty

    return {
        "state": best.get("state") or GOV_VALUE_UNKNOWN,
        "tier": best.get("tier"),
        "unit_value": unit,
        "unit_low": _f(best.get("unit_value_low")) or unit,
        "unit_high": _f(best.get("unit_value_high")) or unit,
        "total_value": total,
        "quantity": qty,
        "evidence": evidence,
        "best": best,
        "source": best.get("source"),
        "confidence": best.get("confidence"),
        "final_award_value": best.get("final_award_value", best.get("tier") in {"A", "B"}),
    }


def expected_revenue(gov: dict[str, Any], *, quantity: float | None = None) -> dict[str, Any]:
    qty = _f(quantity) or _f(gov.get("quantity")) or 1.0
    unit = _f(gov.get("unit_value"))
    lo = _f(gov.get("unit_low")) or unit
    hi = _f(gov.get("unit_high")) or unit
    total = _f(gov.get("total_value"))

    if unit is None and total is not None:
        return {
            "basis": "TOTAL",
            "ExpectedRevenueLow": round(total * 0.9, 2) if gov.get("state") == GOV_VALUE_RANGE else total,
            "ExpectedRevenueMid": total,
            "ExpectedRevenueHigh": round(total * 1.1, 2) if gov.get("state") == GOV_VALUE_RANGE else total,
            "quantity": None,
            "quality": gov.get("state"),
        }
    if unit is None:
        return {
            "basis": "UNKNOWN",
            "ExpectedRevenueLow": None,
            "ExpectedRevenueMid": None,
            "ExpectedRevenueHigh": None,
            "quantity": qty,
            "quality": GOV_VALUE_UNKNOWN,
        }
    return {
        "basis": "UNIT",
        "ExpectedRevenueLow": round((lo or unit) * qty, 2),
        "ExpectedRevenueMid": round(unit * qty, 2),
        "ExpectedRevenueHigh": round((hi or unit) * qty, 2),
        "quantity": qty,
        "unit_low": lo,
        "unit_mid": unit,
        "unit_high": hi,
        "quality": gov.get("state"),
    }


def freight_reserve_for_row(row: dict[str, Any]) -> dict[str, Any]:
    title = (row.get("title") or "").lower()
    dest = str(row.get("place_of_performance") or row.get("delivery_state") or row.get("state") or "")
    if re.search(r"\b(alaska|hawaii|ak\b|hi\b)\b", dest + " " + title, re.I):
        return {"amount": 5000.0, "status": FREIGHT_QUOTE_REQUIRED, "reason": "alaska_hawaii"}
    if re.search(
        r"\b(vehicle|truck|pickup|suv|trailer|excavator|loader|forklift|tractor|bobcat|toolcat|"
        r"f[\-\s]?150|f[\-\s]?250|f[\-\s]?350|police\s+responder|expedition)\b",
        title,
        re.I,
    ):
        return {"amount": 2000.0, "status": FREIGHT_LOW_CONFIDENCE_RESERVE, "reason": "vehicle_equipment"}
    if re.search(r"\b(server|laptop|monitor|printer|furniture)\b", title, re.I):
        return {"amount": 250.0, "status": FREIGHT_ESTIMATE, "reason": "standard_parcel"}
    return {"amount": 500.0, "status": FREIGHT_LOW_CONFIDENCE_RESERVE, "reason": "default_reserve"}


def calculate_max_buy_engine(
    *,
    government_unit: float | None = None,
    government_total: float | None = None,
    quantity: float | None = 1.0,
    financing_rate: float = DEFAULT_FINANCING_RATE,
    freight_reserve: float | None = None,
    other_fees: float = 0.0,
    risk_reserve_rate: float = 0.0,
    unit_low: float | None = None,
    unit_high: float | None = None,
) -> dict[str, Any] | None:
    """Max-buy thresholds including margin ceilings. Financing not double-counted."""
    qty = _f(quantity) or 1.0
    gov_unit = _f(government_unit)
    gov_total = _f(government_total)
    freight = float(freight_reserve or 0.0)
    fees = float(other_fees or 0.0)
    rate = float(financing_rate or 0.0)
    denom = 1.0 + rate

    def _thresholds_from_revenue(revenue: float) -> dict[str, float | None]:
        risk = revenue * float(risk_reserve_rate or 0.0)
        out: dict[str, float | None] = {}
        for name, profit in (
            ("BREAK_EVEN_MAX_BUY", 0.0),
            ("MAX_BUY_FOR_5K_PROFIT", 5000.0),
            ("MAX_BUY_FOR_10K_PROFIT", 10000.0),
            ("MAX_BUY_FOR_25K_PROFIT", 25000.0),
        ):
            numer = revenue - freight - fees - risk - profit
            val = numer / denom if denom > 0 else None
            out[name] = round(val, 2) if val and val > 0 else None
        for name, margin in (
            ("MAX_BUY_FOR_15_PERCENT_MARGIN", 0.15),
            ("MAX_BUY_FOR_20_PERCENT_MARGIN", 0.20),
            ("MAX_BUY_FOR_25_PERCENT_MARGIN", 0.25),
        ):
            numer = revenue * (1.0 - margin) - freight - fees - risk
            val = numer / denom if denom > 0 else None
            out[name] = round(val, 2) if val and val > 0 else None
        return out

    if gov_unit is None and gov_total is not None and gov_total > 0:
        th = _thresholds_from_revenue(gov_total)
        target = th.get("MAX_BUY_FOR_10K_PROFIT") or th.get("BREAK_EVEN_MAX_BUY")
        return {
            "kind": "MAXIMUM_BUY_PRICE",
            "basis": "TOTAL",
            "government_total": gov_total,
            "quantity": None,
            "financing_rate": rate,
            "freight_reserve": freight,
            "other_fees": fees,
            "risk_reserve_rate": risk_reserve_rate,
            "thresholds": th,
            "maximum_acquisition_total": target,
            "maximum_acquisition_unit": None,
            "supplier_quote_target": target,
            "label": f"SUPPLIER_QUOTE_REQUIRED — TARGET ≤ ${target:,.0f}" if target else None,
            "ranges": None,
        }

    if gov_unit is None or gov_unit <= 0:
        return None

    # Also keep legacy unit ceilings for compatibility
    legacy = calculate_maximum_buy_price(
        government_unit=gov_unit,
        quantity=qty,
        financing_rate=rate,
        freight_reserve=freight,
        other_fees=fees,
        desired_profits=(0.0, 5000.0, 10000.0, 25000.0),
    )

    def _unit_thresholds(unit: float) -> dict[str, float | None]:
        th = _thresholds_from_revenue(unit * qty)
        # Convert total ceilings back to unit
        return {k: (round(v / qty, 2) if v is not None else None) for k, v in th.items()}

    mid_th = _unit_thresholds(gov_unit)
    cons_th = _unit_thresholds(unit_low) if unit_low else mid_th
    up_th = _unit_thresholds(unit_high) if unit_high else mid_th
    target = mid_th.get("MAX_BUY_FOR_10K_PROFIT") or mid_th.get("BREAK_EVEN_MAX_BUY")
    return {
        "kind": "MAXIMUM_BUY_PRICE",
        "basis": "UNIT",
        "government_unit": gov_unit,
        "quantity": qty,
        "financing_rate": rate,
        "freight_reserve": freight,
        "other_fees": fees,
        "risk_reserve_rate": risk_reserve_rate,
        "thresholds": mid_th,
        "maximum_acquisition_unit": target,
        "maximum_acquisition_total": round(target * qty, 2) if target else None,
        "supplier_quote_target": target,
        "label": f"SUPPLIER_QUOTE_REQUIRED — TARGET ≤ ${target:,.0f}/unit" if target else None,
        "ranges": {
            "conservative_max_buy_unit": cons_th.get("MAX_BUY_FOR_10K_PROFIT") or cons_th.get("BREAK_EVEN_MAX_BUY"),
            "midpoint_max_buy_unit": target,
            "upside_max_buy_unit": up_th.get("MAX_BUY_FOR_10K_PROFIT") or up_th.get("BREAK_EVEN_MAX_BUY"),
        },
        "unit_ceilings": (legacy or {}).get("unit_ceilings"),
    }


def quote_bands(max_buy: dict[str, Any] | None) -> dict[str, Any] | None:
    if not max_buy:
        return None
    th = max_buy.get("thresholds") or {}
    target = _f(max_buy.get("supplier_quote_target") or max_buy.get("maximum_acquisition_unit"))
    be = _f(th.get("BREAK_EVEN_MAX_BUY"))
    p5 = _f(th.get("MAX_BUY_FOR_5K_PROFIT"))
    p25 = _f(th.get("MAX_BUY_FOR_25K_PROFIT"))
    if not target or target <= 0:
        return None
    excellent_ceil = p25 if p25 and p25 > 0 else round(target * 0.85, 2)
    acceptable_ceil = target
    marginal_ceil = p5 if p5 and p5 > target else (be if be and be > target else round(target * 1.08, 2))
    if marginal_ceil <= acceptable_ceil:
        marginal_ceil = round(acceptable_ceil * 1.08, 2)
    return {
        EXCELLENT_QUOTE: {"max": excellent_ceil},
        ACCEPTABLE_QUOTE: {"min": excellent_ceil, "max": acceptable_ceil},
        MARGINAL_QUOTE: {"min": acceptable_ceil, "max": marginal_ceil},
        FAIL_QUOTE: {"min": marginal_ceil},
    }


def classify_quote_band(price: float | None, bands: dict[str, Any] | None) -> str | None:
    p = _f(price)
    if p is None or not bands:
        return None
    if p <= float((bands.get(EXCELLENT_QUOTE) or {}).get("max") or -1):
        return EXCELLENT_QUOTE
    if p <= float((bands.get(ACCEPTABLE_QUOTE) or {}).get("max") or -1):
        return ACCEPTABLE_QUOTE
    if p <= float((bands.get(MARGINAL_QUOTE) or {}).get("max") or -1):
        return MARGINAL_QUOTE
    return FAIL_QUOTE


def quote_outcome_simulator(
    max_buy: dict[str, Any] | None,
    *,
    revenue_mid: float | None,
    financing_rate: float = DEFAULT_FINANCING_RATE,
    freight: float = 0.0,
    other_fees: float = 0.0,
    quantity: float = 1.0,
) -> list[dict[str, Any]]:
    if not max_buy:
        return []
    target = _f(max_buy.get("supplier_quote_target") or max_buy.get("maximum_acquisition_unit"))
    if not target or target <= 0:
        return []
    qty = float(quantity or 1.0)
    rev = _f(revenue_mid)
    if rev is None and max_buy.get("government_unit"):
        rev = float(max_buy["government_unit"]) * qty
    if rev is None and max_buy.get("government_total"):
        rev = float(max_buy["government_total"])
    if rev is None:
        return []

    out = []
    for frac in (0.60, 0.75, 0.90, 1.00, 1.10):
        if max_buy.get("basis") == "TOTAL":
            acq_total = target * frac
            acq_unit = None
        else:
            acq_unit = target * frac
            acq_total = acq_unit * qty
        financing = acq_total * financing_rate
        net = rev - acq_total - financing - freight - other_fees
        margin = (net / rev) if rev else None
        out.append(
            {
                "quote_fraction_of_target": frac,
                "acquisition_unit": round(acq_unit, 2) if acq_unit is not None else None,
                "acquisition_total": round(acq_total, 2),
                "financing_cost": round(financing, 2),
                "freight": freight,
                "other_fees": other_fees,
                "gross_spread": round(rev - acq_total, 2),
                "estimated_net": round(net, 2),
                "margin": round(margin, 4) if margin is not None else None,
                "go_no_go": "GO" if net >= 5000 else "NO_GO",
            }
        )
    return out


def acquisition_headroom(*, max_buy_unit: float | None, observed_or_lead: float | None) -> dict[str, Any] | None:
    mb, obs = _f(max_buy_unit), _f(observed_or_lead)
    if mb is None or obs is None or mb <= 0:
        return None
    dollars = round(mb - obs, 2)
    return {"dollars": dollars, "percentage": round(100.0 * dollars / mb, 1), "within_target": dollars >= 0}


def classify_supplier_quality(candidate: dict[str, Any]) -> dict[str, Any]:
    st = str(candidate.get("source_type") or "").upper()
    domain = str(candidate.get("supplier_domain") or "").lower()
    if any(x in domain for x in ("dell.com", "ford.com", "hp.com", "lenovo.com", "bobcat")):
        role = "OEM_DIRECT"
    elif "AUTHORIZED" in st or "DEALER" in st:
        role = "AUTHORIZED_DEALER"
    elif "DISTRIBUTOR" in st:
        role = "AUTHORIZED_DISTRIBUTOR"
    elif "RESELLER" in st or "VAR" in st:
        role = "MAJOR_COMMERCIAL_RESELLER"
    elif st:
        role = "SPECIALIST_DEALER"
    else:
        role = "UNKNOWN_SELLER"
    return {
        **candidate,
        "supplier_role": role,
        "authorized_status": candidate.get("authorized_status") or ("LIKELY" if "AUTHORIZED" in role else "UNKNOWN"),
        "outreach_authorized": False,
    }


def enrich_suppliers(
    row: dict[str, Any],
    commercial: dict[str, Any] | None = None,
    *,
    limit: int = 6,
    supplier_memory: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    cands = generate_supplier_candidates(row=row, commercial=commercial, limit=limit)
    out = [classify_supplier_quality(c) for c in cands]
    if supplier_memory and commercial:
        fam = str(commercial.get("manufacturer") or commercial.get("model") or "").upper()
        for rec in supplier_memory.get("suppliers") or []:
            if fam and fam in str(rec.get("product_family") or "").upper():
                out.insert(
                    0,
                    classify_supplier_quality(
                        {
                            "supplier_domain": rec.get("supplier"),
                            "source_type": rec.get("role") or "KNOWN",
                            "url": rec.get("url"),
                            "product_fit": rec.get("product_family"),
                            "from_memory": True,
                            "outreach_authorized": False,
                        }
                    ),
                )
    seen: set[str] = set()
    uniq = []
    for c in out:
        d = str(c.get("supplier_domain") or "")
        if d and d not in seen:
            seen.add(d)
            uniq.append(c)
    return uniq[: max(limit, 3)]


def quote_economics_score(
    *,
    gov: dict[str, Any],
    max_buy: dict[str, Any] | None,
    suppliers: list[dict[str, Any]],
    deadline_days: float | None = None,
    freight_status: str | None = None,
    recurring: bool = False,
    commercial_common: bool = False,
) -> dict[str, Any]:
    score = 0
    factors: list[str] = []
    st = gov.get("state")
    score += {GOV_VALUE_EXACT: 30, GOV_VALUE_STRONG: 22, GOV_VALUE_RANGE: 12, GOV_VALUE_COMPARABLE: 8}.get(st, 0)
    if st:
        factors.append(f"gov:{st}")
    target = _f((max_buy or {}).get("supplier_quote_target"))
    if target and target > 1000:
        score += 15
        factors.append("max_buy_headroom")
    n = len(suppliers)
    score += 15 if n >= 3 else 10 if n >= 2 else 5 if n >= 1 else 0
    if n:
        factors.append(f"suppliers_{n}")
    if commercial_common:
        score += 10
        factors.append("common_product")
    if deadline_days is not None:
        if deadline_days >= 14:
            score += 12
        elif deadline_days >= 7:
            score += 8
        elif deadline_days >= 5:
            score += 4
        elif deadline_days < 3:
            score -= 10
        factors.append(f"deadline_{deadline_days}")
    if freight_status == FREIGHT_QUOTE_REQUIRED:
        score -= 5
        factors.append("hard_freight")
    if recurring:
        score += 10
        factors.append("recurring")
    return {"score": max(0, min(100, score)), "factors": factors}


def quote_dependent_positive(
    *,
    max_buy: dict[str, Any] | None,
    suppliers: list[dict[str, Any]],
    gov: dict[str, Any],
) -> dict[str, Any]:
    target = _f((max_buy or {}).get("supplier_quote_target"))
    th = (max_buy or {}).get("thresholds") or {}
    p5 = _f(th.get("MAX_BUY_FOR_5K_PROFIT"))
    p10 = _f(th.get("MAX_BUY_FOR_10K_PROFIT"))
    p25 = _f(th.get("MAX_BUY_FOR_25K_PROFIT"))
    viable = bool(
        target
        and target > 0
        and gov.get("state") in {GOV_VALUE_EXACT, GOV_VALUE_STRONG, GOV_VALUE_RANGE, GOV_VALUE_COMPARABLE}
        and len(suppliers) >= 1
    )
    gov_u = _f((max_buy or {}).get("government_unit"))
    qty = _f((max_buy or {}).get("quantity")) or 1.0
    revenue = (gov_u * qty) if gov_u else _f((max_buy or {}).get("government_total"))
    freight = float((max_buy or {}).get("freight_reserve") or 0)
    ge_50k = bool(viable and revenue and (revenue - freight - 50000) > (revenue * 0.35))
    return {
        "state": QUOTE_DEPENDENT_POSITIVE if viable else None,
        "tiers": {
            "quote_dependent_positive": viable,
            "ge_5k": bool(viable and p5 and p5 > 0),
            "ge_10k": bool(viable and p10 and p10 > 0),
            "ge_25k": bool(viable and p25 and p25 > 0),
            "ge_50k": ge_50k,
        },
        "not_verified_profit": True,
    }


def convert_unknown_lane(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    current_lane: str | None = None,
) -> dict[str, Any]:
    commercial = commercial or {}
    before = current_lane or UNKNOWN_ACQUISITION_CHANNEL
    lane_info = classify_acquisition_lane(row, commercial=commercial)
    after = lane_info.get("acquisition_lane") or before
    if after == UNKNOWN_ACQUISITION_CHANNEL:
        blob = " ".join(str(x or "") for x in (row.get("title"), commercial.get("model"), commercial.get("manufacturer")))
        score = ((lane_info.get("commercial_acquisition_score") or {}).get("score")) or 0
        if score >= 30 or commercial.get("model") or commercial.get("manufacturer"):
            after = QUOTE_REQUIRED_COMMERCIAL
            lane_info = {
                **lane_info,
                "acquisition_lane": after,
                "reason": "unknown_soft_convert_quote",
                "quote_required": True,
            }
        elif re.search(r"\b(NSN|WSDC|SPRTA|mil[\-\s]?spec)\b", blob, re.I):
            after = MILSPEC_SPECIALTY
            lane_info = {**lane_info, "acquisition_lane": after, "reason": "unknown_to_specialty"}
    return {"before": before, "after": after, "converted": before != after, "lane_info": lane_info}


def normalize_uom(row: dict[str, Any], screen: dict[str, Any] | None = None) -> dict[str, Any]:
    screen = screen or {}
    uom = str(screen.get("uom") or row.get("uom") or row.get("unit_of_measure") or "").strip().lower()
    qty = _f(screen.get("quantity") or row.get("quantity"))
    aliases = {
        "ea": "each",
        "each": "each",
        "unit": "each",
        "lot": "lot",
        "pair": "pair",
        "case": "case",
        "pack": "pack",
        "kit": "kit",
        "vehicle": "vehicle",
        "veh": "vehicle",
    }
    if not uom and qty is None:
        return {"uom": None, "status": UOM_UNRESOLVED, "quantity": None}
    return {"uom": aliases.get(uom, uom or "each"), "status": "UOM_RESOLVED" if uom or qty else UOM_UNRESOLVED, "quantity": qty}


def build_quote_packet_l6(
    *,
    row: dict[str, Any],
    commercial: dict[str, Any] | None = None,
    max_buy: dict[str, Any] | None = None,
    suppliers: list[dict[str, Any]] | None = None,
    gov: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = prepare_quote_packet_l3(row=row, commercial=commercial, max_buy=max_buy, suppliers=suppliers)
    target = _f((max_buy or {}).get("supplier_quote_target"))
    base.update(
        {
            "phase": "L.6",
            "send_authorized": False,
            "government_value_state": (gov or {}).get("state"),
            "target_acquisition_price": target,
            "target_label": (max_buy or {}).get("label"),
            "buyer": row.get("agency") or row.get("buyer"),
            "destination": row.get("place_of_performance") or row.get("delivery_location"),
        }
    )
    return base


def remember_buyer_value(
    memory: dict[str, Any], *, buyer: str | None, unit: float | None, product_key: str | None = None
) -> None:
    """Persist buyer×product unit history. Never store agency-wide-only medians."""
    if not buyer or not unit or not product_key:
        return
    memory.setdefault("buyers", {})
    rec = memory["buyers"].setdefault(buyer.upper(), {"by_product": {}, "product_keys": []})
    prod = rec["by_product"].setdefault(str(product_key).upper()[:80], {"samples": []})
    prod["samples"].append(unit)
    samples = prod["samples"][-20:]
    prod["samples"] = samples
    prod["median_unit_price"] = sorted(samples)[len(samples) // 2]
    prod["min_unit_price"] = min(samples)
    prod["max_unit_price"] = max(samples)
    pk = str(product_key).upper()[:80]
    if pk not in rec["product_keys"]:
        rec["product_keys"].append(pk)
    memory["updated_at"] = _utc()


def evaluate_quote_opportunity(
    row: dict[str, Any],
    *,
    commercial: dict[str, Any] | None = None,
    history: dict[str, Any] | None = None,
    stage3: dict[str, Any] | None = None,
    lane: str | None = None,
    lead_price: float | None = None,
    verified_price: float | None = None,
    deadline_days: float | None = None,
    buyer_memory: dict[str, Any] | None = None,
    supplier_memory: dict[str, Any] | None = None,
    original: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full L.6 quote-economics evaluation for one opportunity (no outreach)."""
    commercial = commercial or {}
    gov = assess_government_value(
        row, history=history, stage3=stage3, buyer_memory=buyer_memory, commercial=commercial
    )
    freight = freight_reserve_for_row(row)
    uom = normalize_uom(row, stage3)
    qty = uom.get("quantity") or gov.get("quantity") or 1.0

    # Config mismatch: if history says bundled install/service and title is bare product — downgrade
    config_ok = True
    hist_blob = str((history or {}).get("description") or "")
    if re.search(r"\b(install|warranty\s+package|bundled|turnkey)\b", hist_blob, re.I) and not re.search(
        r"\b(install|warranty|bundle)\b", str(row.get("title") or ""), re.I
    ):
        config_ok = False
        if gov.get("state") == GOV_VALUE_EXACT:
            gov = {**gov, "state": GOV_VALUE_COMPARABLE, "confidence": "LOW", "config_mismatch": True}

    if uom.get("status") == UOM_UNRESOLVED and gov.get("unit_value") and gov.get("total_value"):
        # Block false unit↔total comparison
        econ_block = UOM_UNRESOLVED
    else:
        econ_block = None

    max_buy = None
    if not econ_block:
        if gov.get("unit_value"):
            max_buy = calculate_max_buy_engine(
                government_unit=gov["unit_value"],
                quantity=qty,
                freight_reserve=freight["amount"],
                unit_low=gov.get("unit_low"),
                unit_high=gov.get("unit_high"),
                risk_reserve_rate=0.02 if freight["status"] != FREIGHT_ESTIMATE else 0.0,
            )
        elif gov.get("total_value"):
            max_buy = calculate_max_buy_engine(
                government_total=gov["total_value"],
                freight_reserve=freight["amount"],
                risk_reserve_rate=0.02,
            )

    revenue = expected_revenue(gov, quantity=qty if gov.get("unit_value") else None)
    suppliers = enrich_suppliers(row, commercial, supplier_memory=supplier_memory)
    bands = quote_bands(max_buy)
    sim = quote_outcome_simulator(
        max_buy,
        revenue_mid=revenue.get("ExpectedRevenueMid"),
        freight=freight["amount"],
        quantity=float(qty or 1),
    )
    qdep = quote_dependent_positive(max_buy=max_buy, suppliers=suppliers, gov=gov)
    headroom = acquisition_headroom(
        max_buy_unit=max_buy.get("supplier_quote_target") if max_buy else None,
        observed_or_lead=verified_price or lead_price,
    )
    qscore = quote_economics_score(
        gov=gov,
        max_buy=max_buy,
        suppliers=suppliers,
        deadline_days=deadline_days,
        freight_status=freight["status"],
        commercial_common=bool(commercial.get("manufacturer") or commercial.get("model")),
    )
    packet = build_quote_packet_l6(
        row=row, commercial=commercial, max_buy=max_buy, suppliers=suppliers, gov=gov
    )

    lead_band = classify_quote_band(lead_price, bands)
    apparent_within = bool(headroom and headroom.get("within_target") and lead_price and not verified_price)
    public_within = bool(headroom and headroom.get("within_target") and verified_price)

    remaining = []
    if not verified_price:
        remaining.append("supplier_quote")
    if freight["status"] != FREIGHT_ESTIMATE:
        remaining.append("freight")
    if not config_ok:
        remaining.append("configuration")
    if econ_block:
        remaining.append("uom")
    remaining.extend(["financing_execution", "compliance"])

    return {
        "lane": lane,
        "government_value": gov,
        "expected_revenue": revenue,
        "max_buy": max_buy,
        "quote_bands": bands,
        "quote_simulator": sim,
        "suppliers": suppliers,
        "supplier_count": len(suppliers),
        "quote_packet": packet,
        "quote_dependent": qdep,
        "quote_economics_score": qscore,
        "acquisition_headroom": headroom,
        "freight": freight,
        "uom": uom,
        "econ_block": econ_block,
        "lead_price": lead_price,
        "verified_price": verified_price,
        "lead_quote_band": lead_band,
        "apparently_within_quote_target": apparent_within,
        "public_price_within_target": public_within,
        "economic_state": (
            PUBLIC_PRICE_WITHIN_TARGET
            if public_within
            else APPARENTLY_WITHIN_QUOTE_TARGET
            if apparent_within
            else qdep.get("state")
            or ("MAX_BUY_PRICE_CALCULATED" if max_buy and max_buy.get("supplier_quote_target") else "INSUFFICIENT_EVIDENCE")
        ),
        "original": original,
        "remaining_unknowns": remaining,
        "send_authorized": False,
    }
