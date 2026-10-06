"""Line-level and whole-contract retail profit calculations."""

from __future__ import annotations

from typing import Any

from line_item_economics.models import (
    GRADE_A,
    GRADE_B,
    GRADE_C,
    GRADE_D,
    GREEN,
    GREEN_PLUS,
    RED,
    RETAIL_NEGATIVE,
    RETAIL_PROFIT_LIKELY,
    RETAIL_PROFIT_PROVEN,
    RETAIL_PROFIT_UNKNOWN,
    UNKNOWN_BUCKET,
    YELLOW,
    YELLOW_PLUS,
)
from line_item_economics.normalize import normalize_retail_to_buyer_units


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def attach_retail_evidence(line: dict[str, Any], evidence: dict[str, Any] | None) -> dict[str, Any]:
    """Attach current retail (or permitted-equal) evidence. Does not invent prices."""
    if not evidence:
        return line
    kind = str(evidence.get("kind") or "requested_brand").lower()
    target = "retail_equal" if kind in {"equal", "permitted_equal", "or_equal"} else "retail"
    unit = _f(evidence.get("unit_price") or evidence.get("normalized_unit_price"))
    conv = normalize_retail_to_buyer_units(
        retail_unit_price=unit,
        retail_uom=evidence.get("uom") or evidence.get("retail_uom"),
        retail_pack_size=_f(evidence.get("pack_size") or evidence.get("retail_pack_size")),
        buyer_uom=line.get("unit_of_measure"),
        buyer_pack_size=_f(line.get("pack_size")),
    )
    payload = {
        "unit_price": unit,
        "normalized_unit_price": conv.get("normalized_unit_price") if conv.get("ok") else None,
        "normalized_uom": conv.get("normalized_uom"),
        "conversion_ok": bool(conv.get("ok")),
        "conversion_reason": conv.get("reason"),
        "source_url": evidence.get("source_url") or evidence.get("url"),
        "retailer": evidence.get("retailer") or evidence.get("seller"),
        "stock_status": evidence.get("stock_status"),
        "observed_date": evidence.get("observed_date"),
        "pack_conversion": conv.get("conversion"),
        "shipping_if_explicit": _f(evidence.get("shipping") or evidence.get("shipping_if_explicit")),
        "price_confidence": evidence.get("price_confidence") or evidence.get("confidence") or "UNKNOWN",
        "kind": kind,
    }
    line[target] = payload
    return line


def attach_historical_evidence(line: dict[str, Any], evidence: dict[str, Any] | None) -> dict[str, Any]:
    if not evidence:
        return line
    # Never accept contract ceiling / estimate as paid price
    if str(evidence.get("price_role") or "").upper() in {"CEILING", "ESTIMATE", "NTE", "NOT_TO_EXCEED"}:
        line["historical"] = {
            "rejected": True,
            "reason": "CEILING_OR_ESTIMATE_NOT_PAID_PRICE",
            "source": evidence.get("source"),
        }
        return line
    unit = _f(evidence.get("awarded_unit_price") or evidence.get("unit_price"))
    line["historical"] = {
        "historical_buyer": evidence.get("historical_buyer") or evidence.get("buyer"),
        "award_date": evidence.get("award_date"),
        "exact_item": evidence.get("exact_item") or evidence.get("item"),
        "quantity": _f(evidence.get("quantity")),
        "awarded_unit_price": unit,
        "awarded_extended_total": _f(evidence.get("awarded_extended_total") or evidence.get("extended_total")),
        "bidder_count": evidence.get("bidder_count") or evidence.get("number_of_offers"),
        "winning_vendor": evidence.get("winning_vendor") or evidence.get("vendor"),
        "source": evidence.get("source") or evidence.get("source_type"),
        "match_quality": evidence.get("match_quality") or evidence.get("match"),
        "source_tier": evidence.get("source_tier"),  # 1..5 search order
    }
    return line


def compute_line_economics(line: dict[str, Any]) -> dict[str, Any]:
    qty = _f(line.get("quantity"))
    retail = line.get("retail") if isinstance(line.get("retail"), dict) else None
    retail_eq = line.get("retail_equal") if isinstance(line.get("retail_equal"), dict) else None
    hist = line.get("historical") if isinstance(line.get("historical"), dict) else None

    # Prefer conversion-ok normalized retail; fall back only if same UOM path ok
    retail_unit = None
    retail_source = None
    if retail and retail.get("conversion_ok") and retail.get("normalized_unit_price") is not None:
        retail_unit = _f(retail["normalized_unit_price"])
        retail_source = "requested_brand"
    elif retail and retail.get("unit_price") is not None and not retail.get("conversion_reason"):
        retail_unit = _f(retail["unit_price"])
        retail_source = "requested_brand"

    equal_unit = None
    if retail_eq and retail_eq.get("conversion_ok") and retail_eq.get("normalized_unit_price") is not None:
        equal_unit = _f(retail_eq["normalized_unit_price"])
    elif retail_eq and retail_eq.get("unit_price") is not None and not retail_eq.get("conversion_reason"):
        equal_unit = _f(retail_eq["unit_price"])

    hist_unit = None
    if hist and not hist.get("rejected"):
        hist_unit = _f(hist.get("awarded_unit_price"))

    retail_ext = (retail_unit * qty) if retail_unit is not None and qty is not None else None
    equal_ext = (equal_unit * qty) if equal_unit is not None and qty is not None else None
    hist_ext = (hist_unit * qty) if hist_unit is not None and qty is not None else None

    # Primary economics use requested-brand retail when present; equal shown separately
    primary_retail_ext = retail_ext
    primary_retail_unit = retail_unit
    if primary_retail_ext is None and equal_ext is not None:
        primary_retail_ext = equal_ext
        primary_retail_unit = equal_unit
        retail_source = "permitted_equal"

    spread = None
    if hist_ext is not None and primary_retail_ext is not None:
        spread = hist_ext - primary_retail_ext

    margin_pct = None
    if hist_ext and primary_retail_ext is not None and hist_ext != 0:
        margin_pct = (spread / hist_ext) * 100.0 if spread is not None else None

    # Required discounts relative to retail cost for break-even / profit targets on THIS line
    # discount_d such that hist_ext - retail_ext*(1-d) = target  => d = 1 - (hist_ext - target)/retail_ext
    def _discount_for(target_profit: float) -> float | None:
        if primary_retail_ext is None or primary_retail_ext <= 0 or hist_ext is None:
            return None
        # Need hist - retail*(1-d) >= target => retail*(1-d) <= hist - target
        need_cost = hist_ext - target_profit
        if need_cost <= 0:
            return 1.0  # even free goods insufficient for target on this line alone
        d = 1.0 - (need_cost / primary_retail_ext)
        return max(0.0, d)

    priced = primary_retail_ext is not None
    hist_matched = hist_ext is not None
    exact_match = str((hist or {}).get("match_quality") or "").upper() in {
        "EXACT",
        "EXACT_MODEL",
        "EXACT_PART_NUMBER",
        "EXACT_NSN",
        "EXACT_MATCH",
    } or str(line.get("identity_class") or "").startswith("EXACT")

    unresolved = not priced or not hist_matched
    reason = None
    if not priced and not hist_matched:
        reason = "NO_RETAIL_NO_HISTORICAL"
    elif not priced:
        reason = "NO_RETAIL"
    elif not hist_matched:
        reason = "NO_HISTORICAL"
    if line.get("unit_mismatch"):
        reason = "UNIT_MISMATCH"
        unresolved = True
        priced = False
        primary_retail_ext = None
        spread = None

    line["resolved"] = not unresolved
    line["unresolved_reason"] = reason if unresolved else None
    econ = {
        "quantity": qty,
        "retail_unit": primary_retail_unit,
        "retail_extended_cost": primary_retail_ext,
        "retail_source": retail_source,
        "requested_brand_retail_extended": retail_ext,
        "permitted_equal_retail_extended": equal_ext,
        "historical_gov_unit": hist_unit,
        "historical_gov_extended": hist_ext,
        "line_retail_spread": spread,
        "retail_margin_pct": margin_pct,
        "discount_break_even": _discount_for(0.0),
        "discount_for_5k_contrib": _discount_for(5000.0),
        "discount_for_10k_contrib": _discount_for(10000.0),
        "priced": priced,
        "historical_matched": hist_matched,
        "exact_matched": bool(exact_match and hist_matched),
        "unresolved": unresolved,
    }
    line["economics"] = econ
    return line


def _completeness_grade(coverage_pct: float) -> str:
    if coverage_pct >= 95:
        return GRADE_A
    if coverage_pct >= 80:
        return GRADE_B
    if coverage_pct >= 60:
        return GRADE_C
    return GRADE_D


def required_discounts(
    *,
    total_retail: float | None,
    total_hist: float | None,
    targets: tuple[float, ...] = (0.0, 2500.0, 5000.0, 10000.0),
) -> dict[str, Any]:
    if total_retail is None or total_retail <= 0 or total_hist is None:
        return {
            "break_even": None,
            "profit_2500": None,
            "profit_5000": None,
            "profit_10000": None,
            "detail": {},
        }
    detail = {}
    out: dict[str, Any] = {}
    labels = {0.0: "break_even", 2500.0: "profit_2500", 5000.0: "profit_5000", 10000.0: "profit_10000"}
    for t in targets:
        need_cost = total_hist - t
        if need_cost <= 0:
            d = 1.0
        else:
            d = max(0.0, 1.0 - (need_cost / total_retail))
        # If already above target at 0% discount, report 0
        current_spread = total_hist - total_retail
        if current_spread >= t:
            d = 0.0
        key = labels.get(t, f"profit_{int(t)}")
        out[key] = round(d * 100.0, 2)  # percent
        detail[key] = {"discount_pct": round(d * 100.0, 2), "target_profit": t}
    out["detail"] = detail
    return out


def classify_bucket(
    *,
    post_freight_spread: float | None,
    discount_for_5k_pct: float | None,
    coverage_pct: float,
) -> str:
    if post_freight_spread is None or coverage_pct < 60:
        return UNKNOWN_BUCKET
    if post_freight_spread >= 10000:
        return GREEN_PLUS
    if post_freight_spread >= 5000:
        return GREEN
    # Needs supplier discount to reach $5K
    if discount_for_5k_pct is not None:
        if discount_for_5k_pct <= 10:
            return YELLOW
        if discount_for_5k_pct <= 20:
            return YELLOW_PLUS
        return RED
    if post_freight_spread > 0:
        return YELLOW
    return RED


def classify_proof_label(
    *,
    grade: str,
    post_freight_spread: float | None,
    unresolved_value_share: float,
    coverage_pct: float,
) -> str:
    if post_freight_spread is None or coverage_pct < 60:
        return RETAIL_PROFIT_UNKNOWN
    if post_freight_spread < 0:
        return RETAIL_NEGATIVE
    # Proven only when high coverage AND unresolved cannot erase profit
    if grade in {GRADE_A, GRADE_B} and unresolved_value_share <= 0.15 and post_freight_spread >= 5000:
        # Unresolved share of known hist value — if unresolved could wipe profit, not proven
        return RETAIL_PROFIT_PROVEN
    if post_freight_spread > 0 and coverage_pct >= 60:
        return RETAIL_PROFIT_LIKELY
    return RETAIL_PROFIT_UNKNOWN


def rollup_contract(
    lines: list[dict[str, Any]],
    *,
    freight_estimate: float | None = None,
    financing_cost: float | None = None,
) -> dict[str, Any]:
    total = len(lines)
    priced = 0
    hist_matched = 0
    exact = 0
    unresolved = 0
    known_retail = 0.0
    known_hist = 0.0
    known_spread = 0.0
    has_retail = False
    has_hist = False
    unresolved_hist_proxy = 0.0
    known_hist_for_share = 0.0

    for line in lines:
        econ = line.get("economics") or {}
        if econ.get("priced"):
            priced += 1
            if econ.get("retail_extended_cost") is not None:
                known_retail += float(econ["retail_extended_cost"])
                has_retail = True
        if econ.get("historical_matched"):
            hist_matched += 1
            if econ.get("historical_gov_extended") is not None:
                known_hist += float(econ["historical_gov_extended"])
                known_hist_for_share += float(econ["historical_gov_extended"])
                has_hist = True
        if econ.get("exact_matched"):
            exact += 1
        if econ.get("unresolved"):
            unresolved += 1
            # Approximate unresolved share using hist when present else retail
            if econ.get("historical_gov_extended") is not None:
                unresolved_hist_proxy += float(econ["historical_gov_extended"])
            elif econ.get("retail_extended_cost") is not None:
                unresolved_hist_proxy += float(econ["retail_extended_cost"])
        if econ.get("line_retail_spread") is not None:
            known_spread += float(econ["line_retail_spread"])

    coverage_lines = (min(priced, hist_matched) / total * 100.0) if total else 0.0
    # Prefer two-sided coverage: lines with both retail and hist
    both = sum(
        1
        for line in lines
        if (line.get("economics") or {}).get("priced") and (line.get("economics") or {}).get("historical_matched")
    )
    coverage_pct = (both / total * 100.0) if total else 0.0
    denom = known_hist_for_share + unresolved_hist_proxy
    unresolved_share = (unresolved_hist_proxy / denom) if denom > 0 else (unresolved / total if total else 1.0)

    total_retail = known_retail if has_retail else None
    total_hist = known_hist if has_hist else None
    total_spread = (total_hist - total_retail) if (total_hist is not None and total_retail is not None) else None

    freight = _f(freight_estimate)
    financing = _f(financing_cost) or 0.0
    post_freight = (total_spread - freight) if (total_spread is not None and freight is not None) else (
        total_spread if total_spread is not None and freight is None else None
    )
    # If freight unknown, do not pretend post-freight equals pre-freight for proven claims
    freight_known = freight is not None
    post_financing = (post_freight - financing) if post_freight is not None else None

    discounts = required_discounts(total_retail=total_retail, total_hist=total_hist)
    # Recompute discount using post-freight target when freight known
    discount_5k = discounts.get("profit_5000")
    if freight_known and total_retail and total_hist is not None:
        # Need hist - retail*(1-d) - freight >= 5000
        adj_hist = total_hist - float(freight)
        discounts_adj = required_discounts(total_retail=total_retail, total_hist=adj_hist)
        discount_5k = discounts_adj.get("profit_5000")
        discounts = {
            **discounts,
            "break_even_post_freight": discounts_adj.get("break_even"),
            "profit_5000_post_freight": discounts_adj.get("profit_5000"),
            "profit_10000_post_freight": discounts_adj.get("profit_10000"),
            "profit_2500_post_freight": discounts_adj.get("profit_2500"),
        }

    grade = _completeness_grade(coverage_pct)
    bucket_spread = post_freight if freight_known else total_spread
    bucket = classify_bucket(
        post_freight_spread=bucket_spread,
        discount_for_5k_pct=discount_5k,
        coverage_pct=coverage_pct,
    )
    proof = classify_proof_label(
        grade=grade,
        post_freight_spread=bucket_spread if freight_known else None if freight is None and total_spread else bucket_spread,
        unresolved_value_share=unresolved_share,
        coverage_pct=coverage_pct,
    )
    # Stricter: no PROVEN when freight unknown and spread would be eroded-sensitive
    if proof == RETAIL_PROFIT_PROVEN and not freight_known:
        proof = RETAIL_PROFIT_LIKELY
    if unresolved_share > 0.20 and proof == RETAIL_PROFIT_PROVEN:
        proof = RETAIL_PROFIT_LIKELY

    return {
        "total_line_count": total,
        "lines_priced": priced,
        "lines_historical_matched": hist_matched,
        "lines_exact_matched": exact,
        "lines_unresolved": unresolved,
        "lines_both_sided": both,
        "coverage_pct": round(coverage_pct, 2),
        "unresolved_value_share": round(unresolved_share, 4),
        "completeness_grade": grade,
        "TOTAL_KNOWN_RETAIL_COST": round(total_retail, 2) if total_retail is not None else None,
        "TOTAL_KNOWN_HISTORICAL_VALUE": round(total_hist, 2) if total_hist is not None else None,
        "TOTAL_KNOWN_RETAIL_SPREAD": round(total_spread, 2) if total_spread is not None else None,
        "estimated_freight": round(freight, 2) if freight is not None else None,
        "freight_known": freight_known,
        "post_freight_spread": round(post_freight, 2) if post_freight is not None else None,
        "financing_cost": round(financing, 2),
        "post_financing_profit": round(post_financing, 2) if post_financing is not None else None,
        "required_discounts": discounts,
        "profit_bucket": bucket,
        "proof_label": proof,
        "note": (
            "Totals use known lines only. Unresolved lines are not treated as zero cost."
        ),
    }
