"""Transaction funding requirement + financier profiles + compatibility + funding gate."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from application_clock import now_utc
from commercial_verification_constants import (
    AUTH_OFFICIAL,
    AUTH_UNKNOWN,
    CREDIT_DEP,
    CREDIT_DEP_UNKNOWN,
    CREDIT_NOT_MATERIAL,
    CV_FINANCIER_PATH_FAILED,
    ECON_ATTRACTIVE_FUNDING_UNVERIFIED,
    ECON_ATTRACTIVE_FUNDING_VERIFY_REQ,
    FICO_KNOWN,
    FICO_NOT_STATED,
    FICO_UNKNOWN,
    FIN_COMPATIBLE,
    FIN_INCOMPATIBLE,
    FIN_LIKELY_INCOMPAT,
    FIN_POTENTIAL,
    FIN_UNCERTAINTY,
    FIN_UNKNOWN,
    FINANCIER_INCOMPATIBLE,
    FUND_COND_FEASIBLE,
    FUND_EXHAUSTED,
    FUND_INCOMPATIBLE,
    FUND_NOT_REQUIRED,
    FUND_PATH_ID,
    FUND_RESEARCH_ONLY,
    FUND_REQ_UNKNOWN,
    FUND_VERIFY_REQ,
    FUND_VERIFIED,
    FUTURE_ACTION,
    PATH_CONDITIONAL,
    PATH_DECLINED,
    PATH_PUBLIC_POSSIBLE,
    PATH_UNTESTED,
    PATH_VERIFIED_BAD,
    PATH_VERIFIED_OK,
    PATH_VERIFY_REQ,
    PG_NOT_REQUIRED,
    PG_REQUIRED,
    PG_UNKNOWN,
    PULL_NOT_REQUIRED,
    PULL_REQUIRED,
    PULL_UNKNOWN,
    PURSUIT_KEEP,
    PURSUIT_KEEP_UNCERTAIN,
    PURSUIT_STOP_FUNDING,
    STRUCT_COMBO,
    STRUCT_DEPOSIT_PO,
    STRUCT_DISTRIBUTOR,
    STRUCT_FACTORING,
    STRUCT_GOV_CONTRACT,
    STRUCT_PO,
    STRUCT_PO_AR,
    STRUCT_SUPPLIER_TERMS,
    STRUCT_WC,
    UW_BROKERED,
    UW_EXTERNAL,
    UW_HYBRID,
    UW_INTERNAL,
    UW_UNKNOWN,
)
from economic_integrity import min_actual_profit_usd
from funding_underwriting import (
    CREDIT_ROLE_MATERIAL,
    CREDIT_ROLE_PRIMARY,
    GUARANTEE_FULL_RECOURSE,
    GUARANTEE_NO_PG,
)


def build_transaction_funding_requirement(
    *,
    acquisition_cost: float | None = None,
    freight: float | None = None,
    supplier_deposit: float | None = None,
    supplier_payment_timing: str | None = None,
    government_payment_terms: str | None = None,
    estimated_cash_conversion_days: int | None = None,
    advance_percentage_required: float | None = None,
    maximum_financing_cost: float | None = None,
    maximum_cash_contribution: float | None = 0.0,
    required_funding_date: str | None = None,
    repayment_source: str = "government_receivable",
    government_obligor: str | None = None,
    assignment_requirements: str | None = None,
    minimum_target_profit_after_financing: float | None = None,
    financing_deadline: str | None = None,
    bid_revenue: float | None = None,
) -> dict[str, Any]:
    acq = acquisition_cost
    fr = freight or 0.0
    deposit = supplier_deposit
    if acq is None:
        amount = None
        status = "UNKNOWN"
    else:
        # Funding typically covers acquisition (+ freight if prepaid)
        amount = float(acq) + (float(fr) if freight is not None else 0.0)
        if deposit is not None:
            amount = max(amount, float(deposit))
        status = "CALCULATED"

    floor = float(minimum_target_profit_after_financing if minimum_target_profit_after_financing is not None else min_actual_profit_usd())
    max_fin = maximum_financing_cost
    if max_fin is None and bid_revenue is not None and acq is not None and freight is not None:
        max_fin = float(bid_revenue) - float(acq) - float(freight) - floor

    advance = advance_percentage_required
    if advance is None and amount is not None and acq is not None and amount > 0:
        advance = round(float(acq) / amount * 100.0, 2)

    return {
        "kind": "TransactionFundingRequirement",
        "acquisition_cost": acq,
        "freight": freight,
        "supplier_deposit": deposit,
        "supplier_payment_timing": supplier_payment_timing or "UNKNOWN",
        "government_payment_terms": government_payment_terms or "UNKNOWN",
        "estimated_cash_conversion_days": estimated_cash_conversion_days,
        "financing_amount_required": amount,
        "advance_percentage_required": advance,
        "maximum_financing_cost": max_fin,
        "maximum_cash_contribution": maximum_cash_contribution,
        "required_funding_date": required_funding_date,
        "repayment_source": repayment_source,
        "government_obligor": government_obligor,
        "assignment_payment_control_requirements": assignment_requirements or "UNKNOWN",
        "minimum_target_profit_after_financing": floor,
        "financing_deadline": financing_deadline,
        "status": status,
        "funding_required": None if acq is None else (amount is not None and amount > 0),
        "models_supplier_payment_timing": True,
        "models_government_payment_timing": True,
    }


def financier_profile(
    *,
    financier: str,
    financing_type: str | None = None,
    po_finance: bool | None = None,
    ar_factoring: bool | None = None,
    combined_po_ar: bool | None = None,
    government_contract_specialization: bool | None = None,
    startup_eligibility: bool | None = None,
    min_transaction: float | None = None,
    max_transaction: float | None = None,
    advance_percentage: float | None = None,
    direct_supplier_payment: bool | None = None,
    underwriting_model: str = UW_UNKNOWN,
    broker_vs_direct: str | None = None,
    source_of_capital: str | None = None,
    customer_credit_reliance: str | None = None,
    contractor_history_reliance: str | None = None,
    personal_guarantee: str | None = None,  # REQUIRED / NOT_REQUIRED / UNKNOWN
    personal_credit_pull: str | None = None,
    minimum_fico: int | None = None,
    personal_credit_dependency: str | None = None,  # NONE / INFORMATIONAL / MATERIAL / PRIMARY / UNKNOWN
    cash_contribution: str | None = None,
    collateral: str | None = None,
    ucc: bool | None = None,
    assignment_of_claims: bool | None = None,
    lockbox: bool | None = None,
    fee_structure: str | None = None,
    rate_structure: str | None = None,
    typical_duration: str | None = None,
    industries: list[str] | None = None,
    exclusions: list[str] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    evidence_date: str | None = None,
    confidence: str = "UNKNOWN",
) -> dict[str, Any]:
    return {
        "kind": "FinancierProfile",
        "financier": financier,
        "financing_type": financing_type or "UNKNOWN",
        "po_finance": po_finance,
        "ar_factoring": ar_factoring,
        "combined_po_ar": combined_po_ar,
        "government_contract_specialization": government_contract_specialization,
        "startup_eligibility": startup_eligibility,
        "minimum_transaction_size": min_transaction,
        "maximum_transaction_size": max_transaction,
        "advance_percentage": advance_percentage,
        "direct_supplier_payment": direct_supplier_payment,
        "underwriting_model": underwriting_model,
        "broker_vs_direct": broker_vs_direct or "UNKNOWN",
        "source_of_capital": source_of_capital or "UNKNOWN",
        "customer_credit_reliance": customer_credit_reliance or "UNKNOWN",
        "contractor_financial_history_reliance": contractor_history_reliance or "UNKNOWN",
        "personal_guarantee": personal_guarantee or "UNKNOWN",
        "personal_credit_pull": personal_credit_pull or "UNKNOWN",
        "minimum_fico": minimum_fico,  # None = UNKNOWN
        "personal_credit_dependency": personal_credit_dependency or "UNKNOWN",
        "cash_contribution": cash_contribution or "UNKNOWN",
        "collateral": collateral or "UNKNOWN",
        "ucc": ucc,
        "assignment_of_claims": assignment_of_claims,
        "lockbox_payment_control": lockbox,
        "fee_structure": fee_structure or "UNKNOWN",
        "rate_structure": rate_structure or "UNKNOWN",
        "typical_duration": typical_duration or "UNKNOWN",
        "industries": industries or [],
        "exclusions": exclusions or [],
        "evidence": evidence or [],
        "evidence_date": evidence_date,
        "confidence": confidence,
        "unknown_preserved": True,
        "fabricated_policies": False,
    }


def load_financier_profiles_from_underwriting_artifact(
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Map existing underwriting profiles without fabricating missing policies."""
    root = Path(__file__).resolve().parent
    p = path or (root / "artifacts" / "funding_underwriting_profiles.json")
    if not p.exists():
        return default_financier_profiles()
    data = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for raw in data.get("profiles") or []:
        org = raw.get("organization") or raw.get("financier") or "UNKNOWN"
        pc = raw.get("personal_credit") or {}
        pg = raw.get("personal_guarantee") or {}
        uw = raw.get("underwriting") or raw.get("capital_structure") or {}

        min_fico = pc.get("minimum_fico")
        credit_checked = pc.get("personal_credit_checked")
        credit_role = pc.get("personal_credit_role") or "UNKNOWN"
        pull = "NONE" if credit_checked in (False, "NONE", None) and credit_role == "NOT_CONSIDERED" else (
            "UNKNOWN" if credit_checked is None else str(credit_checked)
        )
        # Distinguish PG from credit dependency
        pg_state = pg.get("guarantee_state") or pg.get("personal_guarantee") or "UNKNOWN"
        if pg_state == "NO_PG":
            pg_norm = "NOT_REQUIRED"
        elif pg_state in {"FULL_RECOURSE_PG", "REQUIRED", GUARANTEE_FULL_RECOURSE}:
            pg_norm = "REQUIRED"
        else:
            pg_norm = "UNKNOWN"

        dep = "UNKNOWN"
        if credit_role in {"NOT_CONSIDERED", "NONE"}:
            dep = "NONE"
        elif credit_role in {"INFORMATIONAL_ONLY", "INFORMATIONAL"}:
            dep = "INFORMATIONAL"
        elif credit_role in {CREDIT_ROLE_MATERIAL, "MATERIAL_UNDERWRITING_FACTOR"}:
            dep = "MATERIAL"
        elif credit_role in {CREDIT_ROLE_PRIMARY, "PRIMARY_UNDERWRITING_FACTOR"}:
            dep = "PRIMARY"

        uw_model = UW_UNKNOWN
        role = str(uw.get("provider_role") or raw.get("provider_role") or "").upper()
        if "BROKER" in role:
            uw_model = UW_BROKERED
        elif "DIRECT" in role or uw.get("internal_underwriting") is True:
            uw_model = UW_INTERNAL
        elif uw.get("internal_underwriting") is False and uw.get("places_with_lenders") is True:
            uw_model = UW_EXTERNAL
        elif uw.get("hybrid"):
            uw_model = UW_HYBRID

        # Evidence snippets only — do not invent FICO/PG if missing
        evidence = []
        for block in (pc.get("evidence") or {}).values():
            if isinstance(block, dict) and block.get("source_quote_or_summary"):
                evidence.append(
                    {
                        "quote": block.get("source_quote_or_summary"),
                        "url": block.get("source_url"),
                        "status": block.get("verification_status"),
                    }
                )

        out.append(
            financier_profile(
                financier=org,
                po_finance=raw.get("po_finance"),
                government_contract_specialization=True if raw.get("government") else None,
                startup_eligibility=pc.get("startup_eligible"),
                personal_guarantee=pg_norm,
                personal_credit_pull=pull if pull != "False" else "NONE",
                minimum_fico=int(min_fico) if isinstance(min_fico, (int, float)) else None,
                personal_credit_dependency=dep,
                cash_contribution="UNKNOWN" if pc.get("zero_cash_accepted") is None else (
                    "NOT_REQUIRED" if pc.get("zero_cash_accepted") else "REQUIRED"
                ),
                underwriting_model=uw_model,
                broker_vs_direct="BROKER" if uw_model == UW_BROKERED else (
                    "DIRECT" if uw_model == UW_INTERNAL else "UNKNOWN"
                ),
                evidence=evidence,
                evidence_date=None,
                confidence="LOW" if not evidence else "MEDIUM",
            )
        )
    return out or default_financier_profiles()


# Fix: financier_profile doesn't take website — remove that kwarg by adjusting loader
def default_financier_profiles() -> list[dict[str, Any]]:
    """Safe defaults with UNKNOWN for unverified policy fields — no fabrication."""
    names = [
        "STAR Funding",
        "SouthStar Capital",
        "King Trade Capital",
        "ELINT Capital",
        "Noble Capital",
    ]
    return [
        financier_profile(
            financier=n,
            government_contract_specialization=None,
            personal_guarantee="UNKNOWN",
            personal_credit_pull="UNKNOWN",
            minimum_fico=None,
            personal_credit_dependency="UNKNOWN",
            underwriting_model=UW_UNKNOWN,
            confidence="UNKNOWN",
            evidence=[],
        )
        for n in names
    ]


def normalize_pg_credit_fico_states(profile: dict[str, Any]) -> dict[str, Any]:
    """Independent PG / pull / dependency / FICO vocabulary — UNKNOWN stays UNKNOWN."""
    pg_raw = profile.get("personal_guarantee") or "UNKNOWN"
    if pg_raw in {"REQUIRED", PG_REQUIRED}:
        pg = PG_REQUIRED
    elif pg_raw in {"NOT_REQUIRED", PG_NOT_REQUIRED, "NO_PG"}:
        pg = PG_NOT_REQUIRED
    else:
        pg = PG_UNKNOWN

    pull_raw = profile.get("personal_credit_pull") or "UNKNOWN"
    if pull_raw in {"REQUIRED", True, "YES", PULL_REQUIRED}:
        pull = PULL_REQUIRED
    elif pull_raw in {"NONE", "NOT_REQUIRED", False, "NO", PULL_NOT_REQUIRED}:
        pull = PULL_NOT_REQUIRED
    else:
        pull = PULL_UNKNOWN

    dep_raw = profile.get("personal_credit_dependency") or "UNKNOWN"
    if dep_raw in {"MATERIAL", "PRIMARY", CREDIT_DEP}:
        dep = CREDIT_DEP
    elif dep_raw in {"NONE", "INFORMATIONAL", CREDIT_NOT_MATERIAL}:
        dep = CREDIT_NOT_MATERIAL
    else:
        dep = CREDIT_DEP_UNKNOWN

    min_fico = profile.get("minimum_fico")
    if min_fico is not None:
        fico_state = FICO_KNOWN
    elif profile.get("minimum_fico_not_stated"):
        fico_state = FICO_NOT_STATED
    else:
        fico_state = FICO_UNKNOWN

    return {
        "pg_state": pg,
        "personal_credit_pull_state": pull,
        "personal_credit_dependency_state": dep,
        "minimum_fico_state": fico_state,
        "minimum_fico": min_fico,
        "pg_automatically_incompatible": False,
        "unknown_preserved": True,
    }


def record_affirmative_incompatibility(
    *,
    financier: str,
    reason: str,
    evidence: str | None,
    authority: str,
    curable: bool = False,
    transaction_specific: bool = False,
    evidence_date: str | None = None,
) -> dict[str, Any]:
    """Financier may be FINANCIER_INCOMPATIBLE only with affirmative evidence."""
    return {
        "kind": "AffirmativeIncompatibility",
        "financier": financier,
        "state": FINANCIER_INCOMPATIBLE,
        "reason": reason,
        "evidence": evidence,
        "authority": authority,
        "date": evidence_date or now_utc().isoformat(),
        "curable": curable,
        "transaction_specific_or_policy": "transaction" if transaction_specific else "policy",
        "marketing_alone_insufficient": authority in {"PUBLIC_MARKETING", AUTH_UNKNOWN, "INFERRED"},
    }


def assess_financing_compatibility(
    *,
    profile: dict[str, Any],
    funding_requirement: dict[str, Any],
    operator_fico: int | None = None,
    operator_cash_available: float | None = None,
    pg_acceptable: bool | None = None,
    path_lifecycle: str | None = None,
) -> dict[str, Any]:
    """Compatibility assessment — does NOT predict approval. UNKNOWN ≠ incompatible."""
    reasons: list[str] = []
    amount = funding_requirement.get("financing_amount_required")
    max_fin_cost = funding_requirement.get("maximum_financing_cost")
    max_cash = funding_requirement.get("maximum_cash_contribution") or 0.0
    norms = normalize_pg_credit_fico_states(profile)
    affirmative: dict[str, Any] | None = None

    state = FIN_UNKNOWN
    path_state = path_lifecycle or PATH_UNTESTED
    min_fico = profile.get("minimum_fico")

    # Affirmative hard fails only
    if min_fico is not None and operator_fico is not None and operator_fico < int(min_fico):
        affirmative = record_affirmative_incompatibility(
            financier=str(profile.get("financier") or "UNKNOWN"),
            reason=f"verified_minimum_fico_{min_fico}_exceeds_operator_profile_{operator_fico}",
            evidence=f"minimum_fico={min_fico}; operator_fico={operator_fico}",
            authority=AUTH_OFFICIAL,
            curable=False,
        )
        return {
            "kind": "FinancingCompatibilityAssessment",
            "financier": profile.get("financier"),
            "state": FIN_INCOMPATIBLE,
            "financier_level_state": FINANCIER_INCOMPATIBLE,
            "path_state": PATH_VERIFIED_BAD,
            "reasons": [affirmative["reason"]],
            "affirmative_incompatibility": affirmative,
            "pg_required": norms["pg_state"] == PG_REQUIRED,
            "personal_credit_dependent": norms["personal_credit_dependency_state"] == CREDIT_DEP,
            "predicts_approval": False,
            "unknown_preserved": True,
            **norms,
        }

    if amount is not None:
        mn = profile.get("minimum_transaction_size")
        mx = profile.get("maximum_transaction_size")
        if mn is not None and amount < mn:
            affirmative = record_affirmative_incompatibility(
                financier=str(profile.get("financier") or "UNKNOWN"),
                reason="verified_below_minimum_transaction_size",
                evidence=f"amount={amount}; minimum={mn}",
                authority=AUTH_OFFICIAL,
            )
            state = FIN_INCOMPATIBLE
            path_state = PATH_VERIFIED_BAD
            reasons.append(affirmative["reason"])
        if mx is not None and amount > mx:
            affirmative = record_affirmative_incompatibility(
                financier=str(profile.get("financier") or "UNKNOWN"),
                reason="verified_above_maximum_transaction_size",
                evidence=f"amount={amount}; maximum={mx}",
                authority=AUTH_OFFICIAL,
            )
            state = FIN_INCOMPATIBLE
            path_state = PATH_VERIFIED_BAD
            reasons.append(affirmative["reason"])

    # Cash UNKNOWN → verification required, never silent rejection
    cash = profile.get("cash_contribution") or "UNKNOWN"
    if cash == "UNKNOWN":
        reasons.append("cash_contribution_UNKNOWN_requires_verification")
        if state not in {FIN_INCOMPATIBLE}:
            state = FIN_UNCERTAINTY
            path_state = PATH_VERIFY_REQ
    elif cash == "REQUIRED":
        reasons.append("cash_contribution_REQUIRED_amount_must_be_verified")
        if operator_cash_available == 0 and float(max_cash) == 0:
            # Still not exhausted globally — this path may be likely incompatible
            reasons.append("cash_contribution_required_but_operator_cash_and_max_are_zero")
            if state != FIN_INCOMPATIBLE:
                state = FIN_LIKELY_INCOMPAT
                path_state = PATH_VERIFY_REQ  # alternatives may cover
        elif state == FIN_UNKNOWN:
            state = FIN_UNCERTAINTY
            path_state = PATH_VERIFY_REQ

    # PG: never auto-incompatible
    if norms["pg_state"] == PG_REQUIRED:
        reasons.append("PG_REQUIRED_distinct_from_personal_credit_dependency")
        if pg_acceptable is False:
            state = FIN_LIKELY_INCOMPAT if state != FIN_INCOMPATIBLE else state
            reasons.append("operator_rejects_PG")
            path_state = PATH_VERIFY_REQ
        elif state == FIN_UNKNOWN:
            state = FIN_POTENTIAL
            path_state = PATH_VERIFY_REQ

    if norms["personal_credit_dependency_state"] == CREDIT_DEP:
        reasons.append("personal_credit_dependency=MATERIAL_OR_PRIMARY")
        if state not in {FIN_INCOMPATIBLE, FIN_LIKELY_INCOMPAT}:
            state = FIN_UNCERTAINTY
            path_state = PATH_VERIFY_REQ
    elif norms["personal_credit_dependency_state"] == CREDIT_DEP_UNKNOWN:
        reasons.append("personal_credit_dependency_UNKNOWN")

    if norms["pg_state"] == PG_NOT_REQUIRED and norms["personal_credit_dependency_state"] == CREDIT_NOT_MATERIAL:
        reasons.append("no_pg_and_credit_not_material")
        if state == FIN_UNKNOWN:
            state = FIN_POTENTIAL
            path_state = PATH_PUBLIC_POSSIBLE if profile.get("evidence") else PATH_VERIFY_REQ

    if norms["minimum_fico_state"] in {FICO_UNKNOWN, FICO_NOT_STATED}:
        reasons.append("minimum_fico_UNKNOWN_not_a_rejection")

    if state == FIN_UNKNOWN and profile.get("evidence"):
        state = FIN_UNCERTAINTY
        path_state = PATH_PUBLIC_POSSIBLE
        reasons.append("public_evidence_present_but_transaction_terms_unverified")
    elif state == FIN_UNKNOWN:
        path_state = PATH_UNTESTED
        reasons.append("insufficient_evidence_UNKNOWN_preserved")

    if state == FIN_POTENTIAL and path_state == PATH_UNTESTED:
        path_state = PATH_VERIFY_REQ

    if state == FIN_INCOMPATIBLE and affirmative is None:
        # Should not happen without affirmative record — force verify instead
        state = FIN_UNCERTAINTY
        path_state = PATH_VERIFY_REQ
        reasons.append("incompatibility_without_affirmative_evidence_downgraded")

    return {
        "kind": "FinancingCompatibilityAssessment",
        "financier": profile.get("financier"),
        "state": state,
        "financier_level_state": FINANCIER_INCOMPATIBLE if state == FIN_INCOMPATIBLE else state,
        "path_state": path_state,
        "reasons": reasons or ["insufficient_evidence"],
        "affirmative_incompatibility": affirmative,
        "pg_required": norms["pg_state"] == PG_REQUIRED,
        "pg_distinct_from_fico": True,
        "personal_credit_pull": profile.get("personal_credit_pull"),
        "minimum_fico": min_fico,
        "personal_credit_dependency": profile.get("personal_credit_dependency"),
        "underwriting_model": profile.get("underwriting_model"),
        "broker_vs_direct": profile.get("broker_vs_direct"),
        "maximum_financing_cost_for_deal": max_fin_cost,
        "predicts_approval": False,
        "unknown_preserved": True,
        **norms,
    }


def funding_path_coverage(
    compatibility: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """How many paths identified / untested / require verification / exhausted?"""
    comps = compatibility or []
    counts = {
        "identified": len(comps),
        "untested": 0,
        "public_evidence_supports_possibility": 0,
        "verification_required": 0,
        "conditionally_feasible": 0,
        "verified_compatible": 0,
        "verified_incompatible": 0,
        "transaction_declines": 0,
        "expired": 0,
    }
    for c in comps:
        ps = c.get("path_state") or PATH_UNTESTED
        if ps == PATH_UNTESTED:
            counts["untested"] += 1
        elif ps == PATH_PUBLIC_POSSIBLE:
            counts["public_evidence_supports_possibility"] += 1
        elif ps in {PATH_VERIFY_REQ, "TRANSACTION_INQUIRY_PREPARED"}:
            counts["verification_required"] += 1
        elif ps == PATH_CONDITIONAL:
            counts["conditionally_feasible"] += 1
        elif ps == PATH_VERIFIED_OK:
            counts["verified_compatible"] += 1
        elif ps == PATH_VERIFIED_BAD:
            counts["verified_incompatible"] += 1
        elif ps == PATH_DECLINED:
            counts["transaction_declines"] += 1
        elif ps == "EXPIRED":
            counts["expired"] += 1
        else:
            # UNKNOWN/MATERIAL → still needs verification
            counts["verification_required"] += 1

    remaining = (
        counts["untested"]
        + counts["public_evidence_supports_possibility"]
        + counts["verification_required"]
        + counts["conditionally_feasible"]
        + counts["verified_compatible"]
    )
    return {
        "kind": "FundingPathCoverage",
        **counts,
        "reasonable_paths_still_available": remaining > 0,
        "transaction_funding_exhausted": remaining == 0 and counts["identified"] > 0,
        "untested_is_not_negative": True,
    }


def recognized_funding_structures() -> list[dict[str, Any]]:
    """Legitimate structures M3 may recognize — availability not fabricated."""
    return [
        {"structure": STRUCT_PO, "availability": "UNKNOWN"},
        {"structure": STRUCT_PO_AR, "availability": "UNKNOWN"},
        {"structure": STRUCT_FACTORING, "availability": "UNKNOWN"},
        {"structure": STRUCT_SUPPLIER_TERMS, "availability": "UNKNOWN"},
        {"structure": STRUCT_DEPOSIT_PO, "availability": "UNKNOWN"},
        {"structure": STRUCT_DISTRIBUTOR, "availability": "UNKNOWN"},
        {"structure": STRUCT_GOV_CONTRACT, "availability": "UNKNOWN"},
        {"structure": STRUCT_WC, "availability": "UNKNOWN"},
        {"structure": STRUCT_COMBO, "availability": "UNKNOWN"},
    ]


def evaluate_funding_gate(
    *,
    funding_requirement: dict[str, Any] | None,
    compatibility: list[dict[str, Any]] | None = None,
    indication_received: bool = False,
    verified_for_transaction: bool = False,
    economically_attractive: bool = False,
    economics_cannot_support_funding: bool = False,
    hard_structural_constraint: bool = False,
) -> dict[str, Any]:
    """
    NOT YET VERIFIED != NOT POSSIBLE.
    TRANSACTION_FUNDING_EXHAUSTED is a high-bar state.
    One financier incompatible does not exhaust the transaction.
    """
    req = funding_requirement or {}
    coverage = funding_path_coverage(compatibility)

    if req.get("funding_required") is False:
        return {
            "state": FUND_NOT_REQUIRED,
            "label": None,
            "economically_attractive_but_funding_unverified": False,
            "transaction_funding_exhausted": False,
            "path_coverage": coverage,
            "pursuit": PURSUIT_KEEP,
            "separate_from_economics_gate": True,
        }

    if req.get("funding_required") is None or req.get("financing_amount_required") is None:
        label = ECON_ATTRACTIVE_FUNDING_VERIFY_REQ if economically_attractive else None
        return {
            "state": FUND_REQ_UNKNOWN,
            "label": label,
            "economically_attractive_but_funding_unverified": economically_attractive,
            "economically_attractive_funding_verification_required": economically_attractive,
            "transaction_funding_exhausted": False,
            "path_coverage": coverage,
            "pursuit": PURSUIT_KEEP_UNCERTAIN if economically_attractive else PURSUIT_KEEP,
            "separate_from_economics_gate": True,
        }

    comps = compatibility or []

    # High-bar exhaustion only
    exhausted = False
    exhaustion_reasons: list[str] = []
    if economics_cannot_support_funding:
        exhausted = True
        exhaustion_reasons.append("transaction_economics_cannot_support_commercially_reasonable_funding")
    if hard_structural_constraint:
        exhausted = True
        exhaustion_reasons.append("hard_structural_constraint_with_authoritative_evidence")
    if comps and coverage.get("transaction_funding_exhausted"):
        # All paths verified incompatible / declined — no remaining reasonable paths
        exhausted = True
        exhaustion_reasons.append("all_reasonable_identified_funding_paths_have_affirmative_incompatibility")

    if exhausted:
        return {
            "state": FUND_EXHAUSTED,
            "legacy_state": FUND_INCOMPATIBLE,
            "label": None,
            "economically_attractive_but_funding_unverified": False,
            "transaction_funding_exhausted": True,
            "exhaustion_reasons": exhaustion_reasons,
            "path_coverage": coverage,
            "pursuit": PURSUIT_STOP_FUNDING,
            "separate_from_economics_gate": True,
        }

    if verified_for_transaction:
        return {
            "state": FUND_VERIFIED,
            "label": None,
            "economically_attractive_but_funding_unverified": False,
            "transaction_funding_exhausted": False,
            "path_coverage": coverage,
            "pursuit": PURSUIT_KEEP,
            "separate_from_economics_gate": True,
        }
    if indication_received:
        return {
            "state": FUND_COND_FEASIBLE,
            "label": None,
            "economically_attractive_but_funding_unverified": False,
            "transaction_funding_exhausted": False,
            "path_coverage": coverage,
            "pursuit": PURSUIT_KEEP,
            "separate_from_economics_gate": True,
        }

    # Remaining paths → verification required (never treat LIKELY_INCOMPAT as global fail)
    if any(c.get("state") in {FIN_COMPATIBLE, FIN_POTENTIAL} for c in comps):
        state = FUND_PATH_ID
    elif comps:
        state = FUND_VERIFY_REQ
    else:
        state = FUND_RESEARCH_ONLY

    # Prefer VERIFY_REQ label when attractive and not yet verified
    unverified = economically_attractive and state in {
        FUND_PATH_ID,
        FUND_VERIFY_REQ,
        FUND_RESEARCH_ONLY,
        FUND_REQ_UNKNOWN,
    }
    # Normalize attractive financed deals toward VERIFICATION_REQUIRED messaging
    display_state = FUND_VERIFY_REQ if unverified and state in {FUND_PATH_ID, FUND_RESEARCH_ONLY} else state
    label = ECON_ATTRACTIVE_FUNDING_VERIFY_REQ if unverified else None
    if unverified:
        # keep alias for older consumers
        alias = ECON_ATTRACTIVE_FUNDING_UNVERIFIED
    else:
        alias = None

    return {
        "state": display_state if unverified else state,
        "underlying_state": state,
        "label": label,
        "legacy_label": alias,
        "economically_attractive_but_funding_unverified": unverified,
        "economically_attractive_funding_verification_required": unverified,
        "transaction_funding_exhausted": False,
        "path_coverage": coverage,
        "pursuit": PURSUIT_KEEP_UNCERTAIN if unverified else PURSUIT_KEEP,
        "not_yet_verified_is_not_impossible": True,
        "separate_from_economics_gate": True,
        "recognized_structures": recognized_funding_structures(),
    }


def record_financing_outcome(
    *,
    financier: str,
    transaction_type: str = "government_product_resale",
    government_customer: str | None = None,
    transaction_size: float | None = None,
    supplier_structure: str | None = None,
    advance_requested: float | None = None,
    company_age: str | None = None,
    revenue_history_context: str | None = None,
    pg: str | None = None,
    credit_pull: str | None = None,
    fico_requirement: int | None = None,
    cash_contribution: str | None = None,
    decision: str,
    decline_reason: str | None = None,
    conditions: str | None = None,
    cost: float | None = None,
    time_to_decision: str | None = None,
) -> dict[str, Any]:
    """Structured historical evidence only — no ML, no universal rules from one outcome."""
    return {
        "kind": "FinancingOutcomeEvidence",
        "financier": financier,
        "transaction_type": transaction_type,
        "government_customer": government_customer,
        "transaction_size": transaction_size,
        "supplier_structure": supplier_structure,
        "advance_requested": advance_requested,
        "company_age": company_age,
        "revenue_history_context": revenue_history_context,
        "pg": pg or "UNKNOWN",
        "credit_pull": credit_pull or "UNKNOWN",
        "fico_requirement": fico_requirement,
        "cash_contribution": cash_contribution or "UNKNOWN",
        "decision": decision,
        "decline_reason": decline_reason,
        "conditions": conditions,
        "cost": cost,
        "time_to_decision": time_to_decision,
        "recorded_at": now_utc().isoformat(),
        "generalizes_to_all_lenders": False,
        "ml_inference": False,
    }


def financing_verification_script(
    *,
    funding_requirement: dict[str, Any],
    award_value: float | None = None,
    agency: str | None = None,
    supplier: str | None = None,
    delivery_period: str | None = None,
) -> dict[str, Any]:
    """PREPARE ONLY — do not send."""
    amt = funding_requirement.get("financing_amount_required")
    acq = funding_requirement.get("acquisition_cost")
    return {
        "kind": "FinancingVerificationScript",
        "proposed_action": FUTURE_ACTION,
        "sent": False,
        "script": (
            f"We have a government product-resale transaction with:\n"
            f"Award value: ${award_value or 'X'}\n"
            f"Supplier cost: ${acq or 'Y'}\n"
            f"Funding requirement: ${amt or 'Z'}\n"
            f"Government customer: {agency or 'AGENCY'}\n"
            f"Supplier: {supplier or 'SUPPLIER'}\n"
            f"Delivery period: {delivery_period or 'X days'}\n"
            f"Government payment terms: {funding_requirement.get('government_payment_terms')}\n\n"
            f"Can you finance this transaction?\n\n"
            f"Please confirm:\n"
            f"1. maximum advance\n"
            f"2. supplier-payment mechanics\n"
            f"3. expected financing cost\n"
            f"4. whether a PG is required\n"
            f"5. whether personal credit is pulled\n"
            f"6. whether there is a minimum FICO\n"
            f"7. whether approval depends materially on personal credit\n"
            f"8. whether cash contribution is required\n"
            f"9. whether startup operating history is required\n"
            f"10. whether assignment/lockbox is required\n"
            f"11. required documents\n"
            f"12. expected approval timeline"
        ),
    }


# Alias kept for imports that expect CV_FINANCIER_PATH_FAILED nearby
_FINANCIER_PATH_FAILED = CV_FINANCIER_PATH_FAILED
