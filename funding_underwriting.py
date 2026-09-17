"""Pre-bid transaction financing underwriting — personal credit ≠ personal guarantee.

Hierarchy:
  PREFERRED | WORKABLE_WITH_OPERATOR_APPROVAL | NEEDS_VERIFICATION | REJECT

A personal guarantee alone does NOT auto-REJECT. Operator decides PG risk.
"""

from __future__ import annotations

from typing import Any

from application_clock import now_utc
from funding_path_constants import (
    EV_INFERRED,
    EV_UNKNOWN,
    EV_VERIFIED_BY_CALL,
    EV_VERIFIED_PUBLIC,
    MATCH_CONDITIONAL,
    MATCH_MATCH,
    MATCH_NEEDS_VERIFICATION,
    MATCH_REJECT,
    PG_CONDITIONAL,
    PG_NOT_REQUIRED,
    PG_REQUIRED,
    PG_UNKNOWN,
    VERIFIED_EVIDENCE,
)

# --- Path fit hierarchy (replaces PG auto-reject) ---
PATH_PREFERRED = "PREFERRED"
PATH_WORKABLE_OPERATOR_APPROVAL = "WORKABLE_WITH_OPERATOR_APPROVAL"
PATH_NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
PATH_REJECT = "REJECT"

# --- Personal guarantee types ---
GUARANTEE_NO_PG = "NO_PG"
GUARANTEE_FULL_RECOURSE = "FULL_RECOURSE_PG"
GUARANTEE_LIMITED = "LIMITED_PG"
GUARANTEE_BAD_BOY = "BAD_BOY_GUARANTEE"
GUARANTEE_CONDITIONAL = "CONDITIONAL_PG"
GUARANTEE_UNKNOWN = "UNKNOWN"

# --- Personal credit pull ---
CREDIT_NONE = "NONE"
CREDIT_SOFT_PULL = "SOFT_PULL"
CREDIT_HARD_PULL = "HARD_PULL"
CREDIT_UNKNOWN = "UNKNOWN"

# --- Personal credit underwriting role ---
CREDIT_ROLE_NOT_CONSIDERED = "NOT_CONSIDERED"
CREDIT_ROLE_INFORMATIONAL = "INFORMATIONAL_ONLY"
CREDIT_ROLE_SECONDARY = "SECONDARY_FACTOR"
CREDIT_ROLE_MATERIAL = "MATERIAL_UNDERWRITING_FACTOR"
CREDIT_ROLE_PRIMARY = "PRIMARY_UNDERWRITING_FACTOR"
CREDIT_ROLE_UNKNOWN = "UNKNOWN"

# --- Funding timing ---
TIMING_COMPATIBLE = "FUNDING_TIMING_COMPATIBLE"
TIMING_AT_RISK = "FUNDING_TIMING_AT_RISK"
TIMING_INCOMPATIBLE = "FUNDING_TIMING_INCOMPATIBLE"
TIMING_UNKNOWN = "FUNDING_TIMING_UNKNOWN"

# --- Pre-bid financing deal statuses ---
PREBID_NOT_READY = "FUNDING_NOT_YET_READY_FOR_REVIEW"
PREBID_READY_FOR_REVIEW = "READY_FOR_PRE_BID_FINANCING_REVIEW"
PREBID_REVIEW_REQUIRED = "PRE_BID_FINANCING_REVIEW_REQUIRED"
PREBID_CONDITIONAL_PATH = "CONDITIONAL_FUNDING_PATH_IDENTIFIED"
PREBID_VERIFIED = "FUNDING_PATH_VERIFIED_PRE_BID"
PREBID_REJECTED = "FUNDING_PATH_REJECTED"
PREBID_POST_AWARD = "POST_AWARD_VERIFICATION_REQUIRED"
PREBID_UNKNOWN = "UNKNOWN"

# --- Provider public fit (not approval) ---
FIT_STRONG_PUBLIC = "STRONG_PUBLIC_FIT"
FIT_POSSIBLE_PUBLIC = "POSSIBLE_PUBLIC_FIT"
FIT_NEEDS_CONFIRMATION = "NEEDS_DEAL_CONFIRMATION"
FIT_KNOWN_MISMATCH = "KNOWN_MISMATCH"
FIT_UNKNOWN = "UNKNOWN"

# --- Evidence classes (funding-specific) ---
EV_CLASS_PROVIDER_OFFICIAL_CURRENT = "PROVIDER_OFFICIAL_CURRENT"
EV_CLASS_PROVIDER_OFFICIAL_ARCHIVED = "PROVIDER_OFFICIAL_ARCHIVED"
EV_CLASS_PROVIDER_CASE_STUDY = "PROVIDER_CASE_STUDY"
EV_CLASS_PROVIDER_INTERVIEW = "PROVIDER_INTERVIEW"
EV_CLASS_GOVERNMENT_REFERENCE = "GOVERNMENT_REFERENCE"
EV_CLASS_REPUTABLE_SECONDARY = "REPUTABLE_SECONDARY"
EV_CLASS_MARKETING_AGGREGATOR = "MARKETING_AGGREGATOR"
EV_CLASS_INFERRED = "INFERRED"
EV_CLASS_UNKNOWN = "UNKNOWN"

# Operator profile for credit evaluation (deal-specific fact, not global lender veto)
DEFAULT_OPERATOR_PERSONAL_FICO = 480

# Profit floor
MINIMUM_ACTUAL_PROFIT_USD = 10000.0


def empty_personal_guarantee_model() -> dict[str, Any]:
    return {
        "guarantee_state": GUARANTEE_UNKNOWN,
        "guarantor": None,
        "guaranteed_amount": None,
        "percentage_guaranteed": None,
        "duration": None,
        "burn_off_release_conditions": None,
        "recourse_triggers": None,
        "fraud_misrepresentation_carveouts": None,
        "diversion_of_proceeds_provisions": None,
        "collection_cost_provisions": None,
        "cross_default": None,
        "cross_collateralization": None,
        "continuing_guarantee": None,
        "survival_after_payoff": None,
        "source": None,
        "provenance": None,
        "evidence_state": EV_UNKNOWN,
        "requires_legal_review": True,
        "legal_advice": False,
        "note": "Not legal advice. Unusual terms route to OPERATOR/LEGAL REVIEW.",
    }


def parse_guarantee_state(
    *,
    personal_guarantee: str | None = None,
    guarantee_type: str | None = None,
    evidence_status: str | None = None,
) -> dict[str, Any]:
    """Map legacy PG fields + type hints into structured guarantee model."""
    out = empty_personal_guarantee_model()
    pg = (personal_guarantee or PG_UNKNOWN).upper()
    gtype = (guarantee_type or "").upper().strip()
    ev = (evidence_status or EV_UNKNOWN).upper()
    out["evidence_state"] = ev

    if gtype in {
        GUARANTEE_NO_PG,
        GUARANTEE_FULL_RECOURSE,
        GUARANTEE_LIMITED,
        GUARANTEE_BAD_BOY,
        GUARANTEE_CONDITIONAL,
        GUARANTEE_UNKNOWN,
    }:
        out["guarantee_state"] = gtype
    elif pg == PG_NOT_REQUIRED:
        out["guarantee_state"] = GUARANTEE_NO_PG
    elif pg == PG_REQUIRED:
        out["guarantee_state"] = GUARANTEE_FULL_RECOURSE if not gtype else (gtype or GUARANTEE_FULL_RECOURSE)
        if out["guarantee_state"] == GUARANTEE_UNKNOWN:
            out["guarantee_state"] = GUARANTEE_FULL_RECOURSE
    elif pg == PG_CONDITIONAL:
        out["guarantee_state"] = GUARANTEE_CONDITIONAL
    else:
        out["guarantee_state"] = GUARANTEE_UNKNOWN

    out["requires_legal_review"] = out["guarantee_state"] not in {GUARANTEE_NO_PG}
    return out


def empty_personal_credit_model() -> dict[str, Any]:
    return {
        "personal_credit_checked": CREDIT_UNKNOWN,
        "personal_credit_role": CREDIT_ROLE_UNKNOWN,
        "minimum_fico": None,
        "poor_credit_explicitly_acceptable": None,
        "startup_eligible": None,
        "first_contract_eligible": None,
        "business_credit_required": None,
        "minimum_time_in_business": None,
        "minimum_revenue": None,
        "minimum_prior_government_contract_history": None,
        "evidence": {},
        "confidence": "UNKNOWN",
    }


def normalize_credit_checked(value: Any) -> str:
    if value is None or value == "" or str(value).upper() == "UNKNOWN":
        return CREDIT_UNKNOWN
    if value is False or str(value).upper() in {"NONE", "NO", "FALSE", "0"}:
        return CREDIT_NONE
    s = str(value).upper()
    if s in {CREDIT_NONE, CREDIT_SOFT_PULL, CREDIT_HARD_PULL, CREDIT_UNKNOWN}:
        return s
    if s in {"SOFT", "SOFT_PULL", "SOFTPULL"}:
        return CREDIT_SOFT_PULL
    if s in {"HARD", "HARD_PULL", "HARDPULL"}:
        return CREDIT_HARD_PULL
    if value is True:
        return CREDIT_UNKNOWN  # checked=True without pull type ≠ HARD automatically
    return CREDIT_UNKNOWN


def build_personal_credit_model(source: dict[str, Any] | None = None) -> dict[str, Any]:
    src = source or {}
    out = empty_personal_credit_model()
    checked_raw = src.get("personal_credit_checked")
    # Prefer explicit pull type field when present
    pull = src.get("personal_credit_pull") or src.get("credit_pull_type")
    if pull:
        out["personal_credit_checked"] = normalize_credit_checked(pull)
    elif checked_raw is False:
        out["personal_credit_checked"] = CREDIT_NONE
    elif checked_raw is True:
        out["personal_credit_checked"] = normalize_credit_checked(
            src.get("personal_credit_pull") or CREDIT_UNKNOWN
        )
    else:
        out["personal_credit_checked"] = normalize_credit_checked(checked_raw)

    role = src.get("personal_credit_role") or src.get("personal_credit_underwriting_role")
    if role:
        out["personal_credit_role"] = str(role).upper()
    elif out["personal_credit_checked"] == CREDIT_NONE:
        # Explicit no-pull (False/NONE) → not considered; do not require separate marketing claim
        out["personal_credit_role"] = CREDIT_ROLE_NOT_CONSIDERED
    elif src.get("personal_credit_used_for_approval") is False:
        # Marketing "not primary" must NOT become NOT_CONSIDERED without stronger evidence
        out["personal_credit_role"] = CREDIT_ROLE_UNKNOWN
    elif src.get("poor_credit_explicitly_acceptable") is True:
        out["personal_credit_role"] = CREDIT_ROLE_INFORMATIONAL
    else:
        out["personal_credit_role"] = CREDIT_ROLE_UNKNOWN

    fico = src.get("minimum_personal_fico")
    try:
        out["minimum_fico"] = float(fico) if fico is not None and str(fico).upper() != "UNKNOWN" else None
    except (TypeError, ValueError):
        out["minimum_fico"] = None

    out["poor_credit_explicitly_acceptable"] = src.get("poor_credit_explicitly_acceptable")
    out["startup_eligible"] = (
        src.get("startup_eligible")
        if src.get("startup_eligible") is not None
        else src.get("startup_allowed")
    )
    out["first_contract_eligible"] = (
        src.get("first_contract_eligible")
        if src.get("first_contract_eligible") is not None
        else src.get("first_government_contract_allowed")
    )
    out["business_credit_required"] = src.get("business_credit_required")
    out["minimum_time_in_business"] = src.get("minimum_time_in_business")
    out["minimum_revenue"] = src.get("minimum_annual_revenue") or src.get("minimum_historical_revenue")
    out["minimum_prior_government_contract_history"] = src.get(
        "minimum_prior_government_contract_history"
    )
    out["evidence"] = {
        k: (src.get("criteria_evidence") or {}).get(k)
        for k in (
            "personal_credit_checked",
            "personal_credit_pull",
            "personal_credit_role",
            "minimum_personal_fico",
            "poor_credit_explicitly_acceptable",
            "personal_credit_used_for_approval",
        )
        if (src.get("criteria_evidence") or {}).get(k)
    }
    return out


def _criterion_status(source: dict[str, Any], key: str) -> str:
    ev = (source.get("criteria_evidence") or {}).get(key)
    if isinstance(ev, dict):
        return (ev.get("verification_status") or EV_UNKNOWN).upper()
    return EV_UNKNOWN


def _is_verified(source: dict[str, Any], key: str) -> bool:
    return _criterion_status(source, key) in VERIFIED_EVIDENCE


def classify_path_underwriting(
    source: dict[str, Any],
    *,
    operator_personal_fico: float | int | None = DEFAULT_OPERATOR_PERSONAL_FICO,
    uncovered_operator_cash: float | None = None,
    funding_timing_status: str | None = None,
    actual_expected_profit: float | None = None,
    transaction_amount: float | None = None,
) -> dict[str, Any]:
    """
    Classify a financing path under the new hierarchy.
    PG alone → WORKABLE_WITH_OPERATOR_APPROVAL (not REJECT).
    """
    src = source or {}
    reasons: list[str] = []
    reject_reasons: list[str] = []
    verify_reasons: list[str] = []

    credit = build_personal_credit_model(src)
    guarantee = parse_guarantee_state(
        personal_guarantee=src.get("personal_guarantee"),
        guarantee_type=src.get("guarantee_type") or src.get("personal_guarantee_type"),
        evidence_status=_criterion_status(src, "personal_guarantee"),
    )

    # --- Personal credit REJECT rules ---
    min_fico = credit.get("minimum_fico")
    if (
        min_fico is not None
        and operator_personal_fico is not None
        and _is_verified(src, "minimum_personal_fico")
        and float(operator_personal_fico) < float(min_fico)
    ):
        reject_reasons.append("verified_minimum_fico_above_operator_score")

    role = credit.get("personal_credit_role")
    if role in {CREDIT_ROLE_MATERIAL, CREDIT_ROLE_PRIMARY} and _is_verified(
        src, "personal_credit_role"
    ):
        # Material/primary underwriting + operator below min or no explicit poor-credit OK
        if credit.get("poor_credit_explicitly_acceptable") is not True:
            if min_fico is not None and operator_personal_fico is not None:
                if float(operator_personal_fico) < float(min_fico):
                    reject_reasons.append("personal_credit_materially_disqualifies_operator")
            elif min_fico is None and _is_verified(src, "personal_credit_role"):
                # Material role verified but no FICO floor — needs verification, not auto-reject
                verify_reasons.append("material_credit_role_minimum_fico_unknown")

    # Legacy: personal_credit_checked True with verified min FICO above operator
    if (
        src.get("personal_credit_checked") is True
        and min_fico is not None
        and operator_personal_fico is not None
        and _is_verified(src, "personal_credit_checked")
        and _is_verified(src, "minimum_personal_fico")
        and float(operator_personal_fico) < float(min_fico)
    ):
        if "verified_minimum_fico_above_operator_score" not in reject_reasons:
            reject_reasons.append("verified_minimum_fico_above_operator_score")

    # Explicit poor credit acceptable + no min above operator → not reject on credit
    if credit.get("poor_credit_explicitly_acceptable") is True and _is_verified(
        src, "poor_credit_explicitly_acceptable"
    ):
        reasons.append("poor_credit_explicitly_acceptable")

    # Credit role UNKNOWN → verification (but NONE pull verified is OK)
    if credit.get("personal_credit_checked") == CREDIT_UNKNOWN:
        verify_reasons.append("personal_credit_role_or_pull_unknown")
    elif (
        role == CREDIT_ROLE_UNKNOWN
        and credit.get("personal_credit_checked") not in {CREDIT_NONE}
        and not _is_verified(src, "personal_credit_role")
    ):
        verify_reasons.append("personal_credit_role_or_pull_unknown")

    # --- Cash contribution ---
    cash_req = src.get("borrower_cash_contribution_required")
    cash_pct = src.get("minimum_cash_contribution_pct")
    if _is_verified(src, "borrower_cash_contribution_required"):
        if cash_req is True or (cash_pct is not None and float(cash_pct or 0) > 0):
            if uncovered_operator_cash is None or float(uncovered_operator_cash) > 0:
                reject_reasons.append("verified_personal_cash_contribution_required_uncovered")
            else:
                reasons.append("cash_contribution_covered_by_composite")
    else:
        verify_reasons.append("cash_contribution_unknown")

    # --- Transaction limits ---
    mn = src.get("minimum_transaction") or src.get("minimum_funding_amount")
    mx = src.get("maximum_transaction") or src.get("maximum_funding_amount")
    if transaction_amount is not None:
        if mn is not None and _is_verified(src, "minimum_transaction") and transaction_amount < float(mn):
            reject_reasons.append("transaction_below_verified_minimum")
        if mx is not None and _is_verified(src, "maximum_transaction") and transaction_amount > float(mx):
            reject_reasons.append("transaction_above_verified_maximum")

    # --- Timing ---
    if funding_timing_status == TIMING_INCOMPATIBLE:
        reject_reasons.append("financing_cannot_arrive_before_supplier_payment")
    elif funding_timing_status == TIMING_UNKNOWN:
        verify_reasons.append("funding_timing_unknown")
    elif funding_timing_status == TIMING_AT_RISK:
        verify_reasons.append("funding_timing_at_risk")

    # --- Profit economics ---
    if actual_expected_profit is not None and actual_expected_profit < MINIMUM_ACTUAL_PROFIT_USD:
        reject_reasons.append("actual_expected_profit_below_floor")

    # --- PG / guarantee (never auto-reject alone) ---
    pg_required = guarantee["guarantee_state"] not in {GUARANTEE_NO_PG}
    pg_unknown = guarantee["guarantee_state"] == GUARANTEE_UNKNOWN
    if pg_unknown or not _is_verified(src, "personal_guarantee"):
        verify_reasons.append("personal_guarantee_unknown")

    # --- Decide hierarchy ---
    if reject_reasons:
        path_class = PATH_REJECT
    elif pg_unknown or verify_reasons:
        # If only PG required (verified) and credit/cash/timing OK → workable
        credit_ok = (
            credit.get("personal_credit_checked") == CREDIT_NONE
            and _is_verified(src, "personal_credit_checked")
            and (
                credit.get("personal_credit_role")
                in {CREDIT_ROLE_NOT_CONSIDERED, CREDIT_ROLE_INFORMATIONAL}
                or src.get("personal_credit_used_for_approval") is False
            )
        )
        cash_ok = cash_req is False and _is_verified(src, "borrower_cash_contribution_required")
        timing_ok = funding_timing_status in {TIMING_COMPATIBLE, None}
        profit_ok = actual_expected_profit is None or actual_expected_profit >= MINIMUM_ACTUAL_PROFIT_USD

        if (
            guarantee["guarantee_state"]
            in {GUARANTEE_FULL_RECOURSE, GUARANTEE_LIMITED, GUARANTEE_BAD_BOY, GUARANTEE_CONDITIONAL}
            and _is_verified(src, "personal_guarantee")
            and credit_ok
            and cash_ok
            and timing_ok
            and profit_ok
            and not any(
                r.startswith("verified_minimum_fico") or "materially_disqualifies" in r
                for r in reject_reasons
            )
        ):
            # Strip PG-unknown from verify if PG verified required
            verify_reasons = [v for v in verify_reasons if v != "personal_guarantee_unknown"]
            if not verify_reasons:
                path_class = PATH_WORKABLE_OPERATOR_APPROVAL
                reasons.append("pg_required_but_credit_not_material_gate")
            else:
                path_class = PATH_NEEDS_VERIFICATION
        else:
            path_class = PATH_NEEDS_VERIFICATION
    else:
        # Fully known, no reject
        no_pg = guarantee["guarantee_state"] == GUARANTEE_NO_PG and _is_verified(
            src, "personal_guarantee"
        )
        no_credit = credit.get("personal_credit_checked") == CREDIT_NONE and _is_verified(
            src, "personal_credit_checked"
        )
        no_cash = cash_req is False and _is_verified(src, "borrower_cash_contribution_required")
        if no_pg and no_credit and no_cash:
            path_class = PATH_PREFERRED
            reasons.append("no_pg_no_credit_qual_no_cash")
        elif (
            guarantee["guarantee_state"] != GUARANTEE_NO_PG
            and _is_verified(src, "personal_guarantee")
            and no_credit
            and no_cash
        ):
            path_class = PATH_WORKABLE_OPERATOR_APPROVAL
            reasons.append("pg_required_operator_risk_decision")
        else:
            path_class = PATH_NEEDS_VERIFICATION

    return {
        "path_class": path_class,
        "reasons": reasons,
        "reject_reasons": reject_reasons,
        "verification_reasons": sorted(set(verify_reasons)),
        "personal_credit": credit,
        "personal_guarantee": guarantee,
        "operator_personal_fico": operator_personal_fico,
        "operator_pg_review_required": path_class == PATH_WORKABLE_OPERATOR_APPROVAL
        or (
            guarantee["guarantee_state"]
            in {GUARANTEE_FULL_RECOURSE, GUARANTEE_LIMITED, GUARANTEE_BAD_BOY, GUARANTEE_CONDITIONAL}
        ),
        "autonomous_pg_acceptance": False,
        "legal_advice": False,
        "evaluated_at": now_utc().isoformat(),
    }


def map_path_class_to_match_status(path_class: str) -> str:
    if path_class == PATH_REJECT:
        return MATCH_REJECT
    if path_class == PATH_PREFERRED:
        return MATCH_MATCH
    if path_class == PATH_WORKABLE_OPERATOR_APPROVAL:
        return MATCH_CONDITIONAL
    return MATCH_NEEDS_VERIFICATION
