"""Opportunity financing assessment — integrates stack + capital confirmation + persistence."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from financing_intelligence.capital import capital_confirmation_card, confirmed_amount_for_opportunity
from financing_intelligence.constants import (
    BLOCKED_BY_ACQUISITION_UNKNOWN,
    BLOCKED_BY_APPROVAL_NEEDED,
    BLOCKED_BY_CAPITAL,
    BLOCKED_BY_FINANCING_GAP,
    BLOCKED_BY_FREIGHT,
    BLOCKED_BY_INFO_MISSING,
    BLOCKED_BY_LENDER_MINIMUM,
    BLOCKED_BY_LENDER_SIZE,
    BLOCKED_BY_PG,
    BLOCKED_BY_PERSONAL_CREDIT,
    BLOCKED_BY_SUPPLIER_CREDIT,
    BLOCKED_BY_SUPPLIER_TERMS,
    BLOCKED_BY_TIMING,
    BLOCKED_BY_UNSUPPORTED_BUYER,
    BUILD,
    ST_CAPITAL_CONFIRMATION_REQUIRED,
    ST_CAPITAL_STACK_CLOSED,
    ST_EXECUTION_FAIL,
    ST_FINANCEABLE_BUT_TIMING_RISK,
    ST_FINANCING_GAP,
    ST_FINANCING_UNKNOWN,
    ST_LIKELY_FINANCEABLE,
    ST_NEEDS_LENDER_APPROVAL,
    ST_REQUIRES_SUPPLIER_TERMS,
)
from financing_intelligence.outcomes import historical_outcome_patterns
from financing_intelligence.stack_engine import build_capital_stacks
from financing_intelligence.store import load_assessments, load_outcomes, money, money_str, save_assessments, _utc
from financing_intelligence.supplier_bridge import from_m3_commercial_terms


def assess_opportunity_financing(
    *,
    opportunity_id: str,
    contract_value: Any,
    supplier_cost: Any,
    freight: Any = 0,
    other_prepay: Any = 0,
    verified_upfront_fees: Any = 0,
    supplier_terms: dict[str, Any] | None = None,
    jurisdiction: str = "FEDERAL",
    timing_dates: dict[str, Any] | None = None,
    financed_days: int | None = None,
    deal_type: str = "PRODUCT_RESALE",
    persist: bool = True,
) -> dict[str, Any]:
    analysis = build_capital_stacks(
        opportunity_id=opportunity_id,
        contract_value=money(contract_value),
        supplier_cost=money(supplier_cost),
        freight=money(freight),
        other_prepay=money(other_prepay),
        verified_upfront_fees=money(verified_upfront_fees),
        supplier_terms=supplier_terms,
        jurisdiction=jurisdiction,
        timing_dates=timing_dates,
        financed_days=financed_days,
        deal_type=deal_type,
    )
    best = dict(analysis.get("best_stack") or {})
    status = best.get("status") or ST_FINANCING_UNKNOWN

    hist = _historical_compatibility(best.get("primary_source_id"))
    patterns = historical_outcome_patterns(best.get("primary_source_id"))
    confirmation = None
    if status == ST_CAPITAL_CONFIRMATION_REQUIRED:
        confirmation = capital_confirmation_card(
            opportunity_id=opportunity_id,
            required_amount=best.get("company_capital_required_to_close") or best.get("unfunded_gap"),
        )

    blocked_reason = _blocked_reason(status, best.get("blockers") or [])
    blocked_profit = None
    # Only count verified/estimated profit when economics are real (not unknown acquisition)
    if status in {
        ST_FINANCING_GAP,
        ST_EXECUTION_FAIL,
        ST_CAPITAL_CONFIRMATION_REQUIRED,
        ST_REQUIRES_SUPPLIER_TERMS,
        ST_FINANCEABLE_BUT_TIMING_RISK,
    }:
        blocked_profit = best.get("profit_after_financing") or best.get("profit_before_financing")

    owner_cash = best.get("owner_cash_required") or best.get("company_capital_required_to_close") or "0.00"
    card = {
        "kind": "OpportunityFinancingCard",
        "build": BUILD,
        "opportunity_id": opportunity_id,
        "status": status,
        "plain_status": status.replace("_", " ").title(),
        "status_means": _status_means(status),
        "contract_value": analysis.get("contract_value"),
        "product_cost": best.get("product_cost") or analysis.get("supplier_cost"),
        "freight": best.get("freight") or analysis.get("freight"),
        "total_prepayment_need": analysis.get("total_prepayment_need"),
        "po_financing": best.get("po_financing"),
        "supplier_terms_amount": best.get("supplier_terms_amount"),
        "confirmed_company_capital": money_str(confirmed_amount_for_opportunity(opportunity_id)),
        "owner_cash_required": owner_cash,
        "suggested_stack": best.get("layers") or [],
        "unfunded_gap": best.get("unfunded_gap"),
        "estimated_financing_cost": best.get("estimated_financing_cost"),
        "financing_cost_unknown": best.get("financing_cost_unknown"),
        "financing_cost_components": best.get("financing_cost_components"),
        "profit_before_financing": best.get("profit_before_financing"),
        "profit_after_financing": best.get("profit_after_financing"),
        "company_capital_confirmed": money_str(confirmed_amount_for_opportunity(opportunity_id)),
        "company_capital_used": best.get("company_capital_used"),
        "estimated_roi_on_company_capital_pct": best.get("estimated_roi_on_company_capital_pct"),
        "expected_financed_days": best.get("expected_financed_days"),
        "timing_risk": best.get("timing_risk"),
        "timing": best.get("timing") or analysis.get("timing"),
        "next_action": best.get("next_action"),
        "primary_lender": best.get("primary_source_name"),
        "historical_compatibility": hist,
        "historical_patterns": patterns,
        "eligibility": best.get("eligibility") or (analysis.get("eligibility") or [None])[0],
        "capital_confirmation": confirmation,
        "blocked_profit": blocked_profit,
        "blocked_reason": blocked_reason,
        "potential_unlock": _potential_unlock(blocked_reason, best),
        "lender_approval_still_required": status
        in {
            ST_LIKELY_FINANCEABLE,
            ST_CAPITAL_STACK_CLOSED,
            ST_NEEDS_LENDER_APPROVAL,
            ST_FINANCEABLE_BUT_TIMING_RISK,
        },
        "never_fabricated": True,
        "supplier_cover": analysis.get("supplier_cover"),
    }
    if status == ST_LIKELY_FINANCEABLE:
        card["lender_approval_still_required"] = True
        card["detail"] = "Verified rules indicate compatibility; specific lender approval still required."
    if status == ST_CAPITAL_STACK_CLOSED:
        card["detail"] = "Prepayment covered including confirmed company capital; lender approval may still be required."
    if status == ST_FINANCEABLE_BUT_TIMING_RISK:
        card["detail"] = "Capital stack closes, but known dates show a timing gap."

    out = {
        "ok": True,
        "card": card,
        "analysis": analysis,
        "assessed_at": _utc(),
    }
    if persist:
        doc = load_assessments()
        doc.setdefault("by_opportunity", {})[opportunity_id] = {
            "card": card,
            "best_stack": best,
            "assessed_at": out["assessed_at"],
        }
        save_assessments(doc)
    return out


def _status_means(status: str) -> str:
    return {
        ST_LIKELY_FINANCEABLE: "Deal looks financeable with verified rules; lender must still approve this transaction.",
        ST_NEEDS_LENDER_APPROVAL: "Compatible structure, but transaction-specific lender approval is required.",
        ST_REQUIRES_SUPPLIER_TERMS: "Outside financing alone is not enough — verified supplier terms are needed.",
        ST_CAPITAL_CONFIRMATION_REQUIRED: "Company cash could close the gap, but only after explicit owner confirmation.",
        ST_CAPITAL_STACK_CLOSED: "All prepayment is covered including confirmed company capital.",
        ST_FINANCING_GAP: "Some prepayment remains uncovered.",
        ST_EXECUTION_FAIL: "Known rules make this deal incompatible with owner constraints.",
        ST_FINANCING_UNKNOWN: "Not enough verified information to decide yet.",
        ST_FINANCEABLE_BUT_TIMING_RISK: "Funded on paper, but cash-cycle timing does not fully bridge.",
    }.get(status, status)


def _blocked_reason(status: str, blockers: list[str]) -> str | None:
    if status == ST_CAPITAL_CONFIRMATION_REQUIRED:
        return BLOCKED_BY_CAPITAL
    if status == ST_FINANCEABLE_BUT_TIMING_RISK:
        return BLOCKED_BY_TIMING
    if status == ST_REQUIRES_SUPPLIER_TERMS:
        return BLOCKED_BY_SUPPLIER_TERMS
    joined = " ".join(blockers)
    if any("pg_" in b for b in blockers):
        return BLOCKED_BY_PG
    if any("personal_credit" in b for b in blockers):
        return BLOCKED_BY_PERSONAL_CREDIT
    if any("below_lender_minimum" in b or "above_lender_maximum" in b for b in blockers):
        return BLOCKED_BY_LENDER_SIZE
    if "federal_not_accepted" in joined or "state_not_accepted" in joined:
        return BLOCKED_BY_UNSUPPORTED_BUYER
    if "BLOCKED_BY_SUPPLIER_CREDIT_INSUFFICIENT" in blockers:
        return BLOCKED_BY_SUPPLIER_CREDIT
    if BLOCKED_BY_SUPPLIER_TERMS in blockers or "BLOCKED_BY_SUPPLIER_TERMS" in blockers:
        return BLOCKED_BY_SUPPLIER_TERMS
    if "freight" in joined.lower():
        return BLOCKED_BY_FREIGHT
    if status == ST_FINANCING_GAP:
        return BLOCKED_BY_FINANCING_GAP
    if status == ST_FINANCING_UNKNOWN:
        return BLOCKED_BY_INFO_MISSING
    if status == ST_LIKELY_FINANCEABLE:
        return BLOCKED_BY_APPROVAL_NEEDED
    if status == ST_EXECUTION_FAIL:
        return BLOCKED_BY_PG if "pg_" in joined else BLOCKED_BY_FINANCING_GAP
    return None


def _potential_unlock(reason: str | None, best: dict[str, Any]) -> str | None:
    if not reason:
        return None
    mapping = {
        BLOCKED_BY_CAPITAL: "Confirm company capital for this deal",
        BLOCKED_BY_PG: "Find a no-PG lender or change owner PG preference",
        BLOCKED_BY_PERSONAL_CREDIT: "Find a lender that does not require personal credit",
        BLOCKED_BY_SUPPLIER_TERMS: "Obtain verified supplier Net terms / credit",
        BLOCKED_BY_SUPPLIER_CREDIT: f"Increase supplier credit by ${best.get('unfunded_gap') or '0'}",
        BLOCKED_BY_FINANCING_GAP: "Add verified financing, supplier terms, or confirmed capital",
        BLOCKED_BY_TIMING: "Extend financing duration or renegotiate supplier due date",
        BLOCKED_BY_FREIGHT: "Confirm freight funding with lender",
        BLOCKED_BY_LENDER_SIZE: "Use a lender whose size band fits this deal",
        BLOCKED_BY_UNSUPPORTED_BUYER: "Use a lender that accepts this buyer type",
        BLOCKED_BY_INFO_MISSING: "Gather verified lender / supplier facts",
        BLOCKED_BY_APPROVAL_NEEDED: "Submit this transaction for lender approval",
        BLOCKED_BY_ACQUISITION_UNKNOWN: "Obtain verified supplier acquisition cost",
        BLOCKED_BY_LENDER_MINIMUM: "Raise deal size or find a lower-minimum lender",
    }
    return mapping.get(reason)


def _historical_compatibility(source_id: str | None) -> dict[str, Any]:
    if not source_id:
        return {"level": "NONE", "deals": [], "note": "No primary source"}
    deals = []
    for o in load_outcomes().get("items") or []:
        if o.get("source_id") != source_id:
            continue
        if o.get("status") in {"APPROVED", "FUNDED", "REPAID", "COMPLETED"}:
            deals.append(
                {
                    "opportunity_id": o.get("opportunity_id"),
                    "contract_value": o.get("contract_value"),
                    "status": o.get("status"),
                    "date": o.get("funding_date") or o.get("approval_date") or o.get("created_at"),
                }
            )
    if len(deals) >= 2:
        level = "HIGH"
    elif len(deals) == 1:
        level = "MODERATE"
    else:
        level = "NONE"
    return {
        "level": level,
        "deals": deals[:5],
        "note": "Based only on recorded approvals/funded deals — not a probability model.",
    }


def attach_financing_to_opportunity_row(row: dict[str, Any]) -> dict[str, Any]:
    """Enrich an opportunity dict in-place for ranking / UI."""
    oid = row.get("canonical_id") or row.get("opportunity_id") or row.get("notice_id") or "unknown"
    cv = row.get("estimated_value") or row.get("proposed_bid") or row.get("contract_value") or 0
    sc = row.get("estimated_supplier_cost") or row.get("supplier_cost") or row.get("acquisition_cost")
    fr = row.get("estimated_freight") or row.get("freight") or 0
    if sc in (None, "", "UNKNOWN"):
        card = {
            "status": ST_FINANCING_UNKNOWN,
            "plain_status": "Financing Unknown",
            "status_means": _status_means(ST_FINANCING_UNKNOWN),
            "next_action": "Need supplier acquisition cost before financing can be evaluated",
            "owner_cash_required": None,
            "blocked_reason": BLOCKED_BY_ACQUISITION_UNKNOWN,
            "lender_approval_still_required": True,
        }
        row["financing_intelligence"] = card
        row["financing_status"] = ST_FINANCING_UNKNOWN
        row["financing_rank_penalty"] = 0
        return row

    supplier_terms = row.get("supplier_financing_terms") or row.get("supplier_terms")
    if not supplier_terms and row.get("supplier_commercial_terms"):
        supplier_terms = from_m3_commercial_terms(
            row.get("supplier_commercial_terms"),
            supplier_name=row.get("selected_supplier_name") or row.get("supplier_name"),
        )
    supplier_terms = supplier_terms or {}

    result = assess_opportunity_financing(
        opportunity_id=str(oid),
        contract_value=cv,
        supplier_cost=sc,
        freight=fr,
        supplier_terms=supplier_terms,
        jurisdiction=str(row.get("jurisdiction") or row.get("buyer_type") or "FEDERAL"),
        timing_dates=row.get("financing_timing") or row.get("timing_dates"),
        financed_days=row.get("expected_financed_days"),
        deal_type=str(row.get("deal_type") or "PRODUCT_RESALE"),
        persist=False,
    )
    card = result["card"]
    row["financing_intelligence"] = card
    row["financing_status"] = card.get("status")
    row["profit_after_financing"] = card.get("profit_after_financing")
    status = card.get("status")
    if status == ST_EXECUTION_FAIL:
        row["financing_rank_penalty"] = 50
        row["execution_financing_blocker"] = True
    elif status == ST_FINANCING_GAP:
        row["financing_rank_penalty"] = 30
    elif status == ST_CAPITAL_CONFIRMATION_REQUIRED:
        row["financing_rank_penalty"] = 10
    elif status == ST_FINANCEABLE_BUT_TIMING_RISK:
        row["financing_rank_penalty"] = 5
    elif status == ST_FINANCING_UNKNOWN:
        row["financing_rank_penalty"] = 0
    else:
        # Prefer deals with better post-financing profit
        row["financing_rank_penalty"] = 0
        try:
            # Soft boost via negative penalty when profit after financing is known positive
            paf = money(card.get("profit_after_financing") or 0)
            if paf > 0 and status in {ST_LIKELY_FINANCEABLE, ST_CAPITAL_STACK_CLOSED}:
                row["financing_rank_boost"] = min(20, int(paf / Decimal("5000")))
        except Exception:
            pass
    return row


def blocked_profit_summary() -> dict[str, Any]:
    doc = load_assessments()
    by_reason: dict[str, Decimal] = {}
    items = []
    for oid, payload in (doc.get("by_opportunity") or {}).items():
        card = payload.get("card") or {}
        reason = card.get("blocked_reason")
        profit = card.get("blocked_profit")
        if not reason or profit in (None, ""):
            continue
        # Do not classify unknown/fake profit
        if card.get("status") == ST_FINANCING_UNKNOWN:
            continue
        by_reason[reason] = by_reason.get(reason, Decimal("0")) + money(profit)
        items.append(
            {
                "opportunity_id": oid,
                "reason": reason,
                "primary_blocker": reason,
                "profit": money_str(profit),
                "status": card.get("status"),
                "potential_unlock": card.get("potential_unlock"),
            }
        )
    return {
        "kind": "BlockedProfitByFinancing",
        "build": BUILD,
        "by_reason": {k: money_str(v) for k, v in by_reason.items()},
        "items": items,
        "total_blocked": money_str(sum(by_reason.values(), Decimal("0"))),
        "blocked_opportunities": len(items),
    }
