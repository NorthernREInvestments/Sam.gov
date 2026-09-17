"""Verification result ingestion, quote/financing comparison, commercial freshness."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from application_clock import now_utc
from commercial_verification_constants import (
    AUTH_BINDING_QUOTE,
    AUTH_COND_FIN,
    AUTH_MARKETING,
    AUTH_TERM_SHEET,
    AUTH_UNKNOWN,
    CF_AGING,
    CF_CURRENT,
    CF_EXPIRED,
    CF_REVERIFY,
    CV_EXPIRED,
    CV_FAILED,
    CV_FINANCIER_PATH_FAILED,
    CV_PARTIAL,
    CV_PASSED,
    CV_PENDING,
    NEG_ACCEPTABLE,
    NEG_HARD_CEILING,
    NEG_TARGET,
    NEG_WALK_AWAY,
)
from transaction_economics import compute_transaction_economics, profit_floor_config


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def evaluate_commercial_freshness(
    *,
    expires_at: str | None = None,
    verified_at: str | None = None,
    aging_days: float = 7.0,
) -> dict[str, Any]:
    exp = _parse(expires_at)
    now = now_utc()
    if exp and now > exp:
        return {"state": CF_EXPIRED, "blocks_final_readiness": True, "expires_at": expires_at}
    if exp and (exp - now) < timedelta(days=aging_days):
        return {"state": CF_AGING, "blocks_final_readiness": False, "expires_at": expires_at}
    if verified_at is None and expires_at is None:
        return {"state": CF_REVERIFY, "blocks_final_readiness": True}
    return {"state": CF_CURRENT, "blocks_final_readiness": False, "expires_at": expires_at}


def ingest_verification_result(
    *,
    opportunity_id: str,
    result_type: str,
    payload: dict[str, Any],
    authority: str = AUTH_UNKNOWN,
    source: str | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    """Manual/structured/future-adapter result — provenance required. No outreach."""
    if authority == AUTH_MARKETING:
        # Marketing cannot be transaction approval
        confidence = "LOW"
        usable_as_binding = False
    else:
        usable_as_binding = authority in {AUTH_BINDING_QUOTE, AUTH_TERM_SHEET, AUTH_COND_FIN, "AUTHORIZATION_LETTER", "OFFICIAL_DOCUMENT", "WRITTEN_COMMERCIAL_CONFIRMATION"}
        confidence = "HIGH" if usable_as_binding else "MEDIUM"

    return {
        "kind": "VerificationResult",
        "opportunity_id": opportunity_id,
        "result_type": result_type,
        "payload": payload,
        "authority": authority,
        "source": source,
        "usable_as_binding": usable_as_binding,
        "confidence": confidence,
        "provenance": {"source": source, "authority": authority, "ingested_at": now_utc().isoformat()},
        "expires_at": expires_at,
        "freshness": evaluate_commercial_freshness(expires_at=expires_at, verified_at=now_utc().isoformat()),
        "network_origin": False,
    }


def negotiation_bands(
    *,
    target: float | None,
    hard_ceiling: float | None,
    acceptable: float | None = None,
) -> dict[str, Any]:
    acc = acceptable if acceptable is not None else (
        (target + hard_ceiling) / 2 if target is not None and hard_ceiling is not None else hard_ceiling
    )
    return {
        NEG_TARGET: target,
        NEG_ACCEPTABLE: acc,
        NEG_HARD_CEILING: hard_ceiling,
        NEG_WALK_AWAY: hard_ceiling,
        "autonomous_negotiation": False,
    }


def apply_supplier_quote_to_economics(
    *,
    bid_revenue: float | None,
    quoted_acquisition: float,
    freight: float | None,
    financing: float | None,
    expense: float | None = 0.0,
    risk: float | None = 0.0,
    hard_ceiling: float | None = None,
    profit_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replace estimate with verified quote and recalculate."""
    cfg = profit_config or profit_floor_config()
    econ = compute_transaction_economics(
        government_bid_revenue=bid_revenue,
        product_acquisition_cost=quoted_acquisition,
        freight_cost=freight,
        financing_cost=financing,
        transaction_expense=expense,
        risk_allowance=risk,
        acquisition_confidence="VERIFIED_BINDING",
        freight_confidence="DEFENSIBLE_ESTIMATE" if freight is not None else "UNKNOWN",
        financing_confidence="DEFENSIBLE_ESTIMATE" if financing is not None else "UNKNOWN",
        profit_config=cfg,
    )
    profit = econ["ExpectedNetTransactionProfit"]["value"]
    floor = cfg["minimum_transaction_profit"]
    failed = False
    reasons = []
    if hard_ceiling is not None and quoted_acquisition > hard_ceiling:
        failed = True
        reasons.append(f"Supplier quote exceeds maximum acquisition ceiling by ${quoted_acquisition - hard_ceiling:,.2f}")
    if profit is not None and profit < floor:
        failed = True
        reasons.append(f"Expected profit ${profit:,.2f} below floor ${floor:,.2f}")

    return {
        "economics": econ,
        "verification_outcome": CV_FAILED if failed else CV_PASSED,
        "reasons": reasons,
        "stale_profit_preserved": False,
    }


def apply_financing_indication_to_economics(
    *,
    bid_revenue: float | None,
    acquisition: float,
    freight: float | None,
    financing_cost: float,
    max_financing_cost: float | None = None,
    expense: float | None = 0.0,
    risk: float | None = 0.0,
    min_fico_required: int | None = None,
    operator_fico: int | None = None,
    profit_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = profit_config or profit_floor_config()
    reasons: list[str] = []
    financier_path_failed = False
    economics_failed = False

    if min_fico_required is not None and operator_fico is not None and operator_fico < min_fico_required:
        financier_path_failed = True
        reasons.append(f"Financier requires minimum {min_fico_required} FICO")
    if max_financing_cost is not None and financing_cost > max_financing_cost:
        financier_path_failed = True
        reasons.append(f"Financing cost exceeds maximum acceptable by ${financing_cost - max_financing_cost:,.2f}")

    econ = compute_transaction_economics(
        government_bid_revenue=bid_revenue,
        product_acquisition_cost=acquisition,
        freight_cost=freight,
        financing_cost=financing_cost,
        transaction_expense=expense,
        risk_allowance=risk,
        acquisition_confidence="VERIFIED_BINDING",
        freight_confidence="DEFENSIBLE_ESTIMATE" if freight is not None else "UNKNOWN",
        financing_confidence="VERIFIED_BINDING",
        profit_config=cfg,
    )
    profit = econ["ExpectedNetTransactionProfit"]["value"]
    if profit is not None and profit < cfg["minimum_transaction_profit"]:
        economics_failed = True
        reasons.append("Profit after financing below floor")

    # One financier path failing ≠ whole commercial verification failed
    if financier_path_failed and not economics_failed:
        outcome = CV_FINANCIER_PATH_FAILED
    elif economics_failed:
        outcome = CV_FAILED
    else:
        outcome = CV_PASSED

    return {
        "economics": econ,
        "verification_outcome": outcome,
        "reasons": reasons,
        "stale_profit_preserved": False,
        "recalculate_remaining_paths": outcome == CV_FINANCIER_PATH_FAILED,
        "global_commercial_failure": outcome == CV_FAILED,
    }


def compare_supplier_quotes(quotes: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for q in quotes:
        landed = None
        if q.get("extended_price") is not None:
            landed = float(q["extended_price"]) + float(q.get("freight") or 0) + float(q.get("taxes") or 0)
        elif q.get("unit_price") is not None and q.get("quantity") is not None:
            landed = float(q["unit_price"]) * float(q["quantity"]) + float(q.get("freight") or 0)
        compliant = q.get("compliance_ok", True) and q.get("delivery_ok", True)
        rows.append({**q, "landed_cost": landed, "eligible": compliant})
    eligible = [r for r in rows if r.get("eligible")]
    # Do not select cheapest if non-compliant
    ranked = sorted(eligible, key=lambda r: (r.get("landed_cost") is None, r.get("landed_cost") or 1e18))
    return {
        "kind": "SupplierQuoteComparison",
        "quotes": rows,
        "preferred": ranked[0] if ranked else None,
        "note": "Cheapest non-compliant quote is never preferred",
        "selects_cheapest_noncompliant": False,
    }


def compare_financing_offers(
    offers: list[dict[str, Any]],
    *,
    bid_revenue: float | None,
    acquisition: float,
    freight: float | None = None,
) -> dict[str, Any]:
    rows = []
    for o in offers:
        fin_cost = o.get("expected_total_financing_cost")
        if fin_cost is None and o.get("fees") is not None:
            fin_cost = float(o["fees"])
        econ = None
        if fin_cost is not None and bid_revenue is not None:
            econ = compute_transaction_economics(
                government_bid_revenue=bid_revenue,
                product_acquisition_cost=acquisition,
                freight_cost=freight,
                financing_cost=float(fin_cost),
                acquisition_confidence="VERIFIED_BINDING",
                freight_confidence="DEFENSIBLE_ESTIMATE" if freight is not None else "UNKNOWN",
                financing_confidence="VERIFIED_BINDING",
            )
        rows.append(
            {
                **o,
                "expected_total_financing_cost": fin_cost,
                "resulting_expected_profit": (econ or {}).get("ExpectedNetTransactionProfit", {}).get("value"),
                "pg": o.get("personal_guarantee"),
                "fico": o.get("minimum_fico"),
                "credit_dependency": o.get("personal_credit_dependency"),
                "cash_contribution": o.get("cash_contribution"),
                "underwriting": o.get("underwriting_model"),
                "ranked_by_rate_only": False,
            }
        )
    # Rank by resulting profit (desc), not rate alone
    ranked = sorted(
        rows,
        key=lambda r: (
            r.get("resulting_expected_profit") is None,
            -(r.get("resulting_expected_profit") or -1e18),
        ),
    )
    return {
        "kind": "FinancingOfferComparison",
        "offers": rows,
        "preferred": ranked[0] if ranked else None,
        "considers_more_than_rate": True,
    }


COMMERCIAL_INVALIDATION_MAP = {
    "QUANTITY_CHANGE": ["supplier_quote_economics", "financing_amount", "profit"],
    "DEADLINE_CHANGE": ["quote_validity", "financing_duration"],
    "SPEC_CHANGE": ["supplier_product_verification", "product_compliance"],
    "DELIVERY_LOCATION_CHANGE": ["freight"],
    "PRODUCT_CHANGE": ["supplier_quote", "authorization"],
}


def invalidate_commercial_evidence(
    state: dict[str, Any],
    *,
    change_type: str,
) -> dict[str, Any]:
    out = dict(state or {})
    conclusions = dict(out.get("conclusions") or {})
    deps = COMMERCIAL_INVALIDATION_MAP.get(change_type, ["commercial_verification"])
    invalidated = []
    for d in deps:
        if d in conclusions:
            conclusions[d] = {"status": "INVALIDATED", "reason": change_type, "prior": conclusions[d]}
        else:
            conclusions[d] = {"status": "INVALIDATED", "reason": change_type}
        invalidated.append(d)
    preserved = [k for k in (state.get("conclusions") or {}) if k not in deps]
    out["conclusions"] = conclusions
    out["last_invalidation"] = {"change_type": change_type, "invalidated": invalidated, "preserved": preserved}
    return out
