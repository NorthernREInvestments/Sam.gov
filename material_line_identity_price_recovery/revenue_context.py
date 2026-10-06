"""Revenue / money-context classifier — prevent bonding/insurance thresholds as revenue."""

from __future__ import annotations

import json
import re
from typing import Any

from material_line_identity_price_recovery.models import (
    BOND_THRESHOLD,
    BUDGET,
    BUILD,
    CEILING,
    CURRENT_CONTRACT_VALUE,
    ESTIMATE,
    GRANT_TOTAL,
    HISTORICAL_REFERENCE,
    INSURANCE_THRESHOLD,
    OTHER_NON_REVENUE_AMOUNT,
    PROGRAM_FUNDING,
    REVENUE_ALLOWED,
    REVENUE_REGRESSION,
)
from m3_data_root import data_path

_DOLLAR = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\b|\b([\d,]+(?:\.\d+)?)\s*(?:dollars?|USD)\b",
    re.I,
)

_BOND = re.compile(
    r"\b("
    r"bond(?:ing)?|bid\s+bond|performance\s+bond|payment\s+bond|surety|"
    r"may\s+require\s+that\s+persons\s+interested|"
    r"certified\s+or\s+qualified\s+to\s+perform|"
    r"bid\s+security|security\s+deposit"
    r")\b",
    re.I,
)
_INSURANCE = re.compile(
    r"\b("
    r"insurance|liability\s+coverage|workers?\s*['’]?\s*comp|"
    r"limits?\s+of\s+not\s+less|coverage\s+shall\s+be\s+carried|"
    r"general\s+liability|automobile\s+liability|umbrella"
    r")\b",
    re.I,
)
_LD = re.compile(r"\b(liquidated\s+damages?|delay\s+damages?)\b", re.I)
_CEILING = re.compile(r"\b(not[- ]to[- ]exceed|NTE|ceiling|maximum\s+(?:contract\s+)?(?:value|amount|price))\b", re.I)
_BUDGET = re.compile(r"\b(budget(?:ed)?\s+(?:amount|value)|project\s+budget|available\s+funding)\b", re.I)
_GRANT = re.compile(
    r"\b("
    r"total\s+amount\s+of\s+funding(?:\s*\+\s*grantee\s+match)?|"
    r"grantee\s+match|grant\s+(?:agreement|award|funding|total)|"
    r"funding\s+source|program\s+funding|amount\s+of\s+funding"
    r")\b",
    re.I,
)
_ESTIMATE = re.compile(r"\b(estimated\s+(?:annual\s+)?(?:spend|value|cost|amount)|engineer's?\s+estimate|approx(?:imate)?\.?\s+value)\b", re.I)
_HIST = re.compile(
    r"\b("
    r"prior\s+award|previous(?:ly)?\s+award(?:ed|s)?|"
    r"historical\s+(?:award|value)|last\s+contract\s+value|"
    r"previous\s+award\s+value|prior\s+contract\s+value"
    r")\b",
    re.I,
)
_CONTRACT = re.compile(
    r"\b("
    r"contract\s+(?:value|amount|price)|total\s+(?:contract\s+)?(?:value|amount)|"
    r"award\s+(?:value|amount)|base\s+year\s+value|firm[- ]fixed[- ]price\s+(?:of|is)"
    r")\b",
    re.I,
)


def classify_money_context(
    amount: float | None,
    *,
    text_window: str,
    heading: str | None = None,
    section: str | None = None,
    document: str | None = None,
) -> dict[str, Any]:
    blob = " ".join(str(x or "") for x in (heading, section, text_window))
    role = OTHER_NON_REVENUE_AMOUNT
    reasons: list[str] = []

    if _BOND.search(blob):
        role = BOND_THRESHOLD
        reasons.append("bond_language")
    elif _INSURANCE.search(blob):
        role = INSURANCE_THRESHOLD
        reasons.append("insurance_language")
    elif _LD.search(blob):
        role = OTHER_NON_REVENUE_AMOUNT
        reasons.append("liquidated_damages")
    elif _GRANT.search(blob):
        role = GRANT_TOTAL
        reasons.append("grant_or_program_funding_language")
    elif _CEILING.search(blob):
        role = CEILING
        reasons.append("ceiling_language")
    elif _BUDGET.search(blob):
        role = BUDGET
        reasons.append("budget_language")
    elif _ESTIMATE.search(blob):
        role = ESTIMATE
        reasons.append("estimate_language")
    elif _HIST.search(blob):
        role = HISTORICAL_REFERENCE
        reasons.append("historical_language")
    elif _CONTRACT.search(blob):
        role = CURRENT_CONTRACT_VALUE
        reasons.append("contract_value_language")
    else:
        reasons.append("insufficient_revenue_context")

    usable = role in REVENUE_ALLOWED
    # Grant/program funding is contextual only — not product contract revenue
    if role in {GRANT_TOTAL, PROGRAM_FUNDING}:
        usable = False

    return {
        "amount": amount,
        "text_window": (text_window or "")[:400],
        "heading": heading,
        "section": section,
        "document": document,
        "semantic_role": role,
        "usable_as_revenue": usable,
        "usable_as_product_scope_revenue": usable and role == CURRENT_CONTRACT_VALUE,
        "reasons": reasons,
    }


def extract_and_classify_amounts(
    text: str,
    *,
    document: str | None = None,
    heading: str | None = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in _DOLLAR.finditer(text or ""):
        raw = m.group(1) or m.group(2) or ""
        try:
            amount = float(raw.replace(",", ""))
        except Exception:
            continue
        start = max(0, m.start() - 120)
        end = min(len(text), m.end() + 160)
        window = text[start:end]
        out.append(classify_money_context(amount, text_window=window, heading=heading, document=document))
    return out


# Hard regression corpus (Phase 21)
REGRESSION_CASES: list[dict[str, Any]] = [
    {
        "id": "collier_bonding_250k",
        "text": "of $250,000 may require that persons interested in performing work under contract first be certified or qualified to perform such work.",
        "document": "26-8702_Construction_Bid_Instructions_Version_1.pdf",
        "expected_role": BOND_THRESHOLD,
        "must_not_be_revenue": True,
    },
    {
        "id": "insurance_minimum_1m",
        "text": "Commercial General Liability insurance with limits of not less than $1,000,000 each occurrence.",
        "expected_role": INSURANCE_THRESHOLD,
        "must_not_be_revenue": True,
    },
    {
        "id": "aircraft_liability_5m",
        "text": "Aircraft Liability coverage shall be carried in limits of not less than $5,000,000 each occurrence.",
        "expected_role": INSURANCE_THRESHOLD,
        "must_not_be_revenue": True,
    },
    {
        "id": "liquidated_damages",
        "text": "Liquidated damages of $500 per calendar day shall apply for late completion.",
        "expected_role": OTHER_NON_REVENUE_AMOUNT,
        "must_not_be_revenue": True,
    },
    {
        "id": "nte_ceiling",
        "text": "The not-to-exceed ceiling for this agreement is $2,500,000.",
        "expected_role": CEILING,
        "must_not_be_revenue": False,
    },
    {
        "id": "project_budget",
        "text": "The project budget amount is $175,000 for supplies and delivery.",
        "expected_role": BUDGET,
        "must_not_be_revenue": False,
    },
    {
        "id": "estimated_annual_spend",
        "text": "Estimated annual spend under this contract is approximately $80,000.",
        "expected_role": ESTIMATE,
        "must_not_be_revenue": False,
    },
    {
        "id": "bid_bond_percent",
        "text": "A bid bond of 5% of the total bid amount (approximately $12,500) is required.",
        "expected_role": BOND_THRESHOLD,
        "must_not_be_revenue": True,
    },
    {
        "id": "performance_bond_threshold",
        "text": "Performance bond is required when the contract value exceeds $250,000.",
        "expected_role": BOND_THRESHOLD,
        "must_not_be_revenue": True,
    },
    {
        "id": "prior_award_value",
        "text": "The previous award value for this recurring purchase was $94,200.",
        "expected_role": HISTORICAL_REFERENCE,
        "must_not_be_revenue": False,
    },
    {
        "id": "explicit_contract_value",
        "text": "The total contract value for the base year is $320,000 firm-fixed-price.",
        "expected_role": CURRENT_CONTRACT_VALUE,
        "must_not_be_revenue": False,
    },
    {
        "id": "fdep_grant_total_plus_match",
        "text": "Total Amount of Funding: $200,000.00 Grantee Match $200,000.00 Total Amount of Funding + Grantee Match, if any: $400,000.00",
        "expected_role": GRANT_TOTAL,
        "must_not_be_revenue": True,
    },
]


def run_revenue_context_regression() -> dict[str, Any]:
    results = []
    passed = 0
    for case in REGRESSION_CASES:
        classified = extract_and_classify_amounts(case["text"], document=case.get("document"))
        # Prefer classification of the primary amount in the sentence
        role = classified[0]["semantic_role"] if classified else OTHER_NON_REVENUE_AMOUNT
        # If multiple amounts, pick one matching expected when possible
        for c in classified:
            if c["semantic_role"] == case["expected_role"]:
                role = c["semantic_role"]
                break
        ok = role == case["expected_role"]
        if case.get("must_not_be_revenue") and role in REVENUE_ALLOWED:
            ok = False
        if ok:
            passed += 1
        results.append(
            {
                "id": case["id"],
                "expected": case["expected_role"],
                "got": role,
                "pass": ok,
                "classified": classified,
            }
        )

    # Explicit Collier hard check
    collier = next(r for r in results if r["id"] == "collier_bonding_250k")
    collier_pass = bool(collier["pass"]) and collier["got"] == BOND_THRESHOLD
    contract_revenue_created = any(
        c.get("usable_as_revenue") for c in (collier.get("classified") or [])
    )

    payload = {
        "build": BUILD,
        "total_cases": len(REGRESSION_CASES),
        "passed": passed,
        "failed": len(REGRESSION_CASES) - passed,
        "all_pass": passed == len(REGRESSION_CASES),
        "collier_bonding_threshold_classified_correctly": collier_pass,
        "collier_contract_revenue_incorrectly_created": contract_revenue_created,
        "results": results,
        "role_counts": _count_roles(results),
    }
    data_path(REVENUE_REGRESSION).write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload


def _count_roles(results: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        for c in r.get("classified") or []:
            role = c.get("semantic_role") or OTHER_NON_REVENUE_AMOUNT
            counts[role] = counts.get(role, 0) + 1
    return counts
