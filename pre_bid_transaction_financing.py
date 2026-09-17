"""Deal-specific pre-bid transaction financing — requirement, composite, timing, economics."""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from funding_underwriting import (
    FIT_KNOWN_MISMATCH,
    FIT_NEEDS_CONFIRMATION,
    FIT_POSSIBLE_PUBLIC,
    FIT_STRONG_PUBLIC,
    FIT_UNKNOWN,
    GUARANTEE_NO_PG,
    MINIMUM_ACTUAL_PROFIT_USD,
    PREBID_CONDITIONAL_PATH,
    PREBID_NOT_READY,
    PREBID_READY_FOR_REVIEW,
    PREBID_REJECTED,
    PREBID_REVIEW_REQUIRED,
    PREBID_UNKNOWN,
    PREBID_VERIFIED,
    TIMING_AT_RISK,
    TIMING_COMPATIBLE,
    TIMING_INCOMPATIBLE,
    TIMING_UNKNOWN,
    build_personal_credit_model,
    classify_path_underwriting,
    parse_guarantee_state,
)


def _f(v: Any) -> float | None:
    if v is None or v == "" or str(v).upper() == "UNKNOWN":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_transaction_funding_requirement(inputs: dict[str, Any] | None = None) -> dict[str, Any]:
    """
    Funding requirement from transaction evidence. UNKNOWN stays UNKNOWN.
    Never fabricates payment timing.
    """
    x = dict(inputs or {})
    supplier_cost = _f(x.get("supplier_cost") or x.get("product_cost"))
    deposit = _f(x.get("supplier_deposit"))
    balance = _f(x.get("supplier_balance"))
    if balance is None and supplier_cost is not None and deposit is not None:
        balance = supplier_cost - deposit
    freight = _f(x.get("freight"))
    insurance = _f(x.get("insurance"))
    inspection = _f(x.get("inspection"))
    storage = _f(x.get("storage"))
    packaging = _f(x.get("packaging"))
    delivery = _f(x.get("delivery"))
    tax = _f(x.get("tax"))
    other = _f(x.get("other_pre_payment_costs"))

    prepay_components = {
        "supplier_deposit": deposit,
        "supplier_balance": balance,
        "product_cost_if_no_split": supplier_cost if deposit is None and balance is None else None,
        "freight": freight,
        "insurance": insurance,
        "inspection": inspection,
        "storage": storage,
        "packaging": packaging,
        "delivery": delivery,
        "tax": tax,
        "other_pre_payment_costs": other,
    }
    known = [v for v in prepay_components.values() if v is not None]
    # Avoid double-counting product_cost when deposit/balance present
    if deposit is not None or balance is not None:
        known = [
            v
            for k, v in prepay_components.items()
            if v is not None and k != "product_cost_if_no_split"
        ]
    total_prepay = sum(known) if known else None

    timeline = {
        "T0_financing_approval_required": x.get("t0_financing_approval") or x.get("financing_approval_date"),
        "T1_supplier_deposit_due": x.get("t1_deposit_due") or x.get("supplier_deposit_due"),
        "T2_supplier_balance_due": x.get("t2_balance_due") or x.get("supplier_balance_due"),
        "T3_shipment": x.get("t3_shipment") or x.get("shipment_date"),
        "T4_delivery": x.get("t4_delivery") or x.get("delivery_date"),
        "T5_government_acceptance": x.get("t5_acceptance") or x.get("acceptance_date"),
        "T6_invoice_submission": x.get("t6_invoice") or x.get("invoice_date"),
        "T7_expected_government_payment": x.get("t7_gov_payment") or x.get("gov_payment_date"),
        "T8_financier_payoff": x.get("t8_payoff") or x.get("payoff_date"),
    }
    # Never invent dates
    timeline_known = {k: v for k, v in timeline.items() if v not in (None, "", "UNKNOWN")}
    timeline_unknown = [k for k, v in timeline.items() if k not in timeline_known]

    funding_amount = total_prepay
    if funding_amount is None and supplier_cost is not None:
        funding_amount = supplier_cost

    pct_cogs = None
    if supplier_cost and funding_amount is not None and supplier_cost > 0:
        # Supplier portion of funding
        supplier_portion = deposit or 0
        if balance is not None:
            supplier_portion = (deposit or 0) + balance
        elif deposit is None and balance is None:
            supplier_portion = supplier_cost
        pct_cogs = round(100.0 * supplier_portion / supplier_cost, 2)

    return {
        "kind": "TransactionFundingRequirement",
        "inputs": {k: x.get(k) for k in x},
        "prepay_components": prepay_components,
        "maximum_cash_exposure": total_prepay,
        "funding_amount_required": funding_amount,
        "funding_amount_status": "CALCULATED" if known and total_prepay is not None else (
            "ESTIMATED_FROM_SUPPLIER_COST" if funding_amount is not None else "UNKNOWN"
        ),
        "supplier_funding_percentage_required": pct_cogs,
        "funding_duration_days": _f(x.get("funding_duration_days")),
        "receivable_financing_requirement": x.get("receivable_financing_required"),
        "estimated_finance_cost": _f(x.get("estimated_finance_cost")),
        "cash_gap": None,  # filled by composite
        "timeline": timeline,
        "timeline_known": timeline_known,
        "timeline_unknown": timeline_unknown,
        "government_revenue": _f(x.get("government_revenue") or x.get("bid_revenue")),
        "data_class": x.get("data_class") or "PRODUCTION",
        "evaluated_at": now_utc().isoformat(),
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "financier_outreach": 0,
    }


def evaluate_funding_timing_compatibility(
    *,
    supplier_payment_deadline: Any = None,
    financier_funding_available_date: Any = None,
    typical_funding_days: float | int | None = None,
    award_to_funding_days: float | int | None = None,
    days_until_supplier_payment: float | int | None = None,
    application_days: float | int | None = None,
) -> dict[str, Any]:
    """Can money reach supplier before supplier must be paid?"""
    # Prefer explicit day deltas when dates unknown
    supplier_days = _f(days_until_supplier_payment)
    fund_days = _f(typical_funding_days)
    if fund_days is None:
        fund_days = _f(award_to_funding_days)
    app_days = _f(application_days) or 0.0

    if supplier_payment_deadline and financier_funding_available_date:
        # String/ISO compare only if both parseable as dates — leave UNKNOWN if not
        try:
            from datetime import date, datetime

            def _d(v: Any):
                if isinstance(v, datetime):
                    return v.date()
                if isinstance(v, date):
                    return v
                return date.fromisoformat(str(v)[:10])

            sd = _d(supplier_payment_deadline)
            fd = _d(financier_funding_available_date)
            if fd <= sd:
                status = TIMING_COMPATIBLE
            else:
                status = TIMING_INCOMPATIBLE
            return {
                "status": status,
                "supplier_payment_deadline": str(supplier_payment_deadline),
                "financier_funding_available_date": str(financier_funding_available_date),
                "question": "Can the money realistically reach the supplier before the supplier must be paid?",
                "answer": status == TIMING_COMPATIBLE,
                "evaluated_at": now_utc().isoformat(),
            }
        except Exception:
            pass

    if supplier_days is None or fund_days is None:
        return {
            "status": TIMING_UNKNOWN,
            "reason": "insufficient_timing_inputs",
            "supplier_days_until_payment": supplier_days,
            "funding_lead_days": fund_days,
            "application_days": app_days if application_days is not None else None,
            "question": "Can the money realistically reach the supplier before the supplier must be paid?",
            "answer": None,
            "evaluated_at": now_utc().isoformat(),
        }

    total_needed = fund_days + app_days
    if total_needed <= supplier_days - 3:
        status = TIMING_COMPATIBLE
    elif total_needed <= supplier_days:
        status = TIMING_AT_RISK
    else:
        status = TIMING_INCOMPATIBLE

    return {
        "status": status,
        "supplier_days_until_payment": supplier_days,
        "funding_lead_days": fund_days,
        "application_days": app_days,
        "total_days_to_funds": total_needed,
        "question": "Can the money realistically reach the supplier before the supplier must be paid?",
        "answer": True if status == TIMING_COMPATIBLE else (False if status == TIMING_INCOMPATIBLE else None),
        "evaluated_at": now_utc().isoformat(),
    }


def build_composite_funding_path(
    requirement: dict[str, Any],
    components: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Composite funding path. ZERO_CASH_SATISFIED only when uncovered required cash = $0
    with known (not hypothetical) component amounts.
    """
    req_amount = _f(requirement.get("funding_amount_required") or requirement.get("maximum_cash_exposure"))
    rows: list[dict[str, Any]] = []
    covered = 0.0
    fees = 0.0
    all_amounts_known = True

    for c in components or []:
        amt = _f(c.get("amount"))
        fee = _f(c.get("fees") or c.get("fee"))
        if amt is None:
            all_amounts_known = False
        else:
            covered += amt
        if fee is not None:
            fees += fee
        pct = None
        if req_amount and amt is not None and req_amount > 0:
            pct = round(100.0 * amt / req_amount, 2)
        rows.append(
            {
                "component": c.get("component") or c.get("name") or c.get("type"),
                "provider": c.get("provider"),
                "amount": amt,
                "percentage": pct if pct is not None else c.get("percentage"),
                "date_available": c.get("date_available"),
                "date_required": c.get("date_required"),
                "fees": fee,
                "repayment_source": c.get("repayment_source"),
                "evidence_state": c.get("evidence_state") or "UNKNOWN",
                "hypothetical": bool(c.get("hypothetical")),
            }
        )
        if c.get("hypothetical") or (c.get("evidence_state") or "").upper() in {"UNKNOWN", "INFERRED"}:
            if amt is not None and (c.get("hypothetical") or (c.get("evidence_state") or "").upper() == "UNKNOWN"):
                # Unknown/hypothetical financing cannot satisfy zero cash
                all_amounts_known = False

    # Only count non-hypothetical known amounts toward coverage
    real_covered = sum(
        float(r["amount"])
        for r in rows
        if r["amount"] is not None and not r.get("hypothetical") and r.get("evidence_state") not in {"UNKNOWN"}
    )
    # Allow VERIFIED / CALCULATED evidence; if evidence_state omitted but amount given and not hypothetical, count
    real_covered = 0.0
    for r in rows:
        if r.get("hypothetical"):
            continue
        if r["amount"] is None:
            continue
        ev = (r.get("evidence_state") or "UNKNOWN").upper()
        if ev in {"UNKNOWN"} and not r.get("count_even_if_unknown"):
            continue
        real_covered += float(r["amount"])

    gap = None
    zero_cash = False
    if req_amount is not None:
        gap = round(req_amount - real_covered, 2)
        zero_cash = gap <= 0.01 and all(
            not r.get("hypothetical")
            and r["amount"] is not None
            and (r.get("evidence_state") or "").upper() not in {"UNKNOWN"}
            for r in rows
            if r.get("amount") is not None or r.get("hypothetical")
        )
        # Simpler rule: zero cash if gap <= 0 and every covering component has known amount + not hypothetical
        covering = [r for r in rows if r["amount"] is not None and not r.get("hypothetical")]
        if covering and gap is not None and gap <= 0.01:
            if all((r.get("evidence_state") or "").upper() not in {"UNKNOWN", "INFERRED"} for r in covering):
                zero_cash = True
            else:
                zero_cash = False
                # partial known still leaves uncertainty
        elif gap is not None and gap > 0.01:
            zero_cash = False

    return {
        "kind": "CompositeFundingPath",
        "components": rows,
        "required_amount": req_amount,
        "covered_amount": round(real_covered, 2),
        "remaining_uncovered_cash_gap": gap,
        "total_fees": round(fees, 2) if fees else fees,
        "ZERO_CASH_SATISFIED": zero_cash,
        "note": "Hypothetical/UNKNOWN financing does not satisfy zero cash",
        "evaluated_at": now_utc().isoformat(),
    }


def calculate_actual_expected_profit(
    *,
    government_revenue: float | None,
    supplier_cost: float | None,
    freight: float | None = None,
    transaction_expenses: float | None = None,
    po_finance_fees: float | None = None,
    factoring_fees: float | None = None,
    other_finance_fees: float | None = None,
) -> dict[str, Any]:
    """
    ACTUAL_EXPECTED_PROFIT = revenue - supplier - freight - expenses - finance costs.
    Expensive financing is NOT itself a rejection — evaluate resulting profit.
    """
    parts = {
        "government_revenue": _f(government_revenue),
        "supplier_cost": _f(supplier_cost),
        "freight": _f(freight) or 0.0 if freight is not None else None,
        "transaction_expenses": _f(transaction_expenses) or 0.0 if transaction_expenses is not None else None,
        "po_finance_fees": _f(po_finance_fees) or 0.0 if po_finance_fees is not None else None,
        "factoring_fees": _f(factoring_fees) or 0.0 if factoring_fees is not None else None,
        "other_finance_fees": _f(other_finance_fees) or 0.0 if other_finance_fees is not None else None,
    }
    if parts["government_revenue"] is None or parts["supplier_cost"] is None:
        return {
            "actual_expected_profit": None,
            "status": "UNKNOWN",
            "reason": "missing_revenue_or_supplier_cost",
            "components": parts,
            "profit_floor_usd": MINIMUM_ACTUAL_PROFIT_USD,
            "profit_floor_satisfied": None,
            "winning_bid_fabricated": False,
            "evaluated_at": now_utc().isoformat(),
        }

    finance_total = 0.0
    for k in ("po_finance_fees", "factoring_fees", "other_finance_fees"):
        if parts[k] is not None:
            finance_total += parts[k]
    opex = 0.0
    for k in ("freight", "transaction_expenses"):
        if parts[k] is not None:
            opex += parts[k]

    profit = parts["government_revenue"] - parts["supplier_cost"] - opex - finance_total
    return {
        "actual_expected_profit": round(profit, 2),
        "financing_cost_total": round(finance_total, 2),
        "status": "CALCULATED",
        "components": parts,
        "profit_floor_usd": MINIMUM_ACTUAL_PROFIT_USD,
        "profit_floor_satisfied": profit >= MINIMUM_ACTUAL_PROFIT_USD,
        "financing_rejected_for_being_expensive": False,
        "winning_bid_fabricated": False,
        "equation": "revenue - supplier - freight - expenses - finance_fees",
        "evaluated_at": now_utc().isoformat(),
    }


def assess_pre_bid_financing_maturity(
    *,
    exact_product_known: bool = False,
    supplier_cost_known: bool = False,
    government_revenue_defined: bool = False,
    funding_amount_estimable: bool = False,
    freight_known: bool = False,
    deal_specific_confirmation: bool = False,
    public_conditional_path: bool = False,
    path_rejected: bool = False,
) -> dict[str, Any]:
    """Public research alone cannot produce FUNDING_PATH_VERIFIED_PRE_BID."""
    if path_rejected:
        status = PREBID_REJECTED
    elif deal_specific_confirmation:
        status = PREBID_VERIFIED
    elif not (exact_product_known and supplier_cost_known and funding_amount_estimable):
        status = PREBID_NOT_READY
    elif public_conditional_path and exact_product_known and supplier_cost_known and government_revenue_defined:
        status = PREBID_CONDITIONAL_PATH
    elif exact_product_known and supplier_cost_known and government_revenue_defined and funding_amount_estimable:
        status = PREBID_READY_FOR_REVIEW
    elif exact_product_known and supplier_cost_known:
        status = PREBID_REVIEW_REQUIRED
    else:
        status = PREBID_UNKNOWN

    outreach_appropriate = status in {
        PREBID_READY_FOR_REVIEW,
        PREBID_REVIEW_REQUIRED,
        PREBID_CONDITIONAL_PATH,
    } and funding_amount_estimable and supplier_cost_known and exact_product_known

    return {
        "pre_bid_financing_status": status,
        "financier_outreach_appropriate": outreach_appropriate,
        "public_research_cannot_verify_funding": True,
        "verified_pre_bid_requires_deal_specific_confirmation": True,
        "prerequisites": {
            "exact_product_known": exact_product_known,
            "supplier_cost_known": supplier_cost_known,
            "government_revenue_defined": government_revenue_defined,
            "funding_amount_estimable": funding_amount_estimable,
            "freight_known": freight_known,
        },
        "evaluated_at": now_utc().isoformat(),
        "financier_outreach": 0,
    }


def build_financier_packet(
    *,
    deal: dict[str, Any],
    provider: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deal-specific packet for a candidate financier. UNKNOWN preserved; never fabricate."""

    def u(key: str, *alts: str) -> Any:
        for k in (key, *alts):
            v = deal.get(k)
            if v is not None and v != "":
                return v
        return "UNKNOWN"

    return {
        "kind": "PreBidFinancierPacket",
        "provider_name": (provider or {}).get("source_name") or (provider or {}).get("organization") or "UNKNOWN",
        "company_entity": u("company_entity", "entity"),
        "solicitation_number": u("solicitation_number"),
        "agency_customer": u("agency", "customer"),
        "product": u("product", "product_description"),
        "quantity": u("quantity"),
        "solicitation_deadline": u("solicitation_deadline", "deadline"),
        "expected_award_timing": u("expected_award_timing"),
        "supplier": u("supplier"),
        "supplier_quote": u("supplier_quote", "supplier_cost"),
        "supplier_payment_terms": u("supplier_payment_terms"),
        "supplier_cost": u("supplier_cost"),
        "freight": u("freight"),
        "total_funding_requirement": u("funding_amount_required", "total_funding_requirement"),
        "proposed_government_price": u("proposed_government_price", "government_revenue"),
        "gross_spread": u("gross_spread"),
        "estimated_financing_duration": u("funding_duration_days", "estimated_financing_duration"),
        "estimated_finance_fees_range": u("estimated_finance_fees"),
        "estimated_profit_after_financing": u("actual_expected_profit"),
        "delivery_requirements": u("delivery_requirements"),
        "government_payment_terms": u("government_payment_terms"),
        "required_funding_date": u("required_funding_date"),
        "repayment_source": u("repayment_source"),
        "unknown_fields": [
            k
            for k, v in {
                "supplier_quote": deal.get("supplier_quote") or deal.get("supplier_cost"),
                "freight": deal.get("freight"),
                "funding_amount": deal.get("funding_amount_required"),
                "proposed_price": deal.get("proposed_government_price") or deal.get("government_revenue"),
            }.items()
            if v is None
        ],
        "fabricated": False,
        "funding_secured": False,
        "generated_at": now_utc().isoformat(),
        "financier_outreach": 0,
    }


CALL_SHEET_QUESTIONS = [
    ("transaction_type_eligible", "Is this transaction type eligible?"),
    ("pre_bid_review", "Can you review it before I submit the bid?"),
    ("conditional_indication", "If the transaction otherwise qualifies, can you provide a conditional indication subject to award?"),
    ("personal_credit_pull", "Do you pull personal credit?"),
    ("hard_or_soft_pull", "Hard or soft pull?"),
    ("minimum_fico", "Is there a minimum FICO?"),
    ("credit_material_to_approval", "Does personal credit materially affect approval?"),
    ("fico_480_disqualify", "Would a personal score around 480 automatically disqualify the transaction?"),
    ("personal_guarantee_required", "Is a personal guarantee required?"),
    ("pg_type_scope", "What type/scope of PG?"),
    ("owner_cash_contribution", "Is any owner cash contribution required?"),
    ("finance_100_pct_supplier", "Can you finance 100% of supplier cost?"),
    ("other_costs_financeable", "What costs besides supplier invoice can be financed?"),
    ("pay_supplier_directly", "Do you pay the supplier directly?"),
    ("startup_eligible", "Are startups/new entities eligible?"),
    ("first_gov_contract", "Can this be the company's first government contract?"),
    ("prior_gov_performance", "Is prior government performance required?"),
    ("minimum_gross_margin", "What minimum gross margin is required?"),
    ("transaction_size_limits", "What transaction-size limits apply?"),
    ("funding_speed_after_award", "How quickly can funding reach the supplier after award?"),
    ("documents_required", "What documents are required?"),
    ("receivable_factoring_required", "Is receivable factoring required after delivery?"),
    ("all_fees", "What are all fees?"),
    ("minimum_duration_fees", "Are there minimum-duration fees?"),
    ("ucc_lien", "Are there UCC/lien requirements?"),
    ("assignment_payment_direction", "Is assignment/payment direction required?"),
    ("recourse_or_nonrecourse", "Is the financing recourse or non-recourse?"),
    ("conditional_capability_letter", "Can you issue a conditional funding/capability letter before bid if appropriate?"),
]


def build_financier_call_sheet(
    *,
    provider: dict[str, Any] | None = None,
    deal: dict[str, Any] | None = None,
    verified_current_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Concise operator call sheet. Skip questions already VERIFIED_CURRENT unless deal-specific."""
    verified = verified_current_keys or set()
    # Map question keys to provider criterion names that may already be verified
    skip_map = {
        "personal_credit_pull": "personal_credit_checked",
        "minimum_fico": "minimum_personal_fico",
        "personal_guarantee_required": "personal_guarantee",
        "owner_cash_contribution": "borrower_cash_contribution_required",
        "startup_eligible": "startup_allowed",
        "first_gov_contract": "first_government_contract_allowed",
        "pay_supplier_directly": "direct_supplier_payment_supported",
        "minimum_gross_margin": "minimum_margin_pct",
        "transaction_size_limits": "minimum_transaction",
    }
    questions = []
    for key, text in CALL_SHEET_QUESTIONS:
        crit = skip_map.get(key)
        if crit and crit in verified:
            # Still ask deal-specific variants for credit/PG/timing that can differ by deal
            if key not in {
                "fico_480_disqualify",
                "credit_material_to_approval",
                "pre_bid_review",
                "conditional_indication",
                "funding_speed_after_award",
                "finance_100_pct_supplier",
            }:
                continue
        questions.append({"key": key, "question": text})

    opening = (
        "I'm preparing a bid on a government procurement and I'm establishing the funding path "
        "before submitting it. If we're awarded, we would need financing to pay the supplier during "
        "the period between purchasing the goods and receiving payment from the government. I can "
        "provide the solicitation, supplier quote, expected contract amount, delivery requirements, "
        "and transaction economics."
    )
    return {
        "kind": "FinancierCallSheet",
        "provider_name": (provider or {}).get("source_name") or (provider or {}).get("organization"),
        "solicitation_number": (deal or {}).get("solicitation_number"),
        "opening_script": opening,
        "questions": questions,
        "personal_credit_and_pg_asked_separately": True,
        "public_marketing_is_not_deal_approval": True,
        "generated_at": now_utc().isoformat(),
        "financier_outreach": 0,
        "autonomous_call": False,
    }


def ingest_financier_call_outcome(outcome: dict[str, Any]) -> dict[str, Any]:
    """Record deal-specific call outcome. Distinguishes PUBLIC_EVIDENCE vs PROVIDER_CONFIRMED_DEAL_SPECIFIC."""
    out = dict(outcome or {})
    out.setdefault("recorded_at", now_utc().isoformat())
    out["evidence_class"] = "PROVIDER_CONFIRMED_DEAL_SPECIFIC"
    out["outranks_public_marketing"] = True
    out["funding_secured"] = bool(out.get("explicitly_confirmed_secured")) and out.get(
        "conditional_indication"
    ) is not False
    if not out.get("explicitly_confirmed_secured"):
        out["funding_secured"] = False
    out["financier_outreach"] = 1 if out.get("contact_occurred") else 0
    # Structured answers preserved as provided
    out.setdefault("structured_answers", {})
    out.setdefault("verbatim_notes", out.get("notes"))
    return out


def build_operator_pg_review(
    *,
    guarantee: dict[str, Any] | None = None,
    transaction_profit: float | None = None,
    financing_duration: Any = None,
    government_customer: str | None = None,
    supplier_risk: str | None = None,
    delivery_risk: str | None = None,
    acceptance_risk: str | None = None,
    return_cancellation_risk: str | None = None,
    guaranteed_amount: float | None = None,
    recourse_terms: str | None = None,
) -> dict[str, Any]:
    from funding_underwriting import empty_personal_guarantee_model

    g = guarantee or empty_personal_guarantee_model()
    return {
        "status": "OPERATOR_PG_REVIEW_REQUIRED",
        "guarantee": g,
        "amount_potentially_guaranteed": guaranteed_amount,
        "transaction_profit": transaction_profit,
        "financing_duration": financing_duration,
        "government_customer": government_customer,
        "supplier_risk": supplier_risk or "UNKNOWN",
        "delivery_risk": delivery_risk or "UNKNOWN",
        "acceptance_risk": acceptance_risk or "UNKNOWN",
        "return_cancellation_risk": return_cancellation_risk or "UNKNOWN",
        "recourse_terms": recourse_terms or "UNKNOWN",
        "maximum_identified_personal_exposure": guaranteed_amount,
        "unknown_legal_terms": True,
        "operator_decision_required": True,
        "autonomous_decision": False,
        "legal_advice": False,
        "note": "M3 does not accept or reject the PG. Operator risk decision only.",
        "evaluated_at": now_utc().isoformat(),
    }


def rank_provider_public_fit(
    provider: dict[str, Any],
    *,
    deal: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Public fit ranking — never fake approval probabilities; never 'best' from marketing."""
    d = deal or {}
    reasons: list[str] = []
    mismatches: list[str] = []

    # Known mismatches from verified criteria
    uw = classify_path_underwriting(
        provider,
        operator_personal_fico=_f(d.get("operator_personal_fico")) or 480,
        transaction_amount=_f(d.get("funding_amount_required") or d.get("supplier_cost")),
        funding_timing_status=d.get("funding_timing_status"),
        actual_expected_profit=_f(d.get("actual_expected_profit")),
        uncovered_operator_cash=_f(d.get("uncovered_operator_cash")),
    )
    if uw["path_class"] == "REJECT":
        fit = FIT_KNOWN_MISMATCH
        mismatches.extend(uw.get("reject_reasons") or [])
    elif provider.get("product_resale_fit") == "STRONG" and provider.get("government_customer_types_supported"):
        fit = FIT_STRONG_PUBLIC
        reasons.append("strong_resale_and_gov_customer_support_public")
    elif provider.get("path_hints_transaction_based") or provider.get("product_resale_fit") in {"POSSIBLE", "STRONG"}:
        fit = FIT_POSSIBLE_PUBLIC
        reasons.append("possible_public_transaction_fit")
    elif uw["path_class"] == "NEEDS_VERIFICATION":
        fit = FIT_NEEDS_CONFIRMATION
    else:
        fit = FIT_UNKNOWN

    return {
        "fit_state": fit,
        "reasons": reasons,
        "mismatches": mismatches,
        "underwriting_path_class": uw.get("path_class"),
        "is_approval": False,
        "is_verified_funding": False,
        "public_fit_is_not_deal_approval": True,
        "provider_name": provider.get("source_name") or provider.get("organization"),
        "evaluated_at": now_utc().isoformat(),
    }
