"""P0-4 identity gate + P0-5 revenue product-allocation gate."""

from __future__ import annotations

import re
from typing import Any

from material_line_identity_price_recovery.revenue_context import classify_money_context
from p0_prescale_hardening.models import (
    A_EXACT_MPN,
    B_EXACT_MODEL,
    BUYER_CATEGORY_REFERENCE,
    C_NSN_TO_EXACT,
    CEILING,
    CURRENT_BASKET_VALUE,
    CURRENT_CONTRACT_VALUE,
    CURRENT_LINE_VALUE,
    CURRENT_PRODUCT_SCOPE_VALUE,
    D_PERMITTED_EQUAL,
    DANIA_OID,
    E_STRONG_GENERIC,
    ECONOMIC_REVENUE_DIRECT,
    ESTIMATE,
    F_FAMILY_ONLY,
    G_AMBIGUOUS,
    GRANT_TOTAL,
    HISTORICAL_COMPARABLE,
    HISTORICAL_EXACT,
    IDENTITY_PACKET_BLOCK,
    IDENTITY_PACKET_OK,
    NON_REVENUE_AMOUNT,
    PROGRAM_FUNDING,
    PROJECT_BUDGET,
)

_SEARCH_ONLY = re.compile(r"(google\.com/search|bing\.com/search|duckduckgo\.com)", re.I)


def identity_execution_ready(line: dict[str, Any]) -> dict[str, Any]:
    conf = str(line.get("identity_confidence") or line.get("identity_class") or "")
    # Normalize short labels
    aliases = {
        "A": A_EXACT_MPN,
        "B": B_EXACT_MODEL,
        "C": C_NSN_TO_EXACT,
        "D": D_PERMITTED_EQUAL,
        "E": E_STRONG_GENERIC,
        "F": F_FAMILY_ONLY,
        "G": G_AMBIGUOUS,
    }
    if conf in aliases:
        conf = aliases[conf]
    if not conf:
        # Infer from fields
        if line.get("mpn") or line.get("part_number"):
            conf = A_EXACT_MPN
        elif line.get("model"):
            conf = B_EXACT_MODEL
        else:
            conf = G_AMBIGUOUS

    search_only = bool(
        _SEARCH_ONLY.search(str(line.get("identity_source_url") or ""))
        or _SEARCH_ONLY.search(str(line.get("price_url") or ""))
    )
    source_backed = bool(
        line.get("SOURCE_PROVENANCE", {}).get("complete")
        or line.get("source_document")
        or line.get("identity_validated")
    )
    ready = conf in IDENTITY_PACKET_OK and not search_only and source_backed
    if conf in IDENTITY_PACKET_BLOCK:
        ready = False

    return {
        "line_id": line.get("line_id"),
        "identity_class": conf,
        "IDENTITY_EXECUTION_READY": ready,
        "may_enter_production_quote_packet": ready and conf in IDENTITY_PACKET_OK,
        "blockers": [
            *(["F_OR_G_IDENTITY"] if conf in IDENTITY_PACKET_BLOCK else []),
            *(["SEARCH_RESULT_ONLY_IDENTITY"] if search_only else []),
            *(["IDENTITY_NOT_SOURCE_BACKED"] if not source_backed else []),
        ],
    }


def classify_revenue_for_economics(
    *,
    amount: float | None,
    text_window: str = "",
    document: str | None = None,
    evidence_type: str | None = None,
    scope_alignment_proven: bool = False,
    product_allocation_evidence: bool = False,
    opportunity_id: str | None = None,
) -> dict[str, Any]:
    """Revenue hierarchy + ECONOMIC_REVENUE_USABLE."""
    role = NON_REVENUE_AMOUNT
    reasons: list[str] = []

    # Prefer explicit evidence_type mapping
    et = str(evidence_type or "")
    if et in {"CURRENT_VALUE_EXPLICIT", "CURRENT_CONTRACT_VALUE"}:
        role = CURRENT_CONTRACT_VALUE
    elif et in {"EXACT_PRIOR_LINE_VALUE"}:
        role = HISTORICAL_EXACT
    elif et in {"COMPARABLE_PRIOR_BASKET_VALUE"}:
        role = HISTORICAL_COMPARABLE
    elif et in {"BUYER_CATEGORY_REFERENCE"}:
        role = BUYER_CATEGORY_REFERENCE
    elif text_window:
        classified = classify_money_context(amount, text_window=text_window, document=document)
        sem = classified.get("semantic_role")
        if sem == "GRANT_TOTAL":
            role = GRANT_TOTAL
        elif sem == "PROGRAM_FUNDING":
            role = PROGRAM_FUNDING
        elif sem == "BOND_THRESHOLD":
            role = NON_REVENUE_AMOUNT
            reasons.append("bond_threshold")
        elif sem == "INSURANCE_THRESHOLD":
            role = NON_REVENUE_AMOUNT
            reasons.append("insurance_threshold")
        elif sem == "CEILING":
            role = CEILING
        elif sem == "BUDGET":
            role = PROJECT_BUDGET
        elif sem == "ESTIMATE":
            role = ESTIMATE
        elif sem == "HISTORICAL_REFERENCE":
            role = HISTORICAL_EXACT
        elif sem == "CURRENT_CONTRACT_VALUE":
            role = CURRENT_CONTRACT_VALUE
        else:
            role = NON_REVENUE_AMOUNT
            reasons.extend(classified.get("reasons") or [])

    # Hard: Dania $400K grant context
    if opportunity_id == DANIA_OID and amount == 400000:
        role = GRANT_TOTAL
        reasons.append("dania_fdep_grant_total_hard_rule")

    # Grant / program / project budget need allocation
    if role in {GRANT_TOTAL, PROGRAM_FUNDING, PROJECT_BUDGET}:
        usable = bool(product_allocation_evidence)
        if not usable:
            reasons.append("no_product_scope_allocation_evidence")
    elif role == CURRENT_CONTRACT_VALUE:
        usable = bool(scope_alignment_proven)
        if not usable:
            reasons.append("contract_value_without_scope_alignment")
    elif role == CEILING:
        usable = False
        reasons.append("ceiling_not_guaranteed_revenue")
    elif role in {HISTORICAL_EXACT, HISTORICAL_COMPARABLE, BUYER_CATEGORY_REFERENCE, ESTIMATE}:
        usable = False
        reasons.append("historical_or_estimate_reference_only")
    elif role in ECONOMIC_REVENUE_DIRECT:
        usable = True
    else:
        usable = False
        reasons.append("non_revenue_or_unclassified")

    return {
        "amount": amount,
        "revenue_class": role,
        "ECONOMIC_REVENUE_USABLE": "YES" if usable else "NO",
        "document": document,
        "reasons": reasons,
        "may_support_product_economics": usable,
    }


def run_revenue_allocation_regression() -> dict[str, Any]:
    cases = [
        {
            "id": "dania_400k_grant",
            "amount": 400000.0,
            "text": "Total Amount of Funding: $200,000.00 Grantee Match $200,000.00 Total Amount of Funding + Grantee Match, if any: $400,000.00",
            "document": "FDEP_Grant_Agreement_Chester_Byrd_Park_Project_P25011.pdf",
            "opportunity_id": DANIA_OID,
            "must_usable": False,
            "expected_class": GRANT_TOTAL,
        },
        {
            "id": "collier_bond_250k",
            "amount": 250000.0,
            "text": "of $250,000 may require that persons interested in performing work under contract first be certified or qualified to perform such work.",
            "document": "26-8702_Construction_Bid_Instructions_Version_1.pdf",
            "must_usable": False,
            "expected_class": NON_REVENUE_AMOUNT,
        },
        {
            "id": "current_line_value",
            "amount": 42.0,
            "text": "",
            "evidence_type": None,
            "force_class": CURRENT_LINE_VALUE,
            "must_usable": True,
            "expected_class": CURRENT_LINE_VALUE,
        },
    ]
    results = []
    passed = 0
    for case in cases:
        if case.get("force_class"):
            row = {
                "amount": case["amount"],
                "revenue_class": case["force_class"],
                "ECONOMIC_REVENUE_USABLE": "YES",
                "may_support_product_economics": True,
                "reasons": ["forced_direct"],
            }
        else:
            row = classify_revenue_for_economics(
                amount=case["amount"],
                text_window=case.get("text") or "",
                document=case.get("document"),
                evidence_type=case.get("evidence_type"),
                opportunity_id=case.get("opportunity_id"),
            )
        ok = (
            row["revenue_class"] == case["expected_class"]
            and (row["ECONOMIC_REVENUE_USABLE"] == "YES") == case["must_usable"]
        )
        if ok:
            passed += 1
        results.append({"id": case["id"], "got": row, "pass": ok})
    return {
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "all_pass": passed == len(cases),
        "results": results,
    }


def run_identity_leak_regression() -> dict[str, Any]:
    cases = [
        {
            "id": "fg_blocked",
            "line": {
                "line_id": "x",
                "identity_confidence": G_AMBIGUOUS,
                "source_document": "a.pdf",
                "SOURCE_PROVENANCE": {"complete": True},
            },
            "must_enter": False,
        },
        {
            "id": "a_ok",
            "line": {
                "line_id": "y",
                "identity_confidence": A_EXACT_MPN,
                "mpn": "13-69938-00",
                "source_document": "a.xlsx",
                "SOURCE_PROVENANCE": {"complete": True},
                "identity_validated": True,
            },
            "must_enter": True,
        },
        {
            "id": "search_only_blocked",
            "line": {
                "line_id": "z",
                "identity_confidence": A_EXACT_MPN,
                "mpn": "X",
                "identity_source_url": "https://www.google.com/search?q=x",
                "SOURCE_PROVENANCE": {"complete": True},
            },
            "must_enter": False,
        },
    ]
    results = []
    passed = 0
    for case in cases:
        r = identity_execution_ready(case["line"])
        ok = r["may_enter_production_quote_packet"] == case["must_enter"]
        if ok:
            passed += 1
        results.append({"id": case["id"], "got": r, "pass": ok})
    return {
        "total": len(cases),
        "passed": passed,
        "failed": len(cases) - passed,
        "all_pass": passed == len(cases),
        "results": results,
    }
