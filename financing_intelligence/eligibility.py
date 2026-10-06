"""Per-deal lender eligibility checklist (PASS / FAIL / UNKNOWN)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from financing_intelligence.constants import (
    BUILD,
    CASE_BY_CASE,
    CREDIT_HARD,
    NO,
    PG_REQUIRED,
    ST_EXECUTION_FAIL,
    ST_NEEDS_LENDER_APPROVAL,
    UNKNOWN,
    YES,
)
from financing_intelligence.store import load_owner_prefs, money, money_str


def _pct(rules: dict[str, Any], key: str) -> Decimal | None:
    raw = rules.get(key)
    if raw in (None, "", UNKNOWN):
        return None
    try:
        return Decimal(str(raw).replace("%", "").strip())
    except Exception:
        return None


def _tri(rules: dict[str, Any], key: str) -> str:
    v = str(rules.get(key) or UNKNOWN).upper()
    if v in {YES, NO, "CONDITIONAL", CASE_BY_CASE, UNKNOWN}:
        return v
    return UNKNOWN


def _check(label: str, result: str, detail: str | None = None) -> dict[str, Any]:
    return {"check": label, "result": result, "detail": detail}


def evaluate_lender_eligibility(
    *,
    source: dict[str, Any],
    rules: dict[str, Any],
    contract_value: Decimal,
    supplier_cost: Decimal,
    freight: Decimal,
    gross_margin_pct: Decimal | None,
    jurisdiction: str = "FEDERAL",
    deal_type: str = "PRODUCT_RESALE",
    prefs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    prefs = prefs or load_owner_prefs()
    checks: list[dict[str, Any]] = []
    hard_fail = False
    missing: list[str] = []

    min_size = money(rules.get("minimum_transaction_size") or rules.get("preferred_deal_size_min") or 0)
    max_size = money(rules.get("maximum_transaction_size") or 0)
    if min_size > 0 and contract_value < min_size:
        checks.append(_check("Deal Size", "FAIL", f"below minimum ${money_str(min_size)}"))
        hard_fail = True
    elif max_size > 0 and contract_value > max_size:
        checks.append(_check("Deal Size", "FAIL", f"above maximum ${money_str(max_size)}"))
        hard_fail = True
    elif min_size == 0 and max_size == 0:
        checks.append(_check("Deal Size", "UNKNOWN", "no size rules verified"))
        missing.append("deal_size_rules")
    else:
        checks.append(_check("Deal Size", "PASS"))

    j = jurisdiction.upper()
    key_map = {
        "FEDERAL": "federal_contracts_accepted",
        "STATE": "state_contracts_accepted",
        "LOCAL": "local_contracts_accepted",
    }
    key = key_map.get(j, "federal_contracts_accepted")
    fed = _tri(rules, key if rules.get(key) is not None else "federal_contracts_accepted")
    label = "Government Buyer"
    if fed == NO:
        checks.append(_check(label, "FAIL", f"{j} not accepted"))
        hard_fail = True
    elif fed == UNKNOWN:
        checks.append(_check(label, "UNKNOWN"))
        missing.append("government_buyer_acceptance")
    else:
        checks.append(_check(label, "PASS"))

    dt = deal_type.upper()
    if "PRODUCT" in dt or dt == "PRODUCT_RESALE":
        pr = _tri(rules, "product_resale_supported")
        if pr == NO:
            checks.append(_check("Product Resale", "FAIL"))
            hard_fail = True
        elif pr == UNKNOWN:
            checks.append(_check("Product Resale", "UNKNOWN"))
            missing.append("product_resale_supported")
        else:
            checks.append(_check("Product Resale", "PASS"))

    startup = _tri(rules, "startup_new_company_allowed")
    if startup == NO:
        checks.append(_check("Startup Company", "FAIL"))
        hard_fail = True
    elif startup == UNKNOWN:
        checks.append(_check("Startup Company", "UNKNOWN"))
        missing.append("startup_new_company_allowed")
    else:
        checks.append(_check("Startup Company", "PASS"))

    pg = str(rules.get("personal_guarantee") or UNKNOWN).upper()
    if pg == PG_REQUIRED and not prefs.get("personal_guarantee_allowed"):
        checks.append(_check("Personal Guarantee", "FAIL", "PG required; owner disallows"))
        hard_fail = True
    elif pg in {UNKNOWN, CASE_BY_CASE}:
        checks.append(_check("Personal Guarantee", "UNKNOWN"))
        missing.append("personal_guarantee")
    else:
        checks.append(_check("Personal Guarantee", "PASS"))

    credit = str(rules.get("personal_credit") or UNKNOWN).upper()
    if credit == CREDIT_HARD and not prefs.get("personal_credit_dependency_allowed"):
        checks.append(_check("Personal Credit", "FAIL", "hard pull required; owner disallows"))
        hard_fail = True
    elif credit == UNKNOWN:
        checks.append(_check("Personal Credit", "UNKNOWN"))
        missing.append("personal_credit")
    else:
        checks.append(_check("Personal Credit", "PASS"))

    adv = _pct(rules, "max_advance_pct") or _pct(rules, "typical_advance_pct")
    if adv is None:
        checks.append(_check("Advance Requirement", "UNKNOWN"))
        missing.append("max_advance_pct")
    else:
        checks.append(_check("Advance Requirement", "PASS", f"{adv}%"))

    freight_fund = _tri(rules, "can_fund_freight")
    if freight > 0:
        if freight_fund == NO:
            checks.append(_check("Freight Funding", "FAIL", "freight not fundable"))
        elif freight_fund == UNKNOWN:
            checks.append(_check("Freight Funding", "UNKNOWN"))
            missing.append("freight_funding_confirmation")
        else:
            checks.append(_check("Freight Funding", "PASS"))
    else:
        checks.append(_check("Freight Funding", "PASS", "no freight"))

    min_margin = _pct(rules, "minimum_gross_margin_pct")
    if min_margin is not None and gross_margin_pct is not None:
        if gross_margin_pct < min_margin:
            checks.append(_check("Margin Requirement", "FAIL", f"need {min_margin}%"))
            hard_fail = True
        else:
            checks.append(_check("Margin Requirement", "PASS"))
    elif min_margin is not None:
        checks.append(_check("Margin Requirement", "UNKNOWN", "deal margin unknown"))
        missing.append("deal_margin")
    else:
        checks.append(_check("Margin Requirement", "UNKNOWN", "no lender margin rule"))

    checks.append(_check("Timing", "UNKNOWN", "transaction-specific"))
    missing.append("transaction_specific_underwriting")

    return {
        "kind": "LenderEligibilityEvaluation",
        "build": BUILD,
        "lender": source.get("company_name"),
        "source_id": source.get("source_id"),
        "checks": checks,
        "result": ST_EXECUTION_FAIL if hard_fail else ST_NEEDS_LENDER_APPROVAL,
        "hard_fail": hard_fail,
        "missing": missing,
        "never_auto_approved": True,
        "note": "Structurally compatible ≠ deal approved.",
    }
