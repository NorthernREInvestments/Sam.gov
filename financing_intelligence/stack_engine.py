"""Capital-stack builder + source matching (UNKNOWN ≠ fail)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from financing_intelligence.capital import capital_snapshot, confirmed_amount_for_opportunity
from financing_intelligence.constants import (
    BUILD,
    CASE_BY_CASE,
    CREDIT_HARD,
    NO,
    PG_REQUIRED,
    SRC_SUPPLIER,
    ST_CAPITAL_CONFIRMATION_REQUIRED,
    ST_CAPITAL_STACK_CLOSED,
    ST_EXECUTION_FAIL,
    ST_FINANCEABLE_BUT_TIMING_RISK,
    ST_FINANCING_GAP,
    ST_FINANCING_UNKNOWN,
    ST_LIKELY_FINANCEABLE,
    ST_NEEDS_LENDER_APPROVAL,
    ST_REQUIRES_SUPPLIER_TERMS,
    UNKNOWN,
    YES,
)
from financing_intelligence.cost import estimate_financing_cost as _cost_detail
from financing_intelligence.eligibility import evaluate_lender_eligibility
from financing_intelligence.next_actions import build_next_action
from financing_intelligence.sources import approved_rules_for_source, list_sources
from financing_intelligence.store import load_owner_prefs, money, money_str
from financing_intelligence.supplier_bridge import verified_supplier_cover
from financing_intelligence.timing import evaluate_timing


def _pct(rules: dict[str, Any], key: str, default: Decimal | None = None) -> Decimal | None:
    raw = rules.get(key)
    if raw in (None, "", UNKNOWN):
        return default
    try:
        return Decimal(str(raw).replace("%", "").strip())
    except Exception:
        return default


def _tri(rules: dict[str, Any], key: str) -> str:
    v = str(rules.get(key) or UNKNOWN).upper()
    if v in {YES, NO, "CONDITIONAL", CASE_BY_CASE, UNKNOWN}:
        return v
    return UNKNOWN


def estimate_financing_cost(
    *,
    financed_amount: Decimal,
    rules: dict[str, Any],
    financed_days: int | None = None,
) -> Decimal:
    """Backward-compatible Decimal total from verified rules only."""
    return _cost_detail(
        financed_amount=financed_amount, rules=rules, financed_days=financed_days
    )["total_decimal"]


def match_source_to_opportunity(
    *,
    source: dict[str, Any],
    rules: dict[str, Any],
    contract_value: Decimal,
    supplier_cost: Decimal,
    gross_margin_pct: Decimal | None,
    jurisdiction: str = "FEDERAL",
    prefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prefs = prefs or load_owner_prefs()
    reasons: list[str] = []
    hard_fail = False
    unknown_bits: list[str] = []

    min_size = money(rules.get("preferred_deal_size_min") or rules.get("minimum_transaction_size") or 0)
    max_size = money(rules.get("maximum_transaction_size") or 0)
    if min_size > 0 and contract_value < min_size:
        hard_fail = True
        reasons.append(f"below_lender_minimum:{money_str(min_size)}")
    if max_size > 0 and contract_value > max_size:
        hard_fail = True
        reasons.append(f"above_lender_maximum:{money_str(max_size)}")

    min_margin = _pct(rules, "minimum_gross_margin_pct")
    if min_margin is not None and gross_margin_pct is not None and gross_margin_pct < min_margin:
        hard_fail = True
        reasons.append(f"margin_below_minimum:{min_margin}")

    min_profit = money(rules.get("minimum_expected_profit") or 0)
    if min_profit > 0 and gross_margin_pct is not None:
        # Margin alone doesn't give profit; skip unless contract known — handled in stack
        pass

    pg = str(rules.get("personal_guarantee") or UNKNOWN).upper()
    if pg == PG_REQUIRED and not prefs.get("personal_guarantee_allowed"):
        hard_fail = True
        reasons.append("pg_required_owner_disallows")
    elif pg in {UNKNOWN, CASE_BY_CASE}:
        unknown_bits.append("personal_guarantee")

    credit = str(rules.get("personal_credit") or UNKNOWN).upper()
    if credit == CREDIT_HARD and not prefs.get("personal_credit_dependency_allowed"):
        hard_fail = True
        reasons.append("personal_credit_hard_pull_disallowed")
    elif credit == UNKNOWN:
        unknown_bits.append("personal_credit")

    fed = _tri(rules, "federal_contracts_accepted")
    if jurisdiction.upper() == "FEDERAL" and fed == NO:
        hard_fail = True
        reasons.append("federal_not_accepted")
    elif jurisdiction.upper() == "FEDERAL" and fed == UNKNOWN:
        unknown_bits.append("federal_acceptance")

    for jkey, jname in (
        ("state_contracts_accepted", "STATE"),
        ("local_contracts_accepted", "LOCAL"),
    ):
        if jurisdiction.upper() == jname and _tri(rules, jkey) == NO:
            hard_fail = True
            reasons.append(f"{jname.lower()}_not_accepted")

    advance = _pct(rules, "max_advance_pct") or _pct(rules, "typical_advance_pct")
    if advance is None and _tri(rules, "can_fund_100_percent_supplier_invoice") == YES:
        advance = Decimal("100")
    if advance is None:
        unknown_bits.append("max_advance_pct")

    if _tri(rules, "can_fund_freight") == UNKNOWN:
        unknown_bits.append("can_fund_freight")

    compatible = not hard_fail
    return {
        "source_id": source.get("source_id"),
        "company_name": source.get("company_name"),
        "source_type": source.get("source_type"),
        "compatible": compatible,
        "hard_fail": hard_fail,
        "reasons": reasons,
        "unknown_fields": unknown_bits,
        "max_advance_pct": str(advance) if advance is not None else None,
        "pays_supplier_directly": _tri(rules, "pays_supplier_directly"),
        "rules": {k: v for k, v in rules.items() if not str(k).endswith("__evidence")},
    }


def build_capital_stacks(
    *,
    opportunity_id: str,
    contract_value: Decimal,
    supplier_cost: Decimal,
    freight: Decimal,
    other_prepay: Decimal = Decimal("0"),
    verified_upfront_fees: Decimal = Decimal("0"),
    supplier_terms: dict[str, Any] | None = None,
    jurisdiction: str = "FEDERAL",
    timing_dates: dict[str, Any] | None = None,
    financed_days: int | None = None,
    deal_type: str = "PRODUCT_RESALE",
) -> dict[str, Any]:
    """Search viable stacks. Never auto-uses company cash. Verified supplier terms only."""
    total_prepay = supplier_cost + freight + other_prepay + money(verified_upfront_fees)
    revenue = contract_value
    gross = revenue - supplier_cost - freight
    margin_pct = (gross / revenue * Decimal("100")) if revenue > 0 else None

    prefs = load_owner_prefs()
    supplier_terms = supplier_terms or {}
    cover_info = verified_supplier_cover(supplier_terms)
    supplier_cover = money(cover_info["usable_credit"]) if cover_info.get("verified") else Decimal("0")
    # Explicit covers_full only when verified
    if (
        cover_info.get("verified")
        and supplier_cover <= 0
        and supplier_terms.get("covers_full_supplier_cost")
        and cover_info.get("net_days")
    ):
        supplier_cover = supplier_cost

    timing = evaluate_timing(
        dates=timing_dates,
        supplier_net_days=cover_info.get("net_days"),
        financed_days_override=financed_days,
    )
    use_days = timing.get("expected_financed_days")

    matches = []
    hard_fail_matches = []
    eligibility_by_source: dict[str, Any] = {}
    for src in list_sources(active_only=True):
        rules = approved_rules_for_source(src["source_id"])
        has_signal = any(
            k in rules
            for k in (
                "max_advance_pct",
                "typical_advance_pct",
                "can_fund_100_percent_supplier_invoice",
                "preferred_deal_size_min",
                "minimum_transaction_size",
                "minimum_gross_margin_pct",
                "federal_contracts_accepted",
                "personal_guarantee",
            )
        )
        if not has_signal and src.get("source_type") not in {SRC_SUPPLIER}:
            continue
        m = match_source_to_opportunity(
            source=src,
            rules=rules,
            contract_value=contract_value,
            supplier_cost=supplier_cost,
            gross_margin_pct=margin_pct,
            jurisdiction=jurisdiction,
            prefs=prefs,
        )
        elig = evaluate_lender_eligibility(
            source=src,
            rules=rules,
            contract_value=contract_value,
            supplier_cost=supplier_cost,
            freight=freight,
            gross_margin_pct=margin_pct,
            jurisdiction=jurisdiction,
            deal_type=deal_type,
            prefs=prefs,
        )
        eligibility_by_source[src["source_id"]] = elig
        m["eligibility"] = elig
        if m["hard_fail"]:
            hard_fail_matches.append({**m, "rules": rules})
        elif m["compatible"]:
            matches.append({**m, "rules": rules})

    stacks: list[dict[str, Any]] = []
    confirmed_cash = confirmed_amount_for_opportunity(opportunity_id)
    snap = capital_snapshot(exclude_opportunity_id=opportunity_id)

    finance_sources = [m for m in matches if m.get("source_type") != SRC_SUPPLIER]
    if not finance_sources and matches:
        finance_sources = matches

    # Known incompatibility with no viable alternate lender → EXECUTION_FAIL
    # (partial supplier terms do not override a hard PG/credit/buyer incompatibility
    # when that lender was the intended financing path and no compatible lender exists)
    if not finance_sources and hard_fail_matches:
        blockers = list({r for h in hard_fail_matches for r in (h.get("reasons") or [])})
        next_act = build_next_action(
            status=ST_EXECUTION_FAIL,
            lender_name=(hard_fail_matches[0].get("company_name") if hard_fail_matches else None),
            blockers=blockers,
        )
        return {
            "kind": "CapitalStackAnalysis",
            "build": BUILD,
            "opportunity_id": opportunity_id,
            "contract_value": money_str(contract_value),
            "supplier_cost": money_str(supplier_cost),
            "freight": money_str(freight),
            "verified_upfront_fees": money_str(verified_upfront_fees),
            "total_prepayment_need": money_str(total_prepay),
            "gross_margin_pct": str(margin_pct.quantize(Decimal("0.01"))) if margin_pct is not None else None,
            "best_stack": {
                "status": ST_EXECUTION_FAIL,
                "layers": [],
                "total_prepayment_need": money_str(total_prepay),
                "total_funded": "0.00",
                "unfunded_gap": money_str(total_prepay),
                "estimated_financing_cost": "0.00",
                "profit_before_financing": money_str(gross),
                "profit_after_financing": money_str(gross),
                "next_action": next_act,
                "blockers": blockers,
                "company_capital_confirmed": money_str(confirmed_cash),
                "company_capital_required_to_close": money_str(total_prepay),
                "owner_cash_required": money_str(total_prepay),
                "timing": timing,
            },
            "all_stacks": [],
            "matched_sources": [],
            "hard_fail_sources": [
                {"source_id": h["source_id"], "company_name": h["company_name"], "reasons": h["reasons"]}
                for h in hard_fail_matches
            ],
            "supplier_cover": cover_info,
            "eligibility": list(eligibility_by_source.values()),
            "capital_snapshot": snap,
            "timing": timing,
        }

    for m in finance_sources or [None]:
        layers: list[dict[str, Any]] = []
        remaining = total_prepay
        financing_cost = Decimal("0")
        fee_unknown = False
        fee_components: list[dict[str, Any]] = []
        freight_unknown = False
        elig_missing: list[str] = []

        if m and m.get("max_advance_pct") is not None:
            adv = Decimal(m["max_advance_pct"])
            rules = m.get("rules") or {}
            fund_freight = str(rules.get("can_fund_freight") or UNKNOWN).upper()
            # Include freight only when explicitly YES; UNKNOWN/NO → product only
            advance_base = supplier_cost
            if fund_freight == YES:
                advance_base = supplier_cost + freight
            elif fund_freight == UNKNOWN and freight > 0:
                freight_unknown = True
            covered = (advance_base * adv / Decimal("100")).quantize(Decimal("0.01"))
            covered = min(covered, remaining)
            layers.append(
                {
                    "role": "OUTSIDE_FINANCING",
                    "source_id": m.get("source_id"),
                    "label": m.get("company_name") or "Lender",
                    "amount": money_str(covered),
                    "advance_pct": str(adv),
                    "advance_base": money_str(advance_base),
                }
            )
            remaining -= covered
            cost_info = _cost_detail(financed_amount=covered, rules=rules, financed_days=use_days)
            financing_cost += cost_info["total_decimal"]
            fee_components.extend(cost_info.get("components") or [])
            if cost_info.get("partially_unknown"):
                fee_unknown = True
            elig = m.get("eligibility") or {}
            elig_missing = list(elig.get("missing") or [])
        elif m is None:
            pass

        if remaining > 0 and supplier_cover > 0:
            use = min(supplier_cover, remaining)
            layers.append(
                {
                    "role": "SUPPLIER_TERMS",
                    "label": cover_info.get("supplier_name") or supplier_terms.get("supplier_name") or "Supplier terms",
                    "amount": money_str(use),
                    "net_days": cover_info.get("net_days"),
                    "verified": True,
                }
            )
            remaining -= use
        elif remaining > 0 and not cover_info.get("verified") and supplier_terms:
            # Unverified terms present but not counted
            pass

        if remaining > 0 and confirmed_cash > 0:
            use = min(confirmed_cash, remaining)
            layers.append(
                {
                    "role": "COMPANY_CAPITAL_CONFIRMED",
                    "label": "Confirmed company capital",
                    "amount": money_str(use),
                }
            )
            remaining -= use

        unfunded = remaining
        needs_cash = unfunded
        total_funded = total_prepay - unfunded
        status = ST_FINANCING_UNKNOWN
        blockers: list[str] = []

        if m and m.get("hard_fail"):
            status = ST_EXECUTION_FAIL
            blockers.extend(m.get("reasons") or [])
        elif not finance_sources and supplier_cover <= 0 and confirmed_cash <= 0:
            status = ST_FINANCING_UNKNOWN
        elif unfunded <= 0 and confirmed_cash > 0 and any(L.get("role") == "COMPANY_CAPITAL_CONFIRMED" for L in layers):
            status = ST_CAPITAL_STACK_CLOSED
        elif unfunded <= 0:
            status = ST_LIKELY_FINANCEABLE
            if any(L.get("role") == "OUTSIDE_FINANCING" for L in layers):
                status = ST_LIKELY_FINANCEABLE
        elif unfunded > 0:
            deployable = money(snap["deployable_capital"])
            if deployable >= unfunded:
                status = ST_CAPITAL_CONFIRMATION_REQUIRED
            else:
                status = ST_FINANCING_GAP
                if supplier_cover <= 0:
                    if not cover_info.get("verified"):
                        blockers.append("BLOCKED_BY_SUPPLIER_TERMS")
                        if remaining == total_prepay - (
                            money(layers[0]["amount"]) if layers and layers[0].get("role") == "OUTSIDE_FINANCING" else 0
                        ):
                            status = ST_REQUIRES_SUPPLIER_TERMS if any(
                                L.get("role") == "OUTSIDE_FINANCING" for L in layers
                            ) else status
                    else:
                        blockers.append("BLOCKED_BY_SUPPLIER_CREDIT_INSUFFICIENT")
                blockers.append("BLOCKED_BY_FINANCING_GAP")

        # Timing risk only when stack otherwise closed/likely and dates known
        if status in {ST_LIKELY_FINANCEABLE, ST_CAPITAL_STACK_CLOSED} and timing.get("timing_risk"):
            status = ST_FINANCEABLE_BUT_TIMING_RISK
            blockers.append("BLOCKED_BY_TIMING_MISMATCH")

        # Freight unknown: do not EXECUTION_FAIL; prefer named next action
        if freight_unknown and status == ST_LIKELY_FINANCEABLE and unfunded <= 0:
            # Stack closed without freight in advance — if freight was covered by supplier/cash ok
            pass
        elif freight_unknown and unfunded > 0 and status == ST_FINANCING_GAP:
            # Keep gap; next action will name freight confirmation when appropriate
            pass

        profit_before = gross
        profit_after = gross - financing_cost
        capital_used = Decimal("0")
        po_amt = Decimal("0")
        sup_amt = Decimal("0")
        for L in layers:
            if L.get("role") == "COMPANY_CAPITAL_CONFIRMED":
                capital_used += money(L.get("amount"))
            elif L.get("role") == "OUTSIDE_FINANCING":
                po_amt += money(L.get("amount"))
            elif L.get("role") == "SUPPLIER_TERMS":
                sup_amt += money(L.get("amount"))
        roi = None
        if capital_used > 0:
            roi = str((profit_after / capital_used * Decimal("100")).quantize(Decimal("0.01")))

        next_act = build_next_action(
            status=status,
            lender_name=(m or {}).get("company_name"),
            supplier_name=cover_info.get("supplier_name") or supplier_terms.get("supplier_name"),
            unfunded_gap=unfunded,
            company_cash_required=needs_cash if needs_cash > 0 else 0,
            blockers=blockers,
            freight_unknown=freight_unknown,
            eligibility_missing=elig_missing,
            missing=list(timing.get("missing_dates") or []),
        )

        stacks.append(
            {
                "stack_id": f"stack-{(m or {}).get('source_id') or 'none'}",
                "status": status,
                "layers": layers,
                "product_cost": money_str(supplier_cost),
                "freight": money_str(freight),
                "total_prepayment_need": money_str(total_prepay),
                "po_financing": money_str(po_amt),
                "supplier_terms_amount": money_str(sup_amt),
                "total_funded": money_str(total_funded),
                "unfunded_gap": money_str(unfunded),
                "company_capital_confirmed": money_str(confirmed_cash),
                "company_capital_required_to_close": money_str(needs_cash if needs_cash > 0 else 0),
                "owner_cash_required": money_str(needs_cash if needs_cash > 0 else 0),
                "company_capital_used": money_str(capital_used),
                "estimated_roi_on_company_capital_pct": roi,
                "estimated_financing_cost": money_str(financing_cost),
                "financing_cost_components": fee_components,
                "financing_cost_unknown": fee_unknown,
                "profit_before_financing": money_str(profit_before),
                "profit_after_financing": money_str(profit_after),
                "expected_financed_days": use_days,
                "timing_risk": timing.get("timing_risk"),
                "timing": timing,
                "next_action": next_act,
                "blockers": blockers,
                "freight_funding_unknown": freight_unknown,
                "primary_source_id": (m or {}).get("source_id"),
                "primary_source_name": (m or {}).get("company_name"),
                "eligibility": (m or {}).get("eligibility"),
                "lender_approval_still_required": True,
            }
        )

    rank = {
        ST_CAPITAL_STACK_CLOSED: 0,
        ST_LIKELY_FINANCEABLE: 1,
        ST_FINANCEABLE_BUT_TIMING_RISK: 2,
        ST_NEEDS_LENDER_APPROVAL: 3,
        ST_CAPITAL_CONFIRMATION_REQUIRED: 4,
        ST_REQUIRES_SUPPLIER_TERMS: 5,
        ST_FINANCING_GAP: 6,
        ST_FINANCING_UNKNOWN: 7,
        ST_EXECUTION_FAIL: 8,
    }
    stacks.sort(key=lambda s: (rank.get(s["status"], 99), money(s["unfunded_gap"])))
    best = stacks[0] if stacks else {
        "status": ST_FINANCING_UNKNOWN,
        "layers": [],
        "total_prepayment_need": money_str(total_prepay),
        "total_funded": "0.00",
        "unfunded_gap": money_str(total_prepay),
        "owner_cash_required": money_str(total_prepay),
        "estimated_financing_cost": "0.00",
        "profit_before_financing": money_str(gross),
        "profit_after_financing": money_str(gross),
        "next_action": build_next_action(status=ST_FINANCING_UNKNOWN, missing=["supplier_cost"]),
        "blockers": [],
        "timing": timing,
    }

    return {
        "kind": "CapitalStackAnalysis",
        "build": BUILD,
        "opportunity_id": opportunity_id,
        "contract_value": money_str(contract_value),
        "supplier_cost": money_str(supplier_cost),
        "freight": money_str(freight),
        "verified_upfront_fees": money_str(verified_upfront_fees),
        "total_prepayment_need": money_str(total_prepay),
        "gross_margin_pct": str(margin_pct.quantize(Decimal("0.01"))) if margin_pct is not None else None,
        "best_stack": best,
        "all_stacks": stacks[:5],
        "matched_sources": [
            {"source_id": m["source_id"], "company_name": m["company_name"], "compatible": m["compatible"]}
            for m in matches
        ],
        "supplier_cover": cover_info,
        "eligibility": list(eligibility_by_source.values()),
        "capital_snapshot": snap,
        "timing": timing,
    }
