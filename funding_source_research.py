"""Production funding-source research seed, launch-fit, call queue, export.

Public research evidence from official sites (2026-09-15). No applications,
no OpenAI/SAM/USAspending/paid APIs. Ranking ≠ approval.
"""

from __future__ import annotations
from application_clock import now_utc

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from funding_path_constants import (
    CALL_AFTER_FIRST_AWARD,
    CALL_DO_NOT_CALL_NOW,
    CALL_LOW_PRIORITY,
    CALL_NOW,
    DATA_PRODUCTION,
    EV_VERIFIED_PUBLIC,
    FIT_FUTURE_MATCH,
    FIT_NEEDS_VERIFICATION,
    FIT_NOT_APPLICABLE,
    FIT_POTENTIAL_MATCH,
    FIT_REJECT_NOW,
    HARD_POLICY,
    PG_CONDITIONAL,
    PG_NOT_REQUIRED,
    PG_REQUIRED,
    PG_UNKNOWN,
    RESALE_NO,
    RESALE_POSSIBLE,
    RESALE_STRONG,
    RESALE_UNKNOWN,
    RESALE_WEAK,
    ROLE_BROKER,
    ROLE_DIRECT_FUNDER,
    ROLE_UNKNOWN,
)
from funding_source_kb import (
    criterion_evidence,
    empty_funding_source_profile,
    set_criterion,
)

VERIFIED_AT = "2026-09-15T00:00:00+00:00"
RESEARCH_HTTP_USED = 28  # public GET/search fetches this pass (budget max 40)

CRITICAL_UNKNOWN_KEYS = (
    "personal_guarantee",
    "personal_credit_checked",
    "personal_credit_used_for_approval",
    "minimum_personal_fico",
    "borrower_cash_contribution_required",
    "new_entity_allowed",
    "startup_allowed",
    "first_government_contract_allowed",
    "minimum_time_in_business",
    "minimum_annual_revenue",
    "minimum_transaction",
    "maximum_transaction",
    "minimum_margin_pct",
    "maximum_supplier_cost_coverage_pct",
    "direct_supplier_payment_supported",
    "direct_ship_supported",
    "government_customer_types_supported",
    "pre_bid_screening_available",
    "assignment_of_claims_required",
)


def _vp(
    name: str,
    value: Any,
    *,
    url: str,
    title: str,
    quote: str,
) -> dict[str, Any]:
    return criterion_evidence(
        criterion_name=name,
        value=value,
        verification_status=EV_VERIFIED_PUBLIC,
        source_url=url,
        source_title=title,
        source_quote_or_summary=quote,
        verified_at=VERIFIED_AT,
        confidence="HIGH",
    )


def _profile(
    *,
    org: str,
    program: str,
    role: str,
    website: str,
    phone: str | None,
    email: str | None,
    ask_for: str,
    path_type: str,
    parent_org_id: str | None = None,
    program_id: str | None = None,
) -> dict[str, Any]:
    p = empty_funding_source_profile(data_class=DATA_PRODUCTION)
    p.update(
        {
            "organization_name": org,
            "source_name": org,
            "program_name": program,
            "program_id": program_id or f"{org}|{program}",
            "parent_organization_id": parent_org_id or org,
            "funding_provider_role": role,
            "source_type": path_type,
            "website": website,
            "general_phone": phone,
            "government_contract_phone": phone,
            "contact_email": email,
            "preferred_contact_name": None,  # never invent names
            "preferred_contact_title": ask_for,
            "preferred_department": ask_for,
            "who_to_ask_for": ask_for,
            "named_contact_invented": False,
            "last_verified_at": VERIFIED_AT,
            "research_notes": [],
            "product_resale_fit": RESALE_UNKNOWN,
            "product_resale_fit_evidence": None,
            "maximum_supplier_cost_coverage_pct": None,
            "zero_cash_execution_possible": None,
            "borrower_gap_required": None,
            "launch_fit": None,
            "call_priority": None,
            "approved": False,  # ranking never means approved
            "funding_secured": False,
            "LIVE_API_REQUESTS": 0,
            "OpenAI": 0,
            "SAM": 0,
            "USAspending": 0,
            "paid": 0,
            "applications_submitted": 0,
        }
    )
    return p


def _set(p: dict[str, Any], name: str, value: Any, ev: dict[str, Any]) -> dict[str, Any]:
    out = set_criterion(p, name, value, ev)
    out[name] = value
    return out


def launch_company_profile() -> dict[str, Any]:
    """Policy-level launch company facts — no sensitive personal numbers."""
    return {
        "personal_cash_available_for_transaction": 0,
        "personal_guarantee_allowed": False,
        "personal_credit_dependency_allowed": False,
        "established_business_credit": False,
        "operating_history": "minimal_new_entity",
        "historical_operating_revenue": "none_or_minimal",
        "government_contract_performance_history": "none",
        "company_is_new_entity": True,
        "is_first_government_contract": True,
        "time_in_business_years": 0,
        "annual_revenue": 0,
        "expected_early_transaction_size": 150000,
        "hard_policy": dict(HARD_POLICY),
    }


def evaluate_zero_cash(
    *,
    coverage_pct: float | None,
    supplier_cost: float = 100000.0,
    other_upfront_cash_required: float = 0.0,
    gap_covered_by_other_verified_component: bool = False,
) -> dict[str, Any]:
    """90% coverage leaves 10% gap → not zero-cash unless gap financed elsewhere."""
    if coverage_pct is None:
        return {
            "maximum_supplier_cost_coverage_pct": None,
            "borrower_gap_required": None,
            "gap_can_be_financed": None,
            "zero_cash_execution_possible": None,
            "status": "NEEDS_VERIFICATION",
        }
    covered = supplier_cost * (float(coverage_pct) / 100.0)
    gap = max(0.0, supplier_cost - covered) + float(other_upfront_cash_required or 0)
    zero = gap <= 0 or (gap > 0 and gap_covered_by_other_verified_component)
    return {
        "maximum_supplier_cost_coverage_pct": coverage_pct,
        "supplier_cost_assumed": supplier_cost,
        "amount_covered": covered,
        "borrower_gap_required": gap,
        "gap_can_be_financed": gap_covered_by_other_verified_component if gap > 0 else True,
        "zero_cash_execution_possible": zero,
        "status": "PASS" if zero else "HARD_FAIL_GAP",
    }


def critical_unknown_matrix(profile: dict[str, Any]) -> dict[str, str]:
    matrix: dict[str, str] = {}
    evs = profile.get("criteria_evidence") or {}
    for key in CRITICAL_UNKNOWN_KEYS:
        ev = evs.get(key)
        if isinstance(ev, dict) and ev.get("verification_status") == EV_VERIFIED_PUBLIC:
            matrix[key] = f"KNOWN:{ev.get('value')}"
        elif profile.get(key) is not None and key in (
            "product_resale_fit",
        ):
            matrix[key] = f"KNOWN:{profile.get(key)}"
        else:
            val = profile.get(key)
            if val is not None and isinstance(ev, dict) and ev.get("is_verified"):
                matrix[key] = f"KNOWN:{val}"
            elif val is not None and key in profile and isinstance(ev, dict):
                matrix[key] = f"KNOWN:{val}" if ev.get("is_verified") else f"UNVERIFIED:{val}"
            elif val is not None and (profile.get("criteria_evidence") or {}).get(key):
                st = ((profile.get("criteria_evidence") or {}).get(key) or {}).get("verification_status")
                matrix[key] = f"KNOWN:{val}" if st == EV_VERIFIED_PUBLIC else f"UNVERIFIED:{val}"
            elif val is not None and key in {
                "maximum_supplier_cost_coverage_pct",
            }:
                matrix[key] = f"UNVERIFIED:{val}" if not isinstance(ev, dict) else (
                    f"KNOWN:{val}" if ev.get("verification_status") == EV_VERIFIED_PUBLIC else f"UNVERIFIED:{val}"
                )
            else:
                matrix[key] = "UNKNOWN"
    # Force UNKNOWN when no evidence
    for key in CRITICAL_UNKNOWN_KEYS:
        ev = evs.get(key)
        if not isinstance(ev, dict) or ev.get("verification_status") != EV_VERIFIED_PUBLIC:
            if matrix.get(key, "UNKNOWN").startswith("KNOWN:"):
                continue
            if isinstance(ev, dict) and ev.get("verification_status") == EV_VERIFIED_PUBLIC:
                continue
            # If set via _set with VERIFIED_PUBLIC it should already be KNOWN
            if isinstance(ev, dict) and ev.get("is_verified") and ev.get("verification_status") == EV_VERIFIED_PUBLIC:
                matrix[key] = f"KNOWN:{ev.get('value')}"
            elif profile.get(key) is not None and isinstance(ev, dict) and ev.get("is_verified"):
                matrix[key] = f"KNOWN:{profile.get(key)}"
            elif profile.get(key) is not None and isinstance(ev, dict):
                matrix[key] = "UNKNOWN" if not ev.get("is_verified") else f"KNOWN:{profile.get(key)}"
            else:
                matrix[key] = "UNKNOWN"
    # Cleaner pass
    out: dict[str, str] = {}
    for key in CRITICAL_UNKNOWN_KEYS:
        ev = evs.get(key)
        if isinstance(ev, dict) and ev.get("verification_status") == EV_VERIFIED_PUBLIC:
            out[key] = f"KNOWN:{ev.get('value')}"
        else:
            out[key] = "UNKNOWN"
    return out


def classify_launch_fit(profile: dict[str, Any], company: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deterministic launch fit — not approval."""
    co = company or launch_company_profile()
    reasons: list[str] = []
    future_reasons: list[str] = []
    unknowns = critical_unknown_matrix(profile)

    # Hard rejects from VERIFIED public criteria vs launch facts
    tib = profile.get("minimum_time_in_business")
    ev_tib = (profile.get("criteria_evidence") or {}).get("minimum_time_in_business")
    if (
        tib is not None
        and isinstance(ev_tib, dict)
        and ev_tib.get("verification_status") == EV_VERIFIED_PUBLIC
        and float(tib) > float(co.get("time_in_business_years") or 0)
    ):
        reasons.append(f"minimum_time_in_business={tib} exceeds launch history")

    if profile.get("startup_allowed") is False and (
        ((profile.get("criteria_evidence") or {}).get("startup_allowed") or {}).get("verification_status")
        == EV_VERIFIED_PUBLIC
    ):
        reasons.append("startup_allowed=False")

    if profile.get("new_entity_allowed") is False and (
        ((profile.get("criteria_evidence") or {}).get("new_entity_allowed") or {}).get("verification_status")
        == EV_VERIFIED_PUBLIC
    ):
        reasons.append("new_entity_allowed=False")

    if profile.get("first_government_contract_allowed") is False and (
        ((profile.get("criteria_evidence") or {}).get("first_government_contract_allowed") or {}).get(
            "verification_status"
        )
        == EV_VERIFIED_PUBLIC
    ):
        reasons.append("first_government_contract_allowed=False")

    rev = profile.get("minimum_annual_revenue") or profile.get("minimum_historical_revenue")
    ev_rev = (profile.get("criteria_evidence") or {}).get("minimum_annual_revenue") or (
        profile.get("criteria_evidence") or {}
    ).get("minimum_historical_revenue")
    if (
        rev is not None
        and isinstance(ev_rev, dict)
        and ev_rev.get("verification_status") == EV_VERIFIED_PUBLIC
        and float(rev) > float(co.get("annual_revenue") or 0)
    ):
        reasons.append(f"minimum_revenue={rev} exceeds launch revenue")

    # Recurring revenue / prior factoring / experience requirements
    if profile.get("requires_prior_factoring_relationship") is True:
        reasons.append("requires_prior_factoring_relationship")
    if profile.get("requires_recurring_weekly_revenues") is True:
        reasons.append("requires_recurring_weekly_revenues")
    if profile.get("requires_prior_client_experience") is True:
        reasons.append("requires_prior_client_experience")

    mn = profile.get("minimum_transaction") or profile.get("minimum_funding_amount")
    ev_mn = (profile.get("criteria_evidence") or {}).get("minimum_transaction") or (
        profile.get("criteria_evidence") or {}
    ).get("minimum_funding_amount")
    early = float(co.get("expected_early_transaction_size") or 150000)
    if (
        mn is not None
        and isinstance(ev_mn, dict)
        and ev_mn.get("verification_status") == EV_VERIFIED_PUBLIC
        and float(mn) > early * 5  # clearly beyond early awards (e.g. $4M+)
    ):
        future_reasons.append(f"minimum_funding/transaction={mn} far above early deals")
        reasons.append(f"transaction_size_floor_too_high:{mn}")

    if profile.get("personal_guarantee") == PG_REQUIRED and (
        ((profile.get("criteria_evidence") or {}).get("personal_guarantee") or {}).get("verification_status")
        == EV_VERIFIED_PUBLIC
    ):
        reasons.append("personal_guarantee_REQUIRED")

    if profile.get("borrower_cash_contribution_required") is True and (
        ((profile.get("criteria_evidence") or {}).get("borrower_cash_contribution_required") or {}).get(
            "verification_status"
        )
        == EV_VERIFIED_PUBLIC
    ):
        reasons.append("borrower_cash_contribution_required")

    if profile.get("personal_credit_used_for_approval") is True and (
        ((profile.get("criteria_evidence") or {}).get("personal_credit_used_for_approval") or {}).get(
            "verification_status"
        )
        == EV_VERIFIED_PUBLIC
    ):
        reasons.append("personal_credit_dependent_approval")

    zc = evaluate_zero_cash(coverage_pct=profile.get("maximum_supplier_cost_coverage_pct"))
    if zc["status"] == "HARD_FAIL_GAP":
        reasons.append("zero_cash_gap_uncovered")

    if profile.get("product_resale_fit") == RESALE_NO:
        return {
            "launch_fit": FIT_NOT_APPLICABLE,
            "reasons": ["product_resale_fit=NO"],
            "unknowns": unknowns,
            "zero_cash": zc,
            "approved": False,
            "funding_secured": False,
        }

    if reasons and any(
        r.startswith("minimum_time_in_business")
        or r.startswith("requires_")
        or r.startswith("startup_allowed")
        or r.startswith("new_entity")
        or r.startswith("first_government")
        or r.startswith("minimum_revenue")
        or r.startswith("personal_guarantee_REQUIRED")
        or r.startswith("borrower_cash")
        or r.startswith("personal_credit_dependent")
        or r.startswith("transaction_size_floor")
        for r in reasons
    ):
        # Size-only may be FUTURE; history/factoring = REJECT_NOW
        if all(r.startswith("transaction_size_floor") for r in reasons) or (
            any(r.startswith("transaction_size_floor") for r in reasons)
            and all(
                r.startswith("transaction_size_floor") or r in future_reasons
                for r in reasons
            )
        ):
            fit = FIT_FUTURE_MATCH
        elif any(
            x in " ".join(reasons)
            for x in (
                "minimum_time_in_business",
                "requires_prior",
                "requires_recurring",
                "startup_allowed=False",
                "new_entity_allowed=False",
                "first_government_contract_allowed=False",
                "minimum_revenue",
                "personal_guarantee_REQUIRED",
                "borrower_cash",
                "personal_credit_dependent",
            )
        ):
            fit = FIT_REJECT_NOW
        else:
            fit = FIT_FUTURE_MATCH
        return {
            "launch_fit": fit,
            "reasons": reasons,
            "future_reasons": future_reasons,
            "unknowns": unknowns,
            "zero_cash": zc,
            "approved": False,
            "funding_secured": False,
        }

    # Critical unknowns around hard constraints → NEEDS_VERIFICATION
    critical_u = [
        k
        for k in (
            "personal_guarantee",
            "borrower_cash_contribution_required",
            "personal_credit_used_for_approval",
            "first_government_contract_allowed",
            "new_entity_allowed",
            "maximum_supplier_cost_coverage_pct",
        )
        if unknowns.get(k) == "UNKNOWN"
    ]
    if critical_u:
        # If public evidence shows compatible signals (no PG claim, no personal credit pull)
        # still NEEDS_VERIFICATION until call confirms first-contract/cash/coverage
        compatible_signals = (
            profile.get("personal_guarantee") in {PG_NOT_REQUIRED, PG_CONDITIONAL}
            or profile.get("personal_credit_checked") is False
        )
        fit = FIT_NEEDS_VERIFICATION
        if (
            compatible_signals
            and profile.get("personal_guarantee") == PG_NOT_REQUIRED
            and profile.get("personal_credit_checked") is False
            and not critical_u
        ):
            fit = FIT_POTENTIAL_MATCH
        return {
            "launch_fit": fit,
            "reasons": [f"critical_unknown:{u}" for u in critical_u],
            "unknowns": unknowns,
            "zero_cash": zc,
            "compatible_public_signals": compatible_signals,
            "approved": False,
            "funding_secured": False,
        }

    return {
        "launch_fit": FIT_POTENTIAL_MATCH,
        "reasons": ["verified_criteria_compatible_pending_transaction_approval"],
        "unknowns": unknowns,
        "zero_cash": zc,
        "approved": False,
        "funding_secured": False,
        "note": "POTENTIAL_MATCH is not APPROVED and not FUNDING_SECURED",
    }


def assign_call_priority(profile: dict[str, Any], fit: dict[str, Any]) -> str:
    lf = fit.get("launch_fit")
    if lf == FIT_REJECT_NOW:
        return CALL_DO_NOT_CALL_NOW
    if lf == FIT_NOT_APPLICABLE:
        return CALL_DO_NOT_CALL_NOW
    if lf == FIT_FUTURE_MATCH:
        return CALL_AFTER_FIRST_AWARD
    # Thin discovery candidates without phone / without official criteria → low priority
    if not profile.get("general_phone") and not any(
        isinstance(ev, dict) and ev.get("verification_status") == EV_VERIFIED_PUBLIC
        for ev in (profile.get("criteria_evidence") or {}).values()
    ):
        return CALL_LOW_PRIORITY
    resale = profile.get("product_resale_fit")
    if lf in {FIT_NEEDS_VERIFICATION, FIT_POTENTIAL_MATCH}:
        if resale == RESALE_WEAK and profile.get("post_award_only"):
            return CALL_AFTER_FIRST_AWARD
        if resale in {RESALE_STRONG, RESALE_POSSIBLE, RESALE_UNKNOWN}:
            if profile.get("general_phone"):
                return CALL_NOW
            return CALL_LOW_PRIORITY
        if resale == RESALE_WEAK:
            return CALL_LOW_PRIORITY
    return CALL_LOW_PRIORITY


# --- Production program seeds -------------------------------------------------


def _seed_programs() -> list[dict[str, Any]]:
    programs: list[dict[str, Any]] = []

    # A. ELINT CAPITAL
    elint_url = "https://www.elintcapital.com"
    elint = _profile(
        org="Elint Capital",
        program="Contract-Backed Federal Working Capital",
        role=ROLE_DIRECT_FUNDER,
        website=elint_url,
        phone="(202) 988-4468",
        email="info@elintcapital.com",
        ask_for="Government Contract Finance Specialist",
        path_type="CONTRACT_FINANCING_PRIVATE",
    )
    elint = _set(
        elint,
        "personal_guarantee",
        PG_NOT_REQUIRED,
        _vp(
            "personal_guarantee",
            PG_NOT_REQUIRED,
            url=elint_url,
            title="Elint Capital — Embedded Financing for Government Contractors",
            quote="No personal guarantees or blanket liens",
        ),
    )
    elint = _set(
        elint,
        "personal_credit_checked",
        False,
        _vp(
            "personal_credit_checked",
            False,
            url=elint_url,
            title="Elint Capital",
            quote="No personal credit pull — ever; Prequalify … no credit pull",
        ),
    )
    elint = _set(
        elint,
        "personal_credit_used_for_approval",
        False,
        _vp(
            "personal_credit_used_for_approval",
            False,
            url=elint_url,
            title="Elint Capital",
            quote="underwriting your contracts — not your personal credit",
        ),
    )
    elint = _set(
        elint,
        "government_customer_types_supported",
        ["federal"],
        _vp(
            "government_customer_types_supported",
            ["federal"],
            url=elint_url,
            title="Elint Capital",
            quote="purpose-built for federal contracts; active federal awards as collateral",
        ),
    )
    elint = _set(
        elint,
        "typical_approval_days",
        2,
        _vp(
            "typical_approval_days",
            2,
            url=elint_url,
            title="Elint Capital",
            quote="Approval in as few as 48 hours",
        ),
    )
    elint["product_resale_fit"] = RESALE_UNKNOWN
    elint["product_resale_fit_evidence"] = {
        "status": "UNKNOWN",
        "note": "Public materials emphasize federal contract working capital; product-resale/goods PO suitability not stated",
        "url": elint_url,
    }
    elint["path_hints_transaction_based"] = True
    elint["official_urls_reviewed"] = [elint_url]
    programs.append(elint)

    # B. Commerce Commercial Credit — PO Funding
    ccc_url = "https://www.commercecommercialcredit.com/financial-services/purchase-order-funding"
    ccc = _profile(
        org="Commerce Commercial Credit",
        program="Purchase Order Funding",
        role=ROLE_DIRECT_FUNDER,
        website=ccc_url,
        phone="1-800-928-7004",
        email=None,
        ask_for="Purchase Order Finance Underwriter",
        path_type="PO_FINANCING",
    )
    for name, val, q in [
        ("minimum_time_in_business", 1, "Must be in business for at least one year."),
        ("minimum_transaction", 100000, "Must have at least an initial $100,000 transaction minimum."),
        ("minimum_margin_pct", 25, "Must retain a minimum of 25% profit."),
    ]:
        ccc = _set(ccc, name, val, _vp(name, val, url=ccc_url, title="Purchase Order Funding | Commerce Commercial Credit", quote=q))
    ccc["requires_prior_factoring_relationship"] = True
    ccc["criteria_evidence"]["requires_prior_factoring_relationship"] = _vp(
        "requires_prior_factoring_relationship",
        True,
        url=ccc_url,
        title="Purchase Order Funding | Commerce Commercial Credit",
        quote="6 Months minimum factoring receivables with Commerce Commercial Credit.",
    )
    ccc["requires_prior_client_experience"] = True
    ccc["criteria_evidence"]["requires_prior_client_experience"] = _vp(
        "requires_prior_client_experience",
        True,
        url=ccc_url,
        title="Purchase Order Funding | Commerce Commercial Credit",
        quote="Must have experience and previous transactions with client or other similar clients.",
    )
    ccc = _set(
        ccc,
        "direct_ship_supported",
        True,
        _vp(
            "direct_ship_supported",
            True,
            url=ccc_url,
            title="Purchase Order Funding | Commerce Commercial Credit",
            quote="Direct shipment/Drop ship orders",
        ),
    )
    ccc = _set(
        ccc,
        "government_customer_types_supported",
        ["federal", "state", "local"],
        _vp(
            "government_customer_types_supported",
            ["federal", "state", "local"],
            url=ccc_url,
            title="Purchase Order Funding | Commerce Commercial Credit",
            quote="Government Contracts listed among considered funding types",
        ),
    )
    ccc["product_resale_fit"] = RESALE_STRONG
    ccc["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "Wholesale distributors; Finished Goods; Direct shipment/Drop ship; Government Contracts",
        "url": ccc_url,
    }
    ccc["official_urls_reviewed"] = [ccc_url]
    # PG / cash / personal credit remain UNKNOWN
    programs.append(ccc)

    # C. 1st Commercial Credit — PO Financing (separate from gov recurring program)
    fcc_url = "https://www.1stcommercialcredit.com/financial-services/purchase-order-financing"
    fcc = _profile(
        org="1st Commercial Credit",
        program="Purchase Order Financing",
        role=ROLE_DIRECT_FUNDER,
        website=fcc_url,
        phone="1-800-876-6071",
        email=None,
        ask_for="Purchase Order Finance Underwriter",
        path_type="PO_FINANCING",
        parent_org_id="1st Commercial Credit",
    )
    for name, val, q in [
        ("minimum_time_in_business", 1, "Must be in business for at least one year with tax returns."),
        ("minimum_transaction", 100000, "Must have at least an initial $100,000 transaction minimum."),
        ("minimum_margin_pct", 25, "Must retain a minimum of 25% profit."),
    ]:
        fcc = _set(fcc, name, val, _vp(name, val, url=fcc_url, title="Purchase Order (PO) Financing | 1st Commercial Credit", quote=q))
    fcc["requires_prior_factoring_relationship"] = True
    fcc["criteria_evidence"]["requires_prior_factoring_relationship"] = _vp(
        "requires_prior_factoring_relationship",
        True,
        url=fcc_url,
        title="Purchase Order (PO) Financing | 1st Commercial Credit",
        quote="client must already be factoring receivables in order to apply for our purchase order finance program",
    )
    fcc["requires_recurring_weekly_revenues"] = True
    fcc["criteria_evidence"]["requires_recurring_weekly_revenues"] = _vp(
        "requires_recurring_weekly_revenues",
        True,
        url=fcc_url,
        title="Purchase Order (PO) Financing | 1st Commercial Credit",
        quote="Recurring weekly revenues.",
    )
    fcc = _set(
        fcc,
        "direct_ship_supported",
        True,
        _vp(
            "direct_ship_supported",
            True,
            url=fcc_url,
            title="Purchase Order (PO) Financing | 1st Commercial Credit",
            quote="Direct shipment/Drop ship orders; Government Contracts; Wholesale distributors",
        ),
    )
    fcc = _set(
        fcc,
        "direct_supplier_payment_supported",
        True,
        _vp(
            "direct_supplier_payment_supported",
            True,
            url=fcc_url,
            title="Purchase Order (PO) Financing | 1st Commercial Credit",
            quote="1st Commercial Credit pays the supplier for goods, shipping, and applicable duty fees",
        ),
    )
    fcc["product_resale_fit"] = RESALE_STRONG
    fcc["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "Finished goods; wholesale distributors; government contracts; drop ship",
        "url": fcc_url,
    }
    fcc["official_urls_reviewed"] = [fcc_url]
    programs.append(fcc)

    # C2. 1st CC — Government Recurring Fulfillment (separate program)
    fcc_gov_url = "https://www.1stcommercialcredit.com/blog/how-to-finance-government-contracts-the-right-way-recurring-fulfillment-po-funding-with-invoice-factoring-support"
    fcc_gov = _profile(
        org="1st Commercial Credit",
        program="Government Recurring Fulfillment PO + Factoring",
        role=ROLE_DIRECT_FUNDER,
        website=fcc_gov_url,
        phone="1-800-876-6071",
        email=None,
        ask_for="Government Contract Finance Specialist",
        path_type="HYBRID_PO_FINANCE_PLUS_FACTORING",
        parent_org_id="1st Commercial Credit",
    )
    fcc_gov = _set(
        fcc_gov,
        "minimum_time_in_business",
        1,
        _vp(
            "minimum_time_in_business",
            1,
            url=fcc_gov_url,
            title="How to Finance Government Contracts — 1st Commercial Credit",
            quote="At least 12 months in operation; Startups with no operational history not eligible",
        ),
    )
    fcc_gov["startup_allowed"] = False
    fcc_gov["criteria_evidence"]["startup_allowed"] = _vp(
        "startup_allowed",
        False,
        url=fcc_gov_url,
        title="How to Finance Government Contracts — 1st Commercial Credit",
        quote="One-time sales, startup ventures, or construction projects are not eligible",
    )
    fcc_gov["first_government_contract_allowed"] = None  # FAQ elsewhere says maybe with 12 mo — keep program-specific UNKNOWN for first gov if only recurring
    fcc_gov["requires_prior_factoring_relationship"] = True
    fcc_gov["product_resale_fit"] = RESALE_POSSIBLE
    fcc_gov["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "Recurring fulfillment government PO funding — not one-time POs",
        "url": fcc_gov_url,
    }
    fcc_gov["official_urls_reviewed"] = [fcc_gov_url]
    programs.append(fcc_gov)

    # D. Noble — Government PO / Work Order (separate)
    noble_po_url = "https://www.noblebusinessloans.com/purchase-order-work-order-financing-government-contractors/"
    noble_po = _profile(
        org="Noble Funding",
        program="Government PO / Work Order Financing",
        role=ROLE_DIRECT_FUNDER,
        website=noble_po_url,
        phone="1-800-916-3196",
        email=None,
        ask_for="Government Contract Finance Specialist",
        path_type="PO_FINANCING",
        parent_org_id="Noble Funding",
        program_id="Noble Funding|Government PO / Work Order Financing",
    )
    noble_po = _set(
        noble_po,
        "minimum_funding_amount",
        4000000,
        _vp(
            "minimum_funding_amount",
            4000000,
            url=noble_po_url,
            title="Purchase Order and Work Order Financing for Government Contractors | Noble Funding",
            quote="best suited for established contractors … need $4 million+ to deliver; If your company needs $4M+",
        ),
    )
    noble_po = _set(
        noble_po,
        "minimum_transaction",
        4000000,
        _vp(
            "minimum_transaction",
            4000000,
            url=noble_po_url,
            title="Purchase Order and Work Order Financing for Government Contractors | Noble Funding",
            quote="need $4 million+ tied to a large government award, P.O., or work order",
        ),
    )
    noble_po = _set(
        noble_po,
        "personal_guarantee",
        PG_CONDITIONAL,
        _vp(
            "personal_guarantee",
            PG_CONDITIONAL,
            url=noble_po_url,
            title="Purchase Order and Work Order Financing for Government Contractors | Noble Funding",
            quote="Do I need a personal guarantee? It depends… offers no personal guarantee options for qualified borrowers, but each transaction is evaluated individually.",
        ),
    )
    noble_po = _set(
        noble_po,
        "government_customer_types_supported",
        ["federal", "state", "local"],
        _vp(
            "government_customer_types_supported",
            ["federal", "state", "local"],
            url=noble_po_url,
            title="Noble Funding PO/Work Order",
            quote="federal, state, county, city, school district, or public agency",
        ),
    )
    noble_po["product_resale_fit"] = RESALE_POSSIBLE
    noble_po["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "municipal P.O. for equipment, supplies; also services/construction oriented language",
        "url": noble_po_url,
    }
    noble_po["official_urls_reviewed"] = [noble_po_url]
    programs.append(noble_po)

    # E. Noble — Government Contract Financing (separate)
    noble_cf_url = "https://www.noblebusinessloans.com/government-contract-financing.html"
    noble_cf = _profile(
        org="Noble Funding",
        program="Government Contract Financing (Junior Capital)",
        role=ROLE_DIRECT_FUNDER,
        website=noble_cf_url,
        phone="1-800-916-3196",
        email=None,
        ask_for="Government Contract Finance Specialist",
        path_type="CONTRACT_FINANCING_PRIVATE",
        parent_org_id="Noble Funding",
        program_id="Noble Funding|Government Contract Financing (Junior Capital)",
    )
    noble_cf = _set(
        noble_cf,
        "minimum_funding_amount",
        300000,
        _vp(
            "minimum_funding_amount",
            300000,
            url=noble_cf_url,
            title="Government Contract Financing | Noble Funding",
            quote="financing from $300,000 to $10 million",
        ),
    )
    noble_cf = _set(
        noble_cf,
        "maximum_funding_amount",
        10000000,
        _vp(
            "maximum_funding_amount",
            10000000,
            url=noble_cf_url,
            title="Government Contract Financing | Noble Funding",
            quote="$300,000 to $10 million",
        ),
    )
    noble_cf = _set(
        noble_cf,
        "personal_guarantee",
        PG_CONDITIONAL,
        _vp(
            "personal_guarantee",
            PG_CONDITIONAL,
            url=noble_cf_url,
            title="Government Contract Financing | Noble Funding",
            quote="No personal guarantee options; Do you require a personal guarantee? Not always. Qualified borrowers…",
        ),
    )
    noble_cf = _set(
        noble_cf,
        "minimum_annual_revenue",
        5000000,
        _vp(
            "minimum_annual_revenue",
            5000000,
            url=noble_cf_url,
            title="Government Contract Financing | Noble Funding",
            quote="Annual revenue of $5 million to $150 million",
        ),
    )
    noble_cf = _set(
        noble_cf,
        "government_customer_types_supported",
        ["federal", "state", "local"],
        _vp(
            "government_customer_types_supported",
            ["federal", "state", "local"],
            url=noble_cf_url,
            title="Government Contract Financing | Noble Funding",
            quote="federal, state, and local contractors",
        ),
    )
    noble_cf = _set(
        noble_cf,
        "typical_funding_days",
        3,
        _vp(
            "typical_funding_days",
            3,
            url=noble_cf_url,
            title="Government Contract Financing | Noble Funding",
            quote="Funding can occur in 2-3 business days for qualified borrowers",
        ),
    )
    noble_cf["product_resale_fit"] = RESALE_WEAK
    noble_cf["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "Lists manufacturing and supply among industries but program framed as junior working capital for established contractors",
        "url": noble_cf_url,
    }
    noble_cf["official_urls_reviewed"] = [noble_cf_url]
    programs.append(noble_cf)

    # Additional programs (10+)
    star_url = "https://starfunding.com/why-star-funding/purchase-order-funding-for-government-contracts/"
    star = _profile(
        org="STAR Funding",
        program="PO Funding for Government Contracts",
        role=ROLE_DIRECT_FUNDER,
        website=star_url,
        phone="(212) 768-9900",
        email=None,
        ask_for="Government Contract / PO Finance Underwriter",
        path_type="PO_FINANCING",
    )
    star = _set(
        star,
        "maximum_supplier_cost_coverage_pct",
        100,
        _vp(
            "maximum_supplier_cost_coverage_pct",
            100,
            url=star_url,
            title="Purchase Order Funding for Government Contracts | STAR Funding",
            quote="provide up to 100% funding; Clients can receive funding for up to 100% of cost of goods",
        ),
    )
    star = _set(
        star,
        "assignment_of_claims_required",
        True,
        _vp(
            "assignment_of_claims_required",
            True,
            url=star_url,
            title="STAR Funding Government PO",
            quote="Assignment of Claims: Is your contract eligible for assignment? (core requirement for federal financing)",
        ),
    )
    star = _set(
        star,
        "government_customer_types_supported",
        ["federal", "state", "local"],
        _vp(
            "government_customer_types_supported",
            ["federal", "state", "local"],
            url=star_url,
            title="STAR Funding Government PO",
            quote="federal or state contract; government purchase order financing",
        ),
    )
    star["product_resale_fit"] = RESALE_STRONG
    star["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "Finished Goods Financing; purchase of goods for resale",
        "url": star_url,
    }
    star["path_hints_transaction_based"] = True
    star["official_urls_reviewed"] = [star_url]
    programs.append(star)

    ktc_url = "https://www.kingtradecapital.com/our-services/"
    ktc = _profile(
        org="King Trade Capital",
        program="Purchase Order / Government Contract Finance",
        role=ROLE_DIRECT_FUNDER,
        website="https://www.kingtradecapital.com/",
        phone="214-368-5100",
        email="info@kingtradecapital.com",
        ask_for="Purchase Order Finance Underwriter",
        path_type="PO_FINANCING",
    )
    ktc = _set(
        ktc,
        "government_customer_types_supported",
        ["federal", "state", "local"],
        _vp(
            "government_customer_types_supported",
            ["federal", "state", "local"],
            url=ktc_url,
            title="Our Services - King Trade Capital",
            quote="financing based upon firm valid purchase orders issued by creditworthy companies or governmental entities; Government Contract Finance since 1993",
        ),
    )
    ktc = _set(
        ktc,
        "direct_supplier_payment_supported",
        True,
        _vp(
            "direct_supplier_payment_supported",
            True,
            url="https://www.kingtradecapital.com/electronic-parts-broker/",
            title="Electronic Parts Broker - King Trade Capital",
            quote="underwrite a financing package that secured payment directly to the supplier",
        ),
    )
    # Startup case study is deal anecdote — INFERRED only for new_entity, not VERIFIED universal
    ktc["product_resale_fit"] = RESALE_STRONG
    ktc["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "PO/trade finance for inventory to fulfill orders/contracts; government supplies and equipment",
        "url": ktc_url,
    }
    ktc["path_hints_transaction_based"] = True
    ktc["official_urls_reviewed"] = [ktc_url, "https://www.kingtradecapital.com/", "https://www.kingtradecapital.com/electronic-parts-broker/"]
    ktc["research_notes"].append(
        "Case study describes <4 months in business financed — treat as anecdote, not verified underwriting rule for all deals"
    )
    programs.append(ktc)

    crest_url = "https://www.crestmontcapital.com/purchase-order-financing"
    crest = _profile(
        org="Crestmont Capital",
        program="Purchase Order Financing",
        role=ROLE_BROKER,  # marketplace/advisor style — treat as broker/referral unless proven direct book
        website=crest_url,
        phone="(800) 949-0401",
        email=None,
        ask_for="Business Development — PO Finance",
        path_type="PO_FINANCING",
    )
    # Partner guidelines on broker site → do NOT encode as this broker's guaranteed criteria as VERIFIED funder rules
    # Only verify contact/role; criteria from their published "typical threshold" table as DISCOVERY not underwriting fact of a named lender
    crest["funding_provider_role"] = ROLE_BROKER
    crest["product_resale_fit"] = RESALE_STRONG
    crest["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "Wholesalers, distributors, resellers; government contractor supply scenario",
        "url": crest_url,
    }
    crest["path_hints_transaction_based"] = True
    crest["official_urls_reviewed"] = [crest_url]
    crest["research_notes"].append(
        "Published typical thresholds (15%+ margin, etc.) are marketing/education — not encoded as VERIFIED underwriting for a specific capital provider"
    )
    programs.append(crest)

    basecamp_url = "https://basecampfunding.com/loans/purchase-order-financing"
    base = _profile(
        org="Basecamp Funding",
        program="Purchase Order Financing",
        role=ROLE_BROKER,
        website=basecamp_url,
        phone="(720) 743-6367",
        email=None,
        ask_for="Business Development — PO Finance",
        path_type="PO_FINANCING",
    )
    base["product_resale_fit"] = RESALE_STRONG
    base["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "Federal supply vendor; distribution & wholesale; physical goods; ~20%+ margin typical",
        "url": basecamp_url,
    }
    # Soft-pull marketing — CONDITIONAL / needs verification for personal credit dependency
    base["research_notes"].append(
        "Page mentions soft-pull only — personal_credit_used_for_approval remains UNKNOWN until verified"
    )
    base["path_hints_transaction_based"] = True
    base["official_urls_reviewed"] = [basecamp_url]
    # Example: ~80% coverage from case study — encode as UNVERIFIED anecdote not VERIFIED_PUBLIC rule
    base["maximum_supplier_cost_coverage_pct"] = None
    base["research_notes"].append("Case example ~80% supplier cost — not verified universal coverage")
    programs.append(base)

    bf_url = "https://businessfactors.com/government-contract-financing/"
    bf = _profile(
        org="Business Factors & Finance",
        program="Government Invoice Factoring / Receivables Finance",
        role=ROLE_DIRECT_FUNDER,
        website=bf_url,
        phone="1-800-672-3844",
        email="sales@businessfactors.com",
        ask_for="Government Receivables Finance",
        path_type="INVOICE_FACTORING",
    )
    bf = _set(
        bf,
        "government_award_required",
        True,
        _vp(
            "government_award_required",
            True,
            url=bf_url,
            title="Government Contract Financing | Business Factors",
            quote="An awarded contract or task order, approved invoices…",
        ),
    )
    bf = _set(
        bf,
        "maximum_supplier_cost_coverage_pct",
        None,  # advances 70-90% of invoice — post-performance, not supplier cost
        _vp(
            "post_invoice_advance_pct_range",
            "70-90% of invoice",
            url=bf_url,
            title="Government Contract Financing | Business Factors",
            quote="advance of 70 to 90% of the invoice upon approval",
        ),
    )
    # Remove bogus None set — fix: don't set coverage for factoring as supplier coverage
    bf.pop("maximum_supplier_cost_coverage_pct", None)
    bf["maximum_supplier_cost_coverage_pct"] = None
    bf["product_resale_fit"] = RESALE_WEAK
    bf["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "Primarily invoice/receivable advances after performance — does not by itself fund pre-delivery supplier cost",
        "url": bf_url,
    }
    bf["official_urls_reviewed"] = [bf_url]
    bf["post_award_only"] = True
    programs.append(bf)

    ss_url = "https://www.southstarcapital.com/purchase-order-financing/"
    ss = _profile(
        org="SouthStar Capital",
        program="Purchase Order Financing",
        role=ROLE_DIRECT_FUNDER,
        website=ss_url,
        phone="1-800-763-3021",
        email=None,
        ask_for="Purchase Order Finance Underwriter",
        path_type="PO_FINANCING",
    )
    ss = _set(
        ss,
        "direct_supplier_payment_supported",
        True,
        _vp(
            "direct_supplier_payment_supported",
            True,
            url="https://www.southstarcapital.com/how-does-purchase-order-financing-work/",
            title="How does Purchase Order Financing Work? - SouthStar Capital",
            quote="advance money to the third-party manufacturer to cover the cost of the goods",
        ),
    )
    ss = _set(
        ss,
        "government_customer_types_supported",
        ["federal", "state", "local"],
        _vp(
            "government_customer_types_supported",
            ["federal", "state", "local"],
            url="https://www.southstarcapital.com/government-contracting-financing/",
            title="Government Contract Financing Solutions | SouthStar Capital",
            quote="Prime and Sub-contractors working on State, Federal, and Municipal government projects",
        ),
    )
    ss["product_resale_fit"] = RESALE_STRONG
    ss["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "PO financing pays manufacturer for goods; government contracting financing offered",
        "url": ss_url,
    }
    ss["path_hints_transaction_based"] = True
    ss["official_urls_reviewed"] = [
        ss_url,
        "https://www.southstarcapital.com/how-does-purchase-order-financing-work/",
        "https://www.southstarcapital.com/government-contracting-financing/",
    ]
    programs.append(ss)

    ss_gov = _profile(
        org="SouthStar Capital",
        program="Government Contract Financing",
        role=ROLE_DIRECT_FUNDER,
        website="https://www.southstarcapital.com/government-contracting-financing/",
        phone="1-800-763-3021",
        email=None,
        ask_for="Government Contract Finance Specialist",
        path_type="CONTRACT_FINANCING_PRIVATE",
        parent_org_id="SouthStar Capital",
        program_id="SouthStar Capital|Government Contract Financing",
    )
    ss_gov = _set(
        ss_gov,
        "assignment_of_claims_required",
        True,
        _vp(
            "assignment_of_claims_required",
            True,
            url="https://www.southstarcapital.com/government-contracting-financing/",
            title="Government Contract Financing Solutions | SouthStar Capital",
            quote="Securing an approved Assignment of Claims is a detailed process",
        ),
    )
    ss_gov["product_resale_fit"] = RESALE_POSSIBLE
    ss_gov["product_resale_fit_evidence"] = {
        "status": EV_VERIFIED_PUBLIC,
        "quote": "Government contract financing for primes/subs — product-resale vs services mix UNKNOWN",
        "url": "https://www.southstarcapital.com/government-contracting-financing/",
    }
    ss_gov["official_urls_reviewed"] = ["https://www.southstarcapital.com/government-contracting-financing/"]
    programs.append(ss_gov)

    # Additional discovery candidates with limited verified criteria
    for org, program, role, website, phone, ask, path, resale, notes, urls in [
        (
            "Drip Capital",
            "Government PO Financing (Educational/Program)",
            ROLE_UNKNOWN,
            "https://www.dripcapital.com/en-us/resources/finance-guides/government-po-financing",
            None,
            "Purchase Order Finance Underwriter",
            "PO_FINANCING",
            RESALE_POSSIBLE,
            "Guide content used for discovery only — underwriting criteria not encoded as VERIFIED without product page confirmation",
            ["https://www.dripcapital.com/en-us/resources/finance-guides/government-po-financing"],
        ),
        (
            "EPOCH Financial",
            "Government Purchase Order Financing",
            ROLE_UNKNOWN,
            "https://www.epochfinancial.com/blog/government-purchase-order-financing",
            None,
            "Purchase Order Finance Underwriter",
            "PO_FINANCING",
            RESALE_POSSIBLE,
            "Blog/educational — discovery only; no VERIFIED min sizes encoded",
            ["https://www.epochfinancial.com/blog/government-purchase-order-financing"],
        ),
        (
            "Icarus Fund",
            "Government Supply Contract PO Financing",
            ROLE_UNKNOWN,
            "https://icarus-fund.com/",
            None,
            "Purchase Order Finance Underwriter",
            "PO_FINANCING",
            RESALE_STRONG,
            "Marketing claims first-time contractors / no personal collateral — NOT encoded as VERIFIED (page fetch 404 on article; treat UNKNOWN)",
            ["https://icarus-fund.com/"],
        ),
        (
            "Capstone Trade Finance",
            "Purchase Order / Trade Finance",
            ROLE_DIRECT_FUNDER,
            "https://www.capstonetradefinance.com/",
            None,
            "Purchase Order Finance Underwriter",
            "PO_FINANCING",
            RESALE_POSSIBLE,
            "Candidate for follow-up research — criteria not verified this pass",
            [],
        ),
        (
            "Prestige Capital",
            "Purchase Order Financing",
            ROLE_DIRECT_FUNDER,
            "https://www.prestigecapital.com/",
            None,
            "Purchase Order Finance Underwriter",
            "PO_FINANCING",
            RESALE_POSSIBLE,
            "Candidate for follow-up — criteria not verified this pass",
            [],
        ),
        (
            "Gateway Trade Funding",
            "Purchase Order Financing",
            ROLE_DIRECT_FUNDER,
            "https://www.gatewaytradefunding.com/",
            None,
            "Purchase Order Finance Underwriter",
            "PO_FINANCING",
            RESALE_POSSIBLE,
            "Candidate for follow-up — criteria not verified this pass",
            [],
        ),
    ]:
        p = _profile(
            org=org,
            program=program,
            role=role,
            website=website,
            phone=phone,
            email=None,
            ask_for=ask,
            path_type=path,
        )
        p["product_resale_fit"] = resale
        p["product_resale_fit_evidence"] = {"status": "DISCOVERY", "note": notes, "url": website}
        p["official_urls_reviewed"] = urls
        p["research_notes"].append(notes)
        p["path_hints_transaction_based"] = True
        programs.append(p)

    return programs


def enrich_program(profile: dict[str, Any]) -> dict[str, Any]:
    fit = classify_launch_fit(profile)
    out = dict(profile)
    out["launch_fit"] = fit["launch_fit"]
    out["launch_fit_detail"] = fit
    out["critical_unknown_matrix"] = fit.get("unknowns") or critical_unknown_matrix(profile)
    out["critical_unknown_count"] = sum(
        1 for v in (out["critical_unknown_matrix"] or {}).values() if v == "UNKNOWN"
    )
    zc = fit.get("zero_cash") or evaluate_zero_cash(
        coverage_pct=profile.get("maximum_supplier_cost_coverage_pct")
    )
    out["zero_cash_analysis"] = zc
    out["zero_cash_execution_possible"] = zc.get("zero_cash_execution_possible")
    out["borrower_gap_required"] = zc.get("borrower_gap_required")
    out["call_priority"] = assign_call_priority(out, fit)
    out["approved"] = False
    out["funding_secured"] = False
    return out


def build_production_funding_kb() -> dict[str, Any]:
    programs = [enrich_program(p) for p in _seed_programs()]
    orgs = sorted({p["organization_name"] for p in programs})
    return {
        "data_class": DATA_PRODUCTION,
        "verified_at": VERIFIED_AT,
        "public_financing_http_requests_used": RESEARCH_HTTP_USED,
        "max_public_financing_http": 40,
        "private_lenders": programs,
        "programs": programs,
        "organizations": orgs,
        "verified_private_lender_count": sum(
            1
            for p in programs
            if any(
                isinstance(ev, dict) and ev.get("verification_status") == EV_VERIFIED_PUBLIC
                for ev in (p.get("criteria_evidence") or {}).values()
            )
        ),
        "counts": {
            "programs": len(programs),
            "organizations": len(orgs),
            "DIRECT_FUNDER": sum(1 for p in programs if p.get("funding_provider_role") == ROLE_DIRECT_FUNDER),
            "BROKER": sum(1 for p in programs if p.get("funding_provider_role") == ROLE_BROKER),
            "UNKNOWN_ROLE": sum(1 for p in programs if p.get("funding_provider_role") == ROLE_UNKNOWN),
            "REJECT_NOW": sum(1 for p in programs if p.get("launch_fit") == FIT_REJECT_NOW),
            "POTENTIAL_MATCH": sum(1 for p in programs if p.get("launch_fit") == FIT_POTENTIAL_MATCH),
            "NEEDS_VERIFICATION": sum(1 for p in programs if p.get("launch_fit") == FIT_NEEDS_VERIFICATION),
            "FUTURE_MATCH": sum(1 for p in programs if p.get("launch_fit") == FIT_FUTURE_MATCH),
            "NOT_APPLICABLE": sum(1 for p in programs if p.get("launch_fit") == FIT_NOT_APPLICABLE),
            "CALL_NOW": sum(1 for p in programs if p.get("call_priority") == CALL_NOW),
        },
        "government_program_notes": [
            {
                "topic": "FAR_PART_32",
                "summary": (
                    "FAR 32.106 prefers private financing without Government guarantee; "
                    "FAR 32.104 recognizes prudent contract financing; "
                    "FAR 32.202-1 commercial financing normally contractor responsibility."
                ),
                "applies_to_specific_solicitation": False,
                "deal_availability": "NEEDS_VERIFICATION",
            }
        ],
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
        "USAspending": 0,
        "paid": 0,
        "applications_submitted": 0,
    }


def build_call_now_queue(kb: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    kb = kb or build_production_funding_kb()
    from funding_path_intelligence import build_operator_call_sheet, build_funding_requirement

    req = build_funding_requirement(
        opportunity={
            "government_customer_type": "federal",
            "product_category": "equipment",
            "title": "Government product-resale supply",
        },
        economics={
            "estimated_bid_value": 185000,
            "estimated_supplier_cost": 132000,
            "estimated_freight": 3000,
            "estimated_other_performance_cost": 0,
        },
        extras={
            "cash_required_before_delivery": 132000,
            "company_is_new_entity": True,
            "is_first_government_contract": True,
            "supplier_can_ship_direct_to_government": True,
        },
    )
    queue = []
    call_now = [p for p in kb["programs"] if p.get("call_priority") == CALL_NOW]
    # Rank: resale strong first, then fewer unknowns, then no-PG evidence
    call_now.sort(
        key=lambda p: (
            0 if p.get("product_resale_fit") == RESALE_STRONG else 1 if p.get("product_resale_fit") == RESALE_POSSIBLE else 2,
            p.get("critical_unknown_count") or 99,
            0 if p.get("personal_guarantee") == PG_NOT_REQUIRED else 1,
            p.get("organization_name") or "",
        )
    )
    for i, p in enumerate(call_now, 1):
        sheet = build_operator_call_sheet(requirement=req, source=p, call_priority=i)
        # Force first-three verification questions at front
        first_three = [
            {
                "key": "first_gov_product_resale",
                "question": (
                    "Can you finance a company's FIRST government product-resale contract when "
                    "the company has little/no operating revenue history?"
                ),
            },
            {
                "key": "zero_cash_no_pg",
                "question": (
                    "Can the transaction be funded with ZERO borrower cash contribution and "
                    "WITHOUT a personal guarantee?"
                ),
            },
            {
                "key": "transaction_vs_personal_credit",
                "question": (
                    "Is approval based primarily on the government customer, awarded PO/contract, "
                    "supplier and transaction rather than the owner's personal credit?"
                ),
            },
        ]
        opening = (
            "We're building a product-resale government contracting operation and I want to "
            "understand your transaction criteria before bidding. The contracts would involve an "
            "established supplier, a government customer, and ideally direct shipment. We need "
            "financing structures based on the award/PO, supplier and government receivable rather "
            "than personal-credit underwriting."
        )
        sheet["opening"] = opening
        sheet["questions"] = first_three + [
            q for q in (sheet.get("questions") or []) if q.get("key") not in {x["key"] for x in first_three}
        ]
        sheet["who_to_ask_for"] = p.get("who_to_ask_for") or p.get("preferred_contact_title")
        sheet["named_contact_invented"] = False
        sheet["preferred_contact_name"] = None
        queue.append(
            {
                "rank": i,
                "organization": p["organization_name"],
                "program": p["program_name"],
                "phone": p.get("general_phone"),
                "email": p.get("contact_email"),
                "website": p.get("website"),
                "who_to_ask_for": sheet["who_to_ask_for"],
                "launch_fit": p.get("launch_fit"),
                "product_resale_fit": p.get("product_resale_fit"),
                "why_survived": p.get("launch_fit_detail", {}).get("reasons"),
                "critical_unknowns": [
                    k for k, v in (p.get("critical_unknown_matrix") or {}).items() if v == "UNKNOWN"
                ],
                "call_sheet": sheet,
                "approved": False,
                "funding_secured": False,
            }
        )
    return queue


def call_result_entry_template() -> dict[str, Any]:
    """Fast operator entry form — maps into KB criteria on save."""
    fields = [
        "SOURCE",
        "PERSON_SPOKEN_TO",
        "TITLE",
        "DATE",
        "FIRST_CONTRACT",
        "NEW_COMPANY",
        "PERSONAL_CREDIT",
        "MINIMUM_FICO",
        "PG",
        "BORROWER_CASH",
        "MIN_TRANSACTION",
        "MAX_TRANSACTION",
        "MIN_MARGIN",
        "SUPPLIER_COVERAGE",
        "DIRECT_SUPPLIER_PAYMENT",
        "DIRECT_SHIP",
        "FEDERAL",
        "STATE",
        "LOCAL",
        "PRE_BID",
        "POST_AWARD",
        "APPROVAL_TIME",
        "FUNDING_TIME",
        "FEES",
        "ASSIGNMENT",
        "LOCKBOX",
        "UCC",
        "OTHER_NOTES",
    ]
    field_to_criterion = {
        "FIRST_CONTRACT": "first_government_contract_allowed",
        "NEW_COMPANY": "new_entity_allowed",
        "PERSONAL_CREDIT": "personal_credit_checked",
        "MINIMUM_FICO": "minimum_personal_fico",
        "PG": "personal_guarantee",
        "BORROWER_CASH": "borrower_cash_contribution_required",
        "MIN_TRANSACTION": "minimum_transaction",
        "MAX_TRANSACTION": "maximum_transaction",
        "MIN_MARGIN": "minimum_margin_pct",
        "SUPPLIER_COVERAGE": "maximum_supplier_cost_coverage_pct",
        "DIRECT_SUPPLIER_PAYMENT": "direct_supplier_payment_supported",
        "DIRECT_SHIP": "direct_ship_supported",
        "PRE_BID": "pre_bid_screening_available",
        "ASSIGNMENT": "assignment_of_claims_required",
        "LOCKBOX": "lockbox_required",
        "UCC": "UCC_required",
        "APPROVAL_TIME": "typical_approval_days",
        "FUNDING_TIME": "typical_funding_days",
        "FEES": "fee_type",
    }
    return {
        "template_fields": fields,
        "field_to_criterion": field_to_criterion,
        "free_form_notes_field": "OTHER_NOTES",
        "retain_verbatim": True,
        "ai_extraction_requires_confirmation": True,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def apply_call_result_to_criteria(entry: dict[str, Any]) -> dict[str, Any]:
    """Map operator template entry → proposed criterion updates (not auto-verified)."""
    tmpl = call_result_entry_template()
    mapping = tmpl["field_to_criterion"]
    proposed = {}
    for field, crit in mapping.items():
        if field in entry and entry[field] not in (None, "", "UNKNOWN"):
            proposed[crit] = {
                "value": entry[field],
                "verification_status": "PROPOSED_FROM_CALL_UNCONFIRMED",
                "requires_confirmation": True,
            }
    return {
        "source": entry.get("SOURCE"),
        "contact_name": entry.get("PERSON_SPOKEN_TO"),
        "contact_title": entry.get("TITLE"),
        "contact_date": entry.get("DATE"),
        "raw_operator_notes": entry.get("OTHER_NOTES"),
        "proposed_criteria": proposed,
        "auto_verified": False,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def supplier_funding_verification_queue(
    *,
    supplier_name: str | None = None,
    product_category: str | None = None,
) -> dict[str, Any]:
    questions = [
        "Can you waive or reduce deposit for this government PO?",
        "What payment terms can you offer (Net-30/60/etc.) for this transaction?",
        "Will you accept direct payment from a purchase-order / contract financier?",
        "Can you direct-ship to the government customer?",
        "Will you ship before receiving government payment?",
        "Will you accept assignment / payment-control structures?",
        "Can you extend project-specific credit based on the government PO/award?",
        "Have you worked with PO finance companies before on government deals?",
        "Will you accept a letter of credit?",
    ]
    return {
        "queue_type": "SUPPLIER_FUNDING_VERIFICATION",
        "supplier_name": supplier_name,
        "product_category": product_category,
        "priority_rule": "Evaluate supplier funding BEFORE assuming an expensive lender is necessary",
        "questions": questions,
        "note": "Do not research thousands of suppliers until opportunity-specific candidates exist",
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def government_financing_verification_action() -> dict[str, Any]:
    return {
        "availability": "NEEDS_VERIFICATION",
        "far_context": [
            "FAR 32.106 preference for private financing without Government guarantee",
            "FAR 32.104 prudent contract financing as performance tool",
            "FAR 32.202-1 commercial financing normally contractor responsibility",
        ],
        "action": (
            "When solicitation financing terms are unclear, ask the contracting officer whether "
            "commercial advance/interim/installment or other FAR Part 32 financing is authorized "
            "for this contract — do not claim availability from FAR text alone."
        ),
        "deal_specific_availability": False,
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
    }


def export_funding_source_research(
    *,
    out_dir: str | Path | None = None,
    kb: dict[str, Any] | None = None,
) -> dict[str, Any]:
    kb = kb or build_production_funding_kb()
    root = Path(out_dir) if out_dir else Path(__file__).resolve().parent / "artifacts"
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "funding_sources_verification_queue.json"
    csv_path = root / "funding_sources_verification_queue.csv"

    rows = []
    for p in kb["programs"]:
        ev_urls = []
        for ev in (p.get("criteria_evidence") or {}).values():
            if isinstance(ev, dict) and ev.get("source_url"):
                ev_urls.append(ev["source_url"])
        ev_urls.extend(p.get("official_urls_reviewed") or [])
        rows.append(
            {
                "source": p.get("organization_name"),
                "program": p.get("program_name"),
                "provider_role": p.get("funding_provider_role"),
                "launch_fit": p.get("launch_fit"),
                "call_priority": p.get("call_priority"),
                "product_resale_fit": p.get("product_resale_fit"),
                "min_transaction": p.get("minimum_transaction") or p.get("minimum_funding_amount"),
                "max_transaction": p.get("maximum_transaction") or p.get("maximum_funding_amount"),
                "pg_status": p.get("personal_guarantee") or PG_UNKNOWN,
                "personal_credit_status": p.get("personal_credit_checked"),
                "borrower_cash_status": p.get("borrower_cash_contribution_required"),
                "startup_status": p.get("startup_allowed"),
                "first_contract_status": p.get("first_government_contract_allowed"),
                "min_margin": p.get("minimum_margin_pct"),
                "supplier_coverage": p.get("maximum_supplier_cost_coverage_pct"),
                "federal": "federal" in (p.get("government_customer_types_supported") or []),
                "state": "state" in (p.get("government_customer_types_supported") or []),
                "local": "local" in (p.get("government_customer_types_supported") or []),
                "direct_ship": p.get("direct_ship_supported"),
                "supplier_direct_payment": p.get("direct_supplier_payment_supported"),
                "pre_bid_availability": p.get("pre_bid_screening_available"),
                "approval_time": p.get("typical_approval_days"),
                "funding_time": p.get("typical_funding_days"),
                "critical_unknown_count": p.get("critical_unknown_count"),
                "official_phone": p.get("general_phone"),
                "who_to_ask_for": p.get("who_to_ask_for"),
                "last_verified": p.get("last_verified_at"),
                "evidence_urls": ";".join(sorted(set(ev_urls))),
                "approved": False,
                "funding_secured": False,
            }
        )

    payload = {
        "generated_at": now_utc().isoformat(),
        "kb_summary": kb.get("counts"),
        "launch_company": launch_company_profile(),
        "call_now_queue": build_call_now_queue(kb),
        "call_result_template": call_result_entry_template(),
        "supplier_funding_queue": supplier_funding_verification_queue(),
        "government_financing_verification": government_financing_verification_action(),
        "programs": kb["programs"],
        "export_rows": rows,
        "PUBLIC_HTTP_REQUESTS": RESEARCH_HTTP_USED,
        "SAM": 0,
        "OpenAI": 0,
        "USAspending": 0,
        "paid": 0,
        "applications_submitted": 0,
    }
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)

    return {
        "json_path": str(json_path),
        "csv_path": str(csv_path),
        "program_count": len(rows),
        "call_now_count": kb["counts"]["CALL_NOW"],
        "LIVE_API_REQUESTS": 0,
        "OpenAI": 0,
        "SAM": 0,
    }
